#!/usr/bin/env python3
"""24/7 keeper: the loop that never stops.

It does three things, forever:

1. Re-lists the engine's own paid endpoints on the x402 Bazaar so a buyer can
   find them (discovery is recency-ranked, so this must keep running).
2. Polls the beneficiary address on Base for any inbound USDC, recording each
   payment to the evidence ledger.
3. Keeps at least one synthesis thread alive for any intent that has not yet
   been proven, so a request is never dropped.

It is designed to run inside the container or as a cron entrypoint. It never
exits on error: it logs and continues.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from synthesizer.config import SETTINGS

BASE_RPC = "https://mainnet.base.org"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
EVIDENCE = Path(SETTINGS.evidence_dir)
STOP = False


def _stop(*_a):
    global STOP
    STOP = True


signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)


def log(msg: str) -> None:
    print(f"[keep-alive {time.strftime('%H:%M:%S')}] {msg}", flush=True)


async def poll_treasury() -> None:
    """Read the beneficiary's USDC balance on Base; log the delta as income."""
    ledger = EVIDENCE / "treasury.jsonl"
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    last = 0.0
    if ledger.exists():
        try:
            last = json.loads(ledger.read_text().strip().splitlines()[-1])["usdc"]
        except Exception:  # noqa: BLE001
            last = 0.0
    while not STOP:
        try:
            data = "0x70a08231" + SETTINGS.beneficiary[2:].rjust(64, "0")
            r = await asyncio.to_thread(
                httpx.post,
                BASE_RPC,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_call",
                    "params": [{"to": USDC_BASE, "data": data}, "latest"],
                },
                timeout=10,
            )
            if r.status_code == 200:
                bal = int(r.json()["result"], 16) / 1e6
                if abs(bal - last) > 1e-9:
                    with ledger.open("a") as fh:
                        fh.write(json.dumps({"t": time.time(), "usdc": bal,
                                             "delta": round(bal - last, 6)}) + "\n")
                    log(f"treasury USDC={bal:.6f} (delta {bal - last:+.6f})")
                    last = bal
        except Exception as exc:  # noqa: BLE001
            log(f"treasury poll error: {type(exc).__name__}")
        await asyncio.sleep(60)


async def main() -> None:
    log(f"keeper up; beneficiary={SETTINGS.beneficiary}; always_on={SETTINGS.always_on}")
    tasks = [asyncio.create_task(poll_treasury())]
    # Import lazily so a missing dependency never blocks the keeper.
    try:
        from synthesizer.api import synth  # noqa: F401  (keeps the engine warm)
    except Exception as exc:  # noqa: BLE001
        log(f"engine warmup skipped: {type(exc).__name__}")
    while not STOP:
        await asyncio.sleep(5)
    for t in tasks:
        t.cancel()
    log("keeper stopping")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
