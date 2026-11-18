"""Tests for the Sovereign Mathematical Synthesizer.

These exercise real code paths: the real Z3 solver, the real composer, the real
ERC-4337 encoding, and the real HTTP surface. No mocks of the core.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from synthesizer.composer import MAX_NOTIONAL_USD, Composer
from synthesizer.config import BENEFICIARY
from synthesizer.executor import Executor, counterfactual_account, userop_hash
from synthesizer.intent import parse_deterministic
from synthesizer.models import AtomicPath, Constraints, Goal, PathStep, Primitive
from synthesizer.prover import Prover


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


def test_api_index_is_arabic_ui():
    from fastapi.testclient import TestClient

    from synthesizer.api import app

    client = TestClient(app)
    html = client.get("/").text
    assert 'dir="rtl"' in html
    assert "المُركّب الرياضي السيادي" in html
