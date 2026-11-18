"""HTTP surface: Arabic chat in, proven synthesis out.

Endpoints
    GET  /                 -> the Arabic chat UI
    GET  /health           -> liveness
    GET  /status           -> engine + chain state
    POST /synthesize       -> {"intent": "..."} -> IntentResult
    GET  /primitives       -> the current live primitive set
    GET  /proofs/{id}      -> the proof certificate for an intent
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import __version__
from .config import SETTINGS
from .executor import BASE_RPC
from .loop import Synthesizer

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="Sovereign Mathematical Synthesizer", version=__version__)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

synth = Synthesizer()


class IntentBody(BaseModel):
    intent: str


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__, "engine": "sovereign-synthesizer"}


@app.get("/status")
async def status() -> dict:
    primitives = synth.scanner.scan()
    live = synth.scanner.fresh_count()
    return {
        "status": "ok",
        "version": __version__,
        "beneficiary": SETTINGS.beneficiary,
        "prover": synth.prover.solver_name,
        "primitives_total": len(primitives),
        "primitives_live": live,
        "lookback_hours": SETTINGS.github_lookback_hours,
        "loop_budget_seconds": SETTINGS.loop_budget_seconds,
        "always_on": SETTINGS.always_on,
        "treasury": await _treasury_state(),
    }


@app.get("/primitives")
def primitives() -> dict:
    return {
        "primitives": [
            {
                "name": p.name,
                "repo": p.repo,
                "category": p.category,
                "source": p.properties.get("source"),
                "freshness_hours": p.freshness_hours,
                "url": p.url,
            }
            for p in synth.scanner.scan()
        ]
    }


@app.post("/synthesize")
async def synthesize(body: IntentBody) -> JSONResponse:
    result = await synth.synthesize(body.intent)
    return JSONResponse(result.to_dict())


@app.get("/proofs/{iid}")
async def proof(iid: str) -> JSONResponse:
    result = synth.latest(iid)
    if result is None:
        return JSONResponse({"error": "unknown_intent", "intent_id": iid}, status_code=404)
    return JSONResponse(
        {
            "intent_id": iid,
            "proof": result.proof.__dict__ if result.proof else None,
            "equations": result.equations,
            "spec": synth.prover.spec(result.chosen_path, result.constraints)
            if result.chosen_path
            else None,
        }
    )


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


async def _treasury_state() -> dict:
    """Read the beneficiary's real balances from public RPCs (keyless)."""
    out: dict = {"address": SETTINGS.beneficiary, "network": "base-mainnet"}
    try:
        # eth_getBalance on Base
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_getBalance",
            "params": [SETTINGS.beneficiary, "latest"],
        }
        r = await asyncio.to_thread(
            httpx.post, BASE_RPC, json=payload, timeout=8
        )
        if r.status_code == 200:
            out["eth_wei"] = int(r.json()["result"], 16)
            out["eth"] = out["eth_wei"] / 1e18
    except Exception as exc:  # noqa: BLE001
        out["error"] = type(exc).__name__
    return out
