"""Overlapping settle runs must not lose each other's settlements.

The job commits to the branch it publishes from, so a push can be rejected while
another run is in flight. Picking a side of that conflict would quietly drop
settlements, so the merge is by transaction id and keeps both.
"""
from __future__ import annotations

import json

from scripts import merge_ledgers


def _ledger(path, rows):
    path.write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )


def _rows(path):
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_both_sides_survive_the_merge(tmp_path):
    local, other = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    _ledger(local, [{"tx_hash": "0xmine"}])
    _ledger(other, [{"tx_hash": "0xtheirs"}])

    merge_ledgers.main(["merge", str(local), str(other)])

    assert {r["tx_hash"] for r in _rows(local)} == {"0xmine", "0xtheirs"}


def test_the_newer_copy_of_a_shared_settlement_wins(tmp_path):
    local, other = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    _ledger(local, [{"tx_hash": "0xsame", "verified_now": True}])
    _ledger(other, [{"tx_hash": "0xsame", "verified_now": False}])

    merge_ledgers.main(["merge", str(local), str(other)])

    assert _rows(local) == [{"tx_hash": "0xsame", "verified_now": True}]


def test_a_missing_other_ledger_is_not_an_error(tmp_path):
    local = tmp_path / "a.jsonl"
    _ledger(local, [{"tx_hash": "0xmine"}])

    assert merge_ledgers.main(["merge", str(local), str(tmp_path / "nope")]) == 0
    assert len(_rows(local)) == 1
