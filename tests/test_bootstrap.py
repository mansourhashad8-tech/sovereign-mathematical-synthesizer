"""Tests for the fork -> proof -> chain path.

The properties under test are the ones that make the settlement claim mean
something: the delta is measured rather than asserted, the proof binds it, the
verifier accepts an honest proof, and the contract refuses every forgery that a
caller could otherwise record as a settlement.
"""

from __future__ import annotations

import pytest

from synthesizer import zkproof
from synthesizer.bootstrap import Bootstrap
from synthesizer.gateway import (
    ForkGateway,
    _bytes32,
    _selector,
    _word,
    intent_id_bytes,
)
from synthesizer.localfork import LocalFork, session_key
from synthesizer.zkproof import solidity_calldata

BENEFICIARY = "0xe3d1c7dcd5e95b6441716b65f9c46b73e91b4d47"


@pytest.fixture(scope="module")
def proof():
    return zkproof.prove_settlement(
        beneficiary=BENEFICIARY,
        delta=5_000_000,
        net_profit=5_000_000,
        min_profit=1_000_000,
        step_count=3,
        total_gas=21_000,
    )


@pytest.fixture()
def chain():
    from eth_account import Account

    key = session_key()
    fork = LocalFork(beneficiary=BENEFICIARY, fund=[Account.from_key(key).address])
    gateway = ForkGateway(fork, key)
    gateway.deploy()
    return fork, gateway, key


def test_proof_verifies_locally(proof):
    assert proof.verified_locally
    assert proof.public_signals[0] == "1"


def test_public_signals_carry_the_proven_figures(proof):
    ok, beneficiary, min_profit, delta, net_profit, commitment = proof.public_signals
    assert int(ok) == 1
    assert int(beneficiary) == int(BENEFICIARY, 16)
    assert int(delta) == 5_000_000
    assert int(net_profit) == 5_000_000
    assert int(min_profit) == 1_000_000
    assert int(commitment) == int(proof.commitment)


def test_honest_proof_is_accepted_on_chain(chain, proof):
    _, gateway, _ = chain
    gateway.register("intent-a", proof.commitment, 1_000_000)
    recorded = gateway.record(proof, "intent-a")
    assert recorded.verified_onchain
    assert recorded.delta == 5_000_000
    assert recorded.net_profit == 5_000_000


def _record(fork, gateway, key, proof, signals, intent, commitment):
    p_a, p_b, p_c, _ = solidity_calldata(proof.proof, signals)
    data = _selector(
        "recordSettlement(bytes32,bytes32,uint256[2],uint256[2][2],uint256[2],uint256[6])"
    )
    payload = (
        intent_id_bytes(intent)
        + _bytes32(commitment)
        + b"".join(_word(int(x, 16)) for x in p_a)
        + b"".join(_word(int(x, 16)) for pair in p_b for x in pair)
        + b"".join(_word(int(x, 16)) for x in p_c)
        + b"".join(_word(int(s)) for s in signals)
    )
    return fork.send(key, gateway.gateway, data + payload)


def test_replay_is_rejected(chain, proof):
    fork, gateway, key = chain
    gateway.register("intent-b", proof.commitment, 1_000_000)
    assert _record(fork, gateway, key, proof, proof.public_signals, "intent-b", proof.commitment).status == 1
    assert _record(fork, gateway, key, proof, proof.public_signals, "intent-b", proof.commitment).status == 0


def test_inflated_profit_is_rejected(chain, proof):
    fork, gateway, key = chain
    gateway.register("intent-c", proof.commitment, 1_000_000)
    inflated = list(proof.public_signals)
    inflated[4] = "999999999"
    step = _record(fork, gateway, key, proof, inflated, "intent-c", proof.commitment)
    assert step.status == 0


def test_wrong_beneficiary_is_rejected(chain, proof):
    fork, gateway, key = chain
    gateway.register("intent-d", proof.commitment, 1_000_000)
    swapped = list(proof.public_signals)
    swapped[1] = str(int("0x1111111111111111111111111111111111111111", 16))
    step = _record(fork, gateway, key, proof, swapped, "intent-d", proof.commitment)
    assert step.status == 0


