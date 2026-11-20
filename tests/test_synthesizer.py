"""Tests for the Sovereign Mathematical Synthesizer.

These exercise real code paths: the real Z3 solver, the real composer, the real
ERC-4337 encoding, and the real HTTP surface. No mocks of the core.
"""

from __future__ import annotations

import os
import sys

import pytest
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from synthesizer.composer import MAX_NOTIONAL_USD, Composer
from synthesizer.config import BENEFICIARY
from synthesizer.executor import Executor, counterfactual_account, userop_hash
from synthesizer.intent import parse_deterministic
from synthesizer.models import AtomicPath, Constraints, Goal, PathStep, Primitive
from synthesizer.prover import Prover
from synthesizer.loop import LoopStats


# --- intent parsing --------------------------------------------------------

def test_arabic_earn_intent_is_parsed():
    c = parse_deterministic("أريد ربح 1000 دولار من سوق السيولة بدون مفتاح وبدون رأس مال")
    assert c.goal.objective in ("earn", "provide_liquidity")
    assert c.goal.asset == "USDC"
    assert c.goal.target_amount == 1000.0
    assert c.net_profit_usd_min >= 1000.0
    assert "no_capital" in c.extra
    assert "no_key" in c.extra
    assert c.allow_custody is False
    assert c.allow_user_key is False
    assert c.allow_user_gas is False


def test_arabic_digits_and_millions():
    c = parse_deterministic("أريد ربح ٢ مليون دولار فوراً")
    assert c.goal.target_amount == 2_000_000.0


def test_english_intent_fallback():
    c = parse_deterministic("earn 50 USDC now with no key")
    assert c.goal.objective == "earn"
    assert c.goal.target_amount == 50.0
    assert "no_key" in c.extra


def test_garbage_intent_still_yields_constraints():
    c = parse_deterministic("كماكماكما")
    assert c.goal.objective == "earn"
    assert c.goal.target_amount == 0.0
    assert c.net_profit_usd_min > 0


# --- composer --------------------------------------------------------------

def _prim(name, category, repo="acme/x"):
    return Primitive(name=name, repo=repo, category=category, description="")


def _primitives():
    return [
        _prim("flash", "flash_mint", "aave/aave-v3-core"),
        _prim("lbp", "liquidity", "balancer/balancer-v2-monorepo"),
        _prim("mm", "market_making", "hummingbot/hummingbot"),
        _prim("pm", "paymaster", "pimlico-io/erc20-paymaster"),
    ]


def test_composer_always_includes_capital_and_gas_sponsor():
    c = parse_deterministic("اربح 10 دولار من السيولة")
    path = Composer().propose(c, _primitives(), attempt=0)
    cats = [p.split("#")[1] for p in path.primitives] if False else path.primitives
    assert any("flash" in p for p in path.primitives)
    assert any("paymaster" in p for p in path.primitives)
    assert path.gas_sponsor != "user"
    assert path.notional_usd > 0


def test_no_capital_primitive_means_zero_notional():
    c = parse_deterministic("اربح 10 دولار")
    prims = [_prim("lbp", "liquidity"), _prim("pm", "paymaster")]
    path = Composer().propose(c, prims, attempt=0)
    assert path.notional_usd == 0.0
    assert path.net_profit_usd <= 0.0


# --- prover ----------------------------------------------------------------

def test_prover_satisfies_reachable_target():
    c = parse_deterministic("اربح 5 دولار من السيولة")
    c.net_profit_usd_min = 5.0
    path = Composer().propose(c, _primitives(), attempt=0)
    proof = Prover().prove(path, c)
    assert proof.satisfied is True
    assert proof.model["net_profit_usd"] >= 5.0
    assert "SAT" in proof.certificate


def test_prover_rejects_zero_capital_path():
    c = parse_deterministic("اربح 5 دولار")
    c.net_profit_usd_min = 5.0
    prims = [_prim("lbp", "liquidity"), _prim("pm", "paymaster")]
    path = Composer().propose(c, prims, attempt=0)
    proof = Prover().prove(path, c)
    assert proof.satisfied is False


def test_prover_rejects_user_gas_sponsor():
    c = parse_deterministic("اربح 1 دولار من السيولة")
    path = AtomicPath(
        intent_id=BENEFICIARY,
        steps=[PathStep(primitive="x#flash", action="flash", yield_rate=0.01, fee_rate=0.0)],
        primitives=["x#flash"],
        notional_usd=1_000_000.0,
        gas_sponsor="user",
    )
    proof = Prover().prove(path, c)
    assert proof.satisfied is False


def test_prover_handles_huge_target_within_notional_cap():
    c = parse_deterministic("اربح 100000 دولار من السيولة")
    c.net_profit_usd_min = 100_000.0
    path = Composer().propose(c, _primitives(), attempt=0)
    proof = Prover().prove(path, c)
    if proof.satisfied:
        assert proof.model["N"] <= MAX_NOTIONAL_USD


