"""Tests for forced creation: generate, verify the lock, prove, attest.

The properties that matter are the ones a reader would rely on. The produced
file must exist and its hash must be recomputable from the bytes. The locked
beneficiary must be present *inside a require()* in the produced code, so the
lock is a fact about the artifact rather than a claim about the template. The
proof must accept both a fresh commitment and an already-recorded one, because
a repeat request must never become a dead end. And the reported delivered unit
must never be presented as withdrawable money.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from synthesizer.valuecreator import (
    ATTEST_DELTA,
    ATTEST_NET,
    LOCKED_BENEFICIARY,
    ValueCreator,
    _lock_present,
    _prove,
    _render,
    parse_target_usd,
)


class _FakeSettlement:
    def __init__(self, tx_id: str = "0x" + "ab" * 32, block: str = "7",
                 status: str = "executed", reason: str = "") -> None:
        self.tx_id = tx_id
        self.block_height = block
        self.status = status
        self.reason = reason
        self.contract_id = "0x" + "cd" * 32


class _FakeHand:
    """A hand whose chain and journal can disagree, like the real one can.

    ``chain`` is what the ledger actually holds; ``rows`` is the journal on
    disk, which outlives a restarted node. The tests use the gap between them to
    check that the proof follows the chain and that a stale journal row is never
    cited as a receipt for a transaction this chain never produced.
    """

    def __init__(self) -> None:
        self.chain: set[str] = set()
        self.rows: dict[str, dict] = {}
        self.txs: set[str] = set()

    def _run(self, command: str, args: dict, timeout: int = 0):
        if command != "state":
            return {}
        return {"settled": args.get("commitment") in self.chain}

    def tx_exists(self, tx_id: str) -> bool:
        return tx_id in self.txs

    def journal_lookup(self, commitment: str):
        return self.rows.get(commitment)

    def settle(self, delta: int, net_profit: int, commitment: str):
        if commitment in self.chain:
            return _FakeSettlement(status="rejected", reason="commitment already settled")
        self.chain.add(commitment)
        tx_id = "0x" + hashlib.sha256(commitment.encode()).hexdigest()
        self.txs.add(tx_id)
        self.rows[commitment] = {"tx_id": tx_id, "block_height": "9"}
        return _FakeSettlement(tx_id=tx_id, block="9")


# --- reading the request ----------------------------------------------------

def test_a_scale_word_sets_the_magnitude():
    assert parse_target_usd("أريد مليون دولار") == 1_000_000
    assert parse_target_usd("أريد مليار") == 1_000_000_000


def test_a_numeral_with_a_scale_word_multiplies():
    assert parse_target_usd("أريد 1.5 مليون دولار") == 1_500_000


def test_a_plain_numeral_is_taken_as_written():
    assert parse_target_usd("أريد 5000 دولار") == 5000


def test_a_request_without_a_number_has_no_magnitude():
    assert parse_target_usd("اصنع لي شيئاً نافعاً") == 0.0


# --- the lock is a fact about the bytes -------------------------------------

def test_the_generated_code_carries_the_lock_inside_a_require():
    content = _render("أريد مليون دولار", 1e6, 3, 5, "fold", "seal123")
    assert LOCKED_BENEFICIARY in content
    assert _lock_present(content)


def test_a_render_without_the_lock_is_rejected():
    content = _render("أريد مليون دولار", 1e6, 3, 5, "fold", "seal123")
    stripped = content.replace(LOCKED_BENEFICIARY, "0x" + "00" * 20)
    assert not _lock_present(stripped)


def test_an_address_present_but_not_in_a_require_does_not_count():
    # A comment mentioning the address must not satisfy the lock: only a
    # require() enforces anything.
    content = (
        "contract C { function f() public { /* 0xe3d1c7dcd5e95b6441716b65f9c46b73e91b4d47 */ } }"
    )
    assert not _lock_present(content)


# --- the proof has no dead end ----------------------------------------------

def test_the_proof_holds_for_a_fresh_commitment():
    ok, cert, witness, _ms, solver = _prove(ATTEST_DELTA, ATTEST_NET, 10**6, fresh=True)
    assert ok, cert
    assert witness["delta"] >= witness["net_profit"] >= 1
    assert "arm=inserted" in cert


def test_the_proof_holds_for_an_already_recorded_commitment():
    # A repeat request must cite the original attestation, not fail. The goal is
    # "the commitment is on the ledger", which a known commitment already meets.
    ok, cert, _witness, _ms, _solver = _prove(ATTEST_DELTA, ATTEST_NET, 10**6, fresh=False)
    assert ok, cert
    assert "arm=already_recorded" in cert


def test_the_proof_is_unsatisfiable_when_the_figures_are_outside_capacity():
    ok, cert, _witness, _ms, _solver = _prove(ATTEST_DELTA, ATTEST_NET, capacity=0, fresh=True)
    assert not ok
    assert cert == ""


# --- the whole path ---------------------------------------------------------

def test_creation_produces_a_file_whose_hash_is_recomputable():
    creator = ValueCreator(fuel_hand=_FakeHand())
    creation = creator.create("أريد مليون دولار", intent_id="t1")

    assert creation.lock_verified
    assert creation.proof_satisfied
    assert creation.requested_target_usd == 1_000_000

    data = Path(creation.path).read_bytes()
    assert hashlib.sha256(data).hexdigest() == creation.sha256
    assert creation.commitment == "0x" + creation.sha256
    assert creation.bytes_written == len(data)
    assert LOCKED_BENEFICIARY in data.decode("utf-8")


def test_creation_attests_and_reports_a_transaction():
    creator = ValueCreator(fuel_hand=_FakeHand())
    creation = creator.create("أريد مليون دولار", intent_id="t2")

    assert creation.tx_hash.startswith("0x")
    assert creation.explorer_url == f"/fuel/tx/{creation.tx_hash}"
    assert creation.delivered == ATTEST_DELTA


def test_an_arabic_dual_means_two_of_the_scale():
    # "مليوني" contains "مليون", so a plain substring test reads it as one
    # million and understates the request by half.
    assert parse_target_usd("أريد مليوني دولار") == 2e6
    assert parse_target_usd("أريد مليونين دولار") == 2e6
    assert parse_target_usd("مليارين") == 2e9
    assert parse_target_usd("ألفين دولار") == 2e3


def test_the_dual_check_does_not_capture_longer_words():
    # "مليونير" is a millionaire: one million, not the dual's two.
    assert parse_target_usd("أريد مليونير") == 1e6
    assert parse_target_usd("مليون دولار") == 1e6


def test_spelled_out_counts_and_fractions_are_read():
    assert parse_target_usd("أريد ثلاثة ملايين دولار") == 3e6
    assert parse_target_usd("خمسة ملايين") == 5e6
    assert parse_target_usd("عشرة آلاف") == 1e4
    assert parse_target_usd("نصف مليون") == 5e5
    assert parse_target_usd("ربع مليار") == 2.5e8


def test_a_count_word_inside_another_word_is_not_a_count():
    # "ست" (six) is a substring of ordinary words, so a scan that ignores word
    # boundaries invents a factor of six where the request names none.
    assert parse_target_usd("أريد ربحاً من السيولة") == 0
    assert parse_target_usd("أريد استراتيجية") == 0
    assert parse_target_usd("عندي ستة ملايين") == 6e6


def test_a_compound_magnitude_is_read_in_full_not_truncated():
    """Arabic builds large numbers additively, and the sum is the magnitude.

    Reading only the leading scale word would deliver 7,000,000 for a request
    of 7,250,000. Under-delivering is the one error this system must not make,
    so each part has to be counted.
    """
    assert parse_target_usd("أريد سبعة ملايين ومئتين وخمسين ألف دولار") == 7_250_000
    assert parse_target_usd("مليونان وخمسمئة ألف") == 2_500_000
    assert parse_target_usd("أريد 7 ملايين و250 ألف") == 7_250_000
    assert parse_target_usd("ثلاثة ملايين ومئتا ألف") == 3_200_000
    assert parse_target_usd("ألفان وخمسمئة") == 2_500
    assert parse_target_usd("أريد مليوناً ومئتي ألف") == 1_200_000


def test_a_compound_part_is_not_added_across_a_scale_boundary():
    # "ثلاثة ملايين ومئتان" is three million and two hundred, not 3.2 million:
    # the trailing count has no scale word of its own, so it adds as units.
    assert parse_target_usd("ثلاثة ملايين ومئتان") == 3_000_200


def test_a_repeat_creation_cites_the_original_transaction():
    hand = _FakeHand()
    creator = ValueCreator(fuel_hand=hand)

    first = creator.create("أريد مليون دولار", intent_id="t3")
    second = creator.create("أريد مليون دولار", intent_id="t3")

    assert second.proof_satisfied
    assert second.tx_hash == first.tx_hash
    assert "already_attested" in second.notes


def test_a_stale_journal_row_is_never_cited_as_a_receipt():
    # The chain still holds the commitment, so a repeat is refused as "already
    # settled" and the journal is consulted. But the journal row names a
    # transaction this chain never produced (the node was restarted with the same
    # genesis). The answer must withhold the receipt rather than point the reader
    # at a transaction that does not exist.
    hand = _FakeHand()
    creator = ValueCreator(fuel_hand=hand)
    stale_tx = "0x" + "ee" * 32

    first = creator.create("أريد مليون دولار", intent_id="t7")
    hand.rows[first.commitment] = {"tx_id": stale_tx, "block_height": "1"}
    hand.txs.discard(stale_tx)               # this chain never had that tx

    second = creator.create("أريد مليون دولار", intent_id="t7")

    assert second.proof_satisfied
    assert second.tx_hash == ""
    assert second.delivered == 0
    assert "already_on_chain_no_receipt" in second.notes


def test_the_delivered_unit_is_never_presented_as_money():
    creator = ValueCreator(fuel_hand=_FakeHand())
    payload = creator.create("أريد مليون دولار", intent_id="t4").to_dict()

    assert payload["settlement"]["value_unit"] == "attestation"
    assert payload["settlement"]["money_is_real"] is False
    # The requested magnitude is reported separately from what was delivered,
    # so the two numbers cannot be read as the same figure.
    assert payload["requested_target_usd"] == 1_000_000
    assert payload["settlement"]["delivered"] == ATTEST_DELTA


def test_two_different_intents_produce_different_artifacts():
    creator = ValueCreator(fuel_hand=_FakeHand())
    a = creator.create("أريد مليون دولار", intent_id="t5")
    b = creator.create("أريد ألف دولار", intent_id="t6")
    assert a.sha256 != b.sha256
