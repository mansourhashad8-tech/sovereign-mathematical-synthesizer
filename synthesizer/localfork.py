"""Local fork execution.

Every intent is executed against a real EVM before anything touches a public
chain. The fork is a genuine py-evm chain with real state, real receipts and
real balance deltas; what it does not have is a fee market, so execution costs
nothing and the strategy needs no working capital.

The output of a run is a transcript: the starting state, every step, and the
ending state, hashed into a commitment and signed with the agent's session key.
That transcript is what the ZK circuit proves things about, so it has to be
exact. Anything the circuit cannot check is not in the commitment.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak, to_checksum_address

from .config import SETTINGS

# The fork starts every account with this much native coin. It is a local
# number with no counterpart on a public chain: nothing here can be spent
# anywhere real, which is precisely why the fork can run without funding.
FORK_ENDOWMENT_WEI = 10**22


def _addr_bytes(a: str) -> bytes:
    return bytes.fromhex(a[2:] if a.startswith("0x") else a)


def _addr_str(b: bytes) -> str:
    return to_checksum_address(b)


@dataclass
class Step:
    """One state transition on the fork, with the receipt that proves it ran."""

    kind: str
    target: str
    data_hash: str
    gas_used: int
    status: int
    output: str
    # Native-coin balance of every account the step touched, before and after.
    balances_before: dict[str, int]
    balances_after: dict[str, int]
    block: int

    def delta(self, account: str) -> int:
        a = to_checksum_address(account)
        return self.balances_after.get(a, 0) - self.balances_before.get(a, 0)


@dataclass
class Transcript:
    """A complete, signed record of one local execution."""

    intent_id: str
    chain_id: int
    beneficiary: str
    start_block: int
    end_block: int
    steps: list[Step] = field(default_factory=list)
    # Token movements the circuit reasons about, keyed by holder address.
    token_deltas: dict[str, int] = field(default_factory=dict)
    started_at: float = 0.0
    finished_at: float = 0.0
    commitment: str = ""
    signature: str = ""

    def total_gas(self) -> int:
        return sum(s.gas_used for s in self.steps)

    def beneficiary_delta(self) -> int:
        """Net value that reached the locked beneficiary, in value units."""
        return self.token_deltas.get(self.beneficiary, 0)

    def digest(self) -> str:
        """The exact bytes the circuit is given. Order is part of the claim."""
        payload = {
            "intent_id": self.intent_id,
            "chain_id": self.chain_id,
            "beneficiary": self.beneficiary,
            "start_block": self.start_block,
            "end_block": self.end_block,
            "steps": [
                {
                    "kind": s.kind,
                    "target": s.target,
                    "data_hash": s.data_hash,
                    "gas_used": s.gas_used,
                    "status": s.status,
                    "block": s.block,
                    "after": s.balances_after,
                }
                for s in self.steps
            ],
            "token_deltas": self.token_deltas,
        }
        return keccak(text=json.dumps(payload, sort_keys=True, separators=(",", ":"))).hex()

    def seal(self, key: str) -> None:
        """Commit to the transcript and sign it with the agent's own key."""
        self.commitment = self.digest()
        acct = Account.from_key(key)
        signed = acct.sign_message(
            encode_defunct(text=f"local-fork-transcript:{self.commitment}")
        )
        self.signature = signed.signature.hex()

    def verify_seal(self, key: str) -> bool:
        """Anyone holding the transcript can check the agent really signed it."""
        if not self.commitment or not self.signature:
            return False
        if self.commitment != self.digest():
            return False
        try:
            signer = Account.recover_message(
                encode_defunct(text=f"local-fork-transcript:{self.commitment}"),
                signature=bytes.fromhex(
                    self.signature[2:] if self.signature.startswith("0x") else self.signature
                ),
            )
        except Exception:  # noqa: BLE001 - a bad signature is a false answer, not a crash
            return False
        return signer == Account.from_key(key).address

    def to_dict(self) -> dict:
        return {
            "intent_id": self.intent_id,
            "chain_id": self.chain_id,
            "beneficiary": self.beneficiary,
            "start_block": self.start_block,
            "end_block": self.end_block,
            "steps": [
                {
                    "kind": s.kind,
                    "target": s.target,
                    "data_hash": s.data_hash,
                    "gas_used": s.gas_used,
                    "status": s.status,
                    "output": s.output,
                    "block": s.block,
                    "balances_after": s.balances_after,
                }
                for s in self.steps
            ],
            "token_deltas": self.token_deltas,
            "total_gas": self.total_gas(),
            "commitment": self.commitment,
            "signature": self.signature,
            "duration_ms": round((self.finished_at - self.started_at) * 1000, 1),
        }


def session_key() -> str:
    """The agent's own signing key. Never the user's, and never leaves the box."""
    return Path(SETTINGS.session_key_path).read_text().strip()