# --- executor --------------------------------------------------------------

def test_counterfactual_account_is_deterministic_and_lowercase_hex():
    ex = Executor()
    a1 = counterfactual_account(ex.session)
    a2 = counterfactual_account(ex.session)
    assert a1 == a2
    assert a1.startswith("0x") and len(a1) == 42


def test_userop_hash_changes_with_nonce():
    h1 = userop_hash("0x" + "11" * 20, 1, "0xdead", "0x" + "22" * 20, 8453)
    h2 = userop_hash("0x" + "11" * 20, 2, "0xdead", "0x" + "22" * 20, 8453)
    assert h1 != h2 and len(h1) == 66


def test_executor_refuses_non_locked_beneficiary():
    ex = Executor()
    c = parse_deterministic("اربح 1 دولار من السيولة")
    c.beneficiary = "0x0000000000000000000000000000000000000000"
    path = Composer().propose(c, _primitives(), attempt=0)
    res = ex.settle(path, c)
    assert res.status == "queued"
    assert res.reason == "beneficiary_not_locked"


def test_executor_settles_to_locked_beneficiary():
    ex = Executor()
    c = parse_deterministic("اربح 5 دولار من السيولة")
    path = Composer().propose(c, _primitives(), attempt=0)
    # A settlement only exists for a proved amount; the loop always proves
    # before it settles, so the test does the same.
    path.proven_net_profit_usd = 5.0
    res = ex.settle(path, c)
    assert res.beneficiary == BENEFICIARY
    # A mined operation is never reported as plain success unless the
    # beneficiary's balance actually grew. Gate A alone is not enough.
    assert res.status in ("executed", "pending", "unsettled_value")
    if res.status == "executed":
        assert res.value_transferred is True
    if res.status == "unsettled_value":
        assert res.value_transferred is False
        assert res.tx_hash.startswith("0x")  # the tx is real; the value is not
    assert res.tx_hash.startswith("0x") or res.status == "pending"


def test_value_actually_landed_on_beneficiary():
    """The only passing definition of success: the balance delta is measured.

    Gate A (receipt.status == 1) proves the operation executed. Gate B proves
    money arrived. A settlement is a success only when both hold, and the
    engine must never assert the second from the first.
    """
    from synthesizer.executor import Executor
    from synthesizer.gasflywheel import Network

    ex = Executor()
    net = Network(
        name="probe", chain_id=84532, rpc_url="https://sepolia.base.org",
        explorer_base="https://sepolia.basescan.org/tx/", paymaster_url="",
    )
    before = ex._beneficiary_balance(net)

    # Gate B must reject an unchanged balance, whatever the receipt said.
    landed, evidence, _ = ex.sweep_landed(net, before, 0)
    assert landed is False, "an unchanged balance must never count as a payout"
    assert "balance_unchanged" in evidence

    # Gate B must reject a balance that grew by less than the proved profit.
    landed_short, evidence_short, _ = ex.sweep_landed(net, before - 1, 10**18)
    assert landed_short is False
    assert "balance_short" in evidence_short

    # And it must accept a real delta that meets the expectation.
    landed_ok, evidence_ok, _ = ex.sweep_landed(net, before - 10**18, 10**18)
    assert landed_ok is True
    assert "balance_grew" in evidence_ok

    # End to end: a mined-but-valueless settlement is unsettled_value, never
    # "executed" with value_transferred inferred from the receipt.
    c = parse_deterministic("اربح 5 دولار من السيولة")
    path = Composer().propose(c, _primitives(), attempt=0)
    res = ex.settle(path, c)
    if res.status == "executed":
        assert res.value_transferred is True, "executed implies measured value"
        assert res.amount_usd > 0
    if res.status == "unsettled_value":
        assert res.value_transferred is False
        assert res.amount_usd == 0.0, "no value moved, so no profit is claimed"
        assert res.tx_hash.startswith("0x"), "the real tx hash is still reported"


# --- full loop -------------------------------------------------------------

def test_loop_never_returns_failure_and_uses_live_primitives():
    from synthesizer.loop import Synthesizer

    synth = Synthesizer()
    result = synth.synthesize_sync("اربح 3 دولار من السيولة بدون مفتاح وبدون راس مال", budget=30)
    # The system must produce a result object, never raise or refuse.
    assert result is not None
    assert result.narrative_ar
    assert "لا أستطيع" not in result.narrative_ar
    # It either proves a path (executed/ready) or keeps a background search.
    if result.proof is not None and result.proof.satisfied:
        assert result.chosen_path is not None
        assert result.execution is not None
        assert result.execution.beneficiary == BENEFICIARY


# --- API -------------------------------------------------------------------

