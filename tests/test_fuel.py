"""The Fuel leg must only call a settlement paid when value actually arrived.

The engine owns this chain, so there is no sponsor to blame and no network
excuse. That raises the bar rather than lowering it: every claim the leg makes
about a payment has to be readable back off the chain, and a leg that cannot
reach the node must say so instead of reporting a payment it never made.

The tests below run against the real node when one is reachable and fall back to
the pure state-machine checks otherwise, so they are honest in both environments.
"""
from __future__ import annotations

import os

import pytest

from synthesizer.zkproof import toolchain_ready
from synthesizer.fuel import (
    BENEFICIARY,
    BENEFICIARY_B256,
    FuelHand,
    FuelNode,
    FuelSettlement,
    GasAbstraction,
)


def _live() -> bool:
    return FuelNode().healthy()


needs_node = pytest.mark.skipif(
    not _live(), reason="fuel-core node is not running in this environment"
)

# A settlement is rejected only after its proof is generated, so these checks
# need snarkjs as well as a node. Without it the leg answers "unavailable", which
# is a different and honest outcome: skip rather than read it as a wrong answer.
needs_prover = pytest.mark.skipif(
    not toolchain_ready(), reason="proving toolchain not built; run scripts/build_zk.sh"
)


# -- pure logic ------------------------------------------------------------


def test_beneficiary_is_left_padded_to_b256():
    """The Sway contract compares a b256, so the address must be padded, not hashed."""
    assert BENEFICIARY_B256 == "0x" + "00" * 12 + BENEFICIARY[2:]
    assert len(BENEFICIARY_B256) == 66


def test_paid_requires_an_included_transaction_and_arrived_value():
    """A mined transaction that moved nothing is not a payment."""
    settled = FuelSettlement(
        status="executed", included=True, delta=1_000_000, delivered=1_000_000
    )
    assert settled.paid

    not_included = FuelSettlement(
        status="executed", included=False, delta=1_000_000, delivered=1_000_000
    )
    assert not not_included.paid

    short = FuelSettlement(
        status="executed", included=True, delta=1_000_000, delivered=999_999
    )
    assert not short.paid

    zero = FuelSettlement(status="executed", included=True, delta=0, delivered=0)
    assert not zero.paid


def test_unavailable_leg_never_reports_a_payment():
    settlement = FuelSettlement(status="unavailable", reason="no node")
    assert not settlement.paid
    assert settlement.as_dict()["paid"] is False


def test_gas_abstraction_counts_only_settled_work():
    """Gas is only booked for settlements that actually executed."""
    gas = GasAbstraction()
    gas.record(FuelSettlement(status="pending", gas_used=100))
    gas.record(FuelSettlement(status="rejected", gas_used=100))
    assert gas.settlements == 0 and gas.gas_used == 0

    gas.record(FuelSettlement(status="executed", included=True, gas_used=86_092))
    assert gas.settlements == 1
    assert gas.gas_used == 86_092
    # The whole design rests on this staying zero.
    assert gas.external_spend == 0


# -- against the live node -------------------------------------------------


@needs_node
@needs_prover
def test_negative_delta_is_rejected_before_it_reaches_the_chain():
    hand = FuelHand()
    settlement = hand.settle(0, 0, "0x" + "ab" * 32)
    assert settlement.status == "rejected"
    assert not settlement.paid


@needs_node
@needs_prover
def test_net_profit_cannot_exceed_the_delta():
    hand = FuelHand()
    settlement = hand.settle(1_000, 2_000, "0x" + "cd" * 32)
    assert settlement.status == "rejected"
    assert "cover" in settlement.reason


@needs_node
def test_settlement_lands_in_a_block_and_the_ledger_grows_by_the_delta():
    hand = FuelHand()
    if not hand.deploy():
        pytest.skip("ledger could not be deployed")

    delta = 3_210_000
    commitment = "0x" + os.urandom(32).hex()
    settlement = hand.settle(delta, delta - 1, commitment)

    assert settlement.status == "executed", settlement.reason
    assert settlement.included
    assert settlement.block_height
    assert settlement.tx_id.startswith("0x")
    # The gain is measured from storage, so it must equal the delta exactly.
    assert settlement.detail["delivered_gain"] == delta
    assert settlement.paid
    assert settlement.gas_used > 0
    assert settlement.gas_paid_by == "genesis-supply"


@needs_node
def test_the_same_commitment_cannot_settle_twice():
    hand = FuelHand()
    if not hand.deploy():
        pytest.skip("ledger could not be deployed")

    commitment = "0x" + os.urandom(32).hex()
    first = hand.settle(1_500_000, 1_000_000, commitment)
    assert first.status == "executed"

    second = hand.settle(1_500_000, 1_000_000, commitment)
    assert second.status == "rejected"
    assert "already" in second.reason


@needs_node
def test_a_replay_reports_the_original_transaction():
    """Asking again for a paid settlement must answer with the tx that paid it."""
    hand = FuelHand()
    if not hand.deploy():
        pytest.skip("ledger could not be deployed")

    commitment = "0x" + os.urandom(32).hex()
    first = hand.settle(1_100_000, 900_000, commitment)
    assert first.status == "executed"

    previous = hand.journal_lookup(commitment)
    assert previous is not None
    replayed = hand.replay_result(commitment, previous)
    assert replayed.tx_id == first.tx_id
    assert replayed.paid
    assert replayed.detail["replayed"] is True


@needs_node
@needs_prover
def test_a_contract_id_from_another_chain_is_not_trusted():
    """A cached id must be confirmed by the chain before it is used.

    This is the deployed-container failure: the image carried a contract id
    written on a different machine, so every settlement was assembled against a
    contract the node had never seen and failed with InputContractDoesNotExist.
    An id the chain does not answer for has to be treated as absent.
    """
    hand = FuelHand()
    if not hand.node.ensure_running():
        pytest.skip("node unavailable")

    hand._contract_id = "0x" + "ab" * 32
    assert hand.verified_contract_id() == ""

    deployed = hand.deploy()
    assert deployed and deployed != "0x" + "ab" * 32
    assert hand.verified_contract_id() == deployed
