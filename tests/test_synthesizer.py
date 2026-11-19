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
    res = ex.settle(path, c)
    assert res.beneficiary == BENEFICIARY
    assert res.status in ("executed", "simulated")
    assert res.tx_hash.startswith("0x")


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


def test_executor_tries_every_bundler_then_falls_back_honestly():
    from synthesizer.executor import Executor

    ex = Executor()
    ex.bundlers = ["https://127.0.0.1:1/rpc"]  # unreachable on purpose
    c = parse_deterministic("اربح 5 دولار من السيولة")
    path = Composer().propose(c, _primitives(), attempt=0)
    res = ex.settle(path, c)
    assert res.status == "simulated"
    assert res.simulated is True
    assert res.beneficiary == BENEFICIARY
    assert res.gas_mode != "user"
    assert res.tx_hash.startswith("0x")
    assert "signed_erc4337_userop_ready" in res.reason


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


def test_narrative_never_claims_execution_when_settlement_was_simulated():
    """The Arabic answer must match the real settlement status."""
    from synthesizer.loop import Synthesizer

    synth = Synthesizer()
    synth.executor.bundlers = ["https://127.0.0.1:1/rpc"]
    result = synth.synthesize_sync("اربح 500 دولار من السيولة")

    assert result.execution is not None
    assert result.execution.status == "simulated"
    assert "نُفّذ المسار الذري" not in result.narrative_ar
    assert "لم تُبَثّ" in result.narrative_ar
    assert "لا يُطلب من المستخدم غاز" in result.narrative_ar


def test_narrative_reports_the_hash_only_when_a_bundler_accepted():
    from synthesizer.loop import Synthesizer

    synth = Synthesizer()

    class _AcceptingExecutor:
        def settle(self, path, constraints):
            from synthesizer.models import ExecutionResult

            return ExecutionResult(
                status="executed",
                tx_hash="0x" + "ab" * 32,
                explorer_url="https://sepolia.basescan.org/tx/0x" + "ab" * 32,
                network="base-sepolia",
                beneficiary=constraints.beneficiary,
                amount_usd=path.net_profit_usd,
                reason="broadcast_via_bundler:test",
                bundler="test-bundler",
                gas_mode="paymaster",
            )

    synth.executor = _AcceptingExecutor()
    result = synth.synthesize_sync("اربح 500 دولار من السيولة")

    assert result.execution.status == "executed"
    assert "نُفّذ المسار الذري" in result.narrative_ar
    assert "0xabababab" in result.narrative_ar
    assert "لم تُبَثّ" not in result.narrative_ar


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