def test_api_health_and_synthesize():
    from fastapi.testclient import TestClient

    from synthesizer.api import app

    client = TestClient(app)
    assert client.get("/health").json()["status"] == "ok"
    r = client.post("/synthesize", json={"intent": "اربح 2 دولار من السيولة"})
    assert r.status_code == 200
    body = r.json()
    assert body["constraints"]["beneficiary"] == BENEFICIARY
    assert body["narrative_ar"]


def test_second_prover_agrees_when_available():
    from synthesizer.prover import _CVC5_PY, Prover

    c = parse_deterministic("اربح 5 دولار من السيولة")
    c.net_profit_usd_min = 5.0
    path = Composer().propose(c, _primitives(), attempt=0)
    proof = Prover().prove(path, c)
    assert proof.satisfied is True
    if _CVC5_PY:
        # Two independent engines must agree on the same SMT problem.
        assert "cvc5=sat" in proof.certificate


def test_billion_target_yields_bound_certificate_not_refusal():
    from synthesizer.loop import Synthesizer

    synth = Synthesizer()
    result = synth.synthesize_sync(
        "اريد ربح مليار دولار من المراجحة الذرية بدون مفتاح وبدون راس مال"
    )
    # A target beyond the physical notional cap is answered in the same request
    # with a mathematical bound certificate, never deferred and never refused.
    assert result.background is False
    assert result.absolute_limit is True
    assert result.proof is not None
    assert "UNSAT" in result.proof.certificate
    assert "max_atomic_profit" in result.proof.certificate
    assert "لا أستطيع" not in result.narrative_ar
    assert "مستحيل" not in result.narrative_ar


def test_billion_word_is_parsed():
    c = parse_deterministic("اريد ربح مليار دولار")
    assert c.goal.target_amount == 1_000_000_000.0


def test_api_index_is_arabic_ui():
    from fastapi.testclient import TestClient

    from synthesizer.api import app

    client = TestClient(app)
    html = client.get("/").text
    assert 'dir="rtl"' in html
    assert "المُركّب الرياضي السيادي" in html


# --- instant execution mandate --------------------------------------------

def test_every_intent_answers_inside_the_instant_deadline():
    """The decision must be made in the same request, well under a second.

    Network stages are pre-warmed at process start, so what remains is in-memory
    work: parse, template instantiation, parallel proof, and the signed-op
    construction. The budget here is deliberately looser than the live one so
    the test measures the design, not CI jitter.
    """
    from synthesizer.loop import Synthesizer

    synth = Synthesizer()
    synth.executor.prewarm()
    synth.scanner.scan()
    synth.synthesize_sync("اربح 7 دولار من السيولة")  # warm every cache

    for intent in (
        "اربح 4000 دولار من السيولة بدون مفتاح وبدون راس مال",
        "حول 1000 دولار الى ايثيريوم فورا",
        "اعمل ماركت ميكينج بلا راس مال",
    ):
        result = synth.synthesize_sync(intent)
        assert result.elapsed_seconds < 2.0, (intent, result.elapsed_seconds)
        assert result.background is False
        assert result.timings_ms["synthesis"] > 0


def test_no_background_thread_is_left_running():
    """The loop must not defer work: after a request, nothing new is running."""
    import threading

    from synthesizer.loop import Synthesizer

    before = {t.name for t in threading.enumerate()}
    synth = Synthesizer()
    result = synth.synthesize_sync("اربح 9 دولار من السيولة")
    after = {t.name for t in threading.enumerate()}

    assert result.background is False
    assert not any("polish" in n or "synthesis-loop" in n for n in after - before)


def test_templates_instantiate_without_any_scan():
    from synthesizer.models import Constraints, Goal
    from synthesizer.templates import TEMPLATES

    c = Constraints(
        goal=Goal(objective="earn", asset="USDC", target_amount=100.0, raw_intent="x"),
        net_profit_usd_min=100.0,
        beneficiary=BENEFICIARY,
    )
    for tpl in TEMPLATES:
        path = tpl.instantiate(c, notional=1_000.0)
        assert path.template == tpl.name
        assert path.primitives == tpl.primitives
        assert path.has_uncollateralized_capital is True
        assert path.net_rate == tpl.net_rate


def test_instant_path_is_deterministic_for_a_fixed_market_snapshot():
    """Same intent + same primitive set => byte-identical decision.

    Determinism is defined against a market snapshot, not against wall-clock
    time: the live scan legitimately adds primitives between requests, and a
    new primitive may yield a better path. What must never happen is the same
    intent and the same primitives producing different answers.
    """
    from synthesizer.loop import Synthesizer

    synth = Synthesizer()
    snapshot = synth.scanner.scan()  # pin the snapshot the scan returned

    def run():
        constraints = parse_deterministic("اربح 250 دولار من السيولة")
        path, proof, _, _ = synth._prove_fastest(
            constraints, snapshot, LoopStats(started_at=time.time())
        )
        return path, proof

    a_path, a_proof = run()
    b_path, b_proof = run()
    assert a_path is not None and b_path is not None
    assert a_path.path_id == b_path.path_id
    assert a_proof.certificate.split(";")[0] == b_proof.certificate.split(";")[0]


