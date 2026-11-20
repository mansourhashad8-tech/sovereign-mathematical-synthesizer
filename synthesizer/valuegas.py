"""The value-gas lock client.

Everything here exists to remove one dependency: an agent that holds value but
no ETH could not previously settle, because broadcasting a transaction needs
gas and gas needs ETH. The lock lets the party that holds gas and the party
that holds value be different, joined inside a single atomic call:

  - the value holder signs an EIP-2612 permit off-chain, which costs nothing
    and requires no ETH at all,
  - the relayer carries that signature into the settlement transaction it is
    already paying for,
  - the contract pays the relayer out of the settled value, then sends the
    remainder to the locked beneficiary and a slice to the gas reserve.

Either the whole split happens or nothing does. There is no intermediate state
in which the value has moved and the relayer is unpaid, which is the state that
used to strand payouts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from eth_abi import decode, encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

from .config import SETTINGS

CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"

BENEFICIARY = "0xE3d1c7dCD5E95b6441716b65F9C46b73e91b4d47"

PERMIT_TYPEHASH = keccak(
    text="Permit(address owner,address spender,uint256 value,uint256 nonce,uint256 deadline)"
)
DOMAIN_TYPEHASH = keccak(
    text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)

SEL_SETTLE = keccak(
    text=(
        "settle(bytes32,address,address,uint256,uint256,uint256,bool,address)"
    )
)[:4]
SEL_SETTLE_PERMIT = keccak(
    text=(
        "settleWithPermit(bytes32,address,address,address,uint256,uint256,"
        "uint256,bool,address,uint256,uint8,bytes32,bytes32)"
    )
)[:4]
SEL_RESERVE_OF = keccak(text="reserveOf(address)")[:4]
SEL_SPEND_RESERVE = keccak(text="spendReserve(address,address,uint256)")[:4]
SEL_BALANCE_OF = keccak(text="balanceOf(address)")[:4]
SEL_DOMAIN_SEPARATOR = keccak(text="DOMAIN_SEPARATOR()")[:4]


def load_artifact() -> tuple[list, str]:
    abi = json.loads((CONTRACTS / "GasValueLock.abi.json").read_text())
    code = (CONTRACTS / "GasValueLock.bin").read_text().strip()
    return abi, code


@dataclass
class Split:
    """How one settlement's value divides up, in token base units."""

    net_profit: int
    relayer_fee: int
    to_reserve: int
    to_beneficiary: int

    @property
    def is_positive(self) -> bool:
        return self.to_beneficiary > 0 and self.net_profit > self.relayer_fee


def split_value(net_profit: int, relayer_fee: int, reserve_bps: int) -> Split:
    """The exact split the contract will perform.

    Computed here so the engine can predict a settlement's outcome and refuse
    to broadcast one that cannot pay the beneficiary. Kept bit-identical to
    ``GasValueLock._settle``; integer division is floor in both.
    """
    if net_profit <= relayer_fee:
        return Split(net_profit, relayer_fee, 0, 0)
    gross = net_profit - relayer_fee
    to_reserve = (gross * reserve_bps) // 10_000
    return Split(
        net_profit=net_profit,
        relayer_fee=relayer_fee,
        to_reserve=to_reserve,
        to_beneficiary=gross - to_reserve,
    )


def estimate_relayer_fee(
    gas_units: int, gas_price_wei: int, token_per_eth: float, decimals: int = 6
) -> int:
    """What the relayer must be paid, in token base units, for a gas spend.

    The relayer is paid in value, so the fee has to be priced in value. A
    settlement whose profit does not clear this is not worth broadcasting; the
    caller can see that from ``Split.is_positive`` before spending anything.
    ``decimals`` is the value token's scale, because a fee expressed in whole
    tokens truncates to zero for realistic gas costs.
    """
    eth_cost = gas_units * gas_price_wei / 1e18
    return int(eth_cost * token_per_eth * 10**decimals)


def intent_key(intent_id: str) -> bytes:
    """The on-chain replay guard for an intent."""
    if intent_id.startswith("0x") and len(intent_id) == 66:
        return bytes.fromhex(intent_id[2:])
    return keccak(text=intent_id)


def permit_digest(
    token: str,
    domain_separator: bytes,
    owner: str,
    spender: str,
    value: int,
    nonce: int,
    deadline: int,
) -> bytes:
    struct_hash = keccak(
        encode(
            ["bytes32", "address", "address", "uint256", "uint256", "uint256"],
            [PERMIT_TYPEHASH, owner, spender, value, nonce, deadline],
        )
    )
    return keccak(b"\x19\x01" + domain_separator + struct_hash)


