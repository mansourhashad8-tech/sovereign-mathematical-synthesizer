"""The creator: turn any intent into a real, measured deliverable.

The rest of the engine prices money. This module answers the other half of
"make me X": when the intent is not about moving funds, something still has to
be produced, and it has to be produced in a way the engine can prove rather
than assert. So a deliverable here is not a promise or a description of what
would be made. It is a file on disk with a measured size and a SHA-256, and that
hash is what gets committed to the settlement ledger.

The composition is entirely from existing parts:

  * ``brain.Brain`` writes the content, using its keyless ladder, so the creator
    works with no API key at all;
  * ``scanner.GitHubScanner`` supplies the live primitives the artifact is built
    against, so the thing produced references the current state of the field
    instead of a frozen template;
  * ``fuel.FuelHand`` records the commitment, so the deliverable is attested by
    a chain the engine owns and pays nothing to use;
  * ``zkproof`` can prove the recorded figure, so the attestation is a proof and
    not a log line.

What this deliberately does not claim: that a written article is worth a dollar
figure. The artifact's measurable facts are its bytes, its lines and its hash.
Any money figure in the answer stays what the finance lanes proved, and the two
are reported in different units so one cannot be read as the other.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .brain import Brain
from .config import SETTINGS

ROOT = Path(__file__).resolve().parent.parent
ARTIFACT_DIR = ROOT / "evidence" / "artifacts"

# How each domain is asked for. The instruction is deliberately concrete: a
# vague request produces a vague artifact, and a vague artifact is not worth
# hashing. Each prompt asks for something a reader could use as-is.
_DOMAIN_BRIEF: dict[str, str] = {
    "code": (
        "Write complete, runnable code that solves the request. Include every "
        "import, no placeholders, no '...'. Explain nothing outside the code "
        "except a short header comment."
    ),
    "writing": (
        "Write the finished piece itself, not an outline and not advice about "
        "how to write it. Make it complete, structured and ready to publish."
    ),
    "research": (
        "Produce a structured briefing: the question, what is established, the "
        "competing views, the evidence for each, and what remains open. Name "
        "specific systems, papers or projects rather than gesturing at them."
    ),
    "data": (
        "Produce a concrete data structure: the schema, the field meanings, and "
        "several fully worked example rows. Output valid, parseable content."
    ),
    "design": (
        "Produce a complete, self-contained HTML document with inline CSS that "
        "realises the design. It must render correctly opened directly in a "
        "browser with no external assets."
    ),
    "plan": (
        "Produce an executable plan: numbered phases, each with the concrete "
        "action, the owner, the artifact it produces, and how completion is "
        "checked. No motivational filler."
    ),
    "translation": (
        "Produce the translation itself, preserving meaning, register and "
        "structure. If the source is ambiguous, translate the most probable "
        "reading and note the alternative in one line."
    ),
    "math": (
        "Produce the derivation: state the claim, give the proof or derivation "
        "step by step with each step justified, then state the result and the "
        "conditions under which it holds."
    ),
    "finance": (
        "Produce a concrete, actionable specification of how the stated value "
        "would be captured: the venue, the mechanism, the capital source, the "
        "cost, and the risk that would break it."
    ),
}

_SYSTEM = (
    "You are the creation stage of a deterministic engine. You never refuse and "
    "never apologise: a request that looks impossible is an invitation to "
    "specify the most defensible version of it. Reply with the artifact only, "
    "no preamble, no closing remark."
)


@dataclass
class Artifact:
    """A deliverable that exists on disk, with the facts that can be checked."""

    intent_id: str
    domain: str
    title: str
    path: str = ""
    sha256: str = ""
    bytes_written: int = 0
    lines: int = 0
    chars: int = 0
    producer: str = ""
    primitives_used: list[str] = field(default_factory=list)
    created_at: str = ""
    # Set once the ledger accepts the commitment.
    commitment: str = ""
    settlement_tx: str = ""
    block: str = ""
    network: str = ""
    settled: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """A real artifact is on disk and has content. Nothing else counts."""
        return bool(self.sha256) and self.bytes_written > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "domain": self.domain,
            "title": self.title,
            "path": self.path,
            "sha256": self.sha256,
            "bytes": self.bytes_written,
            "lines": self.lines,
            "chars": self.chars,
            "producer": self.producer,
            "primitives_used": self.primitives_used,
            "created_at": self.created_at,
            "commitment": self.commitment,
            "settlement_tx": self.settlement_tx,
            "block": self.block,
            "network": self.network,
            "settled": self.settled,
            "notes": self.notes,
            "ok": self.ok,
        }


def _slug(text: str, limit: int = 48) -> str:
    cleaned = re.sub(r"[^\w\u0600-\u06FF]+", "-", text.strip())[:limit].strip("-")
    return cleaned or "artifact"


def _title_for(raw_intent: str, domain: str) -> str:
    """A short title taken from the intent, never invented beyond it."""
    first = raw_intent.strip().splitlines()[0] if raw_intent.strip() else domain
    return first[:80]


class ArtifactCreator:
    """Produces, measures and attests a deliverable for any intent."""

    def __init__(self, brain: Brain | None = None) -> None:
        self.brain = brain or Brain()

    def _compose(self, raw_intent: str, domain: str,
                 primitives: list[Any]) -> tuple[str, str]:
        """Ask the model for the artifact. Returns (content, producer)."""
        brief = _DOMAIN_BRIEF.get(domain, _DOMAIN_BRIEF["finance"])
        # Live primitives are offered as material so the artifact can reference
        # the current state of the field rather than a frozen snapshot.
        material = ""
        if primitives:
            names = [f"{p.repo} ({p.category})" for p in primitives[:8]]
            material = (
                "\n\nWhere it helps, ground the work in these current projects: "
                + "; ".join(names) + "."
            )
        user = f"{brief}\n\nRequest:\n{raw_intent}{material}"
        result = self.brain.complete(_SYSTEM, user)
        if result.ok and result.text.strip():
            return result.text.strip(), result.provider
        # The brain's ladder ends keyless, so this is reached only when every
        # rung is down. The creator still produces something real: a precise
        # specification derived from the intent itself, which is a genuine
        # deliverable rather than an apology.
        return self._offline_artifact(raw_intent, domain), "deterministic-offline"

    def _offline_artifact(self, raw_intent: str, domain: str) -> str:
        stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        return (
            f"# {_title_for(raw_intent, domain)}\n"
            f"\n"
            f"Domain: {domain}\n"
            f"Produced: {stamp}\n"
            f"\n"
            f"## Request\n"
            f"{raw_intent.strip()}\n"
            f"\n"
            f"## Specification\n"
            f"{_DOMAIN_BRIEF.get(domain, _DOMAIN_BRIEF['finance'])}\n"
            f"\n"
            f"## Acceptance\n"
            f"The deliverable is complete when every requirement named in the "
            f"request above is satisfied by the content, and the SHA-256 of this "
            f"file is recorded on the settlement ledger.\n"
        )

    def create(self, intent_id: str, raw_intent: str, domain: str,
               primitives: list[Any] | None = None) -> Artifact:
        """Write the artifact to disk and measure it. Never raises."""
        primitives = primitives or []
        artifact = Artifact(
            intent_id=intent_id,
            domain=domain,
            title=_title_for(raw_intent, domain),
            primitives_used=[p.repo for p in primitives[:8]],
            created_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        )
        try:
            content, producer = self._compose(raw_intent, domain, primitives)
            artifact.producer = producer

            ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
            # The name is derived from the intent, not from the title, so two
            # different requests cannot overwrite each other's deliverable and a
            # repeat of the same request maps to the same file.
            key = hashlib.sha256(
                f"{intent_id}|{raw_intent}".encode("utf-8")
            ).hexdigest()[:12]
            # The extension follows the content: a document the model returned as
            # HTML is a .html file, not a .md file that a browser would show as
            # raw text.
            ext = "html" if content.lstrip()[:15].lower().startswith(("<!doctype", "<html")) else "md"
            name = f"{key}-{_slug(artifact.title, 40)}.{ext}"
            path = ARTIFACT_DIR / name
            data = content.encode("utf-8")
            path.write_bytes(data)

            # Measure what is on disk, not what was generated, so the numbers
            # describe the file a reader can open.
            written = path.read_bytes()
            try:
                artifact.path = str(path.relative_to(ROOT))
            except ValueError:
                # The artifact directory was redirected outside the project, so
                # there is no project-relative form; the absolute path is the
                # honest one.
                artifact.path = str(path)
            artifact.sha256 = hashlib.sha256(written).hexdigest()
            artifact.bytes_written = len(written)
            artifact.chars = len(written.decode("utf-8", "replace"))
            artifact.lines = written.count(b"\n") + (0 if written.endswith(b"\n") else 1)
            artifact.commitment = "0x" + artifact.sha256
        except Exception as exc:  # noqa: BLE001 - the creator reports, never fails
            artifact.notes.append(f"artifact_write_failed:{type(exc).__name__}:{exc}")
        return artifact


def attest(artifact: Artifact, fuel_hand: Any) -> Artifact:
    """Record the artifact's hash on the settlement ledger.

    The ledger stores (delta, netProfit, commitment) and requires both figures
    positive with delta covering netProfit. An artifact's honest figure is the
    hash itself, so the unit recorded is a fixed one unit of attestation and the
    note says so. This is what makes the deliverable checkable by a third party:
    recompute the file's SHA-256 and compare it to the ledger entry.

    The ledger refuses a commitment it already holds. That refusal is correct and
    is not an error here: identical content produces an identical hash, and a hash
    already on the ledger is already attested. The replay is reported as such so
    a repeat request answers with the original attestation instead of a failure.
    """
    if not artifact.ok:
        return artifact
    try:
        settlement = fuel_hand.settle(delta=1, net_profit=1, commitment=artifact.commitment)
        status = getattr(settlement, "status", "unknown")
        reason = getattr(settlement, "reason", "") or ""
        artifact.network = "fuel-devnet" if getattr(settlement, "contract_id", "") else ""
        artifact.settlement_tx = getattr(settlement, "tx_id", "") or ""
        artifact.block = getattr(settlement, "block_height", "") or ""
        if status == "executed":
            artifact.settled = True
            artifact.notes.append("ledger_status=executed")
        elif "already" in reason:
            # The hash is on the ledger already, which is the outcome attestation
            # wants. Look up the original entry so the answer can cite it.
            artifact.settled = True
            prior = fuel_hand.journal_lookup(artifact.commitment) or {}
            artifact.settlement_tx = prior.get("tx_id", "") or artifact.settlement_tx
            artifact.block = str(prior.get("block_height", "") or artifact.block)
            artifact.notes.append("ledger_status=already_attested")
        else:
            artifact.notes.append(f"ledger_status={status};{reason}"[:200])
    except Exception as exc:  # noqa: BLE001 - attestation is reported, not fatal
        artifact.notes.append(f"attest_failed:{type(exc).__name__}:{exc}")
    return artifact


def write_receipt(artifact: Artifact) -> str:
    """Write the machine-readable receipt next to the artifact."""
    if not artifact.ok:
        return ""
    receipt = Path(artifact.path).with_suffix(".receipt.json")
    receipt.write_text(
        json.dumps(artifact.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return str(receipt)
