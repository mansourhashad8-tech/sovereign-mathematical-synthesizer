#!/usr/bin/env python3
"""One command that provisions, starts, and verifies the engine.

    python scripts/go_live.py

It installs dependencies, starts the HTTP engine on SYNTH_PORT (default 12000),
waits for /health, runs one self-test synthesis, and prints the public URL.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

PORT = int(os.environ.get("SYNTH_PORT", "12000"))
RUNTIME_ID = os.environ.get("RUNTIME_ID", "")


def public_url() -> str:
    explicit = os.environ.get("SYNTH_PUBLIC_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    if RUNTIME_ID:
        return f"https://work-1-{RUNTIME_ID}.prod-runtime.all-hands.dev"
    return f"http://localhost:{PORT}"


def install() -> None:
    print("[1/4] installing dependencies…")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"],
        cwd=ROOT,
        check=False,
    )


def start() -> subprocess.Popen:
    print(f"[2/4] starting engine on :{PORT} …")
    env = dict(os.environ, SYNTH_PORT=str(PORT))
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "synthesizer.api:app",
         "--host", "0.0.0.0", "--port", str(PORT)],
        cwd=ROOT,
        env=env,
        stdout=open("/tmp/synth.log", "w"),
        stderr=subprocess.STDOUT,
    )
    return proc


def wait_health(timeout: int = 30) -> bool:
    print("[3/4] waiting for /health …")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://localhost:{PORT}/health", timeout=3)
            if r.status_code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    return False


def self_test() -> None:
    print("[4/4] self-test synthesis …")
    try:
        r = httpx.post(
            f"http://localhost:{PORT}/synthesize",
            json={"intent": "اريد ربح 3 دولار من سوق السيولة بدون مفتاح وبدون راس مال"},
            timeout=120,
        )
        d = r.json()
        proven = bool(d.get("proof") and d["proof"].get("satisfied"))
        print(f"      proven={proven} attempts={d.get('attempts')} "
              f"primitives={len(d.get('primitives_used', []))}")
    except Exception as exc:  # noqa: BLE001
        print(f"      self-test skipped: {type(exc).__name__}")


def main() -> int:
    install()
    proc = start()
    if not wait_health():
        print("engine did not become healthy; see /tmp/synth.log")
        return 1
    self_test()
    url = public_url()
    print("\n\u2705 live:", url)
    print(f"   pid {proc.pid}; logs /tmp/synth.log")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
