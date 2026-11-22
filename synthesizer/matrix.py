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
    "creator",
)

CATEGORY_AR = {
    "blockchain": "بلوكشين",
    "api": "واجهات برمجية",
    "computational": "حسابية/رياضية",
    "asset": "توليد أصول",
    "incentive": "حوافز ومكافآت",
    "invented": "مسار مُخترع",
    "creator": "خلق المُخرَج",
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
    # True for lanes whose work is worth doing only if nothing has paid yet. It
    # is separate from ``cpu_bound`` because the reason is different: the creator
    # lane waits on a socket, not on the GIL, but spending a model call on every
    # request that already settled would be waste and would burn a rate limit.
    deferred: bool = False
    # False for lanes that produce a deliverable rather than a payment (the
    # artifact creator). Such a lane can finish and report its output, but it is
    # never selected as the payer, so a produced document cannot be presented as
    # money that arrived.
    pays: bool = True
    # The unit ``delivers`` is denominated in, so a magnitude is never read as
    # dollars by mistake.
    unit: str = "USD"
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
        """True only when value was measured reaching the beneficiary.

        A lane that produces a deliverable instead of a payment never satisfies
        this, because the matrix uses it to choose who paid.
        """
        return self.pays and self.status == "delivered" and self.delivers > 0

    @property
    def produced(self) -> bool:
        """True when a non-paying lane finished and produced its artifact."""
        return not self.pays and self.status == "produced"

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane_id": self.lane_id,
            "category": self.category,
            "category_ar": CATEGORY_AR.get(self.category, self.category),
            "title_ar": self.title_ar,
            "status": self.status,
            "reason": self.reason,
            "delivers": round(self.delivers, 6),
            "unit": self.unit,
            "pays": self.pays,
            "produced": self.produced,
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
        """What arrived for this request, on the ledger that won it.

        Lanes settle on separate ledgers (the engine-owned Fuel chain and the
        local EVM fork are different books). Adding their amounts would report
        one settlement twice: two lanes each delivering 38 were being shown as
        76. The honest single number is the winner's measured delivery, and
        ``delivered_by_network`` keeps the rest visible without adding them.
        """
        return self.winner.delivers if self.winner else 0.0

    @property
    def delivered_by_network(self) -> dict[str, float]:
        """Measured delivery per ledger, so separate books stay separate."""
        totals: dict[str, float] = {}
        for lane in self.lanes:
            if lane.delivered:
                key = lane.network or lane.lane_id
                totals[key] = round(totals.get(key, 0.0) + lane.delivers, 6)
        return totals

    @property
    def producer(self) -> Lane | None:
        """The lane that produced a deliverable, if one did."""
        for lane in self.lanes:
            if lane.produced:
                return lane
        return None

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
            "delivered_by_network": self.delivered_by_network,
            "covered_categories": self.covered_categories,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "lanes": [l.to_dict() for l in self.lanes],
        }


