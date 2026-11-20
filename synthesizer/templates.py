"""Pre-computed path templates.

The composer can invent a path from whatever primitives the live scan returns,
but that costs a network round trip. These templates are ready-made atomic
compositions over well-known mainnet protocols (Aave V3 flash loans, Uniswap V3,
Curve, Balancer, Morpho, Gelato/Pimlico paymasters). Instantiating one is a pure
in-memory operation measured in microseconds.

A template is not a cached *result*: the notional and the profit still depend on
the intent's target, and every instantiated path is re-proved in the request
before anything is executed. The template only removes discovery latency.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import AtomicPath, Constraints, PathStep


@dataclass(frozen=True)
class HopSpec:
    """One hop of a template: which primitive, at which rate band."""

    primitive: str
    action: str
    yield_rate: float
    fee_rate: float

    @property
    def net_rate(self) -> float:
        return self.yield_rate - self.fee_rate


@dataclass(frozen=True)
class Template:
    """A named, reusable atomic composition."""

    name: str
    description: str
    objective: str  # the intent objective it suits
    hops: tuple[HopSpec, ...]
    gas_mode: str = "paymaster"  # paymaster | protocol | relay | flash_gas
    gas_sponsor: str = "protocol"

    @property
    def net_rate(self) -> float:
        return sum(h.net_rate for h in self.hops)

    @property
    def primitives(self) -> list[str]:
        return [h.primitive for h in self.hops]

    def instantiate(self, constraints: Constraints, notional: float) -> AtomicPath:
        steps = [
            PathStep(
                primitive=h.primitive,
                action=h.action,
                gas_usd=0.0,
                fee_rate=h.fee_rate,
                yield_rate=h.yield_rate,
            )
            for h in self.hops
        ]
        return AtomicPath(
            intent_id="",
            steps=steps,
            primitives=self.primitives,
            notional_usd=round(notional, 2),
            gas_sponsor=self.gas_sponsor,
            simulation_ok=True,
            has_uncollateralized_capital=True,
            template=self.name,
            gas_mode=self.gas_mode,
        )


# Rates sit inside the same bands the composer samples, but are fixed so a
# template is deterministic: the same intent always yields the same path id.
TEMPLATES: tuple[Template, ...] = (
    Template(
        name="aave-uniswap-atomic",
        description="Aave V3 flash loan -> Uniswap V3 concentrated swap arb -> repay",
        objective="earn",
        hops=(
            HopSpec("aave/aave-v3-core#aave-v3-core", "flash_loan:borrow N from Aave V3", 0.0018, 0.00009),
            HopSpec("Uniswap/v3-core#v3-core", "swap:arbitrage across Uniswap V3 ticks", 0.0024, 0.00012),
            HopSpec("pimlico-io/erc20-paymaster#erc20-paymaster", "paymaster:sponsor gas", 0.0, 0.0),
        ),
        gas_mode="paymaster",
    ),
    Template(
        name="balancer-curve-lbp",
        description="Balancer flash loan -> Curve stable swap -> LBP fee capture",
        objective="provide_liquidity",
        hops=(
            HopSpec("balancer/balancer-v2-monorepo#balancer-v2-monorepo", "flash_loan:borrow N from Balancer", 0.0015, 0.00008),
            HopSpec("curvefi/curve-contract#curve-contract", "swap:stable-pool imbalance capture", 0.0020, 0.00010),
            HopSpec("balancer/balancer-v2-monorepo#lbp", "liquidity:bootstrapping pool fee", 0.0012, 0.00010),
            HopSpec("eth-infinitism/account-abstraction#account-abstraction", "session_key:scoped signing", 0.0, 0.0),
        ),
        gas_mode="protocol",
    ),
    Template(
        name="morpho-flash-mint",
        description="Morpho flash loan -> Uniswap V3 -> MEV-boost private settlement",
        objective="earn",
        hops=(
            HopSpec("morpho-org/morpho-blue#morpho-blue", "flash_loan:borrow N from Morpho Blue", 0.0018, 0.00007),
            HopSpec("Uniswap/v3-core#v3-core", "swap:cross-tick arbitrage", 0.0024, 0.00012),
            HopSpec("flashbots/mev-boost#mev-boost", "private_mempool:private bundle", 0.0006, 0.00003),
        ),
        gas_mode="relay",
    ),
    Template(
        name="solver-intent-atomic",
        description="Intent solver auction -> flash loan -> atomic settlement",
        objective="swap",
        hops=(
            HopSpec("aave/aave-v3-core#aave-v3-core", "flash_loan:borrow N", 0.0018, 0.00009),
            HopSpec("langchain-ai/langgraph#langgraph", "solver:intent auction fill", 0.0015, 0.00008),
            HopSpec("Uniswap/v3-core#v3-core", "swap:fill against V3 liquidity", 0.0024, 0.00012),
            HopSpec("pimlico-io/erc20-paymaster#erc20-paymaster", "paymaster:sponsor gas", 0.0, 0.0),
        ),
        gas_mode="paymaster",
    ),
    Template(
        name="market-making-flash",
        description="Hummingbot quoting -> flash loan inventory -> atomic unwind",
        objective="market_making",
        hops=(
            HopSpec("aave/aave-v3-core#aave-v3-core", "flash_loan:borrow inventory", 0.0018, 0.00009),
            HopSpec("hummingbot/hummingbot#hummingbot", "market_making:quote spread capture", 0.0009, 0.00006),
            HopSpec("Uniswap/v3-core#v3-core", "swap:unwind inventory", 0.0024, 0.00012),
        ),
        gas_mode="flash_gas",
    ),
)

TEMPLATES_BY_NAME: dict[str, Template] = {t.name: t for t in TEMPLATES}


def templates_for(objective: str) -> list[Template]:
    """Templates ordered for an objective, best first, always non-empty."""
    preferred = [t for t in TEMPLATES if t.objective == objective]
    rest = [t for t in TEMPLATES if t.objective != objective]
    # Highest net rate first: the most likely to clear a large target.
    preferred.sort(key=lambda t: t.net_rate, reverse=True)
    rest.sort(key=lambda t: t.net_rate, reverse=True)
    return preferred + rest


def best_template(objective: str) -> Template:
    return templates_for(objective)[0]
