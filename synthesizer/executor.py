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

import hashlib
import os
from pathlib import Path
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
        """Resolve the agent's signing identity, preferring stability.

        Order: the ``SYNTH_SESSION_KEY`` environment variable, then a key file
        on disk, then a newly generated key written to that file. Persisting
        matters because the smart account address is derived from this key: an
        identity that changes per process is an account that can never hold a
        balance, so value would have nowhere to accumulate. The key is the
        agent's own and never leaves the host.
        """
        raw = os.environ.get("SYNTH_SESSION_KEY", "").strip()
        if raw:
            key = raw if raw.startswith("0x") else "0x" + raw
            return cls(address=Account.from_key(key).address, private_key=key)

        path = Path(SETTINGS.session_key_path)
        try:
            stored = path.read_text().strip()
        except (FileNotFoundError, OSError):
            stored = ""
        if stored:
            key = stored if stored.startswith("0x") else "0x" + stored
            try:
                return cls(address=Account.from_key(key).address, private_key=key)
            except ValueError:
                pass  # Corrupt file: fall through and mint a fresh identity.

        acct = Account.create()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(acct.key.hex())
            path.chmod(0o600)
        except OSError:
            # Read-only filesystem: an ephemeral identity still works, it just
            # will not survive a restart.
            pass
        return cls(address=acct.address, private_key=acct.key.hex())


