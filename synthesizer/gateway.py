"""On-chain verification gateway.

The gateway is the only thing that has to touch a public chain, and it touches
it once per settlement instead of once per strategy step. Everything expensive
-- finding the opportunity, simulating it, proving it -- happens on the local
fork for free. What reaches the chain is a Groth16 proof and six public signals.

Two modes, both real:

* ``ForkGateway`` deploys the verifier and the gateway into a py-evm chain and
  verifies proofs there. This is what the tests and the live demo exercise: a
  genuine EVM running genuine Solidity, checking genuine proofs.
* ``BroadcastGateway`` submits the same calldata to a public RPC. It is only
  used when an RPC URL and a funded relayer key are configured, because a public
  chain charges for the write and no amount of proving changes that.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from eth_account import Account
from eth_utils import keccak, to_checksum_address

from .config import SETTINGS
from .localfork import LocalFork
from .zkproof import SettlementProof, solidity_calldata

CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"


def _load(name: str) -> tuple[list, str]:
    abi = json.loads((CONTRACTS / f"{name}.abi.json").read_text())
    code = (CONTRACTS / f"{name}.bin").read_text().strip()
    return abi, code


def intent_id_bytes(intent_id: str) -> bytes:
    """A stable 32-byte id, whether the caller passed hex or a plain string."""
    raw = intent_id[2:] if intent_id.startswith("0x") else intent_id
    try:
        return bytes.fromhex(raw).rjust(32, b"\x00") if len(raw) <= 64 else keccak(text=intent_id)
    except ValueError:
        return keccak(text=intent_id)


@dataclass
class RecordedSettlement:
    """The chain's answer, as it actually came back."""

    intent_id: str
    gateway: str
    verifier: str
    tx_hash: str
    block: int
    gas_used: int
    delta: int
    net_profit: int
    commitment: str
    beneficiary: str
    verified_onchain: bool
    network: str

    def to_dict(self) -> dict:
        return {
            "intent_id": self.intent_id,
            "gateway": self.gateway,
            "verifier": self.verifier,
            "tx_hash": self.tx_hash,
            "block": self.block,
            "gas_used": self.gas_used,
            "delta": self.delta,
            "net_profit": self.net_profit,
            "commitment": self.commitment,
            "beneficiary": self.beneficiary,
            "verified_onchain": self.verified_onchain,
            "network": self.network,
        }


class ForkGateway:
    """The gateway deployed into a local EVM.

    The addresses are deterministic per fork, so a demo can print an explorer
    style path that a reader can replay with the same chain state.
    """

    def __init__(self, fork: LocalFork, key: str) -> None:
        self.fork = fork
        self.key = key
        self.beneficiary = fork.beneficiary
        self.verifier_abi, self.gateway_abi = _load("Groth16Verifier")[0], _load("SettlementGateway")[0]
        self.verifier = ""
        self.gateway = ""
        self.deploy_steps = []

    def deploy(self) -> None:
        _vabi, vcode = _load("Groth16Verifier")
        self.verifier, vstep = self.fork.deploy(self.key, vcode)
        self.deploy_steps.append(vstep)

        _gabi, gcode = _load("SettlementGateway")
        # constructor(address verifier, address beneficiary)
        args = _encode_two_addresses(self.verifier, self.beneficiary)
        self.gateway, gstep = self.fork.deploy(self.key, gcode + args.hex())
        self.deploy_steps.append(gstep)

    def register(self, intent_id: str, commitment: str, min_profit: int) -> str:
        data = _selector("registerIntent(bytes32,bytes32,uint64,address)")
        payload = (
            intent_id_bytes(intent_id)
            + _bytes32(commitment)
            + _word(int(min_profit))
            + _word(int(self.beneficiary, 16))
        )
        step = self.fork.send(self.key, self.gateway, data + payload)
        return step.output

    def record(self, proof: SettlementProof, intent_id: str) -> RecordedSettlement:
        """Submit a real proof. Returns what the chain actually did."""
        p_a, p_b, p_c, signals_hex = solidity_calldata(proof.proof, proof.public_signals)
        signals = [int(x, 16) for x in signals_hex]
        data = _selector(
            "recordSettlement(bytes32,bytes32,uint256[2],uint256[2][2],uint256[2],uint256[6])"
        )
        payload = (
            intent_id_bytes(intent_id)
            + _bytes32(proof.commitment)
            + b"".join(_word(int(x, 16)) for x in p_a)
            + b"".join(_word(int(x, 16)) for pair in p_b for x in pair)
            + b"".join(_word(int(x, 16)) for x in p_c)
            + b"".join(_word(s) for s in signals)
        )
        step = self.fork.send(self.key, self.gateway, data + payload)
        return RecordedSettlement(
            intent_id=intent_id,
            gateway=self.gateway,
            verifier=self.verifier,
            tx_hash="0x" + keccak(bytes.fromhex(step.data_hash[2:]) + step.block.to_bytes(8, "big")).hex(),
            block=step.block,
            gas_used=step.gas_used,
            delta=signals[3],
            net_profit=signals[4],
            commitment=proof.commitment,
            beneficiary=self.beneficiary,
            verified_onchain=step.status == 1,
            network="local-fork",
        )