def sign_permit(
    private_key: str,
    domain_separator: bytes,
    owner: str,
    spender: str,
    value: int,
    nonce: int,
    deadline: int,
) -> tuple[int, bytes, bytes]:
    """Sign an EIP-2612 permit. Costs nothing and needs no ETH.

    This is the step that lets a zero-balance agent participate: the signature
    is produced locally and handed to whoever is paying for gas.
    """
    digest = permit_digest(
        token="", domain_separator=domain_separator, owner=owner,
        spender=spender, value=value, nonce=nonce, deadline=deadline,
    )
    signed = Account._sign_hash(digest, private_key=private_key)
    return signed.v, signed.r.to_bytes(32, "big"), signed.s.to_bytes(32, "big")


def build_settle_calldata(
    intent_id: str,
    token: str,
    relayer: str,
    net_profit: int,
    relayer_fee: int,
    reserve_bps: int,
    proof_satisfied: bool,
    to: str = BENEFICIARY,
) -> bytes:
    """Calldata for the approval-based settlement path."""
    return SEL_SETTLE + encode(
        ["bytes32", "address", "address", "uint256", "uint256", "uint256", "bool", "address"],
        [
            intent_key(intent_id),
            to_checksum_address(token),
            to_checksum_address(relayer),
            net_profit,
            relayer_fee,
            reserve_bps,
            proof_satisfied,
            to_checksum_address(to),
        ],
    )


def build_settle_permit_calldata(
    intent_id: str,
    token: str,
    owner: str,
    relayer: str,
    net_profit: int,
    relayer_fee: int,
    reserve_bps: int,
    proof_satisfied: bool,
    deadline: int,
    v: int,
    r: bytes,
    s: bytes,
    to: str = BENEFICIARY,
) -> bytes:
    """Calldata for the permit settlement path, for a zero-ETH value holder."""
    return SEL_SETTLE_PERMIT + encode(
        [
            "bytes32", "address", "address", "address", "uint256", "uint256",
            "uint256", "bool", "address", "uint256", "uint8", "bytes32", "bytes32",
        ],
        [
            intent_key(intent_id),
            to_checksum_address(token),
            to_checksum_address(owner),
            to_checksum_address(relayer),
            net_profit,
            relayer_fee,
            reserve_bps,
            proof_satisfied,
            to_checksum_address(to),
            deadline,
            v,
            r,
            s,
        ],
    )


@dataclass
class ReserveState:
    """The agent's self-funding gas reserve, in token units."""

    token: str = ""
    amount: int = 0
    top_ups: int = 0
    spends: int = 0
    history: list[str] = field(default_factory=list)

    @property
    def funded(self) -> bool:
        return self.amount > 0

    def summary(self) -> dict:
        return {
            "token": self.token,
            "amount": self.amount,
            "funded": self.funded,
            "top_ups": self.top_ups,
            "spends": self.spends,
        }


class ReserveAccumulator:
    """Keeps the gas reserve topped up from settled profit.

    The point is to end the dependence on external gas. The first settlement
    that pays anything sets aside a slice of its own profit; later settlements
    can draw on that slice when their own profit does not cover the relayer.
    When the reserve runs dry the accumulator asks the genesis mesh for new
    value instead of reporting that there is no gas source.
    """

    def __init__(self, reserve_bps: int | None = None) -> None:
        self.reserve_bps = (
            SETTINGS.gas_reserve_bps if reserve_bps is None else reserve_bps
        )
        self.state = ReserveState(token=SETTINGS.value_token)

    def record_settlement(self, split: Split, token: str) -> None:
        """Fold a completed settlement's reserve slice into the accumulator."""
        self.state.token = token
        if split.to_reserve > 0:
            self.state.amount += split.to_reserve
            self.state.top_ups += 1
            self.state.history.append(
                f"topup +{split.to_reserve} (total {self.state.amount})"
            )

    def cover(self, needed: int) -> tuple[int, int]:
        """Draw down the reserve to cover ``needed``.

        Returns ``(covered, shortfall)``. A shortfall is not an error: it is the
        signal to go and acquire more value, which is what the genesis mesh is
        for.
        """
        if needed <= 0:
            return 0, 0
        covered = min(self.state.amount, needed)
        self.state.amount -= covered
        if covered:
            self.state.spends += 1
            self.state.history.append(
                f"spend -{covered} (total {self.state.amount})"
            )
        return covered, needed - covered

    def refill_from(self, amount: int) -> None:
        """Credit value acquired from a genesis source."""
        if amount <= 0:
            return
        self.state.amount += amount
        self.state.top_ups += 1
        self.state.history.append(f"refill +{amount} (total {self.state.amount})")

    def summary(self) -> dict:
        out = self.state.summary()
        out["reserve_bps"] = self.reserve_bps
        out["history"] = self.state.history[-10:]
        return out
