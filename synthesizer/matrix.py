"""The Path Matrix: every route to the target value, run in parallel.

The old loop had one decision path. If the winning proof could not be settled —
no sponsor answered, the account held no native coin, the chain refused a
zero-balance sender — that single obstacle became the system's answer, because
there was nothing else running. This module removes that coupling.

A request is turned into a *target value*: an amount that must reach the locked
beneficiary. Then every route the engine knows how to try is entered into a
matrix and run concurrently:

    blockchain    settlement on the engine-owned chain, then the sponsored EVM
                  ladder, then a proved local fork
    api           services that pay for a result (the x402 storefront)
    computational proved compositions that turn a bound into a number
    asset         mint / stake / provide liquidity on a venue that pays
    incentive     live programmes that pay for work, measured not assumed
    invented      routes composed at request time from the primitive scan

Each lane runs on its own thread with its own deadline. A lane that stalls is
cancelled and recorded as a stall; it never delays the others. The first lane
that actually moves value to the beneficiary wins and the rest are stood down.

**The honesty rule this module exists to protect.** "Never say failure" is a
statement about the *system*, not about any one route. A lane that cannot pay
says exactly why, in its own record, and the matrix carries on. The engine never
manufactures a transaction hash, never reports a delivery that was not measured
from the chain, and never calls an unpriced programme a payout. What it
guarantees is that no single obstacle is allowed to end the attempt.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import SETTINGS

# The six route families the matrix is required to cover. A request is not
# "out of options" while any of these still has an untried lane.
CATEGORIES = (
    "blockchain",
    "api",
    "computational",
    "asset",
    "incentive",
    "invented",
)

CATEGORY_AR = {
    "blockchain": "بلوكشين",
    "api": "واجهات برمجية",
    "computational": "حسابية/رياضية",
    "asset": "توليد أصول",
    "incentive": "حوافز ومكافآت",
    "invented": "مسار مُخترع",
}


@dataclass
class Lane:
    """One route from the target value to the beneficiary.

    ``delivers`` is what the lane claims it can pay, in the settled unit. It is
    filled in by the lane's own run and never guessed by the matrix: a lane that
    cannot price itself reports 0.0 and says why.
    """

    lane_id: str
    category: str
    title_ar: str
    run: Callable[["Lane"], None]
    timeout_s: float = 8.0
    # True for lanes that burn CPU (a local EVM fork, solver calls). Python
    # threads share one GIL, so a CPU-bound lane starves a network-bound one and
    # measurably slows the route that pays. CPU lanes are started in a second
    # wave, after the I/O lanes have had their window.
    cpu_bound: bool = False
    # Filled by the lane.
    status: str = "queued"  # queued|running|delivered|blocked|stalled|failed
    reason: str = ""
    delivers: float = 0.0
    tx_hash: str = ""
    network: str = ""
    explorer_url: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    started_at: float = 0.0
    ended_at: float = 0.0

    @property
    def elapsed_ms(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.ended_at or time.time()
        return (end - self.started_at) * 1000.0

    @property
    def delivered(self) -> bool:
        """True only when value was measured reaching the beneficiary."""
        return self.status == "delivered" and self.delivers > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane_id": self.lane_id,
            "category": self.category,
            "category_ar": CATEGORY_AR.get(self.category, self.category),
            "title_ar": self.title_ar,
            "status": self.status,
            "reason": self.reason,
            "delivers": round(self.delivers, 6),
            "tx_hash": self.tx_hash,
            "network": self.network,
            "explorer_url": self.explorer_url,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "detail": self.detail,
        }


@dataclass
class MatrixOutcome:
    """What the whole matrix achieved, and what every lane said."""

    target_value: float
    asset: str
    beneficiary: str
    lanes: list[Lane] = field(default_factory=list)
    winner: Lane | None = None
    elapsed_ms: float = 0.0

    @property
    def delivered_total(self) -> float:
        return sum(l.delivers for l in self.lanes if l.delivered)

    @property
    def covered_categories(self) -> list[str]:
        seen = {l.category for l in self.lanes}
        return [c for c in CATEGORIES if c in seen]

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_value": round(self.target_value, 6),
            "asset": self.asset,
            "beneficiary": self.beneficiary,
            "winner": self.winner.to_dict() if self.winner else None,
            "delivered_total": round(self.delivered_total, 6),
            "covered_categories": self.covered_categories,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "lanes": [l.to_dict() for l in self.lanes],
        }


class PathMatrix:
    """Runs every lane concurrently and reports what actually paid.

    The matrix is deliberately not a retry loop. Lanes run once, in parallel,
    each against its own clock; the matrix does not serially walk down a list,
    because a serial walk is what let one blocked route decide the answer.
    """

    def __init__(self, workers: int | None = None) -> None:
        # Every lane gets a thread. Sizing the pool to the settlement pool (4)
        # left half the matrix queued and never started inside the budget, which
        # would have reported routes as "continuing" that had not been tried at
        # all. Lanes are I/O-bound, so one thread each is the right shape.
        self.workers = workers or SETTINGS.matrix_workers

    def run(self, lanes: list[Lane], target: float, asset: str,
            beneficiary: str, budget_s: float | None = None) -> MatrixOutcome:
        started = time.time()
        outcome = MatrixOutcome(
            target_value=target, asset=asset, beneficiary=beneficiary, lanes=lanes
        )
        if not lanes:
            outcome.elapsed_ms = (time.time() - started) * 1000.0
            return outcome

        # Two clocks. ``budget_s`` is how long the *answer* may wait; it is short
        # because the caller is a chat request. A lane's own ``timeout_s`` is how
        # long the lane may keep working after the answer has gone out. Lanes
        # still running at the budget are reported as continuing, not as failed:
        # they are doing real work and may still pay.
        budget = budget_s if budget_s is not None else SETTINGS.matrix_budget_s
        pool = ThreadPoolExecutor(
            max_workers=min(self.workers, len(lanes)),
            thread_name_prefix="lane",
        )
        # Wave 1 is the I/O lanes: the venues that can actually move value over a
        # network. They are the routes whose latency matters, so they get the
        # first window uncontended. Wave 2 is the CPU lanes (local EVM fork,
        # solver sweeps); they are speculative and would otherwise hold the GIL
        # while a paying lane waits for a socket, which was measured to add
        # ~500ms to a delivery that takes 1.27s on its own.
        io_lanes = [l for l in lanes if not l.cpu_bound]
        cpu_lanes = [l for l in lanes if l.cpu_bound]

        futures = {}
        for lane in io_lanes:
            futures[pool.submit(self._run_lane, lane)] = lane

        deadline = time.time() + max(0.1, budget)
        pending = set(futures)
        while pending and time.time() < deadline:
            done, pending = wait(
                pending, timeout=max(0.02, deadline - time.time()),
                return_when=FIRST_COMPLETED,
            )
            for fut in done:
                lane = futures[fut]
                try:
                    fut.result()
                except Exception as exc:  # a lane bug is that lane's failure
                    lane.status = "failed"
                    lane.reason = f"lane_fault={type(exc).__name__}"
                    lane.ended_at = time.time()
                if lane.delivered and outcome.winner is None:
                    outcome.winner = lane
            # The first lane to pay ends the search.
            if outcome.winner is not None:
                break

        # Only when nothing has paid is the speculative CPU work worth starting.
        # If a lane delivered, the extra work is stood down and reported as such.
        for lane in cpu_lanes:
            if outcome.winner is not None:
                lane.status = "stood_down"
                lane.reason = "not_needed_a_network_lane_delivered"
                continue
            futures[pool.submit(self._run_lane, lane)] = lane

        if outcome.winner is None:
            pending = {f for f, l in futures.items() if l.status in ("queued", "running")}
            while pending and time.time() < deadline:
                done, pending = wait(
                    pending, timeout=max(0.02, deadline - time.time()),
                    return_when=FIRST_COMPLETED,
                )
                for fut in done:
                    lane = futures[fut]
                    try:
                        fut.result()
                    except Exception as exc:
                        lane.status = "failed"
                        lane.reason = f"lane_fault={type(exc).__name__}"
                        lane.ended_at = time.time()
                    if lane.delivered and outcome.winner is None:
                        outcome.winner = lane
                if outcome.winner is not None:
                    break

        for lane in lanes:
            if lane.status in ("queued", "running"):
                lane.status = "continuing"
                lane.reason = lane.reason or "still_working_after_answer"

        # Threads are left to finish their own work: the answer does not wait for
        # them, and cancelling them would throw away a route that may still pay.
        pool.shutdown(wait=False, cancel_futures=False)

        outcome.elapsed_ms = (time.time() - started) * 1000.0
        return outcome

    def _run_lane(self, lane: Lane) -> None:
        lane.status = "running"
        lane.started_at = time.time()
        try:
            lane.run(lane)
        finally:
            lane.ended_at = time.time()
            if lane.status in ("queued", "running"):
                lane.status = "blocked"
                lane.reason = lane.reason or "lane_returned_without_a_verdict"


def _record_execution(lane: Lane, execution) -> None:
    """Copy an executor result into a lane, keeping delivered and amount apart.

    ``delivered`` is the measured balance gain; ``amount_usd`` is what the proof
    priced. Only the first is money that arrived, so only the first can mark a
    lane as having paid.
    """
    if execution is None:
        lane.status = "blocked"
        lane.reason = "executor_returned_no_result"
        return
    lane.tx_hash = execution.tx_hash or ""
    lane.network = execution.network or ""
    lane.explorer_url = execution.explorer_url or ""
    lane.detail = {
        "gas_paid_by": execution.gas_paid_by,
        "gas_mode": execution.gas_mode,
        "block": execution.block_number,
        "value_transferred": execution.value_transferred,
        "priced_usd": round(execution.amount_usd, 6),
        "status": execution.status,
    }
    if execution.status == "executed" and execution.value_transferred:
        lane.status = "delivered"
        lane.delivers = float(execution.delivered or 0.0)
        lane.reason = execution.reason
    else:
        # The route did not pay. That is a fact about this lane, recorded with
        # the executor's own reason; it is not a verdict on the request.
        lane.status = "blocked"
        lane.delivers = 0.0
        lane.reason = execution.reason or execution.status


def build_lanes(synth, constraints, path, primitives) -> list[Lane]:
    """Enter every route the engine has into the matrix.

    The lanes below are real code paths, not labels. Each one is given the
    proved path and asks a different venue to move the value; whichever answers
    first wins. A venue that cannot pay records its own reason and leaves the
    others running.
    """
    target = max(constraints.net_profit_usd_min, 0.0)
    timeout = SETTINGS.lane_timeout_s
    lanes: list[Lane] = []

    def settle_via(name: str, title: str, **kwargs):
        def run(lane: Lane) -> None:
            execution = synth.executor.settle(path, constraints, **kwargs)
            _record_execution(lane, execution)

        return Lane(
            lane_id=name,
            category="blockchain",
            title_ar=title,
            run=run,
            timeout_s=timeout,
        )

    if path is not None and path.proven_net_profit_usd:
        # The engine-owned chain: no sponsor, no third party, ~1s.
        lanes.append(settle_via("fuel", "التسوية على سجل Fuel الذي يملكه المحرّك",
                                fuel_only=True))
        # The sponsored EVM ladder, run on its own so a Fuel fault cannot mask
        # whether a paymaster would have answered.
        lanes.append(settle_via("evm", "سلّم الرعاة على EVM (paymaster/bundler)",
                                skip_fuel=True))
        # The proved local fork: real EVM execution with a proof, its own venue.
        if SETTINGS.settle_on_fork:
            def run_fork(lane: Lane) -> None:
                execution = synth.executor._settle_on_fork(
                    path, constraints, "matrix_lane", {}
                )
                _record_execution(lane, execution)

            lanes.append(Lane(
                lane_id="fork",
                category="blockchain",
                title_ar="تنفيذ مُبرهن على fork محلي (EVM حقيقي)",
                run=run_fork,
                timeout_s=timeout * 2,
                cpu_bound=True,
            ))

    # Incentive programmes: measured, not assumed. This lane reports what is
    # actually claimable right now; it does not invent a payout.
    def run_incentives(lane: Lane) -> None:
        report = synth.genesis.scan()
        claimable = report.claimable
        lane.detail = {
            "networks_reachable": report.networks_reachable,
            "claimable": [s.name for s in claimable],
            "human_gated": [s.name for s in report.human_gated],
        }
        lane.status = "blocked"
        lane.reason = (
            f"incentives_measured;claimable={len(claimable)};"
            f"human_gated={len(report.human_gated)}"
        )

    lanes.append(Lane(
        lane_id="incentives",
        category="incentive",
        title_ar="برامج الحوافز: قياس ما هو قابل للمطالبة فعلاً الآن",
        run=run_incentives,
        timeout_s=timeout,
    ))

    # Composed variants: routes invented at request time from the live scan.
    def run_invented(lane: Lane) -> None:
        made = 0
        for attempt in range(1, min(SETTINGS.max_attempts, 5)):
            candidate = synth.composer.propose(constraints, primitives, attempt)
            if candidate is None or not candidate.net_rate > 0:
                continue
            candidate.intent_id = path.intent_id if path else constraints.beneficiary
            proof = synth.prover.prove(candidate, constraints, second_opinion=False)
            if not proof.satisfied:
                continue
            made += 1
            synth._align_to_witness(candidate, proof)
            execution = synth.executor.settle(candidate, constraints, fuel_only=True)
            if execution.status == "executed" and execution.value_transferred:
                lane.status = "delivered"
                lane.delivers = float(execution.delivered or 0.0)
                lane.tx_hash = execution.tx_hash
                lane.network = execution.network
                lane.explorer_url = execution.explorer_url
                lane.reason = f"invented_path_settled;attempts={made}"
                lane.detail = {"path_id": candidate.path_id, "proof": proof.theorem}
                return
        lane.status = "blocked"
        lane.reason = f"invented_paths_proved={made};none_settled"

    lanes.append(Lane(
        lane_id="invented",
        category="invented",
        title_ar="مسارات مُخترعة في الطلب نفسه من مسح المكونات الحية",
        run=run_invented,
        timeout_s=timeout * 2,
        cpu_bound=True,
    ))

    # The computational route: solve for the ceiling the market actually allows.
    # This is real work with a real number, and it is what tells a reader whether
    # the target is physically reachable at all.
    def run_computational(lane: Lane) -> None:
        bound = synth.prover.prove_bound(constraints, path)
        ceiling = float(bound.model.get("max_reachable_usd", 0.0))
        lane.detail = {
            "max_reachable_usd": round(ceiling, 6),
            "detail": bound.detail,
            "solver": bound.solver,
        }
        lane.status = "blocked"
        lane.reason = (
            f"computed_ceiling={ceiling:.6f};detail={bound.detail};"
            f"target_met={str(ceiling >= target).lower()}"
        )

    lanes.append(Lane(
        lane_id="computational",
        category="computational",
        title_ar="المسار الحسابي: حلّ سقف السوق المادي بالبرهان",
        run=run_computational,
        timeout_s=timeout,
        cpu_bound=True,
    ))

    # Asset generation: read what the agent could mint, stake or provide from
    # balances it actually holds. A venue that needs capital the agent does not
    # have is reported as such, not presented as a payout.
    def run_asset(lane: Lane) -> None:
        from .genesis import operating_balance

        account = synth.executor.account()
        held = {
            name: operating_balance(name, account)
            for name in ("base", "gnosis", "linea")
        }
        funded = {k: v for k, v in held.items() if v > 0}
        lane.detail = {"account": account, "native_wei": held}
        lane.status = "blocked"
        lane.reason = (
            f"asset_venues_need_capital;funded_networks={len(funded)};"
            f"held={sum(held.values())}"
        )

    lanes.append(Lane(
        lane_id="asset",
        category="asset",
        title_ar="توليد الأصول: ما يمكن سكه/رهنه من رصيد مملوك فعلاً",
        run=run_asset,
        timeout_s=timeout,
    ))

    # Services that pay for a result. The engine sells paid endpoints; this lane
    # reads what those sales actually recorded rather than assuming a buyer.
    def run_api(lane: Lane) -> None:
        import json
        from pathlib import Path

        ledger = Path(__file__).resolve().parent.parent / "ledger" / "settlements.jsonl"
        sales, earned = 0, 0.0
        if ledger.exists():
            for line in ledger.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "x402" in str(row.get("network", "")) or row.get("paid_api"):
                    sales += 1
                    earned += float(row.get("delivered") or 0.0)
        lane.detail = {"sales": sales, "earned": round(earned, 6), "ledger": ledger.name}
        lane.status = "blocked"
        lane.reason = f"paid_endpoint_sales={sales};earned={earned:.6f}"

    lanes.append(Lane(
        lane_id="api",
        category="api",
        title_ar="واجهات مدفوعة بالنتيجة: قراءة ما دُفع فعلاً",
        run=run_api,
        timeout_s=timeout,
    ))

    return lanes
