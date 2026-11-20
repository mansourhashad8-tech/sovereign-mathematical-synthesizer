"""Run the entanglement contract on a real EVM.

This is not a mock. It deploys a genuine ERC20 and the real GasValueLock
bytecode into py-evm and executes actual state transitions. The point of the
test is the economic claim the contract makes: an agent account holding exactly
zero ETH can still deliver value, because its gas is paid out of the profit in
the same transaction.

The relayer is the only account that ever needs ETH, and it needs it only for
the outer transaction it signs. That is what the split was about, and this
harness is what lets us prove the settlement half of it without a funded
mainnet key.
"""

from __future__ import annotations

import json
from pathlib import Path

from eth_account import Account
from eth_utils import keccak, to_checksum_address

CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"


def _load() -> tuple[list, str]:
    abi = json.loads((CONTRACTS / "GasValueLock.abi.json").read_text())
    code = (CONTRACTS / "GasValueLock.bin").read_text().strip()
    return abi, code





def _evm():
    from eth.chains.base import MiningChain
    from eth.db.atomic import AtomicDB
    from eth.vm.forks import ShanghaiVM

    class Chain(MiningChain):
        vm_configuration = ((0, ShanghaiVM),)

    deployer = Account.from_key("0x" + "11" * 31 + "12").address
    relayer = Account.from_key("0x" + "33" * 31 + "34").address
    genesis_state = {
        bytes.fromhex(deployer[2:]): {"balance": 10**20, "nonce": 0, "code": b"", "storage": {}},
        bytes.fromhex(relayer[2:]): {"balance": 10**20, "nonce": 0, "code": b"", "storage": {}},
    }
    return Chain.from_genesis(
        AtomicDB(),
        {
            "difficulty": 0,
            "gas_limit": 30_000_000,
            "timestamp": 1_700_000_000,
        },
        genesis_state,
    )


class _Outcome:
    """A uniform success/gas/output view over a py-evm computation."""

    def __init__(self, status_code: int, gas_used: int, output: bytes, error: str):
        self.status_code = status_code
        self.gas_used = gas_used
        self.output = output
        self.error = error

    @property
    def ok(self) -> bool:
        return self.status_code == 1

    def __repr__(self) -> str:
        return (
            f"Outcome(status={self.status_code}, gas={self.gas_used}, "
            f"error={self.error!r})"
        )


def _send(chain, sender_key: str, to: bytes, data: bytes, gas: int):
    acct = Account.from_key(sender_key)
    nonce = chain.get_vm().state.get_nonce(bytes.fromhex(acct.address[2:]))
    from eth.vm.forks.shanghai.transactions import ShanghaiLegacyTransaction

    signed = acct.sign_transaction({
        "nonce": nonce,
        "gasPrice": 10**9,
        "gas": gas,
        "to": to,
        "value": 0,
        "data": data,
        "chainId": 1,
    })
    tx = ShanghaiLegacyTransaction.decode(bytes(signed.raw_transaction))
    result = chain.apply_transaction(tx)
    chain.mine_block()
    # (block, receipt, computation): py-evm puts success on the computation,
    # and gas_used on the receipt. Callers need both, so hand back a view.
    if isinstance(result, tuple) and len(result) == 3:
        _block, receipt, computation = result
        return _Outcome(
            status_code=1 if computation.is_success else 0,
            gas_used=receipt.gas_used,
            output=computation.output,
            error=str(computation.error) if computation.is_error else "",
        )
    return result


def deploy(chain, sender_key: str, abi: list, code: str, args: bytes = b"") -> str:
    acct = Account.from_key(sender_key)
    nonce = chain.get_vm().state.get_nonce(bytes.fromhex(acct.address[2:]))
    _send(chain, sender_key, b"", bytes.fromhex(code) + args, 5_000_000)
    # A deploy's created address is keccak(rlp(sender, nonce))[12:].
    from eth_utils import keccak as k
    from rlp import encode

    return to_checksum_address(
        k(encode([bytes.fromhex(acct.address[2:]), nonce]))[12:]
    )


