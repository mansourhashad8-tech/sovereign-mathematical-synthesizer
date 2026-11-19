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
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import __version__
from .config import SETTINGS
from .loop import Synthesizer
from .templates import TEMPLATES

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

synth = Synthesizer()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Resolve chain identity, gas price, and the primitive cache at boot.

    This is process startup, not a request: by the time the first intent
    arrives, every network read is already cached and the answer is pure
    in-memory work.
    """
    await asyncio.to_thread(synth.executor.prewarm)
    await asyncio.to_thread(synth.scanner.scan)
    yield


app = FastAPI(
    title="Sovereign Mathematical Synthesizer",
    version=__version__,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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
        "scan_rate_limited": synth.scanner.rate_limited(),
        "lookback_hours": SETTINGS.github_lookback_hours,
        "loop_budget_seconds": SETTINGS.loop_budget_seconds,
        "always_on": SETTINGS.always_on,
        "treasury": await _treasury_state(),
    }


@app.post("/primitives/refresh")
def refresh_primitives() -> dict:
    """Force a live rescan. Used after a rate-limit window clears."""
    primitives = synth.scanner.scan(force=True)
    return {
        "primitives_total": len(primitives),
        "primitives_live": synth.scanner.fresh_count(),
        "scan_rate_limited": synth.scanner.rate_limited(),
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
    payload = result.to_dict()
    headers = {
        # The decision time is on the wire so "instant" is auditable, not claimed.
        "X-Synthesis-Ms": f"{result.elapsed_seconds * 1000:.1f}",
        "X-Synthesis-Source": result.source,
        "X-Synthesis-Background": "false",
    }
    if result.execution is not None:
        headers["X-Execution-Status"] = result.execution.status
        headers["X-Gas-Mode"] = result.execution.gas_mode
    return JSONResponse(payload, headers=headers)


@app.get("/templates")
def templates() -> dict:
    """The pre-computed composition library that makes execution instant."""
    return {
        "templates": [
            {
                "name": t.name,
                "objective": t.objective,
                "description": t.description,
                "primitives": t.primitives,
                "gas_mode": t.gas_mode,
                "net_rate": round(t.net_rate, 8),
            }
            for t in TEMPLATES
        ]
    }


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
    """Read the beneficiary's real balance from a public RPC (keyless)."""
    out: dict = {"address": SETTINGS.beneficiary, "network": SETTINGS.settlement_chain}
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_getBalance",
            "params": [SETTINGS.beneficiary, "latest"],
        }
        r = await asyncio.to_thread(
            httpx.post, SETTINGS.rpc_url, json=payload, timeout=8
        )
        if r.status_code == 200:
            out["eth_wei"] = int(r.json()["result"], 16)
            out["eth"] = out["eth_wei"] / 1e18
    except Exception as exc:  # noqa: BLE001
        out["error"] = type(exc).__name__
    return out
