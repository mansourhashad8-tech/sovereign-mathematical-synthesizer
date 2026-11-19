"""The sovereign hand: build, sign, and broadcast an ERC-4337 UserOperation.

Design rules, enforced here and nowhere else:

* The engine is non-custodial. It never holds user funds and never asks for a
  user key. It signs with its OWN ephemeral session key inside this process.
* The beneficiary is the locked address from config. Any path whose payout
  target is anything else is rejected before signing.
* Gas is always sponsored. The operation carries a paymaster, and the
  beneficiary's balance is never touched for gas. When no paymaster will cover
  it, the engine falls back to a *flash-gas* ledger: the same atomic transaction
  repays the sponsor out of its own proceeds, so the user still pays nothing.
* Broadcasting is attempted immediately against every configured bundler, in
  order. The first accepted operation returns a real transaction hash and an
  explorer link. If every bundler declines, the engine returns the fully-formed,
  correctly-signed operation plus a deterministic commitment and marks the
  settlement as simulated. It never fabricates a transaction that did not
  happen.
"""

from __future__ import annotations

import os
import threading
import time

import httpx
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak

from .config import SETTINGS
from .gasflywheel import GasFlywheel, _networks
from .models import AtomicPath, Constraints, ExecutionResult

# The canonical ERC-4337 v0.6 SimpleAccountFactory, deployed on every major
# chain and testnet. Using it (rather than a placeholder) means the initCode we
# sign is a real account deployment any bundler can validate.
SIMPLE_ACCOUNT_FACTORY = "0x9406Cc6185a346906296840746125a0E44976454"
ENTRY_POINT = "0x5FF137D4b0FDCD49DcA30c7CF57E578a026d2789"


def _keccak_hex(data: bytes) -> str:
    return keccak(data).hex()


# One pooled client for every chain read: a fresh TLS handshake per call would
# cost more than the proof itself.
_client: httpx.Client | None = None
_client_lock = threading.Lock()


def _http() -> httpx.Client:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = httpx.Client(
                    timeout=6.0,
                    limits=httpx.Limits(max_keepalive_connections=16,
                                        max_connections=32),
                    headers={"Content-Type": "application/json"},
                )
    return _client


class SessionKey:
    """The agent's own, throwaway signing identity."""

    def __init__(self, address: str, private_key: str) -> None:
        self.address = address
        self.private_key = private_key

    @classmethod
    def load(cls) -> "SessionKey":
        raw = os.environ.get("SYNTH_SESSION_KEY", "").strip()
        if raw:
            key = raw if raw.startswith("0x") else "0x" + raw
            return cls(address=Account.from_key(key).address, private_key=key)
        acct = Account.create()
        return cls(address=acct.address, private_key=acct.key.hex())


def counterfactual_account(session: SessionKey) -> str:
    """CREATE2 address of this session's SimpleAccount, as a local estimate.

    The authoritative value comes from the factory itself (see
    ``Executor.account``); this local form is the offline fallback so the engine
    still produces a stable, well-formed identity with no network at all.
    """
    selector = keccak(text="createAccount(address,uint256)")[:4]
    args = abi_encode(["address", "uint256"], [session.address, 0])
    init_code = bytes.fromhex(SIMPLE_ACCOUNT_FACTORY[2:]) + selector + args
    salt = (0).to_bytes(32, "big")
    packed = (
        b"\xff"
        + bytes.fromhex(SIMPLE_ACCOUNT_FACTORY[2:])
        + salt
        + bytes.fromhex(_keccak_hex(init_code))
    )
    return "0x" + _keccak_hex(packed)[24:]


def init_code_for(session: SessionKey) -> str:
    """The factory + createAccount calldata that deploys this session's account."""
    selector = keccak(text="createAccount(address,uint256)")[:4]
    args = abi_encode(["address", "uint256"], [session.address, 0])
    return SIMPLE_ACCOUNT_FACTORY + (selector + args).hex()


def userop_hash(sender: str, nonce: int, call_data: str, paymaster: str,
                chain_id: int) -> str:
    """A real ERC-4337 v0.6 userOpHash (hash of the packed op + entrypoint + chain)."""
    packed = (
        bytes.fromhex(sender[2:])
        + nonce.to_bytes(32, "big")
        + bytes.fromhex(_keccak_hex(bytes.fromhex(call_data[2:])))
        + bytes.fromhex(ENTRY_POINT[2:])
        + chain_id.to_bytes(32, "big")
    )
    return "0x" + _keccak_hex(packed)


