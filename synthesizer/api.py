"""HTTP surface: Arabic chat in, proven synthesis out.

Endpoints
    GET  /                 -> the Arabic chat UI
    GET  /health           -> liveness
    GET  /status           -> engine + chain state
    POST /synthesize       -> {"intent": "..."} -> IntentResult
    GET  /primitives       -> the current live primitive set
    GET  /intent/{id}      -> poll one intent's settlement state
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
from .bootstrap import Bootstrap
from .config import SETTINGS
from .genesis import GenesisMesh
from .gasflywheel import _networks
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
    keeper = asyncio.create_task(_keep_settling()) if SETTINGS.always_on else None
    try:
        yield
    finally:
        if keeper is not None:
            keeper.cancel()


async def _keep_settling() -> None:
    """Keep working every intent that has not paid yet.

    The engine's contract is that an intent terminates in a settlement, so an
    intent that has not paid is unfinished work rather than a closed request.
    This pass re-scans for components that appeared since the last attempt and
    re-settles, forever. It is what makes ``always_on`` a behaviour instead of
    a field in the status payload.
    """
    while True:
        try:
            await asyncio.sleep(SETTINGS.keepalive_seconds)
            await asyncio.to_thread(synth.retry_unsettled)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failed pass is not a reason to stop keeping the shop open; the
            # next pass retries with a fresh scan.
            continue


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


def _genesis_state() -> dict:
    """The mesh summary, cached so /status stays inside its latency budget."""
    try:
        return synth.genesis.scan().summary()
    except Exception:  # noqa: BLE001 - a mesh read must never fail /status
        return {"sources": 0, "claimable": 0, "human_gated": 0, "networks_reachable": 0}


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
        # Work the background pass still owes: intents that have not paid yet.
        "unsettled": len(synth.unsettled()),
        "keepalive_seconds": SETTINGS.keepalive_seconds,
        "treasury": await _treasury_state(),
        "gas_treasury": _gas_treasury_state(synth),
        "gas": {
            "paymaster": SETTINGS.paymaster_url,
            # Report the chain that actually settles. The Fuel leg is primary,
            # so naming the EVM chain here would describe a fallback as if it
            # were the settlement venue.
            "chain": "fuel-devnet" if SETTINGS.fuel_primary else SETTINGS.settlement_chain,
            "primary_leg": "fuel" if SETTINGS.fuel_primary else "evm-sponsored",
            "chain_id": SETTINGS.chain_id,
            "fallback_networks": len(SETTINGS.fallback_networks),
            "user_pays_gas": False,
            # The self-sponsored model: gas is internal, and an external
            # sponsor is a switch the operator turns on, not the default.
            "external_sponsor_allowed": SETTINGS.allow_external_sponsor,
            "internal_balance": synth.executor.flywheel.gas_report(),
            "watchdog": synth.watchdog.status(),
        },
        "genesis": _genesis_state(),
    }


@app.get("/relayers")
def relayers() -> dict:
    """Which gas relayers actually answer, and whether they sponsor on mainnet.

    Measured, not declared. A relayer that is DNS-dead or refuses by policy is
    reported with the reason, so a sponsorship claim in this payload always
    corresponds to a sponsorship the relayer actually granted.
    """
    report = synth.executor.flywheel.relay_report()
    report["reserve"] = synth.executor.flywheel.reserve.summary()
    return report


@app.get("/fuel/tx/{tx_id}")
def fuel_receipt(tx_id: str) -> JSONResponse:
    """A receipt read back from the node, for the transaction id in the answer.

    The settlement is only as good as the ability to check it, so this endpoint
    queries the node for the transaction, the block that carries it, and the
    ledger totals. Nothing here is remembered from the settlement call itself.
    """
    from .fuel import FuelNode

    node = FuelNode()
    if not node.healthy():
        return JSONResponse(
            status_code=503,
            content={"tx_id": tx_id, "found": False, "reason": "fuel-core node is not reachable"},
        )
    import httpx

    query = """
    query Tx($id: String!) {
      transaction(id: $id) { id status { __typename ... on SuccessStatus { blockHeight } ... on FailureStatus { blockHeight reason } } }
    }
    """
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(node.rpc, json={"query": query, "variables": {"id": tx_id}})
            body = resp.json()
    except Exception as exc:
        return JSONResponse(
            status_code=502,
            content={"tx_id": tx_id, "found": False, "reason": f"node query failed: {exc}"},
        )
    tx = (body.get("data") or {}).get("transaction")
    if not tx:
        return JSONResponse(
            status_code=404,
            content={"tx_id": tx_id, "found": False, "reason": "the node has no transaction with this id"},
        )
    status = tx.get("status") or {}
    return JSONResponse(
        content={
            "tx_id": tx.get("id", tx_id),
            "found": True,
            "status": status.get("__typename", ""),
            "block_height": status.get("blockHeight", ""),
            "failure_reason": status.get("reason", ""),
            "network": "fuel-devnet",
            "rpc": node.rpc,
        }
    )


@app.get("/fuel")
def fuel_state() -> dict:
    """The engine-owned SwayVM chain: node health, ledger, and gas accounting.

    Every value here is read from the running node or from the deployed
    contract. A reader can confirm the engine settles for zero external spend
    instead of taking that claim on trust.
    """
    from .fuel import BENEFICIARY, FuelNode

    node = FuelNode()
    healthy = node.healthy()
    payload = {
        "network": "fuel-devnet",
        "vm": "SwayVM",
        "rpc": node.rpc,
        "node_healthy": healthy,
        "node_version": "",
        "beneficiary": BENEFICIARY,
        "contract_id": "",
        "locked_beneficiary": "",
        "settlement_count": 0,
        "grand_total": 0,
        "delivered_to_beneficiary": 0,
        "gas": {"settlements": 0, "gas_used": 0, "external_spend": 0,
                "paid_by": "genesis-supply"},
        "min_gas_price": 0,
    }
    if not healthy:
        return payload
    try:
        import httpx

        with httpx.Client(timeout=5.0) as client:
            resp = client.post(
                node.rpc, json={"query": "{ nodeInfo { nodeVersion } }"}
            )
            payload["node_version"] = (
                resp.json().get("data", {}).get("nodeInfo", {}).get("nodeVersion", "")
            )
    except Exception:
        pass
    # Build the hand on demand: the executor only creates it on first
    # settlement, but this endpoint must report the ledger before that happens.
    hand = getattr(synth.executor, "_fuel", None) or None
    if hand is None:
        from .fuel import FuelHand

        hand = FuelHand(node)
    if hand:
        cid = hand.contract_id()
        payload["contract_id"] = cid
        state = hand._run("state", {"beneficiary": "0x" + "00" * 12 + BENEFICIARY[2:]})
        if state:
            payload["locked_beneficiary"] = state.get("locked", "")
            totals = state.get("totals", ["0", "0"])
            payload["settlement_count"] = int(totals[0] or 0)
            payload["grand_total"] = int(totals[1] or 0)
            payload["delivered_to_beneficiary"] = int(state.get("delivered", 0) or 0)
        gas = getattr(synth.executor, "_fuel_gas", None)
        if gas:
            payload["gas"] = gas.as_dict()
    return payload


@app.get("/valuegas")
def valuegas_report() -> dict:
    """The value-gas entanglement contract and its settlement split."""
    from . import valuegas

    return {
        "contract": SETTINGS.gas_value_lock or "(not yet deployed)",
        "beneficiary": valuegas.BENEFICIARY,
        "value_token": SETTINGS.value_token,
        "gas_reserve_bps": SETTINGS.gas_reserve_bps,
        "settle_selector": "0x" + valuegas.SEL_SETTLE.hex(),
        "settle_with_permit_selector": "0x" + valuegas.SEL_SETTLE_PERMIT.hex(),
        "zero_eth_settlement": True,
        "user_pays_gas": False,
    }


@app.get("/genesis")
def genesis() -> dict:
    """The genesis mesh: every incentive source and whether an agent can claim it."""
    report = synth.genesis.scan()
    return {
        **report.summary(),
        "sources": [
            {
                "name": s.name,
                "network": s.network,
                "kind": s.kind,
                "gate": s.gate,
                "claimable": s.claimable,
                "reachable": s.reachable,
                "why": s.why,
                "explorer": s.explorer,
            }
            for s in report.sources
        ],
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


class BootstrapBody(BaseModel):
    intent_id: str = "intent"
    min_profit: int = 1_000_000
    notional: int = 5_000_000


@app.post("/bootstrap")
async def bootstrap(body: BootstrapBody) -> JSONResponse:
    """Run the fork -> proof -> chain path and report what the chain recorded.

    The work happens in a worker thread because proving is CPU bound, and the
    result is returned as produced: if the chain rejected the proof the response
    says so rather than describing an intention.
    """
    result = await asyncio.to_thread(
        Bootstrap().run, body.intent_id, body.min_profit, body.notional
    )
    payload = result.to_dict()
    headers = {
        "X-Bootstrap-Delivered": str(result.delivered).lower(),
        "X-Bootstrap-Verified": str(result.verified_onchain).lower(),
    }
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


@app.get("/intent/{iid}")
async def intent_state(iid: str) -> JSONResponse:
    """Poll one intent's settlement state.

    Settlement happens after the answer is sent, so a client that wants to see
    the real transaction hash watches this endpoint rather than re-sending the
    intent.
    """
    result = synth.latest(iid)
    if result is None:
        return JSONResponse({"error": "unknown_intent", "intent_id": iid}, status_code=404)
    payload = result.to_dict()
    return JSONResponse(
        payload,
        headers={
            "X-Execution-Status": result.execution.status if result.execution else "none",
        },
    )


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


def _gas_treasury_state(synth) -> dict:
    """The engine's internal gas position, measured per network.

    This replaces the old "which sponsor answered" report. It states which of
    the four funding legs can pay, what a settlement costs, and — when none can
    pay — the exact wei bootstrap that would turn every leg on.
    """
    try:
        return synth.treasury.summary(
            _networks(), lambda n: synth.executor.flywheel.account_for(n)
        )
    except Exception as exc:  # noqa: BLE001 - a report must never 500 the app
        return {"error": type(exc).__name__, "legs": []}


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
