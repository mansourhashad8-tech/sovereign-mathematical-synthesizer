"""The path matrix: many routes to one target value, run concurrently.

The properties under test are the ones the design promises:

* a blocked route is a fact about that route, never the system's answer;
* a lane that hangs cannot delay a lane that pays;
* nothing is reported as delivered unless the chain measured it;
* every route family is represented, so "no route exists" is never said about a
  matrix that only tried one kind of route.
"""

from __future__ import annotations

import threading
import time

from synthesizer.matrix import (
    CATEGORIES,
    Lane,
    MatrixOutcome,
    PathMatrix,
    _record_execution,
)
from synthesizer.models import ExecutionResult


def _lane(lane_id: str, run, category: str = "blockchain", **kwargs) -> Lane:
    return Lane(
        lane_id=lane_id,
        category=category,
        title_ar=lane_id,
        run=run,
        timeout_s=kwargs.pop("timeout_s", 5.0),
        **kwargs,
    )


def test_a_blocked_lane_does_not_stop_a_lane_that_pays():
    """The central claim: one route's obstacle is not the system's answer."""

    def blocked(lane: Lane) -> None:
        lane.status = "blocked"
        lane.reason = "no_sponsorship"

    def pays(lane: Lane) -> None:
        lane.status = "delivered"
        lane.delivers = 42.0
        lane.tx_hash = "0xabc"

    outcome = PathMatrix(workers=4).run(
        [_lane("dead", blocked), _lane("live", pays)],
        target=42.0, asset="USDC", beneficiary="0xlocked", budget_s=3.0,
    )

    assert outcome.winner is not None
    assert outcome.winner.lane_id == "live"
    assert outcome.delivered_total == 42.0
    dead = next(l for l in outcome.lanes if l.lane_id == "dead")
    assert dead.status == "blocked"
    # The blocked route keeps its own reason; it does not become the verdict.
    assert dead.reason == "no_sponsorship"


def test_a_hanging_lane_cannot_delay_the_answer():
    """A venue that never returns must not hold the request hostage."""
    release = threading.Event()

    def hangs(lane: Lane) -> None:
        release.wait(10)

    def pays(lane: Lane) -> None:
        lane.status = "delivered"
        lane.delivers = 7.0

    started = time.time()
    outcome = PathMatrix(workers=4).run(
        [_lane("hang", hangs), _lane("pay", pays)],
        target=7.0, asset="USDC", beneficiary="0xlocked", budget_s=2.0,
    )
    elapsed = time.time() - started
    release.set()

    assert outcome.winner is not None and outcome.winner.lane_id == "pay"
    assert elapsed < 2.0, elapsed


def test_a_lane_that_pays_nothing_is_never_counted_as_a_delivery():
    """Executed-but-empty is not a payout. Only a measured delta counts."""

    def mines_nothing(lane: Lane) -> None:
        _record_execution(lane, ExecutionResult(
            status="executed", tx_hash="0xdead", value_transferred=False,
            delivered=0.0, amount_usd=100.0,
        ))

    outcome = PathMatrix(workers=2).run(
        [_lane("noop", mines_nothing)],
        target=100.0, asset="USDC", beneficiary="0xlocked", budget_s=2.0,
    )

    assert outcome.winner is None
    assert outcome.delivered_total == 0.0
    lane = outcome.lanes[0]
    assert lane.status == "blocked"
    # The priced amount is preserved for inspection but not reported as money.
    assert lane.detail["priced_usd"] == 100.0
    assert lane.delivers == 0.0


def test_a_measured_delta_is_what_the_lane_reports():
    """When the chain measured a gain, that number is the delivery."""

    def mines_value(lane: Lane) -> None:
        _record_execution(lane, ExecutionResult(
            status="executed", tx_hash="0xbeef", value_transferred=True,
            delivered=12.5, amount_usd=99.0, network="fuel-devnet",
        ))

    outcome = PathMatrix(workers=2).run(
        [_lane("pays", mines_value)],
        target=12.0, asset="USDC", beneficiary="0xlocked", budget_s=2.0,
    )

    assert outcome.winner is not None
    assert outcome.winner.delivers == 12.5
    assert outcome.winner.tx_hash == "0xbeef"


def test_a_lane_that_raises_is_recorded_without_ending_the_run():
    def explodes(lane: Lane) -> None:
        raise RuntimeError("venue bug")

    def pays(lane: Lane) -> None:
        lane.status = "delivered"
        lane.delivers = 3.0

    outcome = PathMatrix(workers=4).run(
        [_lane("boom", explodes), _lane("pay", pays)],
        target=3.0, asset="USDC", beneficiary="0xlocked", budget_s=3.0,
    )

    boom = next(l for l in outcome.lanes if l.lane_id == "boom")
    assert boom.status == "failed"
    assert "RuntimeError" in boom.reason
    assert outcome.winner is not None and outcome.winner.lane_id == "pay"


def test_cpu_lanes_do_not_start_when_a_network_lane_already_paid():
    """Speculative work must not compete with the route that pays."""
    ran = []

    def pays(lane: Lane) -> None:
        lane.status = "delivered"
        lane.delivers = 5.0

    def cpu(lane: Lane) -> None:
        ran.append(lane.lane_id)
        lane.status = "blocked"

    outcome = PathMatrix(workers=4).run(
        [_lane("pay", pays), _lane("cpu", cpu, cpu_bound=True)],
        target=5.0, asset="USDC", beneficiary="0xlocked", budget_s=2.0,
    )

    assert ran == []
    cpu_lane = next(l for l in outcome.lanes if l.lane_id == "cpu")
    assert cpu_lane.status == "stood_down"