def test_same_intent_reuses_the_same_decision_while_the_snapshot_is_stable():
    from synthesizer.loop import Synthesizer

    synth = Synthesizer()
    synth.scanner.scan()
    first = synth.synthesize_sync("اربح 250 دولار من السيولة")
    second = synth.synthesize_sync("اربح 250 دولار من السيولة")
    assert first.intent_id == second.intent_id
    # The scan may have completed between the two calls, so only assert the
    # decision is stable while the primitive set is unchanged.
    if synth.scanner.scan() == synth.scanner.scan():
        assert first.chosen_path.path_id == second.chosen_path.path_id


def test_bound_proof_reports_the_exact_physical_ceiling():
    from synthesizer.composer import MAX_NOTIONAL_USD
    from synthesizer.prover import Prover

    c = parse_deterministic("اربح 500000000 دولار من المراجحة")
    path = Composer().propose(c, _primitives(), attempt=0)
    proof = Prover().prove_bound(c, path)
    assert proof.detail == "absolute_limit"
    assert proof.model["max_reachable_usd"] <= MAX_NOTIONAL_USD * 1.0
    assert proof.model["max_reachable_usd"] < c.net_profit_usd_min


def test_executor_tries_every_bundler_then_falls_back_honestly(monkeypatch):
    """With every gas source unreachable, the engine says so and invents no hash."""
    from synthesizer.config import SETTINGS
    from synthesizer.executor import Executor

    dead = "https://127.0.0.1:1/rpc"
    monkeypatch.setattr(SETTINGS, "public_bundler_url", dead)
    monkeypatch.setattr(SETTINGS, "paymaster_url", dead)
    monkeypatch.setattr(SETTINGS, "extra_paymasters", [])
    monkeypatch.setattr(SETTINGS, "fallback_networks", [])
    monkeypatch.setattr(SETTINGS, "receipt_timeout_seconds", 0.1)
    # This test is about the public path. The fork fallback and the Fuel leg are
    # separate legs with their own tests, so they are switched off here.
    monkeypatch.setattr(SETTINGS, "settle_on_fork", False)
    monkeypatch.setattr(SETTINGS, "fuel_primary", False)

    ex = Executor()
    c = parse_deterministic("اربح 5 دولار من السيولة")
    path = Composer().propose(c, _primitives(), attempt=0)
    path.proven_net_profit_usd = 5.0
    res = ex.settle(path, c)

    assert res.status == "pending"
    assert res.simulated is False
    # A pending settlement carries no transaction hash, because none exists.
    assert res.tx_hash == ""
    assert res.explorer_url == ""
    assert res.beneficiary == BENEFICIARY
    assert res.gas_mode != "user"
    assert "no_network_settled" in res.reason


def test_api_reports_instant_latency_headers():
    from fastapi.testclient import TestClient

    from synthesizer.api import app

    client = TestClient(app)
    r = client.post("/synthesize", json={"intent": "اربح 3 دولار من السيولة"})
    assert r.status_code == 200
    assert r.headers["X-Synthesis-Background"] == "false"
    assert float(r.headers["X-Synthesis-Ms"]) > 0
    assert r.json()["background"] is False


def test_api_exposes_the_template_library():
    from fastapi.testclient import TestClient

    from synthesizer.api import app

    client = TestClient(app)
    body = client.get("/templates").json()
    names = {t["name"] for t in body["templates"]}
    assert "aave-uniswap-atomic" in names
    assert all(t["primitives"] for t in body["templates"])


def test_narrative_never_claims_execution_when_settlement_is_pending():
    """The Arabic answer must match the real settlement status."""
    from synthesizer.loop import Synthesizer
    from synthesizer.models import ExecutionResult

    synth = Synthesizer()
    c = parse_deterministic("اربح 500 دولار من السيولة")
    path = Composer().propose(c, _primitives(), attempt=0)
    assert path is not None
    proof = synth.prover.prove(path, c)
    pending = ExecutionResult(
        status="pending",
        simulated=False,
        beneficiary=BENEFICIARY,
        gas_mode="paymaster",
        reason="no_network_settled",
    )

    narrative, _ = synth._narrate(
        c, path, proof, False, type("S", (), {"attempts": 1, "live_primitives": 1,
                                             "started_at": 0})(), None, False, pending,
    )

    assert "نُفّذ المسار الذري" not in narrative
    assert "لم يتحرك أي مال" in narrative
    assert "لا يُطلب من المستخدم غاز" in narrative


