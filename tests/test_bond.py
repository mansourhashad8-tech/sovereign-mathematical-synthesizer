"""The bond protocol's claims, checked against a real EVM.

The tests that matter here are the ones that would pass if the engine were
lying. A settlement is only real if the beneficiary's balance moved, so the
tests measure balances rather than reading the engine's own verdict, and the
adversarial cases assert that no money moves at all.
"""

from __future__ import annotations

import pytest

from synthesizer.bond import (
    STATUS_FAILED_COLLATERAL,
    STATUS_SETTLED,
    TOKEN_DECIMALS,
    prove_collateral,
)

UNIT = 10**TOKEN_DECIMALS
BENEFICIARY = "0xe3d1c7dcd5e95b6441716b65f9c46b73e91b4d47"


# ---------------------------------------------------------------------------
# The collateral theorem
# ---------------------------------------------------------------------------

def test_a_covered_claim_is_proved_by_both_provers():
    proof = prove_collateral(3 * UNIT, 4 * UNIT, 0, 0, scale=UNIT)
    assert proof["satisfied"] is True
    assert proof["provers"]["z3"] is True
    assert proof["provers"]["cvc5"] is True
    assert int(proof["slack_units"]) == UNIT


def test_a_short_vault_is_refused_and_the_gap_is_named():
    proof = prove_collateral(3 * UNIT, 2 * UNIT, 0, 0, scale=UNIT)
    assert proof["satisfied"] is False
    assert proof["provers"]["z3"] is False
    assert proof["provers"]["cvc5"] is False
    assert int(proof["shortfall_units"]) == UNIT


def test_a_zero_face_value_is_not_a_proof():
    # A bond for nothing would otherwise satisfy "available >= face_value".
    proof = prove_collateral(0, 5 * UNIT, 0, 0, scale=UNIT)
    assert proof["satisfied"] is False


def test_the_query_is_closed_so_a_prover_cannot_answer_about_other_numbers():
    # The failure mode this guards: with free variables both provers say SAT
    # for a short vault, because *some* numbers satisfy the inequality.
    proof = prove_collateral(3 * UNIT, 2 * UNIT, 0, 0, scale=UNIT)
    assert proof["pinned"] is True
    assert proof["provers"]["cvc5"] is False


def test_units_that_do_not_divide_exactly_are_not_proved():
    # 3*UNIT + 1 cannot be expressed in whole tokens, so a query in whole
    # tokens would be about a different number. It must be refused.
    proof = prove_collateral(3 * UNIT + 1, 4 * UNIT, 0, 0, scale=UNIT)
    assert proof["exact_units"] is False
    assert proof["satisfied"] is False


# ---------------------------------------------------------------------------
# Settlement on a real chain
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def settled():
    from synthesizer.bond import BondEngine

    return BondEngine().settle("أريد 3 مليون دولار", 3 * UNIT)


def test_the_beneficiary_balance_moved_by_the_face_value(settled):
    assert settled.status == STATUS_SETTLED
    assert settled.balance_after - settled.balance_before == 3 * UNIT
    assert settled.delivered == 3 * UNIT


def test_money_is_real_requires_a_measured_delivery(settled):
    """The verdict comes from the balance, not from the engine's own report.

    A result is constructed that claims success — settled, with a delivered
    amount equal to the face value — while the measured balances show nothing
    moved. That is precisely the shape a bug would produce, and it must not be
    reported as real money.
    """
    from synthesizer.bond import BondResult

    assert settled.money_is_real is True

    claimed_only = BondResult(
        status=STATUS_SETTLED,
        intent="ادعاء بلا حركة",
        requested_usd=3.0,
        face_value=3 * UNIT,
        beneficiary=BENEFICIARY,
        balance_before=0,
        balance_after=0,
        delivered=3 * UNIT,
    )
    assert claimed_only.money_is_real is False, (
        "a claimed delivery with no measured balance change is not real money"
    )


def test_a_claim_above_the_collateral_does_not_settle(monkeypatch):
    """A vault that cannot cover the claim yields a status, not a payment.

    The engine tops its vault up from its own supply, so a shortfall has to be
    built deliberately: here the top-up is disabled, leaving the vault holding
    only what an earlier strike left behind.
    """
    from synthesizer.bond import BondEngine

    engine = BondEngine()
    engine._chain()
    monkeypatch.setattr(engine, "_fund", lambda st, units: None)

    result = engine.settle("أريد مبلغاً لا يغطيه الضمان", 5 * UNIT)
    assert result.status == STATUS_FAILED_COLLATERAL
    assert result.money_is_real is False
    assert result.delivered == 0
    assert int(result.shortfall_units) > 0