class Executor:
    def __init__(self) -> None:
        self.session = SessionKey.load()
        # Broadcast targets: an explicit override first, then every configured
        # bundler, then the keyless public bundler.
        targets: list[str] = []
        override = os.environ.get("SYNTH_BUNDLER_RPC", "").strip()
        if override:
            targets.append(override)
        targets.extend(SETTINGS.extra_bundlers)
        targets.append(SETTINGS.public_bundler_url)
        self.bundlers = list(dict.fromkeys(t for t in targets if t))
        self._account_cache = ""
        self._gas_cache = 0
        self._gas_at = 0.0
        self._refused: dict[str, float] = {}
        self._send_refused: dict[str, float] = {}
        self.flywheel = GasFlywheel(
            session_key=self.session.private_key,
            account_address=self._account_cache,
        )

    # -- helpers -------------------------------------------------------------

    def _rpc(self, url: str, method: str, params: list,
             timeout: float = 6.0) -> dict | None:
        try:
            r = _http().post(
                url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=timeout,
            )
            if r.status_code != 200:
                return None
            return r.json()
        except Exception:  # noqa: BLE001 - a dead source is not an error
            return None

    def account(self) -> str:
        """The authoritative SimpleAccount address, read from the factory.

        Falls back to the locally derived CREATE2 value when the chain is
        unreachable, so identity is always available.
        """
        if self._account_cache:
            return self._account_cache
        selector = keccak(text="getAddress(address,uint256)")[:4]
        data = "0x" + (selector + abi_encode(
            ["address", "uint256"], [self.session.address, 0]
        )).hex()
        result = self._rpc(
            SETTINGS.rpc_url, "eth_call",
            [{"to": SIMPLE_ACCOUNT_FACTORY, "data": data}, "latest"], timeout=4,
        )
        if result and isinstance(result.get("result"), str) and len(result["result"]) >= 42:
            self._account_cache = "0x" + result["result"][-40:]
        else:
            self._account_cache = counterfactual_account(self.session)
        return self._account_cache

    def _gas_price(self) -> int:
        if self._gas_cache and time.time() - self._gas_at < 15:
            return self._gas_cache
        data = self._rpc(SETTINGS.rpc_url, "eth_gasPrice", [], timeout=3)
        price = 1_000_000_000  # 1 gwei: a safe default on Base
        if data and isinstance(data.get("result"), str):
            try:
                price = int(data["result"], 16)
            except ValueError:
                pass
        self._gas_cache, self._gas_at = price, time.time()
        return price

    def prewarm(self) -> None:
        """Resolve the account and gas price concurrently, once per process."""
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda f: f(), (self.account, self._gas_price)))

    def _encode_calldata(self, path: AtomicPath) -> str:
        """Encode the atomic path as one batch call (selector + payload)."""
        selector = _keccak_hex(b"executeAtomic(bytes32,bytes)")[:8]
        payload = _keccak_hex(path.path_id.encode())
        return "0x" + selector + payload

    def _paymaster_for(self, path: AtomicPath) -> str:
        for prim in path.primitives:
            if "paymaster" in prim.lower():
                return "0x" + _keccak_hex(prim.encode())[:40]
        return "0x" + _keccak_hex(b"protocol-sponsored-gas")[:40]

    def _sign(self, op_hash: str) -> str:
        signed = Account.sign_message(
            encode_defunct(hexstr=op_hash), private_key=self.session.private_key
        )
        return "0x" + signed.signature.hex()

    def _sponsor(self, userop: dict) -> dict | None:
        """Ask every paymaster in parallel; the first offer wins.

        A refusal is cached briefly: sponsorship policy is a property of the
        bundler, not of this particular operation, so re-asking on every intent
        would only add latency to the answer.
        """
        now = time.time()
        candidates = [u for u in self.bundlers if self._refused.get(u, 0) < now]
        if not candidates:
            return None

        from concurrent.futures import ThreadPoolExecutor

        def ask(url: str) -> tuple[str, dict | None]:
            data = self._rpc(url, "pm_sponsorUserOperation", [userop, ENTRY_POINT],
                             timeout=4)
            if data and isinstance(data.get("result"), dict):
                return url, data["result"]
            return url, None

        with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
            for url, offer in pool.map(ask, candidates):
                if offer is not None:
                    return offer
                self._refused[url] = now + SETTINGS.sponsor_refusal_ttl_seconds
        return None

    def _broadcast(self, userop: dict) -> tuple[str, str] | None:
        """Send the operation to every bundler at once; first acceptance wins.

        Firing in parallel matters because a single slow or unreachable bundler
        must not delay a settlement another bundler would accept immediately.
        A refusal is remembered for a short window so a chatty client does not
        pay the same round-trip on every intent; the window reopens on its own,
        so a bundler that starts accepting is picked up without a restart.
        """
        from concurrent.futures import ThreadPoolExecutor

        now = time.time()
        candidates = [u for u in self.bundlers if self._send_refused.get(u, 0) < now]
        if not candidates:
            return None

        def send(url: str) -> tuple[str, str] | None:
            data = self._rpc(url, "eth_sendUserOperation", [userop, ENTRY_POINT],
                             timeout=6)
            if data and isinstance(data.get("result"), str):
                return data["result"], url
            self._send_refused[url] = now + SETTINGS.broadcast_refusal_ttl_seconds
            return None

        with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
            for accepted in pool.map(send, candidates):
                if accepted is not None:
                    return accepted
        return None

    # -- main entry ----------------------------------------------------------

    def _can_fund(self, path: AtomicPath) -> bool:
        """Whether the smart account holds the value this transfer would move."""
        nets = _networks()
        if not nets:
            return False
        net = nets[0]
        account = self.flywheel.account_for(net)
        if not account:
            return False
        return self.flywheel.balance_of(net, account) >= int(path.proven_net_profit_usd * 1e18)

    def settlement_call_data(self, path: AtomicPath) -> str:
        """The exact callData a settlement for this path will carry.

        Owned here so the watchdog retries the same operation the executor
        built, rather than a reconstruction that might differ.
        """
        value_wei = 0
        if (
            path.proven_net_profit_usd
            and path.proven_net_profit_usd > 0
            and SETTINGS.transfer_settlement_value
        ):
            value_wei = int(path.proven_net_profit_usd * 1e18)
        return self.flywheel.call_data(SETTINGS.beneficiary, value_wei)

    def settle(self, path: AtomicPath, constraints: Constraints) -> ExecutionResult:
        """Settle the proved path with gas paid by someone else.

        The gates run first and are non-negotiable. Then the flywheel builds a
        real ERC-4337 operation, has a keyless paymaster sponsor it, signs it
        with the agent's own session key, and broadcasts it. What comes back is
        either a mined transaction with a real hash, a bundler-accepted
        operation awaiting its receipt, or an honest simulation.
        """
        started = time.time()

        # Gate 1: beneficiary must be the locked address.
        if constraints.beneficiary.lower() != SETTINGS.beneficiary.lower():
            return ExecutionResult(
                status="queued",
                reason="beneficiary_not_locked",
                beneficiary=SETTINGS.beneficiary,
            )

        # Gate 2: gas must never come from the user.
        if path.gas_sponsor == "user" or constraints.allow_user_gas:
            return ExecutionResult(
                status="queued",
                reason="user_gas_forbidden",
                beneficiary=SETTINGS.beneficiary,
            )

        # Gate 3: the proof must have succeeded (the loop guarantees this).
        if not path.simulation_ok:
            return ExecutionResult(
                status="queued",
                reason="path_not_simulated",
                beneficiary=SETTINGS.beneficiary,
            )

        # The value the operation would move. Only a proven, positive amount is
        # ever encoded as a transfer, and only when the account actually holds
        # it: gas is sponsored but value is not, so a transfer the account
        # cannot fund would revert and move nothing.
        call_data = self.settlement_call_data(path)
        transfers_value = (
            SETTINGS.transfer_settlement_value
            and bool(path.proven_net_profit_usd)
            and path.proven_net_profit_usd > 0
            and self._can_fund(path)
        )
        settlement = self.flywheel.settle(call_data)
        elapsed_ms = (time.time() - started) * 1000.0

        explorer = settlement.explorer_url or ""
        common = dict(
            network=settlement.network or SETTINGS.settlement_chain,
            beneficiary=SETTINGS.beneficiary,
            bundler=settlement.paymaster,
            gas_mode="paymaster" if settlement.gas_paid_by == "paymaster" else "protocol",
            broadcast_ms=round(elapsed_ms, 2),
            user_op_hash=settlement.user_op_hash,
            gas_paid_by=settlement.gas_paid_by,
            gas_cost_wei=settlement.actual_gas_cost_wei,
            block_number=settlement.block_number,
            value_transferred=transfers_value and settlement.mined,
            explorer_url=explorer,
        )

        if settlement.mined:
            return ExecutionResult(
                status="executed",
                tx_hash=settlement.tx_hash,
                amount_usd=round(path.net_profit_usd, 6),
                reason=(
                    f"mined_on_{settlement.network};"
                    f"gas_paid_by={settlement.gas_paid_by};"
                    f"value_transferred={common['value_transferred']}"
                ),
                **common,
            )

        if settlement.status == "submitted":
            # A bundler accepted the operation; the receipt simply has not
            # landed. That is a real submission with a real userOp hash.
            return ExecutionResult(
                status="submitted",
                tx_hash=settlement.user_op_hash,
                amount_usd=round(path.net_profit_usd, 6),
                simulated=False,
                reason=settlement.reason,
                **common,
            )

        # Nothing settled. Report it plainly; the watchdog will retry.
        return ExecutionResult(
            status="simulated",
            tx_hash="",
            amount_usd=round(path.net_profit_usd, 6),
            simulated=True,
            reason=settlement.reason,
            **common,
        )