def test_cpu_lane_is_not_started_with_no_window_left_to_report():
    """A pure-Python lane started at the deadline cannot answer and costs the next
    request: leftover fork lanes tripled the following synthesis, 130ms -> 780ms.
    It must be recorded as not run, and must not be left running either."""
    ran = []

    def slow_network(lane: Lane) -> None:
        time.sleep(0.30)
        lane.status = "blocked"
        lane.reason = "no_venue_answered"

    def cpu(lane: Lane) -> None:
        ran.append(lane.lane_id)
        lane.status = "blocked"

    outcome = PathMatrix(workers=4).run(
        [_lane("net", slow_network), _lane("cpu", cpu, cpu_bound=True)],
        target=5.0, asset="USDC", beneficiary="0xlocked", budget_s=0.2,
    )

    assert ran == []
    cpu_lane = next(l for l in outcome.lanes if l.lane_id == "cpu")
    assert cpu_lane.status == "stood_down"
    assert "no_window_left_to_report" in cpu_lane.reason


def test_every_route_family_is_represented_in_a_real_request():
    """A matrix that only tried blockchain routes could not claim completeness."""
    from synthesizer.intent import parse_deterministic
    from synthesizer.loop import Synthesizer

    synth = Synthesizer()
    constraints = parse_deterministic("اربح 5 دولار من السيولة بدون مفتاح")
    primitives = synth.scanner.scan()
    stats = type("S", (), {"attempts": 0, "live_primitives": 0})()
    path, proof, _, _ = synth._prove_fastest(constraints, primitives, stats)
    assert proof is not None and proof.satisfied
    synth._align_to_witness(path, proof)

    from synthesizer.matrix import build_lanes

    lanes = build_lanes(synth, constraints, path, primitives)
    covered = {l.category for l in lanes}
    assert covered == set(CATEGORIES), covered
    # Every lane must be callable work, not a placeholder label.
    assert all(callable(l.run) for l in lanes)


def test_the_outcome_reports_every_lane_it_entered():
    """The matrix is the answer to "which paths exist", so none may be dropped."""
    lanes = [
        _lane(f"l{i}", lambda l: setattr(l, "status", "blocked"), category="blockchain")
        for i in range(5)
    ]
    outcome = PathMatrix(workers=4).run(
        lanes, target=1.0, asset="USDC", beneficiary="0xlocked", budget_s=2.0,
    )
    assert len(outcome.lanes) == 5
    payload = outcome.to_dict()
    assert len(payload["lanes"]) == 5
    assert payload["winner"] is None
    assert payload["delivered_total"] == 0.0


def test_no_lane_is_left_in_a_running_state_when_the_answer_is_sent():
    """A lane still working is labelled as continuing, never as a verdict."""

    def slow(lane: Lane) -> None:
        time.sleep(0.5)
        lane.status = "blocked"

    outcome = PathMatrix(workers=2).run(
        [_lane("slow", slow)], target=1.0, asset="USDC",
        beneficiary="0xlocked", budget_s=0.05,
    )
    lane = outcome.lanes[0]
    assert lane.status == "continuing"
    assert "still_working" in lane.reason
    assert lane.delivered is False


def test_two_ledgers_are_not_added_together():
    """Two lanes settling the same amount on different books is not double money.

    The Fuel chain and the local fork are separate ledgers. Summing them reported
    one settlement as two, so a design request showed 76 delivered when 38 had
    arrived on each book. Both lanes really do deliver when they race, so the
    outcome is built directly rather than hoping the scheduler produces one.
    """
    def _lane(lid, net, amount):
        lane = Lane(lane_id=lid, category="blockchain", title_ar=lid,
                    run=lambda l: None, timeout_s=3.0)
        lane.status = "delivered"
        lane.delivers = amount
        lane.network = net
        lane.tx_hash = "0x" + lid
        return lane

    fuel, fork = _lane("fuel", "fuel-devnet", 38.0), _lane("fork", "local-fork", 38.0)
    outcome = MatrixOutcome(target_value=38.0, asset="USDC", beneficiary="0xlocked",
                            lanes=[fuel, fork], winner=fuel)

    assert outcome.delivered_total == 38.0
    assert outcome.delivered_by_network == {"fuel-devnet": 38.0, "local-fork": 38.0}


def test_a_cpu_lane_from_an_earlier_request_does_not_take_the_slot():
    """CPU lanes are serialised process-wide.

    Two of them cannot progress at once under one GIL, so a second one only
    takes the interpreter from the request that is answering now. The lane that
    cannot get the slot is recorded as queued with its reason, not as failed and
    not as silently absent.
    """
    held = threading.Event()
    release = threading.Event()
    started: list[str] = []

    def slow_cpu(lane: Lane) -> None:
        started.append(lane.lane_id)
        held.set()
        release.wait(5.0)
        lane.status = "blocked"

    first = PathMatrix(workers=2)
    second = PathMatrix(workers=2)

    runner = threading.Thread(
        target=first.run,
        args=([_lane("cpu_a", slow_cpu, cpu_bound=True)],),
        kwargs=dict(target=5.0, asset="USDC", beneficiary="0xlocked", budget_s=5.0),
    )
    runner.start()
    assert held.wait(5.0), "the first cpu lane never started"

    try:
        outcome = second.run(
            [_lane("cpu_b", slow_cpu, cpu_bound=True)],
            target=5.0, asset="USDC", beneficiary="0xlocked", budget_s=1.0,
        )
    finally:
        release.set()
        runner.join(5.0)

    assert started == ["cpu_a"], "a second cpu lane ran while the first held the slot"
    lane_b = next(l for l in outcome.lanes if l.lane_id == "cpu_b")
    assert lane_b.delivers == 0.0
    assert "cpu_slot" in lane_b.reason, lane_b.reason