def call(chain, sender_key: str, to: str, data: bytes, gas: int = 3_000_000):
    return _send(chain, sender_key, bytes.fromhex(to[2:]), data, gas)





def build_permit() -> dict:
    """Deploy the permit-capable token plus GasValueLock on a real EVM.

    Both contracts are loaded from committed artifacts, so the tests need no
    Solidity compiler. Compiling in CI would make the suite depend on solc
    being downloadable, and a settlement proof that cannot run is not a proof.
    """
    token_abi = json.loads((CONTRACTS / "TestValueToken.abi.json").read_text())
    token_bin = (CONTRACTS / "TestValueToken.bin").read_text().strip()
    abi, code = _load()
    chain = _evm()
    deployer = "0x" + "11" * 31 + "12"
    agent = "0x" + "22" * 31 + "23"
    relayer = "0x" + "33" * 31 + "34"
    outsider = "0x" + "44" * 31 + "45"
    token = deploy(chain, deployer, token_abi, token_bin)
    lock = deploy(chain, deployer, abi, code)
    return {
        "chain": chain, "abi": abi, "token_abi": token_abi,
        "token": token, "lock": lock,
        "agent": agent, "agent_addr": Account.from_key(agent).address,
        "relayer": relayer, "relayer_addr": Account.from_key(relayer).address,
        "outsider": outsider, "outsider_addr": Account.from_key(outsider).address,
        "deployer": deployer, "deployer_addr": Account.from_key(deployer).address,
    }


def build() -> dict:
    """Compile the token, deploy both contracts, and return the handles."""
    tok_abi = json.loads((CONTRACTS / "TestValueToken.abi.json").read_text())
    tok_bin = (CONTRACTS / "TestValueToken.bin").read_text().strip()

    abi, code = _load()
    chain = _evm()

    deployer = "0x" + "11" * 31 + "12"
    agent = "0x" + "22" * 31 + "23"
    relayer = "0x" + "33" * 31 + "34"
    outsider = "0x" + "44" * 31 + "45"

    # Only the deployer and relayer hold ETH. The agent holds exactly zero,
    # which is the condition this whole design exists to survive.
    token = deploy(chain, deployer, tok_abi, tok_bin)
    lock = deploy(chain, deployer, abi, code)

    return {
        "chain": chain,
        "abi": abi,
        "token_abi": tok_abi,
        "token": token,
        "lock": lock,
        "agent": agent,
        "agent_addr": Account.from_key(agent).address,
        "relayer": relayer,
        "relayer_addr": Account.from_key(relayer).address,
        "outsider": outsider,
        "outsider_addr": Account.from_key(outsider).address,
        "deployer": deployer,
    }


def read(chain, to: str, data: bytes, sender: str) -> bytes:
    """Read-only eth_call against a real VM."""
    vm = chain.get_vm()
    comp = vm.execute_bytecode(
        origin=bytes.fromhex(sender[2:]),
        gas_price=0,
        gas=1_000_000,
        to=bytes.fromhex(to[2:]),
        sender=bytes.fromhex(sender[2:]),
        value=0,
        data=data,
        code=vm.state.get_code(bytes.fromhex(to[2:])),
    )
    return comp.output


def erc20_balance(chain, token: str, who: str, sender: str) -> int:
    from eth_abi import decode
    from eth_utils import keccak

    out = read(chain, token, keccak(text="balanceOf(address)")[:4] + __import__("eth_abi").encode(["address"], [who]), sender)
    return decode(["uint256"], out)[0]


def uint_read(chain, to: str, sig: str, args: list, sender: str) -> int:
    from eth_abi import decode, encode
    from eth_utils import keccak

    out = read(chain, to, keccak(text=sig)[:4] + encode(["address"], args), sender)
    return decode(["uint256"], out)[0]
