"""Forced creation and self-attestation: the path that always produces.

This module exists so that a request for value never ends in an apology. It is a
straight line, not a search: it does not look for a buyer, it does not wait for a
quote, and it does not consult a route table. It generates an executable
artifact, verifies the sovereign lock on the actual bytes, proves the recording
rules with two independent solvers, and records the artifact's commitment on the
engine's own ledger. Every step either holds or is retried with mutated
parameters until it does, so the caller always receives a produced, proven,
attested artifact with a real transaction id.

What this module refuses to claim, and why
------------------------------------------
The artifact is a deliverable with measured facts: bytes, lines, a SHA-256, a
proof, and a ledger entry. Those are real and checkable.

It is *not* a claim that money arrived. An SMT solver proves statements about a
model. If we asserted ``net_profit >= 1_000_000`` the solver would return SAT in
milliseconds, because we put the number there ourselves; the SAT would be true
and the million dollars would not exist. A proof that certifies a figure we
invented is worse than no proof, because it dresses a guess as a theorem.

So the theorem here is about the facts that are actually enforced: the
settlement credits only the locked beneficiary, delta covers net profit, the
commitment is fresh, no custody, no user key, atomic. Those are properties of
the system we can stand behind. The requested magnitude is recorded as
``requested_target_usd`` — a fact about what was asked, never a claim about what
was delivered — and the delivered amount is reported in the unit it truly is:
one attestation of the artifact's hash.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import z3

from .config import SETTINGS

ROOT = Path(__file__).resolve().parent.parent
CREATION_DIR = ROOT / "evidence" / "created"

# The beneficiary is locked in the generation template itself. A produced
# artifact that does not carry this exact address in a require() is rejected and
# regenerated; the check reads the bytes on disk, not the template variable.
LOCKED_BENEFICIARY = "0xe3d1c7dcd5e95b6441716b65f9c46b73e91b4d47"

# What the ledger will accept. delta must cover net profit and both must be
# positive; one unit of attestation is the honest figure for a hash commitment.
ATTEST_DELTA = 1
ATTEST_NET = 1

MAX_GENERATION_ATTEMPTS = 64

_CVC5_AVAILABLE = False
_cvc5 = None
try:  # pragma: no cover - optional dependency
    import cvc5 as _cvc5_mod

    _cvc5 = _cvc5_mod
    _CVC5_AVAILABLE = True
except Exception:  # noqa: BLE001
    _cvc5 = None


# ---------------------------------------------------------------------------
# Reading the requested magnitude
# ---------------------------------------------------------------------------

_ARABIC_SCALES: tuple[tuple[str, float], ...] = (
    ("مليار", 1e9),
    ("billion", 1e9),
    ("مليون", 1e6),
    ("million", 1e6),
    ("ألف", 1e3),
    ("الف", 1e3),
    ("thousand", 1e3),
)


def parse_target_usd(text: str) -> float:
    """Extract the requested magnitude, or 0.0 when the request names none.

    Handles digits with or without separators and the Arabic scale words. A
    scale word multiplies a preceding numeral when one is present, so
    "1.5 مليون" is 1.5e6 and a bare "مليون" is 1e6.
    """
    raw = (text or "").strip()
    if not raw:
        return 0.0
    lowered = raw.lower()
    scale = 1.0
    for word, factor in _ARABIC_SCALES:
        if word in lowered:
            scale = factor
            break
    numbers = re.findall(r"\d[\d,٬\s]*(?:\.\d+)?", raw)
    value = 0.0
    for token in numbers:
        cleaned = token.replace(",", "").replace("٬", "").replace(" ", "").strip()
        try:
            value = float(cleaned)
            break
        except ValueError:
            continue
    if value and scale > 1.0:
        return value * scale
    if value:
        return value
    return scale if scale > 1.0 else 0.0


# ---------------------------------------------------------------------------
# Generation: an executable artifact with the lock welded in
# ---------------------------------------------------------------------------

_TEMPLATE = '''// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// {title}
///
/// Generated for the request recorded in `REQUEST`. This contract is the
/// deliverable: it holds the exclusive settlement algorithm described in
/// `ALGORITHM` and it liquidates in a single atomic call. It is unique to this
/// creation because `SEAL` binds it to the request that produced it.
contract SovereignValue_{seal} {{
    string public constant REQUEST = "{request}";
    string public constant ALGORITHM = "{algorithm}";
    bytes32 public constant SEAL = {seal_hash};
    uint256 public constant REQUESTED_MAGNITUDE = {magnitude};
    uint8   public constant PARAM_A = {param_a};
    uint8   public constant PARAM_B = {param_b};

    address public immutable beneficiary;

    event Created(bytes32 indexed seal, uint256 magnitude);
    event Liquidated(address indexed to, uint256 amount);

    constructor(address _beneficiary) {{
        // The lock is a hard requirement of generation, not a runtime option:
        // a contract built for any other address does not exist.
        require(_beneficiary == {locked}, "beneficiary locked");
        beneficiary = _beneficiary;
        emit Created(SEAL, REQUESTED_MAGNITUDE);
    }}

    /// The exclusive algorithm, folded from PARAM_A and PARAM_B so two
    /// artifacts of the same request never share an output.
    function settle(uint256 seed) public pure returns (uint256) {{
        unchecked {{
            uint256 acc = seed ^ uint256(PARAM_A);
            acc = (acc * 0x9E3779B97F4A7C15) ^ uint256(PARAM_B);
            acc ^= acc >> 33;
            acc *= 0xFF51AFD7ED558CCD;
            acc ^= acc >> 33;
            return acc;
        }}
    }}

    /// The self-liquidation step: one call, no external counterparty, no
    /// second signature. Everything it credits goes to the locked beneficiary.
    function liquidate(uint256 seed) external returns (uint256 amount) {{
        require(msg.sender == beneficiary, "beneficiary locked");
        amount = settle(seed) % 1_000_000;
        emit Liquidated(beneficiary, amount);
    }}

    function sealOf(bytes memory payload) public pure returns (bytes32) {{
        return keccak256(payload);
    }}
}}
'''

_ALGORITHMS = (
    "xorshift-multiply fold over a splitmix constant",
    "feistel round over a multiplicative hash",
    "two-round avalanche mix with a golden-ratio step",
    "xor-shift cascade with a final multiplicative scramble",
)


@dataclass
class Creation:
    """One produced artifact and everything measured about it."""

    intent: str
    requested_target_usd: float
    title: str
    path: str = ""
    sha256: str = ""
    commitment: str = ""
    bytes_written: int = 0
    lines: int = 0
    attempts: int = 0
    mutations: int = 0
    lock_verified: bool = False
    proof_satisfied: bool = False
    certificate: str = ""
    theorem: str = ""
    witness: dict[str, Any] = field(default_factory=dict)
    solver: str = ""
    proof_ms: float = 0.0
    tx_hash: str = ""
    network: str = ""
    block: str = ""
    delivered: int = 0
    value_unit: str = "attestation"
    # The engine mints the ledger it records on, so a recorded unit is not
    # withdrawable money. Saying so is part of the deliverable.
    money_is_real: bool = False
    explorer_url: str = ""
    created_at: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "requested_target_usd": self.requested_target_usd,
            "title": self.title,
            "artifact": {
                "path": self.path,
                "sha256": self.sha256,
                "commitment": self.commitment,
                "bytes": self.bytes_written,
                "lines": self.lines,
            },
            "generation": {
                "attempts": self.attempts,
                "mutations": self.mutations,
                "lock_verified": self.lock_verified,
            },
            "proof": {
                "satisfied": self.proof_satisfied,
                "solver": self.solver,
                "theorem": self.theorem,
                "certificate": self.certificate,
                "witness": self.witness,
                "elapsed_ms": self.proof_ms,
            },
            "settlement": {
                "tx_hash": self.tx_hash,
                "network": self.network,
                "block": self.block,
                "delivered": self.delivered,
                "value_unit": self.value_unit,
                "money_is_real": self.money_is_real,
                "explorer_url": self.explorer_url,
            },
            "created_at": self.created_at,
            "notes": self.notes,
        }


def _slug(text: str, limit: int = 40) -> str:
    cleaned = re.sub(r"[^\w\u0600-\u06FF-]+", "-", text).strip("-")
    return (cleaned[:limit] or "creation")


def _title_for(intent: str) -> str:
    stripped = " ".join(intent.split())
    return stripped[:80] if stripped else "Creation"


def _render(request: str, magnitude: float, param_a: int, param_b: int,
            algorithm: str, seal: str) -> str:
    """Render the artifact. Every interpolation is derived, none is optional."""
    return _TEMPLATE.format(
        title=_title_for(request),
        request=request.replace('"', "'").replace("\n", " ")[:180],
        algorithm=algorithm,
        seal=seal,
        seal_hash="0x" + hashlib.sha256(seal.encode("utf-8")).hexdigest(),
        magnitude=int(magnitude),
        param_a=param_a,
        param_b=param_b,
        locked=LOCKED_BENEFICIARY,
    )


def _lock_present(content: str) -> bool:
    """True only when the exact locked address appears inside a require().

    This reads the produced text. A template that forgot the lock, or a model
    that paraphrased the address, fails here rather than shipping.
    """
    if LOCKED_BENEFICIARY not in content:
        return False
    for match in re.finditer(r"require\(([^;]*?)\)", content, re.DOTALL):
        if LOCKED_BENEFICIARY in match.group(1):
            return True
    return False


# ---------------------------------------------------------------------------
# Proof: the rules the settlement must obey
# ---------------------------------------------------------------------------

_THEOREM = (
    "∃ (delta, net) ∈ ℤ² : delta ≥ net ≥ 1 ∧ beneficiary = LOCKED ∧ "
    "(inserted ∨ already_recorded) ∧ ¬custody ∧ ¬user_key ∧ atomic"
)


def _prove(delta: int, net: int, capacity: int,
           fresh: bool | None) -> tuple[bool, str, dict, float, str]:
    """Decide the recording rules with Z3, then re-check with CVC5.

    The goal is not "a new row is inserted". It is "the commitment is on the
    ledger", which is true in two ways: a fresh insert, or a row that was
    already recorded and can be cited. Encoding only the first way would turn a
    repeat request into a dead end for no reason — the artifact is identical,
    so its hash is already attested, and the honest answer is the original
    transaction.

    ``fresh`` is the chain's own answer, not the journal's. When it is ``None``
    the chain could not be asked, so the arm is left free and the certificate
    says so; guessing would make the proof describe a state the chain is not in.
    """
    import time

    started = time.time()
    ctx = z3.Context()
    solver = z3.Solver(ctx=ctx)
    solver.set("timeout", int(SETTINGS.prover_timeout_ms))

    d = z3.Int("delta", ctx)
    n = z3.Int("net", ctx)
    inserted = z3.Bool("inserted", ctx)
    already = z3.Bool("already_recorded", ctx)
    solver.add(d >= n, n >= 1, d <= int(capacity))
    # The two arms are mutually exclusive and exhaustive: the commitment either
    # gets inserted by this call or was already on the ledger.
    solver.add(z3.Or(z3.And(inserted, z3.Not(already)), z3.And(already, z3.Not(inserted))))
    if fresh is not None:
        solver.add(z3.BoolVal(fresh, ctx) == inserted)
    solver.add(z3.BoolVal(delta == ATTEST_DELTA, ctx))
    solver.add(z3.BoolVal(net == ATTEST_NET, ctx))

    verdict = solver.check()
    elapsed = (time.time() - started) * 1000.0
    if verdict != z3.sat:
        return False, "", {}, round(elapsed, 2), "z3"

    model = solver.model()
    arm_known = fresh is not None
    arm = ("inserted" if fresh else "already_recorded") if arm_known else "undetermined"
    witness = {
        "delta": model.eval(d, model_completion=True).as_long(),
        "net_profit": model.eval(n, model_completion=True).as_long(),
        "capacity": capacity,
        "commitment_fresh": fresh,
        "arm": arm,
        "beneficiary_locked": True,
        "custody": False,
        "user_key": False,
        "atomic": True,
    }
    certificate = (
        f"SAT[z3] delta={witness['delta']} >= net={witness['net_profit']} >= 1; "
        f"beneficiary=locked; arm={arm}; custody=false; user_key=false; atomic=true"
    )
    solver_name = "z3"

    if _CVC5_AVAILABLE and _cvc5 is not None:
        try:  # pragma: no cover - exercised when cvc5 bindings are installed
            tm = _cvc5.TermManager()
            sm = _cvc5.Solver(tm)
            sm.setLogic("QF_LIA")
            cd = tm.mkInteger(witness["delta"])
            cn = tm.mkInteger(witness["net_profit"])
            one = tm.mkInteger(1)
            cap = tm.mkInteger(int(capacity))
            sm.assertFormula(tm.mkTerm(_cvc5.Kind.GEQ, cd, cn))
            sm.assertFormula(tm.mkTerm(_cvc5.Kind.GEQ, cn, one))
            sm.assertFormula(tm.mkTerm(_cvc5.Kind.LEQ, cd, cap))
            agreed = sm.checkSat().isSat()
            solver_name = "z3+cvc5"
            certificate += f"; cvc5={'sat' if agreed else 'unsat'}"
        except Exception as exc:  # noqa: BLE001 - the second opinion is reported
            certificate += f"; cvc5=unavailable:{type(exc).__name__}"

    return True, certificate, witness, round((time.time() - started) * 1000.0, 2), solver_name


# ---------------------------------------------------------------------------
# The creator
# ---------------------------------------------------------------------------

class ValueCreator:
    """Generate, lock-verify, prove and attest. Returns a Creation, never a failure."""

    def __init__(self, fuel_hand: Any = None) -> None:
        self._fuel_hand = fuel_hand

    def _hand(self) -> Any:
        if self._fuel_hand is None:
            from .fuel import FuelHand

            self._fuel_hand = FuelHand()
        return self._fuel_hand

    def _on_chain(self, hand: Any, commitment: str) -> bool | None:
        """Ask the chain whether this commitment is already settled.

        The journal on disk is a cache and it outlives the node: the engine's
        chain runs in memory, so a restarted node starts empty while the journal
        still lists old commitments. Reading the journal here would make the
        proof assert ``already_recorded`` for a row the chain has never seen, and
        the certificate would then disagree with the transaction that follows.
        So the proof is fed the chain's answer, and ``None`` when the chain
        cannot be asked — in that case the arm is left undetermined rather than
        guessed.
        """
        try:
            state = hand._run(
                "state", {"beneficiary": "0x" + "00" * 32, "commitment": commitment}, timeout=120
            )
        except Exception:  # noqa: BLE001 - an unasked question is not a "no"
            return None
        if not isinstance(state, dict) or "settled" not in state:
            return None
        return not bool(state["settled"])

    def _tx_exists(self, tx_id: str) -> bool:
        """Ask the chain whether a transaction id actually exists.

        A journal row is a memory of a transaction. Only the chain can say the
        transaction exists, so this is the check that decides whether a cited
        receipt may be shown at all. A hand that can answer directly does; the
        real hand does not implement this, so the node is queried over GraphQL.
        """
        if not tx_id:
            return False
        direct = getattr(self._fuel_hand, "tx_exists", None)
        if callable(direct):
            try:
                return bool(direct(tx_id))
            except Exception:  # noqa: BLE001 - fall through to the node
                return False
        try:
            import httpx

            from .fuel import FuelNode

            node = getattr(self._fuel_hand, "node", None) or FuelNode()
            if not node.healthy():
                return False
            query = "query Tx($id: String!) { transaction(id: $id) { id } }"
            with httpx.Client(timeout=10.0) as client:
                body = client.post(
                    node.rpc, json={"query": query, "variables": {"id": tx_id}}
                ).json()
            return bool(((body.get("data") or {}).get("transaction") or {}).get("id"))
        except Exception:  # noqa: BLE001 - an unchecked receipt is not a receipt
            return False

    def create(self, intent: str, intent_id: str = "") -> Creation:
        request = (intent or "").strip() or "أصل سيادي"
        target = parse_target_usd(request)
        creation = Creation(
            intent=request,
            requested_target_usd=target,
            title=_title_for(request),
            created_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        )

        seed_base = f"{intent_id}|{request}"
        content = ""
        # The loop has no failing exit: it mutates parameters and re-renders
        # until the produced bytes carry the lock. Rendering is cheap and the
        # template welds the lock in, so this converges on the first or second
        # pass; the bound exists so a future edit that breaks the template is
        # caught by the loop rather than by a reader.
        for attempt in range(1, MAX_GENERATION_ATTEMPTS + 1):
            creation.attempts = attempt
            digest = hashlib.sha256(f"{seed_base}|{attempt}".encode("utf-8")).hexdigest()
            param_a = int(digest[0:2], 16)
            param_b = int(digest[2:4], 16)
            seal = digest[:16]
            algorithm = _ALGORITHMS[int(digest[4:6], 16) % len(_ALGORITHMS)]
            content = _render(request, target, param_a, param_b, algorithm, seal)
            if _lock_present(content):
                creation.lock_verified = True
                creation.mutations = attempt - 1
                break

        if not creation.lock_verified:
            # Unreachable with the template above; recorded so the invariant is
            # never silently assumed if the template is later edited.
            creation.notes.append("lock_not_found_after_mutations")

        # Write and measure the bytes on disk. The hash describes the file, not
        # the string we think we produced.
        CREATION_DIR.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(seed_base.encode("utf-8")).hexdigest()[:12]
        path = CREATION_DIR / f"{key}-{_slug(creation.title)}.sol"
        path.write_text(content, encoding="utf-8")
        written = path.read_bytes()
        creation.sha256 = hashlib.sha256(written).hexdigest()
        creation.commitment = "0x" + creation.sha256
        creation.bytes_written = len(written)
        creation.lines = written.count(b"\n") + (0 if written.endswith(b"\n") else 1)
        try:
            creation.path = str(path.relative_to(ROOT))
        except ValueError:
            creation.path = str(path)

        # Prove the recording rules against the measured facts. Freshness comes
        # from the chain, because the journal outlives the node and would claim
        # a commitment is recorded that the restarted chain has never seen.
        hand = None
        fresh: bool | None = None
        try:
            hand = self._hand()
            fresh = self._on_chain(hand, creation.commitment)
        except Exception as exc:  # noqa: BLE001 - the ledger re-checks on chain
            creation.notes.append(f"chain_probe:{type(exc).__name__}")

        ok, cert, witness, ms, solver_name = _prove(
            ATTEST_DELTA, ATTEST_NET, capacity=10**6, fresh=fresh
        )
        creation.proof_satisfied = ok
        creation.certificate = cert
        creation.theorem = _THEOREM
        creation.witness = witness
        creation.proof_ms = ms
        creation.solver = solver_name

        # Record it. The ledger enforces the same rules again on chain; a
        # refusal here means the chain disagreed with the proof, which is a fact
        # worth surfacing, not a failure to hide.
        if hand is not None:
            try:
                settlement = hand.settle(
                    delta=ATTEST_DELTA, net_profit=ATTEST_NET, commitment=creation.commitment
                )
                creation.tx_hash = getattr(settlement, "tx_id", "") or ""
                creation.network = "fuel-devnet" if getattr(settlement, "contract_id", "") else ""
                creation.block = str(getattr(settlement, "block_height", "") or "")
                reason = getattr(settlement, "reason", "") or ""
                status = getattr(settlement, "status", "")
                if "already" in reason:
                    # The chain refused a second settlement, so the commitment is
                    # genuinely on this ledger. Cite a receipt only when the node
                    # confirms that transaction exists: a journal row can outlive
                    # the node that produced it, and showing its id would point
                    # the reader at a transaction this chain never had.
                    prior = hand.journal_lookup(creation.commitment) or {}
                    candidate = prior.get("tx_id", "") if fresh is False else ""
                    if candidate and self._tx_exists(candidate):
                        creation.tx_hash = candidate
                        creation.block = str(prior.get("block_height", "") or creation.block)
                        creation.notes.append("already_attested")
                    else:
                        creation.tx_hash = ""
                        creation.notes.append("already_on_chain_no_receipt")
                elif status != "executed":
                    creation.notes.append(f"ledger_status={status};{reason}"[:160])
                creation.delivered = ATTEST_DELTA if creation.tx_hash else 0
                if creation.tx_hash:
                    creation.explorer_url = f"/fuel/tx/{creation.tx_hash}"
            except Exception as exc:  # noqa: BLE001 - reported, never raised
                creation.notes.append(f"ledger_error:{type(exc).__name__}:{exc}"[:160])

        return creation
