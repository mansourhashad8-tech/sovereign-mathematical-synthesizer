#!/usr/bin/env python3
"""Publish what the engine has proven into the static site.

GitHub Pages can serve files and nothing else: no Python, no node, no
``/synthesize``. So the permanent site cannot host the engine. What it can do is
carry the engine's output, which this script writes after a CI run has actually
settled on chain. The page then has real transaction ids to show even when no
backend is reachable.

Everything published here is read back from evidence, never assumed: a record
without an executed settlement is published as such.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "evidence" / "synthesis.jsonl"
DOCS = ROOT / "docs"


def _node_answers(tx_id: str, rpc: str) -> bool:
    """True only when the running node knows this transaction.

    Evidence outlives the chain that produced it: each run starts a fresh node,
    so a hash from an earlier run names a transaction that no longer exists.
    Publishing it would invite a reader to check a receipt that 404s, which is
    the same defect as trusting a cached contract id.
    """
    if not tx_id:
        return False
    try:
        import httpx

        with httpx.Client(timeout=5.0) as client:
            resp = client.post(
                rpc,
                json={
                    "query": "query T($id: String!) { transaction(id: $id) { id } }",
                    "variables": {"id": tx_id},
                },
            )
            if resp.status_code != 200:
                return False
            data = resp.json().get("data") or {}
            return bool((data.get("transaction") or {}).get("id"))
    except Exception:  # noqa: BLE001
        return False


def _read_evidence(limit: int = 400) -> list[dict]:
    if not EVIDENCE.exists():
        return []
    rows: list[dict] = []
    for line in EVIDENCE.read_text(encoding="utf-8").splitlines()[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _result(row: dict) -> dict:
    """Flatten one evidence row into what the page renders."""
    ex = row.get("execution") or {}
    proof = row.get("proof") or {}
    path = row.get("chosen_path") or {}
    return {
        "intent": row.get("raw_intent", ""),
        "intent_id": row.get("intent_id", ""),
        "proven": bool(proof.get("satisfied")),
        "certificate": proof.get("certificate", ""),
        "solver": proof.get("solver", ""),
        "path_id": path.get("path_id", ""),
        "steps": len(path.get("steps") or []),
        "net_profit_usd": path.get("net_profit_usd", 0),
        "primitives": (path.get("primitives") or [])[:8],
        "status": ex.get("status", ""),
        "network": ex.get("network", ""),
        "tx_hash": ex.get("tx_hash", ""),
        "block_number": ex.get("block_number", ""),
        "gas_paid_by": ex.get("gas_paid_by", ""),
        "gas_cost_wei": ex.get("gas_cost_wei", 0),
        "delivered": ex.get("delivered", 0),
        "value_transferred": bool(ex.get("value_transferred")),
        "explorer_url": ex.get("explorer_url", ""),
        "equations": (row.get("equations") or [])[:4],
        "narrative_ar": row.get("narrative_ar", ""),
        "settled_at": ex.get("settled_at") or path.get("created_at") or 0,
    }


def build() -> dict:
    rpc = os.environ.get("FUEL_RPC_URL", "http://127.0.0.1:4000/v1/graphql")
    rows = _read_evidence()
    results = [_result(r) for r in rows]
    candidates = [
        r for r in results
        if r["status"] == "executed" and r["tx_hash"] and r["network"] == "fuel-devnet"
    ]
    # Only keep what the live node will answer for. A run that starts a fresh
    # chain inherits old evidence; publishing those ids would hand the reader
    # receipts that 404.
    settled = [r for r in candidates if _node_answers(r["tx_hash"], rpc)]
    unverified = len(candidates) - len(settled)
    settled.sort(key=lambda r: r.get("settled_at") or 0, reverse=True)

    fuel = {}
    fuel_path = ROOT / "sway" / "chain" / "settlements.json"
    if fuel_path.exists():
        try:
            fuel = json.loads(fuel_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            fuel = {}

    return {
        "generated_at": time.time(),
        "generated_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "github-actions",
        # Said plainly so the page never implies a live process is answering.
        "live_probe": False,
        "settled_count": len(settled),
        "unverified_count": unverified,
        "total_results": len(results),
        "beneficiary": "0xe3d1c7dcd5e95b6441716b65f9c46b73e91b4d47",
        "network": "fuel-devnet",
        "fuel_ledger": fuel,
        "results": settled[:25],
    }


def write(payload: dict) -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / "state.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    health = {
        "status": "ok" if payload["settled_count"] else "no_settlements_yet",
        # A static host has no process to probe, so this reports the last CI run
        # rather than pretending to be a live liveness check.
        "live_probe": False,
        "checked_at": payload["generated_at_iso"],
        "engine": "sovereign-synthesizer",
        "version": "2.0.0",
        "network": payload["network"],
        "settled_count": payload["settled_count"],
        "beneficiary": payload["beneficiary"],
    }
    (DOCS / "health.json").write_text(
        json.dumps(health, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> int:
    payload = build()
    write(payload)
    print(
        f"published {payload['settled_count']} settled / "
        f"{payload['total_results']} total at {payload['generated_at_iso']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