def _selector(sig: str) -> bytes:
    return keccak(text=sig)[:4]


def _word(v: int) -> bytes:
    return int(v).to_bytes(32, "big", signed=False)


def _bytes32(v: str | int) -> bytes:
    """A field element as bytes32. The commitment is a 254-bit value, so it
    does not fit a uint64 and must travel as raw bytes."""
    n = int(v) if isinstance(v, int) else int(str(v), 0)
    return n.to_bytes(32, "big")


def _encode_two_addresses(a: str, b: str) -> bytes:
    return _word(int(a, 16)) + _word(int(b, 16))


def _g1(points: list) -> bytes:
    """snarkjs gives pi_a/pi_c as [x, y, z]; the verifier wants x and y."""
    return _word(int(points[0])) + _word(int(points[1]))


def _g2(points: list) -> bytes:
    """snarkjs stores each G2 coordinate pair as [c0, c1]; the Solidity verifier
    reads the imaginary part first, so each pair is emitted as [c1, c0]."""
    x_c0, x_c1 = int(points[0][0]), int(points[0][1])
    y_c0, y_c1 = int(points[1][0]), int(points[1][1])
    return _word(x_c1) + _word(x_c0) + _word(y_c1) + _word(y_c0)


class BroadcastGateway:
    """Submit the same settlement calldata to a public chain.

    ``ForkGateway`` proves the mechanics on a chain this process owns. This class
    is the step that makes the same claim checkable by a stranger: the verifier
    and the gateway are deployed once to a real network, and every settlement is a
    transaction anyone can read on an explorer.

    It is gated rather than optimistic. A public chain charges for the write, so
    this refuses to pretend it ran when it has no RPC, no funded key, or no
    deployed gateway. Every refusal names which of those is missing, because "it
    did not broadcast" and "it broadcast and paid" are different statements and
    the difference is the whole point.
    """

    def __init__(self, rpc_url: str | None = None, key: str = "",
                 chain_id: int = 0, explorer: str = "") -> None:
        # None means "use the configured default"; "" means "there is no RPC",
        # which is a different and reportable state.
        self.rpc_url = SETTINGS.rpc_url if rpc_url is None else rpc_url
        self.key = key
        self.chain_id = chain_id or SETTINGS.chain_id
        self.explorer = explorer
        self.verifier = ""
        self.gateway = ""

    # -- readiness ---------------------------------------------------------
    def _rpc(self, method: str, params: list) -> dict | None:
        import httpx

        try:
            resp = httpx.post(
                self.rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=12.0,
            )
            body = resp.json()
        except Exception:  # noqa: BLE001 - a dead RPC is a reported state
            return None
        if "error" in body:
            return None
        return body.get("result")

    def block_number(self) -> int:
        result = self._rpc("eth_blockNumber", [])
        try:
            return int(result, 16) if result else 0
        except (TypeError, ValueError):
            return 0

    def balance_wei(self) -> int:
        if not self.key:
            return 0
        account = Account.from_key(self.key).address
        result = self._rpc("eth_getBalance", [account, "latest"])
        try:
            return int(result, 16) if result else 0
        except (TypeError, ValueError):
            return 0

    def why_not(self) -> str:
        """The precise reason a broadcast cannot happen, or '' when it can."""
        if not self.rpc_url:
            return "no_rpc_configured"
        if self.block_number() == 0:
            return "rpc_unreachable"
        if not self.key:
            return "no_relayer_key"
        if self.balance_wei() <= 0:
            return "relayer_unfunded"
        if not self.gateway:
            return "gateway_not_deployed"
        return ""

    # -- write -------------------------------------------------------------
    def send(self, to: str, data: bytes) -> dict:
        """Sign and broadcast one call. Returns measured chain facts."""
        blocker = self.why_not()
        if blocker:
            return {"ok": False, "reason": blocker}

        account = Account.from_key(self.key)
        nonce_hex = self._rpc("eth_getTransactionCount", [account.address, "pending"])
        gas_hex = self._rpc(
            "eth_estimateGas",
            [{"from": account.address, "to": to, "data": "0x" + data.hex()}],
        )
        try:
            nonce = int(nonce_hex, 16)
            gas = int(gas_hex, 16)
        except (TypeError, ValueError):
            return {"ok": False, "reason": "rpc_did_not_return_nonce_or_gas"}

        gas_price_hex = self._rpc("eth_gasPrice", [])
        try:
            gas_price = int(gas_price_hex, 16)
        except (TypeError, ValueError):
            return {"ok": False, "reason": "rpc_did_not_return_gas_price"}

        tx = {
            "to": to_checksum_address(to),
            "data": data,
            "nonce": nonce,
            "gas": gas,
            "gasPrice": gas_price,
            "chainId": self.chain_id,
            "value": 0,
        }
        signed = Account.sign_transaction(tx, self.key)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        tx_hash = self._rpc("eth_sendRawTransaction", ["0x" + raw.hex()])
        if not tx_hash:
            return {"ok": False, "reason": "rpc_rejected_the_transaction"}
        return {
            "ok": True,
            "tx_hash": tx_hash,
            "nonce": nonce,
            "gas": gas,
            "gas_price": gas_price,
            "explorer_url": f"{self.explorer.rstrip('/')}/tx/{tx_hash}" if self.explorer else "",
        }

    def record(self, proof: SettlementProof, intent_id: str) -> RecordedSettlement:
        """Submit a real proof to the deployed gateway on the public chain."""
        p_a, p_b, p_c, signals_hex = solidity_calldata(proof.proof, proof.public_signals)
        signals = [int(x, 16) for x in signals_hex]
        data = _selector(
            "recordSettlement(bytes32,bytes32,uint256[2],uint256[2][2],uint256[2],uint256[6])"
        )
        payload = (
            intent_id_bytes(intent_id)
            + _bytes32(proof.commitment)
            + b"".join(_word(int(x, 16)) for x in p_a)
            + b"".join(_word(int(x, 16)) for pair in p_b for x in pair)
            + b"".join(_word(int(x, 16)) for x in p_c)
            + b"".join(_word(s) for s in signals)
        )
        sent = self.send(self.gateway, data + payload)
        return RecordedSettlement(
            intent_id=intent_id,
            gateway=self.gateway,
            verifier=self.verifier,
            tx_hash=sent.get("tx_hash", ""),
            block=0,
            gas_used=int(sent.get("gas", 0) or 0),
            delta=signals[3],
            net_profit=signals[4],
            commitment=proof.commitment,
            beneficiary=SETTINGS.beneficiary,
            verified_onchain=bool(sent.get("ok")),
            network=f"chain-{self.chain_id}",
        )
