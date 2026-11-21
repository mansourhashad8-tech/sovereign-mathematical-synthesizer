"""The engine must never claim a gas source it has not obtained.

Every sponsor leg is refused on every chain the engine can reach (see the
treasury report), so a result that says "gas is funded by a paymaster" before a
leg has paid is a false statement about the market. These tests pin the
honesty of the wording: a pending settlement names no source, and a settled one
names the source that actually paid.
"""
from __future__ import annotations

from synthesizer.loop import Synthesizer
from synthesizer.models import ExecutionResult


def test_pending_execution_does_not_claim_a_paymaster():
    """A dispatched-but-unpaid settlement names no gas source."""
    line = Synthesizer._settlement_line(
        ExecutionResult(status="pending", gas_mode="pending")
    )
    assert "paymaster" not in line
    assert "لم يُحسم" in line


def test_settled_execution_names_the_source_that_paid():
    """A settled settlement reports the leg that actually paid."""
    line = Synthesizer._settlement_line(
        ExecutionResult(
            status="submitted", gas_mode="self_deposit", user_op_hash="0xabc"
        )
    )
    assert "self_deposit" in line
    assert "paymaster" not in line
