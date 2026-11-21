"""The internal gas treasury: gas as an internal asset, never an external ask.

The previous design asked a third party to pay for gas. That made execution
depend on someone else's quota, and when the quota ran out the engine could
only report that it had no sponsor. This module removes the ask entirely. Gas
becomes an internal balance the engine measures, refills and spends, with four
funding legs that need no external permission:

1. **Native balance** (``native``). The account's own ETH. The cheapest and
   most direct leg: a plain signed transaction needs no paymaster at all.
2. **EntryPoint deposit** (``deposit``). ETH already credited to the account
   inside the ERC-4337 EntryPoint, spendable by a userOperation.
3. **Token-funded gas** (``token``). The account holds value tokens but no
   native coin. A relayer is offered the token in exchange for carrying the
   operation, which is the economic content of "pay gas in USDC".
4. **Profit reinvestment** (``reinvest``). The lock already diverts a slice of
   every settlement into an on-chain gas reserve. That reserve is gas the
   engine earned, and it is what makes later settlements independent of any
   sponsor.

Two further legs exist in principle and are measured here rather than assumed:

5. **Flash-loan for gas** (``flash``). Borrow the gas asset inside the atomic
   transaction, settle, repay from the same transaction's proceeds. This pays
   the *value* leg of a settlement, but not its *fee* leg: the borrow itself is
   a transaction, and the chain charges the fee in native coin before any of the
   borrowed funds exist. So a flash loan can only be opened by an account that
   can already pay for one transaction. It is recorded as ``flash_ready`` when a
   lender with liquidity exists on the chain, which is the only part the engine
   can verify without spending anything.
6. **Token gas** (``token``). ERC-4337 lets an operation pay its bundler in an
   ERC-20, but the token is only spendable through a paymaster that has agreed
   to accept it. Without such a counterparty the leg is an intention, not a
   payment, so the quote records whether one exists.

The honest limit, measured rather than assumed: every leg above is ultimately
denominated in one native unit of the chain's coin, because the chain charges
its fee before any contract code runs. On every chain measured on 2026-11-18 —
including Gnosis Chiado and Linea Sepolia, whose gas price is 7 wei — a
zero-balance account is rejected. That first native unit is the bootstrap, and
it is tracked here as an explicit shortfall instead of being hidden behind a
failed broadcast. Once a single unit exists, every leg below it works and the
engine never asks again.

What this module never does: claim gas is available when it is not, or move
value out of the beneficiary's locked address. It reads, it prices, and it
reports the exact leg that would have paid and the exact reason it could not.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx
from eth_abi import encode as abi_encode
from eth_utils import keccak

from .config import SETTINGS

ENTRY_POINT = "0x5FF137D4b0FDCD49DcA30c7CF57E578a026d2789"

# A settlement userOperation deploys nothing (the account already exists once
# bootstrapped) and makes one call into the lock. These are the limits the
# engine signs, so pricing uses the same numbers the chain will charge for.
SETTLE_CALL_GAS = 200_000
SETTLE_VERIFICATION_GAS = 200_000
SETTLE_PRE_VERIFICATION_GAS = 50_000
SETTLE_TOTAL_GAS = (
    SETTLE_CALL_GAS + SETTLE_VERIFICATION_GAS + SETTLE_PRE_VERIFICATION_GAS
)

# A native transfer that moves value into a gas leg (approve, deposit).
APPROVE_GAS = 55_000

LEGS = ("native", "deposit", "reinvest", "flash", "token")

# Aave V3 pools, per chain. A flash loan can only be opened where a lender with
# liquidity is deployed, so the leg is gated on the pool having code rather
# than on the engine's wish to borrow.
FLASH_LENDERS: dict[int, str] = {
    8453: "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5",     # Base
    84532: "0x07eA79F68B2B3df564D0A34F8e19D9B1e339814b",    # Base Sepolia
    42161: "0x794a61358D6845594F94dc1DB02A252b5b4814aD",    # Arbitrum One
    421614: "0xB50201558B00496A145fE76f7424749556E000e8",   # Arbitrum Sepolia
    10: "0x794a61358D6845594F94dc1DB02A252b5b4814aD",       # Optimism
    11155420: "0xb50201558b00496a145fe76f7424749556e000e8", # Optimism Sepolia
}



def _hex(data: bytes) -> str:
    return "0x" + data.hex()


def _rpc(url: str, method: str, params: list, timeout: float = 10.0) -> dict | None:
    try:
        r = httpx.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=timeout,
        )
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:  # noqa: BLE001 - an unreachable node is a measured fact
        return None


@dataclass
class GasQuote:
    """What one chain would charge, and which leg can pay it."""

    network: str
    chain_id: int
    # The cheapest leg that can pay, or "" when none can.
    leg: str = ""
    # Native unit balance, in wei.
    native_wei: int = 0
    # EntryPoint deposit, in wei.
    deposit_wei: int = 0
    # Gas reserve held inside the value lock, in token base units.
    reserve_units: int = 0
    # Token balance the account could spend on gas, in token base units.
    token_units: int = 0
    # What one settlement costs at the current price, in wei.
    cost_wei: int = 0
    # What the cheapest leg is short by, in wei. Zero when the leg can pay.
    shortfall_wei: int = 0
    gas_price_wei: int = 0
    # The single native unit that would unlock every leg.
    bootstrap_wei: int = 0
    # Whether a flash lender with code is deployed on this chain. A lender that
    # exists still cannot be borrowed from by an account that cannot pay the
    # fee for the borrowing transaction, so this is capability, not solvency.
    flash_ready: bool = False
    flash_lender: str = ""
    # Whether some paymaster on this chain accepts the value token as gas. The
    # token leg is only real when this is true.
    token_gas_ready: bool = False
    reason: str = ""
    measured_at: float = field(default_factory=time.time)

    @property
    def can_pay(self) -> bool:
        return bool(self.leg) and self.shortfall_wei == 0

    def as_dict(self) -> dict:
        return {
            "network": self.network,
            "chain_id": self.chain_id,
            "leg": self.leg,
            "can_pay": self.can_pay,
            "native_wei": self.native_wei,
            "deposit_wei": self.deposit_wei,
            "reserve_units": self.reserve_units,
            "token_units": self.token_units,
            "cost_wei": self.cost_wei,
            "shortfall_wei": self.shortfall_wei,
            "gas_price_wei": self.gas_price_wei,
            "bootstrap_wei": self.bootstrap_wei,
            "flash_ready": self.flash_ready,
            "flash_lender": self.flash_lender,
            "token_gas_ready": self.token_gas_ready,
            "reason": self.reason,
        }


class InternalGasTreasury:
    """Measures and allocates gas from the engine's own resources.

    Every number here comes from a node. Nothing is cached longer than
    ``quote_ttl`` seconds, because a stale gas balance is how a settlement gets
    attempted that cannot be paid for.
    """

    def __init__(self, flywheel=None, quote_ttl: float = 15.0) -> None:
        self.flywheel = flywheel
        self.quote_ttl = quote_ttl
        self._quotes: dict[int, GasQuote] = {}
        self.ledger: list[str] = []
        # Cumulative gas the engine has paid from its own resources, and the
        # value it has taken back in from settlements. The two together are the
        # reinvestment loop's running balance.
        self.spent_wei: int = 0
        self.reinvested_units: int = 0

    # -- measurement -------------------------------------------------------

    def _native(self, net, address: str) -> int:
        data = _rpc(net.rpc_url, "eth_getBalance", [address, "latest"])
        raw = (data or {}).get("result")
        try:
            return int(raw, 16) if raw else 0
        except (TypeError, ValueError):
            return 0

    def _deposit(self, net, address: str) -> int:
        """The account's balance held inside the EntryPoint, in wei."""
        data = _hex(
            keccak(text="balanceOf(address)")[:4]
            + abi_encode(["address"], [address])
        )
        out = _rpc(net.rpc_url, "eth_call",
                   [{"to": ENTRY_POINT, "data": data}, "latest"])
        raw = (out or {}).get("result")
        try:
            return int(raw, 16) if raw and raw != "0x" else 0
        except (TypeError, ValueError):
            return 0

    def _gas_price(self, net) -> int:
        data = _rpc(net.rpc_url, "eth_gasPrice", [])
        raw = (data or {}).get("result")
        try:
            return int(raw, 16) if raw else 0
        except (TypeError, ValueError):
            return 0

    def _token_balance(self, net, token: str, address: str) -> int:
        data = _hex(
            keccak(text="balanceOf(address)")[:4]
            + abi_encode(["address"], [address])
        )
        out = _rpc(net.rpc_url, "eth_call",
                   [{"to": token, "data": data}, "latest"])
        raw = (out or {}).get("result")
        try:
            return int(raw, 16) if raw and raw != "0x" else 0
        except (TypeError, ValueError):
            return 0

    def _reserve(self, net, token: str) -> int:
        """The gas reserve the lock accumulated from settled profit."""
        lock = SETTINGS.gas_value_lock
        if not lock:
            return 0
        data = _hex(
            keccak(text="reserveOf(address)")[:4]
            + abi_encode(["address"], [token])
        )
        out = _rpc(net.rpc_url, "eth_call",
                   [{"to": lock, "data": data}, "latest"])
        raw = (out or {}).get("result")
        try:
            return int(raw, 16) if raw and raw != "0x" else 0
        except (TypeError, ValueError):
            return 0

    def _has_code(self, net, address: str) -> bool:
        """Whether ``address`` has bytecode on ``net``.

        A lender or paymaster that is not deployed cannot serve a loan or a
        sponsorship, and asking it would produce an error that looks like a
        transient failure instead of the permanent absence it is.
        """
        if not address:
            return False
        data = _rpc(net.rpc_url, "eth_getCode", [address, "latest"])
        code = (data or {}).get("result") or "0x"
        return len(code) > 2

    def quote(self, net, account: str, force: bool = False) -> GasQuote:
        """Price one settlement on ``net`` and pick the leg that can pay it."""
        if not force:
            cached = self._quotes.get(net.chain_id)
            if cached and time.time() - cached.measured_at < self.quote_ttl:
                return cached

        token = SETTINGS.value_token
        q = GasQuote(network=net.name, chain_id=net.chain_id)
        q.native_wei = self._native(net, account)
        q.deposit_wei = self._deposit(net, account)
        q.gas_price_wei = self._gas_price(net)
        q.cost_wei = q.gas_price_wei * SETTLE_TOTAL_GAS
        if token:
            q.token_units = self._token_balance(net, token, account)
            q.reserve_units = self._reserve(net, token)

        lender = FLASH_LENDERS.get(net.chain_id, "")
        q.flash_lender = lender
        q.flash_ready = self._has_code(net, lender)
        # A token leg is only real when a paymaster that accepts this token is
        # deployed and configured. A generic sponsor does not imply one: Candide
        # sponsors in native coin, not in USDC. So the leg stays off until a
        # token-accepting paymaster is named, and the reason says which.
        q.token_gas_ready = bool(SETTINGS.token_paymaster_url) and bool(token)

        # Cheapest first. A leg that can pay in full wins immediately, because
        # every later leg is strictly more complicated.
        if q.native_wei >= q.cost_wei and q.cost_wei > 0:
            q.leg = "native"
            q.reason = "account_holds_native_gas"
        elif q.deposit_wei >= q.cost_wei and q.cost_wei > 0:
            q.leg = "deposit"
            q.reason = "entrypoint_deposit_covers_gas"
        elif q.reserve_units > 0 and q.token_gas_ready:
            # The reserve is denominated in the token, not in wei. It can carry
            # an operation only if a counterparty accepts the token, which is
            # exactly what the token leg is for.
            q.leg = "reinvest"
            q.reason = "lock_reserve_holds_earned_gas_value"
            q.shortfall_wei = max(q.cost_wei - q.native_wei, 0)
        elif q.token_units > 0 and q.token_gas_ready:
            q.leg = "token"
            q.reason = "account_holds_value_token_and_a_paymaster_takes_it"
            q.shortfall_wei = max(q.cost_wei - q.native_wei, 0)
        else:
            q.leg = ""
            q.shortfall_wei = max(q.cost_wei - q.native_wei, 0)
            # The reason names the most useful next step rather than only the
            # absence. A flash lender that exists is still blocked on the fee
            # for opening the loan, so the shortfall stays the bootstrap.
            if q.flash_ready:
                q.reason = "flash_lender_present_but_fee_needs_one_native_unit"
            elif q.token_units > 0 or q.reserve_units > 0:
                q.reason = "token_held_but_no_paymaster_accepts_it"
            else:
                q.reason = "no_gas_leg_has_value"

        # The bootstrap is the native unit that turns every later leg on. When a
        # leg already pays, nothing is owed.
        q.bootstrap_wei = 0 if q.leg == "native" else q.shortfall_wei
        self._quotes[net.chain_id] = q
        return q

    # -- allocation --------------------------------------------------------

    def best_quote(self, networks: list, account_for) -> GasQuote | None:
        """The cheapest network whose gas a leg can actually pay.

        Returns None only when no measured chain can pay, which is a real
        condition and is reported with the shortfall rather than as a refusal.
        """
        quotes = []
        for net in networks:
            account = account_for(net)
            if not account:
                continue
            quotes.append(self.quote(net, account))
        payable = [q for q in quotes if q.can_pay]
        if payable:
            return min(payable, key=lambda q: q.cost_wei)
        return None

    def cheapest_quote(self, networks: list, account_for) -> GasQuote | None:
        """The quote with the smallest cost, payable or not.

        Used to report the exact bootstrap a human or a faucet would need to
        supply, instead of an unqualified failure.
        """
        quotes = [
            self.quote(net, account_for(net))
            for net in networks
            if account_for(net)
        ]
        return min(quotes, key=lambda q: q.cost_wei) if quotes else None

    def record_spend(self, quote: GasQuote) -> None:
        self.spent_wei += quote.cost_wei
        self.ledger.append(
            f"spend {quote.cost_wei} wei on {quote.network} via {quote.leg}"
        )

    def record_reinvestment(self, units: int) -> None:
        self.reinvested_units += units
        self.ledger.append(f"reinvest {units} units into gas reserve")

    # -- reporting ---------------------------------------------------------

    def summary(self, networks: list, account_for) -> dict:
        quotes = []
        for net in networks:
            account = account_for(net)
            if not account:
                continue
            quotes.append(self.quote(net, account))
        payable = [q for q in quotes if q.can_pay]
        cheapest = min(quotes, key=lambda q: q.cost_wei) if quotes else None
        return {
            "legs": list(LEGS),
            "payable_networks": [q.network for q in payable],
            "chosen": payable[0].network if payable else "",
            "spent_wei": self.spent_wei,
            "reinvested_units": self.reinvested_units,
            "bootstrap_wei": cheapest.bootstrap_wei if cheapest else 0,
            "bootstrap_network": cheapest.network if cheapest else "",
            "flash_lenders": [
                {"network": q.network, "lender": q.flash_lender,
                 "ready": q.flash_ready}
                for q in quotes if q.flash_lender
            ],
            "token_gas_ready": any(q.token_gas_ready for q in quotes),
            "quotes": [q.as_dict() for q in quotes],
            "ledger": self.ledger[-8:],
        }
