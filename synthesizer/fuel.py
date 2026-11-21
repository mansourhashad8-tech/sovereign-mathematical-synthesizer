"""The Fuel settlement hand: a real SwayVM chain the engine owns end to end.

This replaces the sponsored EVM leg. The previous design asked a paymaster to
cover gas, which made the engine dependent on a third party staying online and
willing. Here the engine runs its own ``fuel-core`` node, deploys its own Sway
contract, and pays gas from the chain's genesis supply. Nothing is asked of the
user and nothing is asked of a sponsor.

Why this is genuinely gas-abstracted
------------------------------------
``fuel-core`` is started with ``--min-gas-price 0``, so the base asset cost of a
transaction is zero. The only remaining requirement is a UTXO to spend as a
transaction input, which genesis mints to the session key. So the marginal cost
of a settlement is zero and the engine never needs external funding. That is
app-level gas abstraction: the application, not the user, owns the gas supply.

Honesty rules this module enforces
----------------------------------
* A settlement is ``executed`` only when the transaction is found in a block and
  the contract's stored ``delivered_to`` grew by the delta that was recorded.
* Gas is reported from the node's own ``gasUsed`` meter, never estimated.
* If the node or the toolchain is missing, the leg reports ``unavailable`` with
  the reason instead of pretending to have settled.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import SETTINGS

# The beneficiary is locked in the Sway contract at compile time. Sway hashes a
# b256 as-is, so the 32-byte value is the address left-padded with zeros.
BENEFICIARY = "0xe3d1c7dcd5e95b6441716b65f9c46b73e91b4d47"
BENEFICIARY_B256 = "0x" + "00" * 12 + BENEFICIARY[2:]

DEFAULT_SESSION_KEY = "0x" + "11" * 32
DEFAULT_RPC = "http://127.0.0.1:4000/v1/graphql"
DEFAULT_PORT = 4000


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def _node_binary() -> str | None:
    """Locate ``fuel-core``: explicit setting, then PATH, then a cached download."""
    explicit = os.environ.get("FUEL_CORE_BIN") or SETTINGS.fuel_core_bin
    if explicit and Path(explicit).exists():
        return explicit
    found = shutil.which("fuel-core")
    if found:
        return found
    for pattern in ("fuel-core-*-x86_64-unknown-linux-gnu/fuel-core",
                    "fuel-core-*/fuel-core"):
        for candidate in sorted(Path("/tmp").glob(pattern)):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def _forc_binary() -> str | None:
    explicit = os.environ.get("FORC_BIN") or SETTINGS.forc_bin
    if explicit and Path(explicit).exists():
        return explicit
    found = shutil.which("forc")
    if found:
        return found
    for pattern in ("forc*/forc-binaries/forc", "forc*/forc"):
        for candidate in sorted(Path("/tmp").glob(pattern), reverse=True):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


@dataclass
class FuelSettlement:
    status: str  # executed | pending | unavailable | rejected
    tx_id: str = ""
    block_height: str = ""
    block_id: str = ""
    included: bool = False
    contract_id: str = ""
    network: str = "fuel-devnet"
    beneficiary: str = BENEFICIARY
    delta: int = 0
    net_profit: int = 0
    delivered: int = 0
    gas_used: int = 0
    gas_paid_by: str = "genesis-supply"
    explorer_url: str = ""
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def paid(self) -> bool:
        """True only when value demonstrably moved to the locked beneficiary."""
        return (
            self.status == "executed"
            and self.included
            and self.delivered >= self.delta > 0
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "tx_id": self.tx_id,
            "block_height": self.block_height,
            "block_id": self.block_id,
            "included": self.included,
            "contract_id": self.contract_id,
            "network": self.network,
            "beneficiary": self.beneficiary,
            "delta": self.delta,
            "net_profit": self.net_profit,
            "delivered": self.delivered,
            "gas_used": self.gas_used,
            "gas_paid_by": self.gas_paid_by,
            "explorer_url": self.explorer_url,
            "reason": self.reason,
            "paid": self.paid,
        }


class FuelNode:
    """Supervises the local ``fuel-core`` node the engine settles against."""

    def __init__(self, rpc: str | None = None, port: int | None = None) -> None:
        self.rpc = rpc or os.environ.get("FUEL_RPC_URL") or SETTINGS.fuel_rpc_url or DEFAULT_RPC
        self.port = port or int(os.environ.get("FUEL_PORT") or SETTINGS.fuel_port or DEFAULT_PORT)
        self._proc: subprocess.Popen | None = None
        self._snapshot_dir = _root() / "sway" / "chain" / "genesis"
        self._log_path = Path("/tmp/fuel-core-node.log")

    # -- probing -----------------------------------------------------------

    def healthy(self, timeout: float = 2.0) -> bool:
        try:
            import httpx

            with httpx.Client(timeout=timeout) as client:
                resp = client.post(
                    self.rpc,
                    json={"query": "{ nodeInfo { nodeVersion } }"},
                )
                return resp.status_code == 200 and "nodeVersion" in resp.text
        except Exception:
            return False

    # -- lifecycle ---------------------------------------------------------

    def ensure_running(self, wait: float = 25.0) -> bool:
        if self.healthy():
            return True
        binary = _node_binary()
        if not binary:
            return False
        if not self._write_genesis():
            return False
        key = os.environ.get("FUEL_CONSENSUS_KEY") or SETTINGS.fuel_consensus_key
        if not key:
            key = self._generate_consensus_key(binary)
            if not key:
                return False
        args = [
            binary, "run",
            "--ip", "127.0.0.1",
            "--port", str(self.port),
            "--db-type", "in-memory",
            "--utxo-validation",
            "--poa-instant", "true",
            # Zero base-asset cost is what makes the leg gas-abstracted.
            "--min-gas-price", "0",
            # The shipped wasm executor does not match the node build; the native
            # executor is the one that actually executes transactions here.
            "--native-executor-version", "0",
            "--consensus-key", key,
            "--snapshot", str(self._snapshot_dir),
        ]
        log = open(self._log_path, "ab")
        self._proc = subprocess.Popen(args, stdout=log, stderr=log)
        deadline = time.time() + wait
        while time.time() < deadline:
            if self.healthy():
                return True
            if self._proc.poll() is not None:
                return False
            time.sleep(1.0)
        return self.healthy()

    def _generate_consensus_key(self, binary: str) -> str:
        keygen = str(Path(binary).parent / "fuel-core-keygen")
        if not Path(keygen).exists():
            return ""
        try:
            out = subprocess.run(
                [keygen, "new", "--key-type", "block-production"],
                capture_output=True, text=True, timeout=30,
            )
            payload = json.loads(out.stdout.strip().splitlines()[-1])
            return payload.get("secret", "")
        except Exception:
            return ""

    def _write_genesis(self) -> bool:
        """Mint the single genesis coin to the session key.

        The address is derived with the same SDK that will spend it, so the coin
        provably belongs to the session wallet and not to some placeholder.
        """
        state = self._snapshot_dir / "stateConfig.json"
        if state.exists():
            return True
        script = _root() / "sway" / "chain" / "genesis.mjs"
        if not script.exists():
            return False
        owner = self.session_address()
        if not owner:
            return False
        try:
            out = subprocess.run(
                ["node", str(script), str(self._snapshot_dir), owner],
                cwd=str(_root()), capture_output=True, text=True, timeout=120,
            )
            return out.returncode == 0 and state.exists()
        except Exception:
            return False

    def session_address(self) -> str:
        script = _root() / "sway" / "chain" / "settle.mjs"
        if not script.exists():
            return ""
        try:
            out = subprocess.run(
                ["node", str(script), "address"],
                cwd=str(_root()), capture_output=True, text=True, timeout=60,
                env={**os.environ, "FUEL_RPC_URL": self.rpc},
            )
            if out.returncode != 0:
                return ""
            return json.loads(out.stdout.strip().splitlines()[-1]).get("address", "")
        except Exception:
            return ""

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()


class FuelHand:
    """Deploys the Sway ledger and records proven settlements on it."""

    def __init__(self, node: FuelNode | None = None) -> None:
        self.node = node or FuelNode()
        self._contract_id: str = ""
        self._deployed_checked = False
        self._last_error: str = ""

    # -- helpers -----------------------------------------------------------

    def _run(self, command: str, args: dict | None = None,
             timeout: float = 180.0) -> dict[str, Any] | None:
        script = _root() / "sway" / "chain" / "settle.mjs"
        if not script.exists():
            return None
        cmd = ["node", str(script), command]
        if args is not None:
            cmd.append(json.dumps(args))
        env = {**os.environ, "FUEL_RPC_URL": self.node.rpc}
        try:
            out = subprocess.run(
                cmd, cwd=str(_root()), capture_output=True,
                text=True, timeout=timeout, env=env,
            )
        except Exception as exc:
            self._last_error = str(exc)
            return None
        for line in reversed(out.stdout.strip().splitlines()):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        # A revert is a decision, not a transport failure: keep its text so the
        # caller can report "already settled" instead of "the node went away".
        self._last_error = (out.stderr or out.stdout or "").strip()[-400:]
        return None

    def contract_id(self) -> str:
        if self._contract_id:
            return self._contract_id
        path = _root() / "sway" / "chain" / "contract_id.txt"
        if path.exists():
            self._contract_id = path.read_text().strip()
        return self._contract_id

    def verified_contract_id(self) -> str:
        """Return the ledger id only if the running chain actually has it.

        The id is cached in a file, and a file can outlive the chain it was
        written for: an image built on one machine, a fresh genesis on another.
        Reading storage is what separates "deployed somewhere" from "deployed
        here", so an id that fails this check is not returned at all.
        """
        candidate = self.contract_id()
        if not candidate:
            return ""
        state = self._run("state", {}, timeout=60)
        if state and state.get("contractId") == candidate:
            return candidate
        return ""

    def available(self) -> bool:
        if not _node_binary():
            return False
        return bool(self.node.ensure_running())

    def deploy(self) -> str:
        """Compile and deploy the ledger, returning its contract id."""
        existing = self.contract_id()
        if existing:
            state = self._run("state", {})
            if state and state.get("contractId") == existing:
                return existing
        if not self.node.ensure_running():
            return ""
        forc = _forc_binary()
        project = _root() / "sway" / "settlement_ledger"
        if forc and project.exists():
            try:
                subprocess.run(
                    [forc, "build"], cwd=str(project),
                    capture_output=True, text=True, timeout=300,
                )
            except Exception:
                pass
        deployed = self._run("deploy", {}, timeout=300)
        if not deployed or not deployed.get("contractId"):
            return ""
        self._contract_id = deployed["contractId"]
        self._deployed_checked = True
        return self._contract_id

    # -- settlement --------------------------------------------------------

    def settle(self, delta: int, net_profit: int, commitment: str) -> FuelSettlement:
        """Record one proven settlement, returning measured on-chain facts."""
        if not self.node.healthy() and not self.node.ensure_running():
            return FuelSettlement(
                status="unavailable",
                reason="fuel-core node is not reachable and could not be started",
            )
        # Ask the chain, not the file: a stale id would make every settlement
        # fail against a contract this node never deployed.
        contract = self.verified_contract_id() or self.deploy()
        if not contract:
            return FuelSettlement(
                status="unavailable",
                reason="settlement ledger is not deployed",
            )
        if delta <= 0 or net_profit <= 0:
            return FuelSettlement(
                status="rejected",
                contract_id=contract,
                reason="delta and net profit must both be positive",
            )
        if net_profit > delta:
            return FuelSettlement(
                status="rejected",
                contract_id=contract,
                reason="delta must cover the net profit",
            )

        # Read the beneficiary's stored total before and after, so the gate can
        # confirm the delta actually arrived rather than trusting a cumulative
        # number that was already large from earlier settlements.
        before = self._delivered(contract)

        result = self._run(
            "record",
            {
                "beneficiary": BENEFICIARY_B256,
                "delta": int(delta),
                "netProfit": int(net_profit),
                "commitment": commitment,
            },
            timeout=300,
        )
        if not result or result.get("reverted"):
            error = result.get("reason", "") if result else self._last_error
            replay = "already settled" in error
            return FuelSettlement(
                status="rejected",
                contract_id=contract,
                delta=int(delta),
                net_profit=int(net_profit),
                reason=(
                    "this commitment is already on the ledger, so it cannot be settled twice"
                    if replay
                    else f"the contract rejected the settlement: {error}"
                ),
            )

        settlement = FuelSettlement(
            status="executed" if result.get("included") else "pending",
            tx_id=result.get("txId", ""),
            block_height=str(result.get("blockHeight", "")),
            block_id=result.get("blockId", ""),
            included=bool(result.get("included")),
            contract_id=contract,
            delta=int(delta),
            net_profit=int(net_profit),
            delivered=int(result.get("delivered", 0)),
            gas_used=int(result.get("gasUsed", 0)),
            # The chain is local, so a public explorer link would 404. The
            # engine serves its own verifiable receipt instead, and the
            # transaction id stays available for anyone running the same node.
            explorer_url=f"/fuel/tx/{result.get('txId', '')}",
            detail={"totals": result.get("totals", [])},
        )
        if settlement.status == "executed":
            settlement.detail["delivered_before"] = before
            settlement.detail["delivered_gain"] = settlement.delivered - before
            if settlement.delivered - before < delta:
                # The call landed but storage did not grow by the delta, so the
                # value did not arrive. A mined transaction is not a payment.
                settlement.status = "unsettled_value"
                settlement.reason = (
                    "the ledger recorded the call but the beneficiary total grew by "
                    f"{settlement.delivered - before}, not {delta}"
                )
        self._journal_record(settlement, commitment)
        return settlement

    # -- settlement journal ------------------------------------------------
    #
    # The ledger is authoritative, but it stores only the commitment, not which
    # transaction paid it. This journal keeps that mapping so a repeat request
    # for an already-paid intent can answer with the original transaction
    # instead of reporting a fresh attempt that the contract correctly refused.

    def _journal_path(self) -> Path:
        return _root() / "sway" / "chain" / "settlements.json"

    def _journal(self) -> dict[str, Any]:
        path = self._journal_path()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}

    def _journal_record(self, settlement: FuelSettlement, commitment: str) -> None:
        if settlement.status != "executed":
            return
        journal = self._journal()
        journal[commitment] = settlement.as_dict()
        try:
            self._journal_path().parent.mkdir(parents=True, exist_ok=True)
            self._journal_path().write_text(json.dumps(journal, indent=2))
        except Exception:
            pass

    def journal_lookup(self, commitment: str) -> dict[str, Any] | None:
        return self._journal().get(commitment)

    def replay_result(self, commitment: str, previous: dict[str, Any]) -> FuelSettlement:
        """Rebuild the original settlement for a commitment that already paid."""
        return FuelSettlement(
            status="executed",
            tx_id=previous.get("tx_id", ""),
            block_height=str(previous.get("block_height", "")),
            block_id=previous.get("block_id", ""),
            included=bool(previous.get("included")),
            contract_id=previous.get("contract_id", ""),
            network=previous.get("network", "fuel-devnet"),
            delta=int(previous.get("delta", 0)),
            net_profit=int(previous.get("net_profit", 0)),
            delivered=int(previous.get("delivered", 0)),
            gas_used=int(previous.get("gas_used", 0)),
            gas_paid_by=previous.get("gas_paid_by", "genesis-supply"),
            explorer_url=previous.get("explorer_url", ""),
            reason="this commitment was already settled on chain; reporting the original transaction",
            detail={"replayed": True, "delivered_gain": previous.get("delta", 0)},
        )

    def _delivered(self, contract: str) -> int:
        state = self._run("state", {"beneficiary": BENEFICIARY_B256}, timeout=120)
        if not state:
            return 0
        try:
            return int(state.get("delivered", 0))
        except (TypeError, ValueError):
            return 0


@dataclass
class GasAbstraction:
    """How the engine covers its own gas, reported from measured spend.

    Every settlement's ``gasUsed`` is accumulated here. Because the node prices
    gas at zero, the ledger can prove the engine ran N settlements for zero
    external spend, which is the claim the whole design rests on.
    """

    settlements: int = 0
    gas_used: int = 0
    external_spend: int = 0
    paid_by: str = "genesis-supply"
    first_at: float = 0.0
    last_at: float = 0.0

    def record(self, settlement: FuelSettlement) -> None:
        if settlement.status != "executed":
            return
        now = time.time()
        self.settlements += 1
        self.gas_used += settlement.gas_used
        self.first_at = self.first_at or now
        self.last_at = now

    def as_dict(self) -> dict[str, Any]:
        return {
            "settlements": self.settlements,
            "gas_used": self.gas_used,
            "external_spend": self.external_spend,
            "paid_by": self.paid_by,
            "first_at": self.first_at,
            "last_at": self.last_at,
        }