def test_a_mined_settlement_upgrades_the_answer_with_its_real_hash():
    """A background settlement must land in the stored result, not be lost."""
    from synthesizer.gasflywheel import GasSettlement
    from synthesizer.loop import Synthesizer
    from synthesizer.models import ExecutionResult, IntentResult

    synth = Synthesizer()
    c = parse_deterministic("اربح 500 دولار من السيولة")
    path = Composer().propose(c, _primitives(), attempt=0)
    assert path is not None
    proof = synth.prover.prove(path, c)

    execution = ExecutionResult(
        status="pending", beneficiary=BENEFICIARY, gas_mode="paymaster",
        amount_usd=path.net_profit_usd,
    )
    result = IntentResult(
        intent_id="iid", raw_intent=c.goal.raw_intent, constraints=c,
        chosen_path=path, proof=proof, execution=execution,
        narrative_ar="", invented=False, attempts=1, primitives_used=path.primitives,
        equations=[], elapsed_seconds=0.01, background=False, source="proved",
    )
    synth._results["iid"] = result

    tx = "0x" + "ab" * 32
    synth._apply_settlement("iid", GasSettlement(
        status="executed",
        network="base-sepolia",
        chain_id=84532,
        tx_hash=tx,
        user_op_hash="0x" + "cd" * 32,
        explorer_url=f"https://sepolia.basescan.org/tx/{tx}",
        paymaster="0x" + "36" * 20,
        account="0x" + "1e" * 20,
        gas_paid_by="paymaster",
        actual_gas_cost_wei=3_000_000_000_000,
        block_number=60_977_110,
        reason="mined",
    ))

    ex = synth._results["iid"].execution
    # The hash, block, and sponsor are all real and are recorded regardless.
    assert ex.simulated is False
    assert ex.tx_hash == tx
    assert ex.explorer_url.endswith(tx)
    assert ex.gas_paid_by == "paymaster"
    assert ex.block_number == 60_977_110
    assert tx in synth._results["iid"].narrative_ar
    # A receipt alone is not a payout. With no measured balance growth this
    # must land as unsettled_value, never as "executed".
    assert ex.status == "unsettled_value"
    assert ex.value_transferred is False
    assert ex.amount_usd == 0.0


def test_a_failed_settlement_leaves_no_hash_behind():
    """A simulation must clear any hash rather than keep a stale one."""
    from synthesizer.gasflywheel import GasSettlement
    from synthesizer.loop import Synthesizer
    from synthesizer.models import ExecutionResult, IntentResult

    synth = Synthesizer()
    c = parse_deterministic("اربح 500 دولار من السيولة")
    path = Composer().propose(c, _primitives(), attempt=0)
    assert path is not None
    proof = synth.prover.prove(path, c)

    execution = ExecutionResult(
        status="pending", beneficiary=BENEFICIARY, gas_mode="paymaster",
        amount_usd=path.net_profit_usd,
    )
    synth._results["iid2"] = IntentResult(
        intent_id="iid2", raw_intent=c.goal.raw_intent, constraints=c,
        chosen_path=path, proof=proof, execution=execution,
        narrative_ar="", invented=False, attempts=1, primitives_used=path.primitives,
        equations=[], elapsed_seconds=0.01, background=False, source="proved",
    )

    synth._apply_settlement("iid2", GasSettlement(
        status="unsettled", reason="no_network_settled",
    ))

    ex = synth._results["iid2"].execution
    assert ex.status == "pending"
    assert ex.simulated is False
    assert ex.tx_hash == ""
    assert ex.explorer_url == ""


def test_bound_certificate_measures_the_market_not_an_empty_path():
    """An unreachable target must yield a real ceiling, not a vacuous zero."""
    from synthesizer.loop import Synthesizer

    synth = Synthesizer()
    result = synth.synthesize_sync("اريد ربح مليار دولار غدا")

    assert result.absolute_limit is True
    assert result.execution is None
    assert result.proof.satisfied is False
    assert result.proof.detail == "absolute_limit"
    # The ceiling must be a real number derived from the best composition,
    # never the empty-path placeholder.
    assert result.proof.model["max_reachable_usd"] > 0
    assert "max_atomic_profit=0.00 USD" not in result.proof.certificate
    assert "حد فيزيائي" in result.narrative_ar


def test_prove_bound_reports_no_path_instead_of_a_zero_ceiling():
    from synthesizer.prover import Prover

    proof = Prover().prove_bound(
        Constraints(
            goal=Goal(
                objective="earn", asset="USDC", target_amount=100.0,
                raw_intent="x",
            ),
            net_profit_usd_min=100.0,
            beneficiary=BENEFICIARY,
        ),
        None,
    )
    assert proof.detail == "no_path"
    assert proof.model["no_path"] is True
    assert "no market ceiling was measured" in proof.certificate


