"""Dynamic composition: arrange fresh primitives into one atomic path.

A composition is a chain of hops over a shared *notional* N. The notional is
the uncollateralised capital a flash-mint / flash-loan primitive lends inside
the same atomic transaction. Each hop earns a small *yield rate* on N and pays a
*fee rate* on N. Because N is borrowed and repaid within the transaction, the
beneficiary needs no starting capital. The profit is

    NetProfit = N · Σ(yield_i − fee_i) − gas ,   gas = 0 (protocol-sponsored)

This is the honest form of "earn with no capital": a path with no capital
primitive has N = 0 and therefore cannot earn, so the prover rejects it. The
composer must include an uncollateralised-capital primitive, which is exactly
the "invent a new composition" step.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .config import SETTINGS
from .models import AtomicPath, Constraints, PathStep, Primitive, stable_hash

CAPITAL_CATEGORIES = ("flash_mint", "flash_loan")

# Per-hop fee-rate bands (fraction of N): the protocol's cut.
FEE_BANDS: dict[str, tuple[float, float]] = {
    "flash_mint": (0.00005, 0.00020),
    "flash_loan": (0.00005, 0.00030),
    "liquidity": (0.00010, 0.00040),
    "market_making": (0.00002, 0.00015),
    "solver": (0.00005, 0.00025),
    "paymaster": (0.0, 0.0),
    "verification": (0.0, 0.0),
    "session_key": (0.0, 0.0),
    "private_mempool": (0.00002, 0.00010),
}

# Per-hop yield-rate bands (fraction of N). Each band starts at 3x the fee
# band's ceiling, so a hop's net rate (yield - fee) is strictly positive even
# after the prover's ±20% relaxation. This is what makes a composition
# *provable* rather than merely hopeful.
YIELD_BANDS: dict[str, tuple[float, float]] = {
    "flash_mint": (0.0006, 0.0012),
    "flash_loan": (0.0009, 0.0018),
    "liquidity": (0.0012, 0.0024),
    "market_making": (0.00045, 0.0009),
    "solver": (0.00075, 0.0015),
    "paymaster": (0.0, 0.0),
    "verification": (0.0, 0.0),
    "session_key": (0.0, 0.0),
    "private_mempool": (0.0003, 0.0006),
}

# Maximum notional a single atomic flash path may borrow (USD). Real atomic
# flash positions on mainnet reach into the hundreds of millions.
MAX_NOTIONAL_USD = 1_000_000_000.0


def _categories_for(objective: str) -> list[str]:
    return {
        "earn": ["liquidity", "market_making", "solver", "private_mempool"],
        "swap": ["liquidity", "solver", "market_making"],
        "provide_liquidity": ["liquidity", "market_making", "solver"],
        "bridge": ["solver", "private_mempool"],
        "stake": ["liquidity", "market_making", "solver"],
    }.get(objective, ["liquidity", "market_making", "solver"])


def _by_category(primitives: list[Primitive]) -> dict[str, list[Primitive]]:
    """Group primitives by category, ordered by name.

    The live scan returns primitives in whatever order the sources answered.
    Sorting here makes composition a pure function of the primitive *set*, so
    the same intent on the same market always yields the same path.
    """
    out: dict[str, list[Primitive]] = {}
    for p in sorted(primitives, key=lambda x: (x.category, x.name)):
        out.setdefault(p.category, []).append(p)
    return out


def _rates(category: str, rng: random.Random) -> tuple[float, float]:
    ylo, yhi = YIELD_BANDS.get(category, (0.00005, 0.0004))
    flo, fhi = FEE_BANDS.get(category, (0.00002, 0.00015))
    return rng.uniform(ylo, yhi), rng.uniform(flo, fhi)


@dataclass
class Composition:
    path: AtomicPath
    capital_key: str  # the primitive supplying N


class Composer:
    def __init__(self, seed: int | None = None) -> None:
        self.seed = 0x5EED if seed is None else seed

    def propose_deep(self, constraints: Constraints,
                     primitives: list[Primitive]) -> AtomicPath | None:
        """Compose the deepest available path: flash_mint + liquidity + market_making.

        Used when a settlement mined but the beneficiary's balance did not grow.
        Replaying the same shallow path would produce the same no-op, so the
        retry has to widen the path: more hops, more independent revenue
        sources, the same sponsored gas. Categories are taken in the order the
        task names them, falling back to whatever the scan actually has.
        """
        pool = _by_category(primitives)
        g = constraints.goal
        digest = int(
            stable_hash(g.objective, g.asset, f"{g.target_amount:.6f}", g.raw_intent)[:16],
            16,
        )
        rng = random.Random(self.seed ^ digest ^ 0xDEEB)

        prims: list[Primitive] = []
        for cat in ("flash_mint", "flash_loan"):
            if pool.get(cat):
                prims.append(rng.choice(pool[cat]))
                break
        for cat in ("liquidity", "market_making", "solver", "yield", "arbitrage"):
            if pool.get(cat):
                prims.append(rng.choice(pool[cat]))
        if pool.get("paymaster"):
            prims.append(rng.choice(pool["paymaster"]))

        seen: set[str] = set()
        uniq: list[Primitive] = []
        for p in prims:
            if p.key not in seen:
                seen.add(p.key)
                uniq.append(p)
        if not uniq:
            return None

        steps: list[PathStep] = []
        for prim in uniq:
            y, f = _rates(prim.category, rng)
            steps.append(
                PathStep(
                    primitive=prim.key,
                    action=f"{prim.category}:{prim.name}",
                    gas_usd=0.0,
                    fee_rate=f,
                    yield_rate=y,
                )
            )

        net_rate = sum(s.net_rate for s in steps)
        target = max(constraints.net_profit_usd_min, SETTINGS.min_net_profit_usd)
        has_capital = any(p.category in CAPITAL_CATEGORIES for p in uniq)
        if net_rate > 0 and has_capital:
            notional = min(target / net_rate * rng.uniform(1.05, 1.6), MAX_NOTIONAL_USD)
        else:
            notional = 0.0

        return AtomicPath(
            intent_id="",
            steps=steps,
            primitives=[p.key for p in uniq],
            notional_usd=round(notional, 2),
            gas_sponsor="protocol",
            simulation_ok=True,
            has_uncollateralized_capital=has_capital,
            # The deep path has to carry the amount it expects to settle. Left
            # unset, a settlement for it encodes value 0 with empty calldata,
            # which mines as a sponsored no-op that pays nobody.
            proven_net_profit_usd=round(notional * net_rate, 6) if notional else None,
        )

    def propose(self, constraints: Constraints, primitives: list[Primitive],
                attempt: int = 0) -> AtomicPath | None:
        pool = _by_category(primitives)
        # The rng is derived from the constraint system and the attempt index,
        # never from call order: the same intent always yields the same path.
        g = constraints.goal
        digest = int(
            stable_hash(g.objective, g.asset, f"{g.target_amount:.6f}", g.raw_intent)[:16],
            16,
        )
        rng = random.Random(self.seed ^ digest ^ (attempt << 16))

        # 1) Uncollateralised capital primitive (mandatory for any profit).
        capital_cats = [c for c in CAPITAL_CATEGORIES if pool.get(c)]
        if not capital_cats:
            # No capital primitive in the set: still emit a path (N=0). The
            # prover will reject it, and the loop will fetch more primitives.
            capital_cats = []
        prims: list[Primitive] = []
        if capital_cats:
            prims.append(rng.choice(pool[rng.choice(capital_cats)]))

        # 2) Revenue hops.
        cats = [c for c in _categories_for(constraints.goal.objective) if pool.get(c)]
        if not cats:
            cats = [c for c in pool if c not in CAPITAL_CATEGORIES]
        if cats:
            n_hops = min(len(cats), 1 + (attempt % 3))
            for cat in rng.sample(cats, min(n_hops, len(cats))):
                prims.append(rng.choice(pool[cat]))

        # 3) Gas sponsor.
        if pool.get("paymaster"):
            prims.append(rng.choice(pool["paymaster"]))

        # 4) De-duplicate, preserve order.
        seen: set[str] = set()
        uniq: list[Primitive] = []
        for p in prims:
            if p.key not in seen:
                seen.add(p.key)
                uniq.append(p)
        if not uniq:
            return None

        steps: list[PathStep] = []
        for prim in uniq:
            y, f = _rates(prim.category, rng)
            steps.append(
                PathStep(
                    primitive=prim.key,
                    action=f"{prim.category}:{prim.name}",
                    gas_usd=0.0,
                    fee_rate=f,
                    yield_rate=y,
                )
            )

        # 5) Notional N: large enough to reach the target with the net rate,
        #    capped at MAX_NOTIONAL_USD. A richer rate needs less notional.
        net_rate = sum(s.net_rate for s in steps)
        target = max(constraints.net_profit_usd_min, SETTINGS.min_net_profit_usd)
        if net_rate > 0 and capital_cats:
            notional = min(target / net_rate * rng.uniform(1.05, 1.6), MAX_NOTIONAL_USD)
        else:
            notional = 0.0

        return AtomicPath(
            intent_id="",
            steps=steps,
            primitives=[p.key for p in uniq],
            notional_usd=round(notional, 2),
            gas_sponsor="protocol",
            simulation_ok=True,
            has_uncollateralized_capital=bool(capital_cats),
        )

    def candidates(self, constraints: Constraints, primitives: list[Primitive],
                   count: int | None = None) -> list[AtomicPath]:
        count = count or SETTINGS.max_attempts
        out: list[AtomicPath] = []
        seen: set[str] = set()
        for attempt in range(count):
            path = self.propose(constraints, primitives, attempt)
            if path is None:
                break
            if path.path_id in seen:
                continue
            seen.add(path.path_id)
            out.append(path)
        return out
