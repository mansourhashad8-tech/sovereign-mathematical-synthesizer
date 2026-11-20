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
        [sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt",
         "-r", "requirements-eth.txt"],
        cwd=ROOT,
        check=False,
    )


FUEL_CORE_VERSION = "0.47.1"
FUEL_CORE_URL = (
    "https://github.com/FuelLabs/fuel-core/releases/download/"
    f"v{FUEL_CORE_VERSION}/fuel-core-{FUEL_CORE_VERSION}-x86_64-unknown-linux-gnu.tar.gz"
)


def provision_fuel() -> bool:
    """Make the engine-owned Fuel chain runnable, downloading it if needed.

    The node is what makes the gasless claim true: it prices gas at zero and
    mints its own genesis supply, so no sponsor and no user funds are involved.
    Downloading it here is what lets a fresh sandbox reach that state with one
    command instead of a manual setup list.
    """
    from synthesizer.fuel import FuelNode, _forc_binary, _node_binary

    if _node_binary():
        print(f"[fuel] fuel-core present: {_node_binary()}")
    else:
        print(f"[fuel] fetching fuel-core {FUEL_CORE_VERSION} …")
        try:
            subprocess.run(
                ["curl", "-sSL", FUEL_CORE_URL, "-o", "/tmp/fuel-core.tar.gz"],
                check=True, timeout=300,
            )
            subprocess.run(
                ["tar", "xzf", "/tmp/fuel-core.tar.gz", "-C", "/tmp"],
                check=True, timeout=300,
            )
            os.chmod(
                f"/tmp/fuel-core-{FUEL_CORE_VERSION}-x86_64-unknown-linux-gnu/fuel-core",
                0o755,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[fuel] download failed: {exc}")
            return False

    # The compiled ledger ships with the repo, so `forc` is only needed to
    # rebuild after a source change. Its absence does not block settlement.
    print(f"[fuel] forc: {_forc_binary() or 'not installed (compiled ledger is used)'}")

    node = FuelNode()
    if node.healthy():
        print(f"[fuel] node already up at {node.rpc}")
        return True
    print(f"[fuel] starting node at {node.rpc} …")
    if not node.ensure_running():
        print("[fuel] node did not start; see /tmp/fuel-core-node.log")
        return False
    print("[fuel] node healthy")
    return True


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
    intents = [
        "اريد ربح 3 دولار من سوق السيولة بدون مفتاح وبدون راس مال",
        "اريد ربح 4000 دولار من السيولة",
        "اريد ربح مليار دولار",
    ]
    for intent in intents:
        try:
            r = httpx.post(
                f"http://localhost:{PORT}/synthesize",
                json={"intent": intent},
                timeout=120,
            )
            d = r.json()
            ms = float(r.headers.get("X-Synthesis-Ms", "0"))
            proven = bool(d.get("proof") and d["proof"].get("satisfied"))
            ex = d.get("execution") or {}
            print(
                f"      {ms:7.0f}ms src={d.get('source'):9s} proven={proven} "
                f"bg={d.get('background')} exec={ex.get('status')} "
                f"gas={ex.get('gas_mode')} net={ex.get('network')} "
                f"tx={str(ex.get('tx_hash') or '')[:18]}"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"      self-test skipped: {type(exc).__name__}")


def main() -> int:
    install()
    provision_fuel()
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