# One process-wide slot for CPU-bound lanes. Python threads share a single GIL,
# so running two of them at once buys no throughput and costs the request that
# is waiting to synthesize its proof.
_CPU_LANE_SLOT = threading.BoundedSemaphore(max(1, SETTINGS.cpu_lane_workers))


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
        # A lane that outlives its request keeps working, which is intended: it
        # may still pay. What is not intended is that those lanes accumulate. A
        # run of repeated requests was measured to grow the thread count without
        # bound, and the contention pushed each answer past its deadline. The
        # cap keeps the backlog finite so a new request is never starved by the
        # leftovers of earlier ones.
        self._inflight = threading.BoundedSemaphore(SETTINGS.matrix_max_inflight)
        # CPU lanes are serialised separately, and process-wide. Two of them
        # cannot make progress at once under one GIL; they can only push the
        # next request's synthesis off its deadline. Measured: a single request
        # left fork and evm lanes burning 3-4s of CPU after the answer, which
        # tripled the following synthesis.
        self._cpu_slot = _CPU_LANE_SLOT

    def run(self, lanes: list[Lane], target: float, asset: str,
            beneficiary: str, budget_s: float | None = None,
            require_producer: bool = False) -> MatrixOutcome:
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
        io_lanes = [l for l in lanes if not l.cpu_bound and not l.deferred]
        deferred_lanes = [l for l in lanes if l.deferred and not l.cpu_bound]
        cpu_lanes = [l for l in lanes if l.cpu_bound]

        futures = {}
        for lane in io_lanes:
            futures[pool.submit(self._run_lane, lane)] = lane

        # The first lane to pay ends the search -- unless the caller asked for a
        # deliverable, in which case a payment does not answer the request and the
        # matrix keeps going until the producer lane finishes or the budget ends.
        def _settled() -> bool:
            if outcome.winner is None:
                return False
            return not require_producer or outcome.producer is not None

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
            if _settled():
                break

        # Only when nothing has paid is the speculative CPU work worth starting.
        # If a lane delivered, the extra work is stood down and reported as such.
        # A required deliverable is exempt: it is the answer, so it is started
        # even after a venue paid.
        #
        # A CPU lane is pure Python, so it holds the GIL for as long as it runs.
        # Starting one with no time left to finish it cannot help this answer and
        # measurably hurts the next: leftover fork lanes were taking the following
        # request's synthesis from ~130ms to ~780ms. So it is started only when
        # the remaining window could still let it report, and is otherwise
        # recorded as not run rather than as still working.
        remaining = deadline - time.time()
        can_start_cpu = remaining >= SETTINGS.cpu_lane_min_budget_s
        for lane in cpu_lanes + deferred_lanes:
            if outcome.winner is not None and not require_producer:
                lane.status = "stood_down"
                lane.reason = "not_needed_a_network_lane_delivered"
                continue
            if lane.cpu_bound and not can_start_cpu and not require_producer:
                lane.status = "stood_down"
                lane.reason = f"no_window_left_to_report;remaining_s={max(0.0, remaining):.3f}"
                continue
            futures[pool.submit(self._run_lane, lane)] = lane

        if not _settled():
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
                if _settled():
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
        # A lane that cannot get a slot is reported as deferred to the next
        # request rather than silently skipped: the reason says the engine was
        # busy with earlier work, which is true and is not a failure of the lane.
        if not self._inflight.acquire(timeout=0.05):
            lane.status = "queued"
            lane.reason = "engine_busy_with_earlier_lanes"
            return
        # CPU lanes take the single process-wide slot. If it is held, a lane from
        # an earlier request is still computing; starting this one would not make
        # either faster and would starve the request that is answering now.
        took_cpu = False
        if lane.cpu_bound:
            took_cpu = self._cpu_slot.acquire(timeout=0.05)
            if not took_cpu:
                self._inflight.release()
                lane.status = "queued"
                lane.reason = "cpu_slot_held_by_earlier_lane"
                return
        lane.status = "running"
        lane.started_at = time.time()
        try:
            lane.run(lane)
        finally:
            lane.ended_at = time.time()
            if lane.status in ("queued", "running"):
                lane.status = "blocked"
                lane.reason = lane.reason or "lane_returned_without_a_verdict"
            self._inflight.release()
            if took_cpu:
                self._cpu_slot.release()


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

    def settle_via(name: str, title: str, cpu_bound: bool = False, **kwargs):
        def run(lane: Lane) -> None:
            execution = synth.executor.settle(path, constraints, **kwargs)
            _record_execution(lane, execution)

        return Lane(
            lane_id=name,
            category="blockchain",
            title_ar=title,
            run=run,
            timeout_s=timeout,
            cpu_bound=cpu_bound,
        )

    if path is not None and path.proven_net_profit_usd:
        # The engine-owned chain: no sponsor, no third party, ~1s.
        lanes.append(settle_via("fuel", "التسوية على سجل Fuel الذي يملكه المحرّك",
                                fuel_only=True))
        # The sponsored EVM ladder, run on its own so a Fuel fault cannot mask
        # whether a paymaster would have answered.
        lanes.append(settle_via("evm", "سلّم الرعاة على EVM (paymaster/bundler)",
                                cpu_bound=True, skip_fuel=True))
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

    # The creator: produce the thing the intent asked for, whatever it is, and
    # attest it. This lane does not pay, so it can never be reported as money
    # that arrived; it exists so that an intent which is not about moving funds
    # is still served with a real, measured, on-chain-attested deliverable.
    def run_creator(lane: Lane) -> None:
        from .artifacts import ArtifactCreator, attest, write_receipt
        from .fuel import FuelHand

        domain = getattr(constraints.goal, "domain", "finance")
        creator = ArtifactCreator()
        artifact = creator.create(
            intent_id=path.intent_id if path else constraints.beneficiary,
            raw_intent=constraints.goal.raw_intent,
            domain=domain,
            primitives=primitives,
        )
        if not artifact.ok:
            lane.status = "blocked"
            lane.reason = "artifact_not_produced;" + ";".join(artifact.notes)
            lane.detail = artifact.to_dict()
            return
        artifact = attest(artifact, FuelHand())
        write_receipt(artifact)
        lane.status = "produced"
        lane.delivers = float(artifact.bytes_written)
        lane.unit = "bytes"
        lane.tx_hash = artifact.settlement_tx
        lane.network = artifact.network
        lane.explorer_url = (
            f"/fuel/tx/{artifact.settlement_tx}" if artifact.settlement_tx else ""
        )
        lane.reason = (
            f"artifact_created;domain={domain};bytes={artifact.bytes_written};"
            f"lines={artifact.lines};sha256={artifact.sha256[:16]};"
            f"ledger={'settled' if artifact.settled else 'unsettled'}"
        )
        lane.detail = artifact.to_dict()

    lanes.append(Lane(
        lane_id="creator",
        category="creator",
        title_ar="خالق المُخرَج: إنتاج المُخرَج المطلوب وقياسه وتوثيقه على السجل",
        run=run_creator,
        timeout_s=timeout * 6,
        # Writing is an HTTP call to the model, so it waits on a socket and does
        # not hold the GIL. On a non-finance intent the deliverable *is* the
        # answer, so it runs in the first wave; on a finance intent it is
        # deferred, because a model call is only worth spending when no venue
        # has already paid.
        cpu_bound=False,
        deferred=getattr(constraints.goal, "domain", "finance") == "finance",
        pays=False,
        unit="bytes",
    ))

    return lanes