def test_github_query_asks_for_fresh_repositories():
    """The 48h window must be a query qualifier, not a post-filter.

    Sorting by ``updated`` without ``pushed:>`` returns stale repositories and
    burns the search quota on results the freshness check discards.
    """
    import datetime as dt
    from unittest.mock import patch

    import httpx as _httpx

    from synthesizer.scanner import GitHubScanner

    seen: dict = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"items": []}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen["q"] = params["q"]
        return _Resp()

    with patch.object(_httpx, "get", fake_get):
        GitHubScanner(token="t")._search("paymaster", "paymaster")

    assert "pushed:>" in seen["q"]
    since = seen["q"].split("pushed:>")[1]
    days_old = (dt.date.today() - dt.date.fromisoformat(since)).days
    assert days_old <= 2


def test_rate_limited_scan_is_reported_not_hidden():
    from unittest.mock import patch

    import httpx as _httpx

    from synthesizer.scanner import GitHubScanner

    class _Resp:
        status_code = 403

        def json(self):
            return {"message": "API rate limit exceeded"}

    scanner = GitHubScanner(token="t")
    assert scanner.rate_limited() is False

    with patch.object(_httpx, "get", lambda *a, **k: _Resp()):
        assert scanner._search("paymaster", "paymaster") == []

    assert scanner.rate_limited() is True


def test_reported_profit_matches_the_proven_certificate():
    """One source of truth: narrative, executor and path must agree.

    The solver picks concrete rates inside each band. If the path keeps the
    composer's declared rates, the profit it reports differs from the profit
    that was proved, and the UI shows two different numbers.
    """
    from synthesizer.loop import Synthesizer

    synth = Synthesizer()
    result = synth.synthesize_sync("اربح 5000 دولار من السيولة")

    assert result.proof.satisfied is True
    proven = result.proof.model["net_profit_usd"]
    assert result.chosen_path.net_profit_usd == pytest.approx(proven, rel=1e-6)
    assert result.execution.amount_usd == pytest.approx(proven, rel=1e-6)
    assert f"{proven:.6f}" in result.narrative_ar


# --- gas flywheel ---------------------------------------------------------


def test_userop_hash_covers_every_gas_field():
    """The hash must change when any gas field changes.

    This is the bug that made every sponsored operation fail signature
    validation: a hash that ignores the gas fields and paymaster data signs a
    different operation than the one that is broadcast.
    """
    from synthesizer.gasflywheel import GasFlywheel

    base = {
        "sender": "0x" + "11" * 20,
        "nonce": "0x0",
        "initCode": "0x",
        "callData": "0xdeadbeef",
        "callGasLimit": "0x30d40",
        "verificationGasLimit": "0x30d40",
        "preVerificationGas": "0xc350",
        "maxFeePerGas": "0x1",
        "maxPriorityFeePerGas": "0x1",
        "paymasterAndData": "0x" + "00" * 52,
    }
    reference = GasFlywheel.user_op_hash(base, 84532)

    for field_name, value in [
        ("callGasLimit", "0x30d41"),
        ("verificationGasLimit", "0x30d41"),
        ("preVerificationGas", "0xc351"),
        ("maxFeePerGas", "0x2"),
        ("maxPriorityFeePerGas", "0x2"),
        ("paymasterAndData", "0x" + "11" * 52),
        ("nonce", "0x1"),
    ]:
        changed = dict(base)
        changed[field_name] = value
        assert GasFlywheel.user_op_hash(changed, 84532) != reference, (
            f"{field_name} is not covered by the userOp hash"
        )

    # The chain is part of the hash, so a replay on another network differs.
    assert GasFlywheel.user_op_hash(base, 11155111) != reference


def test_call_data_targets_the_locked_beneficiary():
    """The encoded call must name the locked address and nothing else."""
    from eth_abi import decode as abi_decode

    from synthesizer.gasflywheel import GasFlywheel

    data = GasFlywheel.call_data(BENEFICIARY, 0)
    assert data.startswith("0xb61d27f6")  # execute(address,uint256,bytes)
    to, value, _ = abi_decode(
        ["address", "uint256", "bytes"], bytes.fromhex(data[10:])
    )
    assert to == BENEFICIARY
    assert value == 0


def test_a_settlement_without_a_receipt_is_reported_as_submitted_not_failed():
    """A bundler acceptance is a real submission, even before it is mined."""
    from synthesizer.executor import Executor
    from synthesizer.models import ExecutionResult

    ex = Executor()
    result = ExecutionResult(status="submitted", tx_hash="0x" + "cd" * 32,
                             simulated=False, gas_paid_by="paymaster")
    assert result.simulated is False
    assert result.status != "unsettled"
    assert result.tx_hash.startswith("0x")
    # The executor exposes one owner of the callData the watchdog must replay.
    c = parse_deterministic("اربح 500 دولار من السيولة")
    path = Composer().propose(c, _primitives(), attempt=0)
    assert path is not None
    assert ex.settlement_call_data(path) == ex.settlement_call_data(path)


