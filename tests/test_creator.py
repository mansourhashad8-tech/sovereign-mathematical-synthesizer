"""Tests for the creator: any intent becomes a measured, attested deliverable.

The properties that matter here are the honesty properties. A produced artifact
must be a real file with a real hash, it must never be counted as money, and a
request for a document must not report a financial payout as its outcome.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from synthesizer.artifacts import ArtifactCreator
from synthesizer.intent import parse_deterministic
from synthesizer.matrix import Lane, MatrixOutcome


class _StubBrain:
    """A brain that answers deterministically, so the test measures the engine."""

    class _Result:
        ok = True
        text = "# Deliverable\n\nThe finished content, written to be used as-is.\n"
        provider = "stub"

    def complete(self, system: str, user: str):
        return self._Result()


class _DownBrain:
    """A brain whose whole ladder is unreachable."""

    class _Result:
        ok = False
        text = ""
        provider = "none"

    def complete(self, system: str, user: str):
        return self._Result()


def test_artifact_is_written_and_measured(tmp_path, monkeypatch):
    import synthesizer.artifacts as artifacts

    monkeypatch.setattr(artifacts, "ARTIFACT_DIR", tmp_path)
    creator = ArtifactCreator(brain=_StubBrain())
    art = creator.create("iid-1", "اكتب مقالاً قصيراً", "writing")

    assert art.ok
    written = (tmp_path / Path(art.path).name).read_bytes()
    assert art.bytes_written == len(written)
    assert art.sha256 == hashlib.sha256(written).hexdigest()
    assert art.lines == written.count(b"\n") + (0 if written.endswith(b"\n") else 1)
    # The commitment is the hash: that is what a third party recomputes.
    assert art.commitment == "0x" + art.sha256


def test_artifact_survives_a_dead_model(tmp_path, monkeypatch):
    """A dead ladder still yields a real file, never an apology."""
    import synthesizer.artifacts as artifacts

    monkeypatch.setattr(artifacts, "ARTIFACT_DIR", tmp_path)
    art = ArtifactCreator(brain=_DownBrain()).create("iid-2", "خطة إطلاق", "plan")

    assert art.ok
    assert art.producer == "deterministic-offline"
    body = Path(art.path).read_text(encoding="utf-8")
    assert "خطة إطلاق" in body
    assert "لا أستطيع" not in body


def test_artifact_filename_is_content_addressed(tmp_path, monkeypatch):
    import synthesizer.artifacts as artifacts

    monkeypatch.setattr(artifacts, "ARTIFACT_DIR", tmp_path)
    creator = ArtifactCreator(brain=_StubBrain())
    a = creator.create("iid-a", "طلب أول", "writing")
    b = creator.create("iid-b", "طلب ثانٍ", "writing")

    assert a.path != b.path
    assert len(list(tmp_path.glob("*.md"))) == 2


def test_domain_is_classified_from_the_request():
    assert parse_deterministic("اكتب لي مقالاً عن الاستدامة").goal.domain == "writing"
    assert parse_deterministic("صمم واجهة لتطبيق توصيل").goal.domain == "design"
    assert parse_deterministic("ابحث في أدوات التحقق الشكلي").goal.domain == "research"
    assert parse_deterministic("أريد أن أربح 500 دولار").goal.domain == "finance"


def test_produced_lane_never_counts_as_payment():
    lane = Lane(lane_id="c", category="creator", title_ar="خالق", run=lambda l: None,
                pays=False, unit="bytes")
    lane.status = "produced"
    lane.delivers = 4096.0

    # Producing bytes is not paying money, whatever the magnitude says.
    assert lane.produced
    assert not lane.delivered


def test_money_lane_still_counts_as_payment():
    lane = Lane(lane_id="m", category="blockchain", title_ar="مسار", run=lambda l: None)
    lane.status = "delivered"
    lane.delivers = 0.034

    assert lane.delivered
    assert not lane.produced


def test_outcome_reports_the_producer():
    producer = Lane(lane_id="c", category="creator", title_ar="خالق",
                    run=lambda l: None, pays=False, unit="bytes")
    producer.status = "produced"
    producer.delivers = 100.0
    producer.detail = {"ok": True, "bytes": 100}

    outcome = MatrixOutcome(target_value=1.0, asset="USDC",
                            beneficiary="0x0", lanes=[producer])

    assert outcome.producer is producer
    assert outcome.delivered_total == 0.0
    assert outcome.winner is None


def test_broadcast_gateway_names_the_missing_precondition():
    """A public broadcast must never be claimed without a funded key.

    Each refusal has to name which precondition is absent, because "did not
    broadcast" and "broadcast and paid" are different claims.
    """
    from synthesizer.gateway import BroadcastGateway

    gated = BroadcastGateway(rpc_url="", key="")
    assert gated.why_not() == "no_rpc_configured"

    unreachable = BroadcastGateway(rpc_url="http://127.0.0.1:1", key="")
    assert unreachable.why_not() in {"rpc_unreachable", "no_relayer_key"}


def test_broadcast_gateway_refuses_without_broadcasting():
    from synthesizer.gateway import BroadcastGateway

    gateway = BroadcastGateway(rpc_url="", key="")
    sent = gateway.send("0xe3d1c7dcd5e95b6441716b65f9c46b73e91b4d47", b"\x00")
    assert sent["ok"] is False
    assert sent["reason"] == "no_rpc_configured"


def test_unvetted_repos_are_not_adopted_as_primitives():
    """A repo nobody starred and nobody described is not a component.

    The narrative names the scanned repositories as the parts it composed, so
    adopting an unvetted name would put an unverifiable citation in the answer.
    """
    from synthesizer.scanner import GitHubScanner

    scanner = GitHubScanner()
    items = [
        {"name": "junk", "full_name": "someone/junk", "description": "",
         "html_url": "u", "stargazers_count": 0, "pushed_at": "2099-01-01T00:00:00Z"},
        {"name": "aave-v3-core", "full_name": "aave/aave-v3-core",
         "description": "Lending pools with flash loan support",
         "html_url": "u", "stargazers_count": 9000, "pushed_at": "2099-01-01T00:00:00Z"},
    ]

    class _Resp:
        status_code = 200

        def json(self):
            return {"items": items}

    import synthesizer.scanner as scanner_mod

    original = scanner_mod.httpx.get
    scanner_mod.httpx.get = lambda *a, **k: _Resp()
    try:
        found = scanner._search("lending", "flash_loan", 6)
    finally:
        scanner_mod.httpx.get = original

    assert [p.repo for p in found] == ["aave/aave-v3-core"]


def test_a_starred_but_unrelated_repo_is_not_a_component():
    """Stars prove attention, not relevance.

    GitHub's loose matching returned a physics paper for "formal-verification"
    and a tweet archive for "market-making". The narrative cites these as the
    parts it composed, so the repository's own words have to match the job.
    """
    from synthesizer.scanner import GitHubScanner
    import synthesizer.scanner as scanner_mod

    items = [
        {"name": "observer-patch-holography",
         "full_name": "FloatingPragma/observer-patch-holography",
         "description": "A rigorous and complete description of physics",
         "html_url": "u", "stargazers_count": 766, "pushed_at": "2099-01-01T00:00:00Z"},
        {"name": "z3", "full_name": "Z3Prover/z3",
         "description": "The Z3 theorem prover",
         "html_url": "u", "stargazers_count": 10000, "pushed_at": "2099-01-01T00:00:00Z"},
    ]

    class _Resp:
        status_code = 200

        def json(self):
            return {"items": items}

    scanner = GitHubScanner()
    original = scanner_mod.httpx.get
    scanner_mod.httpx.get = lambda *a, **k: _Resp()
    try:
        found = scanner._search("formal-verification", "verification", 6)
    finally:
        scanner_mod.httpx.get = original

    assert [p.repo for p in found] == ["Z3Prover/z3"]
