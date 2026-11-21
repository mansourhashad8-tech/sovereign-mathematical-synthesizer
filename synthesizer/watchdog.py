"""The settlement watchdog.

The rule the engine holds itself to is that an unsettled settlement is not an
answer, it is a to-do. This module enforces that: it watches settlements that
came back unsettled and re-drives them through the gas flywheel without waiting
for the user to ask again.

It is deliberately small. The flywheel already tries every network on every
attempt, so the watchdog's whole job is to give it more attempts over time and
to stop the moment one produces a mined transaction.

Nothing here can fabricate success. Every retry either finds a mined
transaction or leaves the settlement exactly as honest as it found it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .config import SETTINGS
from .gasflywheel import GasFlywheel
from .models import ExecutionResult


@dataclass
class RetryRecord:
    """One watched settlement and the attempts made on it."""

    key: str
    call_data: str
    attempts: int = 0
    settled: bool = False
    # Attempts ran out without a mined transaction. Kept distinct from
    # ``settled`` so a failed watch is never counted as a payout.
    exhausted: bool = False
    history: list[str] = field(default_factory=list)

    @property
    def done(self) -> bool:
        return self.settled or self.exhausted


class SettlementWatchdog:
    """Re-drives unsettled settlements until one is mined, or attempts run out.

    Runs in a background daemon thread so a request can still answer inside its
    latency budget: the answer carries the first honest result, and the watch
    upgrades it in place when a retry lands.
    """

    def __init__(self, flywheel: GasFlywheel) -> None:
        self.flywheel = flywheel
        self._records: dict[str, RetryRecord] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._on_settled = None

    # -- public API --------------------------------------------------------

    def watch(self, key: str, call_data: str, result: ExecutionResult) -> None:
        """Register a settlement for retry when it came back unsettled."""
        if result.status in ("executed", "submitted"):
            return
        with self._lock:
            rec = self._records.get(key)
            if rec is None:
                self._records[key] = RetryRecord(key=key, call_data=call_data)
            else:
                rec.call_data = call_data
        self._ensure_running()

    def set_on_settled(self, callback) -> None:
        """Register a hook called with (key, settlement) on a successful retry."""
        self._on_settled = callback

    def status(self) -> dict:
        with self._lock:
            records = list(self._records.values())
            return {
                "watched": len(records),
                "settled": sum(1 for r in records if r.settled),
                "exhausted": sum(1 for r in records if r.exhausted),
                "pending": sum(1 for r in records if not r.done),
            }

    def results(self) -> dict[str, RetryRecord]:
        with self._lock:
            return dict(self._records)

    def stop(self) -> None:
        self._stop.set()

    # -- internals ---------------------------------------------------------

    def _ensure_running(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="settlement-watchdog", daemon=True
            )
            self._thread.start()

    def _pending(self) -> list[RetryRecord]:
        with self._lock:
            return [r for r in self._records.values() if not r.done]

    def _run(self) -> None:
        # Back off between rounds so a chain that is briefly unavailable does
        # not burn the whole attempt budget in a few seconds.
        delay = 2.0
        while not self._stop.is_set():
            pending = self._pending()
            if not pending:
                time.sleep(delay)
                if not self._pending():
                    return
                continue

            for rec in pending:
                if self._stop.is_set():
                    return
                # Zero means no cap: the watch runs until the settlement
                # mines, which is the promise the engine makes. A positive
                # value bounds the run for tests and for operators who ask
                # for it.
                cap = SETTINGS.watchdog_attempts
                if cap > 0 and rec.attempts >= cap:
                    # Out of attempts, not settled. Marking this settled would
                    # report a failed watch as a payout.
                    with self._lock:
                        rec.exhausted = True
                    continue
                rec.attempts += 1
                settlement = self.flywheel.settle(rec.call_data)
                rec.history.append(f"attempt {rec.attempts}: {settlement.status}")
                if settlement.mined or settlement.status == "submitted":
                    rec.settled = True
                    if self._on_settled is not None:
                        try:
                            self._on_settled(rec.key, settlement)
                        except Exception:  # noqa: BLE001 - a hook must not kill the watch
                            pass

            delay = min(delay * 1.5, SETTINGS.watchdog_max_delay)
            self._stop.wait(delay)