def counterfactual_account(session: SessionKey) -> str:
    """A deterministic local label for this session's account.

    This is NOT the on-chain account address. The factory's CREATE2 inputs are
    version-specific (implementation, owner and salt in some releases, the
    initCode hash in others), and a wrong guess is indistinguishable from a
    right one until a settlement silently targets an address that holds
    nothing. Use ``GasFlywheel.account_for`` for the authoritative address;
    this exists only so offline code has a stable string to log.
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


def _settlement_key(path: AtomicPath) -> str:
    """The lock's one-shot key for this settlement.

    The lock records every intentId it settles and rejects a repeat, so the key
    has to identify the settlement rather than the beneficiary. A path without
    an intent id still needs a key that is stable for that path and distinct
    from every other, which its own economics supply.
    """
    if path.intent_id:
        return path.intent_id
    material = (
        f"{path.proven_net_profit_usd}:{path.notional_usd}:"
        f"{','.join(path.primitives)}"
    )
    return hashlib.sha256(material.encode()).hexdigest()[:32]


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
        # The engine-owned Fuel chain. Built on first use, never at import, so
        # importing the executor does not start a node.
        self._fuel = None
        self._fuel_gas = None

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
        """Whether the smart account holds the value this settlement would move.

        A lock settlement moves an ERC20 amount, so the funding check has to
        read that token. Reading native balance for a token settlement reports
        zero for an account that is in fact funded, which would suppress a
        settlement that could have succeeded.
        """
        nets = _networks()
        if not nets:
            return False
        net = nets[0]
        account = self.flywheel.account_for(net)
        if not account:
            return False
        if SETTINGS.gas_value_lock:
            held = self.flywheel.token_balance_of(net, SETTINGS.value_token, account)
            return held >= int(path.proven_net_profit_usd * 1e6)
        return self.flywheel.balance_of(net, account) >= int(path.proven_net_profit_usd * 1e18)

    def _beneficiary_balance(self, net) -> int:
        """The beneficiary's balance, in whatever unit the settlement moves.

        Gate B compares this before and after, so it has to measure the same
        asset the settlement transfers. A token settlement judged against the
        native balance would always read zero growth and mislabel a real payout
        as a reverted one.
        """
        if SETTINGS.gas_value_lock:
            return self.flywheel.token_balance_of(net, SETTINGS.value_token, SETTINGS.beneficiary)
        return self.flywheel.balance_of(net, SETTINGS.beneficiary)

    def sweep_landed(self, net, before: int, expected: int) -> tuple[bool, str, int]:
        """Gate B: did the beneficiary's balance actually grow by the profit?

        Gate A (``receipt.status == 1``) only proves the operation executed. It
        says nothing about whether value moved: a sponsored no-op executes
        perfectly and pays nobody. The only evidence that money arrived is the
        beneficiary's balance after minus before, which is why this is measured
        rather than inferred from the receipt.
        """
        after = self._beneficiary_balance(net)
        delta = after - before
        if delta <= 0:
            return False, f"balance_unchanged(before={before},after={after})", delta
        if expected > 0 and delta < expected:
            return False, f"balance_short(delta={delta},expected={expected})", delta
        return True, f"balance_grew(delta={delta})", delta

    def settlement_call_data(self, path: AtomicPath) -> str:
        """The exact callData a settlement for this path will carry.

        Owned here so the watchdog retries the same operation the executor
        built, rather than a reconstruction that might differ.

        When a GasValueLock is configured the settlement is expressed as a call
        into that contract instead of a bare transfer. The lock is what makes
        gas and value one indivisible step: it pays the relayer out of the
        settled value and sends the remainder to the locked beneficiary, or
        reverts and moves nothing.
        """
        if SETTINGS.gas_value_lock and self._lock_settles_in_token(path):
            return self._lock_call_data(path)

        value_wei = 0
        if (
            path.proven_net_profit_usd
            and path.proven_net_profit_usd > 0
            and SETTINGS.transfer_settlement_value
        ):
            value_wei = int(path.proven_net_profit_usd * 1e18)
        return self.flywheel.call_data(SETTINGS.beneficiary, value_wei)

    def _lock_settles_in_token(self, path: AtomicPath) -> bool:
        """Whether this path can settle through the lock.

        The lock settles an ERC20 profit, so it only applies to a proved,
        positive amount and only once the account actually holds that value.
        Claiming a lock settlement for a balance the account does not have
        would revert, which is worse than a sponsored no-op because it burns
        the relayer's gas for nothing.
        """
        if not path.proven_net_profit_usd or path.proven_net_profit_usd <= 0:
            return False
        nets = _networks()
        if not nets:
            return False
        account = self.flywheel.account_for(nets[0])
        if not account:
            return False
        held = self.flywheel.token_balance_of(
            nets[0], SETTINGS.value_token, account
        )
        return held >= int(path.proven_net_profit_usd * 1e6)

    def _lock_call_data(self, path: AtomicPath) -> str:
        """Settlement calldata targeting the lock, with the proved split.

        The relayer fee is priced in value from the expected gas, so the
        contract's ``netProfit > relayerFee`` guard reflects a real cost rather
        than an arbitrary constant.
        """
        from .valuegas import build_settle_calldata, estimate_relayer_fee, split_value

        net_profit = int(path.proven_net_profit_usd * 1e6)
        fee = estimate_relayer_fee(200_000, 10**9, 3000.0)
        # Never let the fee consume the whole profit; if the estimate would,
        # take a slice small enough to leave the beneficiary a positive amount.
        fee = min(fee, max(net_profit // 10, 1))
        split = split_value(net_profit, fee, SETTINGS.gas_reserve_bps)
        inner = build_settle_calldata(
            intent_id=_settlement_key(path),
            token=SETTINGS.value_token,
            relayer=SETTINGS.beneficiary,
            net_profit=net_profit,
            relayer_fee=fee,
            reserve_bps=SETTINGS.gas_reserve_bps,
            proof_satisfied=bool(path.simulation_ok and split.is_positive),
        )
        # The smart account calls the lock; value stays in the account until
        # the lock pulls it, so the outer call carries no native value.
        return self.flywheel.call_data(SETTINGS.gas_value_lock, 0, inner)

    def _ensure_lock_allowance(self, net, path: AtomicPath) -> bool:
        """Grant the lock an allowance for the amount it is about to pull.

        The lock settles with ``transferFrom``, so a settlement without an
        allowance reverts inside the account. That revert looks like a gas
        failure from the outside, which points at the wrong cause, so the
        approval is made here rather than left to chance.

        Returns whether an allowance is in place. A failure to approve is not
        fatal: the settlement attempt that follows reports its own outcome.
        """
        if net is None or not SETTINGS.gas_value_lock or not SETTINGS.value_token:
            return False
        account = self.flywheel.account_for(net)
        if not account:
            return False
        needed = int(path.proven_net_profit_usd * 1e6)
        if needed <= 0:
            return False
        if self.flywheel.token_allowance(net, SETTINGS.value_token, account,
                                         SETTINGS.gas_value_lock) >= needed:
            return True
        approve = (
            keccak(text="approve(address,uint256)")[:4]
            + abi_encode(["address", "uint256"], [SETTINGS.gas_value_lock, needed])
        )
        step = self.flywheel.settle(
            self.flywheel.call_data(SETTINGS.value_token, 0, approve)
        )
        return step.mined

    def expected_beneficiary_delta(self, path: AtomicPath, transfers_value: bool) -> int:
        """What the beneficiary's balance should grow by, in the settled unit.

        Gate B compares this against a measured delta, so the expectation has to
        be denominated in the same asset and net of the same deductions the
        contract makes. Comparing a token delta against a wei expectation, or
        against the gross profit, marks a correct settlement as short.
        """
        if not transfers_value or not path.proven_net_profit_usd:
            return 0
        from .valuegas import estimate_relayer_fee, split_value

        if SETTINGS.gas_value_lock:
            net_profit = int(path.proven_net_profit_usd * 1e6)
            fee = estimate_relayer_fee(200_000, 10**9, 3000.0)
            fee = min(fee, max(net_profit // 10, 1))
            split = split_value(net_profit, fee, SETTINGS.gas_reserve_bps)
            # The relayer fee is paid to SETTINGS.beneficiary, so it lands in
            # the same balance the gate measures.
            return split.to_beneficiary + split.relayer_fee
        return int(path.proven_net_profit_usd * 1e18)

    def settle(self, path: AtomicPath, constraints: Constraints,
               fuel_only: bool = False) -> ExecutionResult:
        """Settle the proved path with gas paid by someone else.

        The gates run first and are non-negotiable. Then the flywheel builds a
        real ERC-4337 operation, has a keyless paymaster sponsor it, signs it
        with the agent's own session key, and broadcasts it. What comes back is
        either a mined transaction with a real hash, a bundler-accepted
        operation awaiting its receipt, or an honest simulation.

        ``fuel_only`` stops after the engine-owned chain. The request path uses
        it because the sponsor ladder is unbounded network work: a caller must
        not wait on a third party to receive an answer, and the flywheel runs
        that ladder in the background instead.
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
        # An operation that would carry no value and no calldata is an empty
        # call to the beneficiary: it mines cleanly, spends the sponsor's gas
        # and pays nobody. That is never a settlement, so it is refused here
        # rather than broadcast as one.
        if not (path.proven_net_profit_usd and path.proven_net_profit_usd > 0):
            return ExecutionResult(
                status="queued",
                reason="no_settlement_amount",
                beneficiary=SETTINGS.beneficiary,
            )
        # Fuel first. The engine owns this chain outright: it runs the node,
        # deployed the Sway ledger, and pays gas from genesis supply, so there is
        # no sponsor to wait on and no third party to be offline. Only when that
        # leg cannot deliver does the sponsored EVM ladder run.
        if SETTINGS.fuel_primary:
            fueled = self._settle_on_fuel(path, constraints, started)
            if fueled is not None:
                return fueled
            if fuel_only:
                return ExecutionResult(
                    status="pending",
                    reason="fuel_leg_unavailable",
                    beneficiary=SETTINGS.beneficiary,
                    network="fuel-devnet",
                )

        transfers_value = (
            SETTINGS.transfer_settlement_value
            and bool(path.proven_net_profit_usd)
            and path.proven_net_profit_usd > 0
            and self._can_fund(path)
        )
        # Read the beneficiary's balance *before* the operation so the sweep can
        # be judged on a measured delta rather than on the receipt alone.
        nets = _networks()
        pre_net = nets[0] if nets else None
        balance_before = self._beneficiary_balance(pre_net) if pre_net else 0

        # The lock pulls value with transferFrom, so it needs an allowance the
        # account has granted. Without one the pull reverts and the paymaster
        # refuses to sponsor, which reads as "no sponsorship" rather than the
        # missing approval it really is. Approve first, then settle.
        if transfers_value and SETTINGS.gas_value_lock:
            self._ensure_lock_allowance(pre_net, path)

        # The direct leg addresses the lock itself, so it is given the lock
        # call rather than the account wrapper. Without this the plain signed
        # transaction would carry the wrapper's calldata to the lock, which
        # would not decode as a settlement.
        direct_to, direct_data = "", ""
        if SETTINGS.gas_value_lock and self._lock_settles_in_token(path):
            direct_to = SETTINGS.gas_value_lock
            direct_data = self._lock_call_data(path)
        settlement = self.flywheel.settle(
            call_data, direct_to=direct_to, direct_data=direct_data
        )
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
            value_transferred=False,
            explorer_url=explorer,
        )

        if settlement.mined:
            # Gate A passed: the operation is on chain with status 1.
            net = next(
                (n for n in _networks() if n.chain_id == settlement.chain_id), pre_net
            )
            expected = self.expected_beneficiary_delta(path, transfers_value)
            landed, evidence, delta = (
                self.sweep_landed(net, balance_before, expected)
                if net else (False, "no_network", 0)
            )
            common["value_transferred"] = landed
            # The settled unit is the value token when a lock is in play, and
            # native wei otherwise. Reporting the gross profit here would
            # overstate the payout by the reserve and the relayer fee.
            scale = 1e6 if SETTINGS.gas_value_lock else 1e18
            common["delivered"] = round(max(delta, 0) / scale, 6)
            if not landed:
                # Gate A passed but Gate B failed: the transaction executed and
                # moved nothing. Calling that "executed" would be a claim about
                # money that never moved, so it is labelled for what it is and
                # handed to the flywheel for a deeper path. The label names the
                # next step rather than declaring the intent lost.
                return ExecutionResult(
                    status="unsettled_value",
                    tx_hash=settlement.tx_hash,
                    amount_usd=0.0,
                    reason=(
                        f"mined_on_{settlement.network}_but_{evidence};"
                        f"gas_paid_by={settlement.gas_paid_by}"
                    ),
                    **common,
                )
            # The settlement paid for itself and the beneficiary gained value.
            # Fold the reserve slice into the reinvestment loop so the next
            # settlement draws on gas this one earned.
            if self.flywheel is not None:
                self.flywheel.record_settlement_gas(
                    settlement.actual_gas_cost_wei, path, landed=True
                )
            return ExecutionResult(
                status="executed",
                tx_hash=settlement.tx_hash,
                amount_usd=round(path.net_profit_usd, 6),
                reason=(
                    f"mined_on_{settlement.network};"
                    f"gas_paid_by={settlement.gas_paid_by};"
                    f"value_transferred=True;{evidence}"
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

        # The public chain did not settle. When the fork fallback is enabled the
        # engine executes the same settlement against real EVM state and records
        # a proof of it, which is the leg that actually completes when no
        # sponsor is reachable. It is labelled local-fork, because a fork payout
        # is not a public-chain delivery and must not be read as one.
        if SETTINGS.settle_on_fork:
            return self._settle_on_fork(path, constraints, settlement.reason, common)
        return ExecutionResult(
            status="pending",
            tx_hash="",
            amount_usd=round(path.net_profit_usd, 6),
            simulated=False,
            reason=settlement.reason,
            **common,
        )

    def _settle_on_fuel(
        self, path: AtomicPath, constraints: Constraints, started: float
    ) -> ExecutionResult | None:
        """Record the settlement on the engine-owned SwayVM chain.

        Returns ``None`` only when the leg is not usable at all, which lets the
        caller fall through to the sponsored EVM ladder. Once the leg answers,
        its answer is final: a rejection or an unarrived delta is reported as
        such rather than quietly retried somewhere else, because a second venue
        would be a different settlement of the same intent.
        """
        hand = self._fuel_hand()
        if hand is None or not hand.available():
            return None

        # The Sway ledger works in integer micro-units of the value asset.
        net = path.proven_net_profit_usd or 0.0
        if net <= 0:
            return None
        # The ledger credits exactly the proved net profit. Padding the delta
        # with a gross figure would make the reported delivery larger than the
        # amount the proof certified, which is a claim the proof does not
        # support, so the two are kept identical here.
        net_units = int(round(net * 1_000_000))
        delta_units = net_units

        commitment = self._fuel_commitment(path)
        settlement = hand.settle(delta_units, net_units, commitment)
        if settlement.status == "unavailable":
            return None
        if settlement.status == "rejected" and "already on the ledger" in settlement.reason:
            # This exact settlement already paid. Report the original
            # transaction rather than calling it pending: the money did arrive,
            # just on an earlier run, and pretending otherwise would be a lie in
            # the other direction.
            previous = hand.journal_lookup(commitment)
            if previous:
                settlement = hand.replay_result(commitment, previous)

        self._fuel_gas.record(settlement)
        elapsed_ms = (time.time() - started) * 1000.0
        # Report the measured gain, never the amount we asked the ledger for.
        measured_gain = int(settlement.detail.get("delivered_gain", 0)) or settlement.delivered
        reason = (
            f"recorded_on_{settlement.network};gas_paid_by={settlement.gas_paid_by};"
            f"block={settlement.block_height};delivered={settlement.delivered}"
        )
        if settlement.reason:
            reason = f"{reason};{settlement.reason}"
        return ExecutionResult(
            status="executed" if settlement.paid else "pending",
            tx_hash=settlement.tx_id,
            explorer_url=settlement.explorer_url,
            network=settlement.network,
            beneficiary=SETTINGS.beneficiary,
            amount_usd=round(net, 6),
            reason=reason,
            simulated=False,
            bundler="fuel-core",
            gas_mode="genesis-supply",
            broadcast_ms=round(elapsed_ms, 2),
            gas_paid_by=settlement.gas_paid_by,
            gas_cost_wei=settlement.gas_used,
            block_number=int(settlement.block_height or 0),
            value_transferred=settlement.paid,
            delivered=round(measured_gain / 1_000_000, 6),
        )

    def _fuel_hand(self):
        """Lazily build the Fuel hand so importing the executor costs nothing."""
        if self._fuel is None:
            try:
                from .fuel import FuelHand, GasAbstraction

                self._fuel = FuelHand()
                self._fuel_gas = GasAbstraction()
            except Exception:
                self._fuel = False
        return self._fuel or None

    @staticmethod
    def _fuel_commitment(path: AtomicPath) -> str:
        """A deterministic commitment for this path, so a replay is detectable.

        It hashes the path's own identity — its intent, its steps, and the amount
        the proof certified — so the same settlement can never be recorded twice
        under two different commitments.
        """
        import hashlib

        steps = ";".join(
            f"{s.primitive}:{s.action}:{s.net_rate}" for s in path.steps
        )
        seed = "|".join(
            [path.intent_id, steps, str(path.proven_net_profit_usd),
             str(path.notional_usd)]
        )
        return "0x" + hashlib.sha256(seed.encode()).hexdigest()

    def _settle_on_fork(
        self,
        path: AtomicPath,
        constraints: Constraints,
        upstream_reason: str,
        common: dict,
    ) -> ExecutionResult:
        """Settle by executing on the fork and proving the result.

        The delta recorded here is read out of fork state, not taken from the
        path's predicted profit, so a path whose prediction was optimistic
        settles for what it actually moved.
        """
        from .bootstrap import Bootstrap

        notional = int(round(path.proven_net_profit_usd * 1e6)) if path.proven_net_profit_usd else 0
        floor = int(round(max(constraints.net_profit_usd_min, 0.0) * 1e6))
        if notional <= 0:
            return ExecutionResult(
                status="pending",
                tx_hash="",
                amount_usd=round(path.net_profit_usd, 6),
                simulated=False,
                reason=f"{upstream_reason};fork_skipped_no_amount",
                **common,
            )
        try:
            run = Bootstrap(beneficiary=SETTINGS.beneficiary).run(
                path.intent_id, min_profit=floor, notional=notional
            )
        except Exception as exc:  # the fork is local; a fault here is a bug
            return ExecutionResult(
                status="pending",
                tx_hash="",
                amount_usd=round(path.net_profit_usd, 6),
                simulated=False,
                reason=f"{upstream_reason};fork_fault={type(exc).__name__}",
                **common,
            )

        if not run.delivered or run.settlement is None:
            # The fork ran and measured what it moved. When that is below the
            # floor the intent asked for, no settlement exists to record, and
            # the reason says which of the two it was.
            return ExecutionResult(
                status="unsettled_value",
                tx_hash="",
                amount_usd=0.0,
                simulated=True,
                reason=f"{upstream_reason};fork_not_delivered;{run.notes[-1] if run.notes else 'no_value'}",
                **common,
            )

        settlement = run.settlement
        return ExecutionResult(
            status="executed",
            tx_hash=settlement.tx_hash,
            explorer_url=f"local-fork://{settlement.gateway}",
            network="local-fork",
            beneficiary=SETTINGS.beneficiary,
            amount_usd=round(run.delta / 1e6, 6),
            reason=(
                f"fork_settled;verified_onchain=True;unit={run.value_unit};"
                f"money_is_real={str(run.money_is_real).lower()};"
                f"delta={run.delta};commitment={run.commitment[:18]}"
            ),
            simulated=True,
            gas_mode="protocol",
            gas_paid_by="fork",
            gas_cost_wei=0,
            block_number=settlement.block,
            value_transferred=True,
            delivered=round(run.token_delivered / 1e6, 6),
        )