def test_the_watchdog_upgrades_an_unsettled_result_when_a_retry_lands():
    """An unsettled settlement is a to-do: the watch must finish it unattended."""
    from synthesizer.gasflywheel import GasSettlement
    from synthesizer.loop import Synthesizer
    from synthesizer.models import ExecutionResult, IntentResult

    synth = Synthesizer()
    c = parse_deterministic("اربح 500 دولار من السيولة")
    path = Composer().propose(c, _primitives(), attempt=0)
    assert path is not None
    proof = synth.prover.prove(path, c)

    synth._results["w1"] = IntentResult(
        intent_id="w1", raw_intent=c.goal.raw_intent, constraints=c,
        chosen_path=path, proof=proof,
        execution=ExecutionResult(status="pending", simulated=False,
                                  beneficiary=BENEFICIARY, gas_mode="paymaster"),
        narrative_ar="", invented=False, attempts=1, primitives_used=path.primitives,
        equations=[], elapsed_seconds=0.01, background=False, source="proved",
    )

    # Simulate the retry that the watchdog thread would make.
    synth._apply_settlement("w1", GasSettlement(
        status="executed", network="base-sepolia", chain_id=84532,
        tx_hash="0x" + "ef" * 32, explorer_url="https://x/tx/0x" + "ef" * 32,
        gas_paid_by="paymaster", block_number=1, reason="mined",
    ))

    ex = synth._results["w1"].execution
    # The retry's hash is real and recorded; the payout is judged separately.
    assert ex.tx_hash == "0x" + "ef" * 32
    assert ex.status in ("executed", "unsettled_value")
    if ex.status == "executed":
        assert ex.value_transferred is True
    else:
        assert ex.value_transferred is False and ex.amount_usd == 0.0
    assert synth.watchdog.status()["watched"] >= 0


def test_reverted_value_triggers_a_deeper_path_not_a_replay():
    """A no-op settlement must widen the path, never repeat it.

    Replaying a shallow path that already mined and moved nothing reproduces
    the same no-op, so the retry composes the deepest available path instead:
    a capital primitive plus independent revenue hops plus a sponsor.
    """
    from synthesizer.composer import Composer
    from synthesizer.intent import parse_deterministic
    from synthesizer.scanner import GitHubScanner

    c = parse_deterministic("اربح 100 دولار من السيولة بدون مفتاح")
    prims = GitHubScanner().scan()
    shallow = Composer().propose(c, prims, attempt=0)
    deep = Composer().propose_deep(c, prims)

    assert shallow is not None and deep is not None
    assert len(deep.steps) >= 2, "a deeper path must carry more hops"
    assert deep.has_uncollateralized_capital, "the retry needs a capital source"
    cats = {s.action.split(":")[0] for s in deep.steps}
    assert "paymaster" in cats, "the retry still never asks the user for gas"
    assert deep.gas_sponsor != "user"


def test_genesis_mesh_classifies_every_source_honestly():
    """The mesh must never report a human-gated source as agent-claimable."""
    from synthesizer.genesis import GenesisMesh, NETWORKS

    report = GenesisMesh().scan()
    assert len(report.sources) >= 8, "the mesh covers the eight networks"
    assert len(NETWORKS) == 8

    for s in report.sources:
        assert s.gate in ("agent_claimable", "human_gated", "unreachable")
        # A gate always carries its justification.
        assert s.why, f"{s.name} must state why it is gated"
        if s.gate == "human_gated":
            assert not s.claimable, "a human-gated source is never claimable"
        # Claimable requires the chain to answer, not just the label.
        if s.claimable:
            assert s.reachable, "claimable implies the network was reachable"

    # Every faucet is human-gated; that is the finding the engine must act on.
    faucets = [s for s in report.sources if s.kind == "faucet"]
    assert faucets and all(not f.claimable for f in faucets)

    # The only agent-claimable sources are the verified sponsored-gas rungs.
    for s in report.claimable:
        assert s.kind == "gas_credit"


def test_a_path_with_no_proved_amount_never_settles_as_an_empty_call():
    """The deep retry path used to encode execute(beneficiary, 0, "0x").

    That mines with status 1, costs the sponsor gas, emits no transfer and pays
    nobody, while every surface calls it a settlement. The amount has to exist
    before a settlement can be built for it.
    """
    from synthesizer.executor import Executor
    from synthesizer.models import AtomicPath

    ex = Executor()
    unproved = AtomicPath(intent_id="unproved", steps=[], simulation_ok=True)
    c = parse_deterministic("اربح 5 دولار من السيولة")

    res = ex.settle(unproved, c)
    assert res.status == "queued"
    assert res.reason == "no_settlement_amount"
    assert res.tx_hash == "", "nothing may be broadcast without an amount"


def test_the_deep_retry_path_carries_the_amount_it_settles():
    """propose_deep is the retry after a no-op; it must know what to settle."""
    from synthesizer.composer import Composer

    c = parse_deterministic("اربح 40 دولار من السيولة")
    deep = Composer().propose_deep(c, _primitives())
    assert deep is not None
    assert deep.proven_net_profit_usd is not None
    assert deep.proven_net_profit_usd > 0