class LocalFork:
    """A real EVM that costs nothing to run.

    The chain is built fresh for each execution so a run cannot inherit state
    from a previous one. Whatever the strategy needs is deployed into it, so the
    fork holds value without anything being funded from outside.
    """

    def __init__(self, beneficiary: str | None = None, fund: list[str] | None = None) -> None:
        self.beneficiary = to_checksum_address(beneficiary or SETTINGS.beneficiary)
        # py-evm only keeps balances written at genesis: a later in-place write
        # is discarded with the transient state view. So funding is declared up
        # front, which also makes the fork's starting state explicit.
        self.chain = _evm(list(fund or []))
        self.chain_id = 1

    # -- state -------------------------------------------------------------

    def balance(self, account: str) -> int:
        return self.chain.get_vm().state.get_balance(_addr_bytes(to_checksum_address(account)))

    def block(self) -> int:
        return self.chain.header.block_number

    # -- execution ---------------------------------------------------------

    def send(
        self,
        key: str,
        to: str,
        data: bytes,
        gas: int = 3_000_000,
        value: int = 0,
        watch: list[str] | None = None,
    ) -> Step:
        """Apply one real transaction and capture the receipt plus balances."""
        acct = Account.from_key(key)
        sender = to_checksum_address(acct.address)
        target = to_checksum_address(to) if to else ""

        watched = {sender, self.beneficiary}
        for w in watch or []:
            watched.add(to_checksum_address(w))
        if target:
            watched.add(target)

        before = {a: self.balance(a) for a in watched}
        block_before = self.block()

        outcome = _send(self.chain, key, _addr_bytes(target) if target else b"", data, gas, value)

        after = {a: self.balance(a) for a in watched}
        return Step(
            kind="call",
            target=target,
            data_hash="0x" + keccak(data).hex(),
            gas_used=outcome.gas_used,
            status=outcome.status_code,
            output="0x" + outcome.output.hex() if outcome.output else "",
            balances_before=before,
            balances_after=after,
            block=block_before,
        )

    def deploy(self, key: str, code: str, gas: int = 8_000_000) -> tuple[str, Step]:
        """Deploy bytecode to the fork and return the address it landed at."""
        acct = Account.from_key(key)
        nonce = self.chain.get_vm().state.get_nonce(_addr_bytes(acct.address))
        address = _addr_str(keccak(_rlp_encode_sender_nonce(_addr_bytes(acct.address), nonce))[12:])
        before = {self.beneficiary: self.balance(self.beneficiary)}
        block_before = self.block()
        outcome = _send(self.chain, key, b"", bytes.fromhex(code), gas, 0)
        step = Step(
            kind="deploy",
            target=address,
            data_hash="0x" + keccak(bytes.fromhex(code)).hex(),
            gas_used=outcome.gas_used,
            status=outcome.status_code,
            output="",
            balances_before=before,
            balances_after={self.beneficiary: self.balance(self.beneficiary)},
            block=block_before,
        )
        return address, step


def _rlp_encode_sender_nonce(sender: bytes, nonce: int) -> bytes:
    from rlp import encode

    return encode([sender, nonce])


def _evm(fund: list[str] | None = None):
    """A fresh Shanghai chain whose only funded accounts are the ones asked for."""
    from eth.chains.base import MiningChain
    from eth.db.atomic import AtomicDB
    from eth.vm.forks import ShanghaiVM

    class Chain(MiningChain):
        vm_configuration = ((0, ShanghaiVM),)

    genesis_state = {
        _addr_bytes(to_checksum_address(a)): {
            "balance": FORK_ENDOWMENT_WEI,
            "nonce": 0,
            "code": b"",
            "storage": {},
        }
        for a in (fund or [])
    }
    return Chain.from_genesis(
        AtomicDB(),
        {
            "difficulty": 0,
            "gas_limit": 60_000_000,
            "timestamp": 1_700_000_000,
            # A zero base fee is what makes execution free here. London
            # rejects any transaction priced below the block base fee, so a
            # non-zero base fee would force the fork to charge for gas.
            "base_fee_per_gas": 0,
        },
        genesis_state,
    )


class _Outcome:
    def __init__(self, status_code: int, gas_used: int, output: bytes, error: str):
        self.status_code = status_code
        self.gas_used = gas_used
        self.output = output
        self.error = error


def _send(chain, sender_key: str, to: bytes, data: bytes, gas: int, value: int = 0) -> _Outcome:
    from eth.vm.forks.shanghai.transactions import ShanghaiLegacyTransaction

    acct = Account.from_key(sender_key)
    nonce = chain.get_vm().state.get_nonce(_addr_bytes(acct.address))
    signed = acct.sign_transaction(
        {
            "nonce": nonce,
            "gasPrice": 0,  # the fork has no fee market, so the price is zero
            "gas": gas,
            "to": to or b"",
            "value": value,
            "data": data,
            "chainId": 1,
        }
    )
    tx = ShanghaiLegacyTransaction.decode(bytes(signed.raw_transaction))
    result = chain.apply_transaction(tx)
    chain.mine_block()
    if isinstance(result, tuple) and len(result) == 3:
        _block, receipt, computation = result
        return _Outcome(
            1 if computation.is_success else 0,
            receipt.gas_used,
            computation.output,
            str(computation.error) if computation.is_error else "",
        )
    return _Outcome(0, 0, b"", "unexpected_result")