def test_unregistered_commitment_is_rejected(chain, proof):
    fork, gateway, key = chain
    step = _record(fork, gateway, key, proof, proof.public_signals, "intent-e", proof.commitment)
    assert step.status == 0


def test_bootstrap_moves_value_and_records_it():
    result = Bootstrap(beneficiary=BENEFICIARY).run("intent-f", min_profit=1_000_000, notional=5_000_000)
    assert result.delivered
    assert result.verified_onchain
    # The delta came from reading token balance before and after, not from a
    # constant, so it must equal what the transfer actually moved.
    assert result.delta == 5_000_000
    assert result.token_delivered == result.delta
    assert result.settlement is not None
    assert result.settlement.beneficiary.lower() == BENEFICIARY.lower()


def test_bootstrap_profit_meets_the_floor():
    result = Bootstrap(beneficiary=BENEFICIARY).run("intent-g", min_profit=4_000_000, notional=5_000_000)
    assert result.delivered
    assert result.net_profit >= 4_000_000


def test_bootstrap_refuses_to_settle_when_nothing_moved():
    # A floor above the notional cannot be met by this path. The run must report
    # no delivery rather than record a settlement the proof cannot support.
    result = Bootstrap(beneficiary=BENEFICIARY).run("intent-h", min_profit=1, notional=0)
    assert not result.delivered
    assert result.settlement is None


def test_circuit_rejects_a_lie_at_witness_time():
    with pytest.raises(Exception):
        zkproof.prove_settlement(
            beneficiary=BENEFICIARY,
            delta=0,
            net_profit=0,
            min_profit=1_000_000,
            step_count=1,
            total_gas=1,
        )


def test_run_never_claims_money_it_minted():
    # The fork mints its own working capital, so a delivered run must say that
    # the unit is not money. Without this flag the delta reads as a payout.
    result = Bootstrap(beneficiary=BENEFICIARY).run("intent-i", min_profit=1_000_000, notional=5_000_000)
    assert result.delivered
    assert result.money_is_real is False
    assert result.value_unit == "FSU"
    assert any("no market value" in n for n in result.notes)


def test_working_capital_is_capped():
    from synthesizer.bootstrap import MAX_NOTIONAL_UNITS

    result = Bootstrap(beneficiary=BENEFICIARY).run("intent-j", min_profit=1, notional=10**15)
    # The cap applies before anything is minted, so the measured delta cannot
    # exceed it however large the request was.
    assert result.delta <= MAX_NOTIONAL_UNITS


def test_floor_above_capacity_is_not_settled():
    # A million from nothing is above the cap, so it cannot be settled.
    result = Bootstrap(beneficiary=BENEFICIARY).run("intent-k", min_profit=10**12, notional=10**12)
    assert not result.delivered
    assert result.settlement is None
    assert any("below the requested floor" in n for n in result.notes)


def test_api_bootstrap_reports_the_chain_outcome():
    from fastapi.testclient import TestClient

    from synthesizer.api import app

    client = TestClient(app)
    r = client.post("/bootstrap", json={"intent_id": "intent-api", "min_profit": 1_000_000, "notional": 5_000_000})
    assert r.status_code == 200
    body = r.json()
    assert body["delivered"] is True
    assert body["verified_onchain"] is True
    # The response must say the unit is not money; that flag is the whole
    # reason the delta can be shown without being read as a payout.
    assert body["money_is_real"] is False
    assert r.headers["X-Bootstrap-Delivered"] == "true"
    assert r.headers["X-Bootstrap-Verified"] == "true"


def test_ordinary_intent_is_not_blocked_by_the_cap():
    # The cap must sit above normal requests: a $12 intent has to settle.
    result = Bootstrap(beneficiary=BENEFICIARY).run("intent-l", min_profit=12_000_000, notional=18_000_000)
    assert result.delivered
    assert result.delta == 18_000_000