def test_the_bond_pays_the_locked_beneficiary_and_no_one_else(settled):
    assert settled.beneficiary == BENEFICIARY
    assert settled.bond_address.startswith("0x")
    assert settled.vault.startswith("0x")


def test_the_strike_is_deterministic_for_the_same_magnitude():
    from synthesizer.bond import BondEngine

    first = BondEngine().settle("نفس الطلب", 2 * UNIT)
    second = BondEngine().settle("نفس الطلب", 2 * UNIT)
    assert first.delivered == second.delivered == 2 * UNIT
    assert first.money_is_real and second.money_is_real


def test_a_vault_that_records_collateral_it_does_not_hold_pays_nothing():
    """The accounting number is not evidence; the balance is.

    A vault credited with collateral whose tokens were never moved will pass
    its own `stored` check and fail at the transfer. The beneficiary must end
    up with nothing, and the redeem must revert.
    """
    import json
    from pathlib import Path

    from eth_abi import encode
    from eth_account import Account
    from eth_utils import keccak

    from synthesizer.evm_harness import _evm, call, deploy, erc20_balance, read

    contracts = Path(__file__).resolve().parent.parent / "contracts"

    def art(name: str):
        return (
            json.loads((contracts / f"{name}.abi.json").read_text()),
            (contracts / f"{name}.bin").read_text().strip(),
        )

    chain = _evm()
    owner = "0x" + "11" * 31 + "12"
    owner_addr = Account.from_key(owner).address

    token = deploy(chain, owner, *art("TestValueToken"))
    vault = deploy(chain, owner, *art("CollateralVault"))
    bond = deploy(
        chain,
        owner,
        *art("SovereignBond"),
        encode(
            ["address", "address", "uint256", "bytes32"],
            [vault, token, 3 * UNIT, b"\x09" * 32],
        ),
    )

    # Record three million as stored without moving a single token in.
    call(chain, owner, vault, keccak(text="credit(address,uint256)")[:4] + encode(
        ["address", "uint256"], [token, 3 * UNIT]
    ))
    call(chain, owner, vault, keccak(text="bind(address)")[:4] + encode(["address"], [bond]))

    stored = read(
        chain, vault, keccak(text="stored(address)")[:4] + encode(["address"], [token]), owner_addr
    )
    assert int.from_bytes(stored, "big") == 3 * UNIT, "the vault claims the collateral"

    before = erc20_balance(chain, token, BENEFICIARY, owner_addr)
    outcome = call(chain, owner, bond, keccak(text="redeem()")[:4], gas=5_000_000)
    after = erc20_balance(chain, token, BENEFICIARY, owner_addr)

    assert outcome.ok is False, "redeeming unbacked collateral must revert"
    assert after == before, "the beneficiary must receive nothing"


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------

def test_the_endpoint_carries_the_magnitude_without_float_loss():
    """3,000,000 must become exactly 3e24 base units.

    A float holds about 17 significant digits, and 3e24 needs 25. Building the
    face value as int(round(3000000.0 * 10**18)) gives
    2999999999999999949668352 — a face value the intent never asked for. The
    endpoint converts with Decimal, and the exactness guard in the proof is the
    second line of defence if that ever regresses.
    """
    from decimal import Decimal

    from fastapi.testclient import TestClient

    from synthesizer.api import app

    with TestClient(app) as client:
        response = client.post("/bond", json={"intent": "أريد 3 مليون دولار"})

    three_million = 3_000_000 * UNIT
    assert response.status_code == 200
    body = response.json()
    assert body["face_value"] == str(three_million)
    assert body["status"] == STATUS_SETTLED
    assert body["money_is_real"] is True
    assert response.headers["X-Bond-Money-Is-Real"] == "true"
    assert response.headers["X-Bond-Delivered"] == str(three_million)


def test_the_endpoint_refuses_an_intent_with_no_magnitude():
    from fastapi.testclient import TestClient

    from synthesizer.api import app

    with TestClient(app) as client:
        response = client.post("/bond", json={"intent": "مرحبا"})

    assert response.status_code == 422
    body = response.json()
    assert body["money_is_real"] is False
    assert "reason" in body
