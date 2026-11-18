#!/usr/bin/env python3
"""Batch entrypoint for the scheduled 24/7 loop.

Reads intents from config/seed_intents.txt (one per line) or from the
SYNTH_INTENT environment variable, synthesizes each, and prints a compact
report. Always exits 0: the loop's job is to keep trying, not to fail a build.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from synthesizer.loop import Synthesizer  # noqa: E402

DEFAULT_INTENTS = [
    "اريد ربح 100 دولار من سوق السيولة بدون مفتاح وبدون راس مال",
    "اريد ربح 1000 دولار من سوق الماركت ميكينج بدون مفتاح",
    "اريد ربح 50 دولار من فرص المراجحة الذرية بدون راس مال",
]


def _intents() -> list[str]:
    env = os.environ.get("SYNTH_INTENT", "").strip()
    if env:
        return [env]
    path = Path("config/seed_intents.txt")
    if path.exists():
        lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
        return [ln for ln in lines if ln and not ln.startswith("#")]
    return DEFAULT_INTENTS


def main() -> int:
    synth = Synthesizer()
    for intent in _intents():
        result = synth.synthesize_sync(intent)
        proven = bool(result.proof and result.proof.satisfied)
        path_id = result.chosen_path.path_id if result.chosen_path else "-"
        net = result.chosen_path.net_profit_usd if result.chosen_path else 0
        tag = "PROVEN" if proven else "SEARCHING"
        print(
            f"[{tag}] {intent}\n"
            f"  path={path_id} net={net} "
            f"attempts={result.attempts} primitives={len(result.primitives_used)}"
        )
        if result.execution:
            print(f"  exec={result.execution.status} tx={result.execution.tx_hash[:20]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
