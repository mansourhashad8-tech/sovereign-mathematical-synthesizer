"""Data models shared across the synthesizer.

Everything is a plain dataclass so it can be serialised to JSON, hashed, and
reasoned about without a framework in the way.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any


def _now() -> float:
    return time.time()


def stable_hash(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


@dataclass
class Goal:
    """What the user wants, in machine terms."""

    objective: str  # e.g. "earn", "swap", "provide_liquidity", "bridge"
    asset: str  # "USDC", "ETH", ...
    target_amount: float  # desired net amount of `asset`
    raw_intent: str  # the original Arabic text


@dataclass
class Constraints:
    """The formal constraint system the composed path must satisfy."""

    goal: Goal
    # Money must strictly increase for the beneficiary.
    net_profit_usd_min: float
    # Security constraints (deny by default).
    allow_custody: bool = False  # must never hold user funds
    allow_user_key: bool = False  # must never require a user key
    allow_user_gas: bool = False  # gas must never come from the user
    require_atomic: bool = True  # path must be all-or-nothing
    require_simulation: bool = True  # path must be simulated before execution
    beneficiary: str = ""
    # Free-form extra constraints parsed from the intent (e.g. "no signup").
    extra: list[str] = field(default_factory=list)

    def to_z3_facts(self) -> dict[str, Any]:
        """Flat dict consumed by the prover."""
        return {
            "net_profit_usd_min": self.net_profit_usd_min,
            "allow_custody": self.allow_custody,
            "allow_user_key": self.allow_user_key,
            "allow_user_gas": self.allow_user_gas,
            "require_atomic": self.require_atomic,
            "require_simulation": self.require_simulation,
            "beneficiary": self.beneficiary,
        }


@dataclass
class Primitive:
    """A reusable building block pulled from a live GitHub repository."""

    name: str
    repo: str
    category: str  # paymaster | flash_mint | liquidity | verification | ...
    description: str
    url: str = ""
    stars: int = 0
    pushed_at: str = ""
    freshness_hours: float = 0.0
    properties: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.name}"


@dataclass
class PathStep:
    """One hop in the atomic execution path.

    ``yield_rate`` and ``fee_rate`` are fractions of the path's shared notional
    ``N`` (the uncollateralised capital lent by a flash primitive). Gas is a
    flat USD cost that is always sponsored by a protocol, so it is not charged
    to the beneficiary.
    """

    primitive: str  # Primitive.key
    action: str  # what this hop does
    gas_usd: float = 0.0
    fee_rate: float = 0.0
    yield_rate: float = 0.0

    @property
    def net_rate(self) -> float:
        return self.yield_rate - self.fee_rate


@dataclass
class AtomicPath:
    """A composed, single-transaction (atomic) execution path."""

    intent_id: str
    steps: list[PathStep]
    primitives: list[str] = field(default_factory=list)
    notional_usd: float = 0.0  # the uncollateralised capital N
    gas_sponsor: str = "protocol"  # protocol | paymaster | barter  (never "user")
    simulation_ok: bool = False
    # True only when a flash-mint / flash-loan hop supplies the notional. A path
    # without it has no uncollateralised capital, so N is pinned to 0 and the
    # path cannot earn. The prover enforces this.
    has_uncollateralized_capital: bool = False
    created_at: float = field(default_factory=_now)

    @property
    def net_rate(self) -> float:
        return sum(s.net_rate for s in self.steps)

    @property
    def total_gas_usd(self) -> float:
        return sum(s.gas_usd for s in self.steps)

    @property
    def gross_revenue_usd(self) -> float:
        return self.notional_usd * sum(s.yield_rate for s in self.steps)

    @property
    def total_cost_usd(self) -> float:
        return self.notional_usd * sum(s.fee_rate for s in self.steps) + self.total_gas_usd

    @property
    def net_profit_usd(self) -> float:
        return self.notional_usd * self.net_rate - self.total_gas_usd

    @property
    def path_id(self) -> str:
        return stable_hash(self.intent_id, *[s.primitive for s in self.steps])

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["gross_revenue_usd"] = round(self.gross_revenue_usd, 6)
        d["total_cost_usd"] = round(self.total_cost_usd, 6)
        d["net_profit_usd"] = round(self.net_profit_usd, 6)
        d["net_rate"] = round(self.net_rate, 8)
        d["path_id"] = self.path_id
        return d


@dataclass
class Proof:
    """The result of a proof attempt for a path."""

    path_id: str
    satisfied: bool
    solver: str = "z3"
    theorem: str = ""
    model: dict[str, Any] = field(default_factory=dict)
    certificate: str = ""
    elapsed_ms: float = 0.0
    detail: str = ""


@dataclass
class ExecutionResult:
    status: str  # "executed" | "simulated" | "queued"
    tx_hash: str = ""
    explorer_url: str = ""
    network: str = "ethereum-mainnet"
    beneficiary: str = ""
    amount_usd: float = 0.0
    reason: str = ""
    simulated: bool = False


@dataclass
class IntentResult:
    """The complete answer returned to the chat."""

    intent_id: str
    raw_intent: str
    constraints: Constraints
    chosen_path: AtomicPath | None
    proof: Proof | None
    execution: ExecutionResult | None
    narrative_ar: str
    invented: bool
    attempts: int
    primitives_used: list[str] = field(default_factory=list)
    equations: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    background: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "raw_intent": self.raw_intent,
            "constraints": asdict(self.constraints),
            "chosen_path": self.chosen_path.to_dict() if self.chosen_path else None,
            "proof": asdict(self.proof) if self.proof else None,
            "execution": asdict(self.execution) if self.execution else None,
            "narrative_ar": self.narrative_ar,
            "invented": self.invented,
            "attempts": self.attempts,
            "primitives_used": self.primitives_used,
            "equations": self.equations,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "background": self.background,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)
