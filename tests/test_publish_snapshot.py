"""The published snapshot must only carry ids the chain still answers for.

The permanent site has no engine, so it shows a file. That file is the only
evidence a reader can check, which makes it exactly the place where a stale id
would do damage: a transaction hash from an earlier run names a transaction on a
chain that has since been thrown away.
"""
from __future__ import annotations

import json

from scripts import publish_snapshot


def _row(tx: str, *, status: str = "executed", network: str = "fuel-devnet") -> dict:
    return {
        "intent_id": "abc123",
        "raw_intent": "اريد ربح 10 دولار بدون مفتاح",
        "chosen_path": {
            "path_id": "p1",
            "steps": [{"a": 1}],
            "net_profit_usd": 10.0,
            "created_at": 1.0,
        },
        "proof": {"satisfied": True, "certificate": "SAT", "solver": "z3"},
        "execution": {
            "status": status,
            "network": network,
            "tx_hash": tx,
            "block_number": "7",
            "gas_paid_by": "genesis-supply",
            "delivered": 10.0,
            "value_transferred": True,
        },
    }


def _write(tmp_path, monkeypatch, rows: list[dict]) -> None:
    evidence = tmp_path / "synthesis.jsonl"
    evidence.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows))
    monkeypatch.setattr(publish_snapshot, "EVIDENCE", evidence)
    # The ledger accumulates across runs, so it must be per-test: pointing at the
    # committed one would let a real settlement leak into these assertions.
    monkeypatch.setattr(publish_snapshot, "LEDGER", tmp_path / "settlements.jsonl")
    monkeypatch.setattr(publish_snapshot, "DOCS", tmp_path)
    monkeypatch.setattr(publish_snapshot, "ROOT", tmp_path)


def test_a_hash_the_node_does_not_know_is_not_published(tmp_path, monkeypatch):
    """Old evidence describes a chain that no longer exists, so it must not ship."""
    _write(tmp_path, monkeypatch, [_row("0xdeadbeef"), _row("0xgood")])
    monkeypatch.setattr(
        publish_snapshot, "_node_answers", lambda tx, rpc: tx == "0xgood"
    )

    payload = publish_snapshot.build()

    published = [r["tx_hash"] for r in payload["results"]]
    assert published == ["0xgood"]
    assert payload["settled_count"] == 1
    assert payload["unverified_count"] == 1


def test_nothing_is_published_when_the_node_is_unreachable(tmp_path, monkeypatch):
    """An unreachable node is not evidence: report zero rather than assume."""
    _write(tmp_path, monkeypatch, [_row("0xabc")])
    monkeypatch.setattr(publish_snapshot, "_node_answers", lambda tx, rpc: False)

    payload = publish_snapshot.build()

    assert payload["results"] == []
    assert payload["settled_count"] == 0
    assert payload["total_results"] == 1


def test_a_simulated_run_is_never_counted_as_settled(tmp_path, monkeypatch):
    """Only an executed settlement is a payment; a simulation moves nothing."""
    _write(tmp_path, monkeypatch, [_row("0xabc", status="simulated")])
    monkeypatch.setattr(publish_snapshot, "_node_answers", lambda tx, rpc: True)

    payload = publish_snapshot.build()

    assert payload["settled_count"] == 0
    assert payload["unverified_count"] == 0


def test_the_static_health_file_does_not_claim_a_live_probe(tmp_path, monkeypatch):
    """A file host cannot probe a process, so the file must not imply one."""
    _write(tmp_path, monkeypatch, [_row("0xabc")])
    monkeypatch.setattr(publish_snapshot, "_node_answers", lambda tx, rpc: True)

    payload = publish_snapshot.build()
    publish_snapshot.write(payload)
    health = json.loads((tmp_path / "health.json").read_text())

    assert health["status"] == "ok"
    assert health["live_probe"] is False
    assert health["settled_count"] == 1


def test_a_later_run_does_not_erase_earlier_settlements(tmp_path, monkeypatch):
    """The chain is rebuilt empty each run, so the record has to outlive it.

    A run that settles one intent must not shrink the page to that one intent:
    the settlements made before it still happened, and a reader returning later
    should see the record, not the last hour.
    """
    _write(tmp_path, monkeypatch, [_row("0xold")])
    monkeypatch.setattr(publish_snapshot, "_node_answers", lambda tx, rpc: True)
    assert publish_snapshot.build()["settled_count"] == 1

    # Next run: a fresh chain that knows only the new settlement.
    _write(tmp_path, monkeypatch, [_row("0xnew")])
    monkeypatch.setattr(
        publish_snapshot, "_node_answers", lambda tx, rpc: tx == "0xnew"
    )
    payload = publish_snapshot.build()

    assert payload["settled_count"] == 2
    assert payload["verified_now_count"] == 1


def test_a_settlement_the_chain_forgot_is_counted_but_not_rendered(
    tmp_path, monkeypatch
):
    """Keep it in the record, but never show it as a receipt.

    A row the node cannot answer for would send a reader to a transaction id
    that 404s, so it stays a record and drops out of what is rendered.
    """
    _write(tmp_path, monkeypatch, [_row("0xgone")])
    monkeypatch.setattr(publish_snapshot, "_node_answers", lambda tx, rpc: True)
    publish_snapshot.build()

    _write(tmp_path, monkeypatch, [_row("0xfresh")])
    monkeypatch.setattr(
        publish_snapshot, "_node_answers", lambda tx, rpc: tx == "0xfresh"
    )
    payload = publish_snapshot.build()

    assert payload["settled_count"] == 2
    assert [r["tx_hash"] for r in payload["results"]] == ["0xfresh"]


def test_the_same_settlement_recorded_twice_is_counted_once(tmp_path, monkeypatch):
    """Evidence is append-only and repeats, so counting rows would inflate it."""
    _write(tmp_path, monkeypatch, [_row("0xsame"), _row("0xsame"), _row("0xsame")])
    monkeypatch.setattr(publish_snapshot, "_node_answers", lambda tx, rpc: True)

    payload = publish_snapshot.build()

    assert payload["settled_count"] == 1