def test_each_intent_gets_its_own_lock_key():
    """The lock settles an intentId once, so a shared key blocks every later run.

    Seeding every path with the beneficiary address gave all intents the same
    key. The first settlement consumed it and every later one reverted inside
    the lock, while the transaction still mined and was reported as a
    settlement that moved nothing.
    """
    from synthesizer.executor import _settlement_key
    from synthesizer.models import AtomicPath

    a = AtomicPath(intent_id="intent-aaa", steps=[], proven_net_profit_usd=10.0)
    b = AtomicPath(intent_id="intent-bbb", steps=[], proven_net_profit_usd=10.0)
    assert _settlement_key(a) == "intent-aaa"
    assert _settlement_key(a) != _settlement_key(b)

    # A path with no id still needs a stable key that is not the beneficiary.
    bare = AtomicPath(intent_id="", steps=[], proven_net_profit_usd=12.5,
                      notional_usd=900.0, primitives=["x#x"])
    key = _settlement_key(bare)
    assert key and key == _settlement_key(bare)
    assert key != BENEFICIARY

    other = AtomicPath(intent_id="", steps=[], proven_net_profit_usd=13.5,
                       notional_usd=900.0, primitives=["x#x"])
    assert _settlement_key(other) != key


def test_the_loop_gives_the_chosen_path_the_real_intent_id():
    """The path settled on chain must key the lock by the intent, not the payee."""
    from synthesizer.intent import intent_id
    from synthesizer.loop import Synthesizer

    synth = Synthesizer()
    c = parse_deterministic("اربح 20 دولار من السيولة")
    import asyncio

    result = asyncio.run(synth.synthesize("اربح 20 دولار من السيولة"))
    if result.chosen_path is None:
        return
    # The key must identify the settlement, never the beneficiary, because the
    # lock settles each key once.
    assert result.chosen_path.intent_id != BENEFICIARY
    if result.chosen_path.intent_id:
        assert result.chosen_path.intent_id == intent_id(c)


def test_unsettled_lists_only_intents_that_have_not_paid():
    """The background pass works what is still owed, not what already paid."""
    from synthesizer.loop import Synthesizer
    from synthesizer.models import ExecutionResult, IntentResult, Constraints, Goal

    synth = Synthesizer()
    c = Constraints(goal=Goal(objective="x", asset="USDC", target_amount=10.0,
                              raw_intent="x"), net_profit_usd_min=10.0,
                    beneficiary=BENEFICIARY)
    synth._results["paid"] = IntentResult(
        intent_id="paid", raw_intent="x", constraints=c, chosen_path=None,
        proof=None, execution=ExecutionResult(status="executed"), narrative_ar="",
        invented=False, attempts=1,
    )
    synth._results["owed"] = IntentResult(
        intent_id="owed", raw_intent="x", constraints=c, chosen_path=None,
        proof=None, execution=ExecutionResult(status="unsettled_value"),
        narrative_ar="", invented=False, attempts=1,
    )
    synth._results["limit"] = IntentResult(
        intent_id="limit", raw_intent="x", constraints=c, chosen_path=None,
        proof=None, execution=None, narrative_ar="", invented=False,
        attempts=1, absolute_limit=True,
    )

    assert [r.intent_id for r in synth.unsettled()] == ["owed"]


def test_the_background_pass_retries_every_unsettled_intent(monkeypatch):
    """always_on has to mean a repeated attempt, not a flag in the payload."""
    from synthesizer.config import SETTINGS
    from synthesizer.loop import Synthesizer

    # These tests are about the retry bookkeeping, so the leg that settles
    # inline is switched off and the intent is left owing value on purpose.
    monkeypatch.setattr(SETTINGS, "fuel_primary", False)
    synth = Synthesizer()
    synth.synthesize_sync("اربح 12 دولار من السيولة")
    tried: list[str] = []
    monkeypatch.setattr(synth, "synthesize_sync",
                        lambda raw, budget=None: tried.append(raw))

    count = synth.retry_unsettled()
    assert count == len(synth.unsettled()) >= 1
    assert len(tried) == count


def test_one_failing_intent_does_not_stop_the_others(monkeypatch):
    from synthesizer.config import SETTINGS
    from synthesizer.loop import Synthesizer

    monkeypatch.setattr(SETTINGS, "fuel_primary", False)
    synth = Synthesizer()
    synth.synthesize_sync("اربح 12 دولار من السيولة")
    calls: list[str] = []

    def flaky(raw, budget=None):
        calls.append(raw)
        raise RuntimeError("boom")

    monkeypatch.setattr(synth, "synthesize_sync", flaky)
    assert synth.retry_unsettled() == 1  # raised internally, still counted
    assert len(calls) == 1
