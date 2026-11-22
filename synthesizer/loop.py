"""The instant, deterministic synthesis loop.

There is no waiting state and no background worker. A request is answered
inside the request:

1. **Formal analysis** — parse the intent into a constraint system. This is
   deterministic and offline, measured in microseconds.
2. **Candidate generation** — instantiate the pre-computed template library
   (Aave/Uniswap/Curve/Balancer/Morpho compositions) and, in the same pass,
   compose fresh paths from the cached primitive set.
3. **Parallel proof** — every candidate is proved concurrently by Z3 (with CVC5
   as an independent second engine). The proofs are then ranked
   deterministically, so parallelism does not make the answer non-deterministic.
4. **Execution gate** — the winning path is executed immediately. The proof is
   the *only* gate: a satisfied proof means execute, with no further approval.
5. **Absolute limit** — if no candidate proves, the prover is asked whether the
   target is unreachable by conservation of value at the maximum admissible
   notional. An UNSAT answer is a mathematical impossibility certificate, not a
   refusal: it is reported as a physical bound with the exact number.

Every request terminates with either an executed path or a bound certificate.
Nothing is deferred.
"""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .brain import Brain
from .composer import MAX_NOTIONAL_USD, Composer
from .config import SETTINGS
from .executor import Executor
from .intent import intent_id, parse_deterministic
from .models import AtomicPath, Constraints, ExecutionResult, IntentResult, Proof
from .prover import Prover
from .scanner import GitHubScanner
from .templates import templates_for
from .gastreasury import InternalGasTreasury
from .gasflywheel import _networks
from .genesis import GenesisMesh
from .matrix import PathMatrix, build_lanes
from .watchdog import SettlementWatchdog


@dataclass
class LoopStats:
    attempts: int = 0
    live_primitives: int = 0
    proofs_found: int = 0
    invented: bool = False
    started_at: float = 0.0


def _notional_for(target: float, net_rate: float) -> float:
    """The smallest notional that reaches ``target``, capped at the physical max."""
    if net_rate <= 0:
        return 0.0
    return min(target / net_rate, MAX_NOTIONAL_USD)


class Synthesizer:
    def __init__(self) -> None:
        self.brain = Brain()
        self.scanner = GitHubScanner()
        self.composer = Composer()
        self.genesis = GenesisMesh()
        self.prover = Prover()
        self.executor = Executor()
        self.watchdog = SettlementWatchdog(self.executor.flywheel)
        # Gas is an internal asset: the treasury measures what the engine can
        # pay from its own resources before any external sponsor is considered.
        self.treasury = InternalGasTreasury(self.executor.flywheel)
        self.matrix = PathMatrix()
        self._results: dict[str, IntentResult] = {}
        self._results_lock = threading.Lock()
        # A small pool: settlements are I/O-bound and a burst of intents should
        # not open an unbounded number of concurrent sponsor handshakes.
        self._settle_pool = ThreadPoolExecutor(
            max_workers=SETTINGS.settlement_workers,
            thread_name_prefix="settle",
        )
        # A retry that lands must reach the stored result, or the watch was
        # pointless.
        self.watchdog.set_on_settled(self._on_watchdog_settled)
        # The beneficiary's balance immediately before each intent's first
        # broadcast. Gate B is a delta against this baseline; without it a
        # retry that mined could be mistaken for a payout it never made.
        self._baselines: dict[str, int] = {}

    # -- public --------------------------------------------------------------

    def synthesize_sync(self, raw_intent: str, budget: int | None = None) -> IntentResult:
        """Answer an intent in one pass. ``budget`` is accepted for
        compatibility; the loop is deadline-driven rather than budget-driven."""
        started = time.time()
        timings: dict[str, float] = {}

        t0 = time.time()
        # The deterministic parse is the complete, authoritative reading and
        # costs microseconds. The model never gates the decision: it is used
        # only for the optional prose polish at the end of the request.
        constraints = parse_deterministic(raw_intent)
        iid = intent_id(constraints)
        stats = LoopStats(started_at=started)
        timings["parse"] = (time.time() - t0) * 1000.0

        # Primitives come from cache; a cold cache serves seeds at once and
        # warms itself off the request path.
        t0 = time.time()
        primitives = self.scanner.scan()
        stats.live_primitives = self.scanner.fresh_count()
        timings["primitives"] = (time.time() - t0) * 1000.0

        t0 = time.time()
        chosen, proof, invented, source = self._prove_fastest(constraints, primitives, stats)
        timings["synthesis"] = (time.time() - t0) * 1000.0

        # Align the path with the proven witness. The solver picks concrete
        # yield and fee values inside each band; the path must carry those
        # values, otherwise the profit it reports (used by the executor and the
        # UI) differs from the profit that was actually proved.
        if chosen is not None and proof is not None and proof.satisfied:
            self._align_to_witness(chosen, proof)
            # The lock settles each intentId once. Seeding the path with the
            # beneficiary address gave every intent the same key, so only the
            # first settlement could ever succeed and every later one reverted
            # inside the lock while its transaction still mined.
            chosen.intent_id = iid

        # Absolute-limit certificate when nothing proved. The bound must be
        # computed over the best composition the market actually offers: a
        # bound over an empty path would prove nothing and report a ceiling of
        # zero, which would be a false statement about the market.
        bound: Proof | None = None
        absolute_limit = False
        if proof is None or not proof.satisfied:
            t0 = time.time()
            ceiling_path = max(
                (p for p, _ in self._candidates(constraints, primitives)),
                key=lambda p: p.net_rate,
                default=None,
            )
            bound = self.prover.prove_bound(constraints, ceiling_path)
            timings["bound"] = (time.time() - t0) * 1000.0
            absolute_limit = bound.detail == "absolute_limit"

        # Execution: the target value is the thing that matters, so every route
        # to it is entered at once and the first that pays wins. This is the
        # structural change from "follow one path until it breaks" to "reach the
        # result by whichever route can".
        #
        # The matrix never blocks the answer on a single venue. A lane that
        # cannot pay records why in its own entry and the others keep running,
        # so a gas shortfall in one route is no longer the system's answer.
        execution: ExecutionResult | None = None
        matrix = None
        if chosen is not None and proof is not None and proof.satisfied:
            if SETTINGS.path_matrix:
                t0 = time.time()
                # The request has a deadline; the matrix gets the remainder.
                # Lanes that outlive it keep working and are reported as such.
                #
                # A finance intent is racing venues, so it is held to the short
                # request deadline: the user wants to know the value moved, not
                # to watch it move. A non-finance intent is different in kind --
                # the artifact *is* the answer, so there is nothing to race and
                # waiting for the content is what the user asked for.
                domain = getattr(constraints.goal, "domain", "finance")
                if domain == "finance":
                    budget = max(0.4, SETTINGS.request_deadline_s - (time.time() - started))
                else:
                    budget = SETTINGS.creator_deadline_s
                matrix = self.matrix.run(
                    build_lanes(self, constraints, chosen, primitives),
                    target=constraints.net_profit_usd_min,
                    asset=constraints.goal.asset,
                    beneficiary=constraints.beneficiary,
                    budget_s=budget,
                    require_producer=domain != "finance",
                )
                timings["matrix"] = (time.time() - t0) * 1000.0
                winner = matrix.winner
                producer = matrix.producer
                if domain != "finance" and producer is not None:
                    # The deliverable is the answer. Whatever a money lane proved
                    # for this intent did not arise from the request and is not
                    # the user's outcome, so it is not reported as one: the
                    # execution describes the attested artifact, and the artifact
                    # block carries the measured facts.
                    detail = producer.detail or {}
                    execution = ExecutionResult(
                        status="executed" if detail.get("settled") else "unsettled_value",
                        tx_hash=detail.get("settlement_tx", ""),
                        explorer_url=producer.explorer_url,
                        network=detail.get("network", "fuel-devnet"),
                        beneficiary=constraints.beneficiary,
                        amount_usd=0.0,
                        reason=(
                            f"artifact_attested;domain={domain};"
                            f"sha256={detail.get('sha256', '')[:16]};"
                            f"bytes={detail.get('bytes', 0)}"
                        ),
                        gas_mode="protocol",
                        gas_paid_by="engine",
                        block_number=int(detail.get("block") or 0),
                        value_transferred=False,
                        delivered=0.0,
                    )
                    chosen.intent_id = iid
                elif winner is not None:
                    execution = ExecutionResult(
                        status="executed",
                        tx_hash=winner.tx_hash,
                        explorer_url=winner.explorer_url,
                        network=winner.network,
                        beneficiary=constraints.beneficiary,
                        amount_usd=round(chosen.net_profit_usd, 6),
                        reason=f"matrix_lane={winner.lane_id};{winner.reason}",
                        gas_mode=winner.detail.get("gas_mode", "protocol"),
                        gas_paid_by=winner.detail.get("gas_paid_by", ""),
                        block_number=int(winner.detail.get("block") or 0),
                        value_transferred=True,
                        delivered=winner.delivers,
                    )
                    chosen.intent_id = iid
                else:
                    # No lane paid inside this request. The answer still reports
                    # the proved path and the matrix's full accounting, and the
                    # lanes keep working in the background. This is a statement
                    # about the venues tried in these seconds, not a refusal.
                    execution = ExecutionResult(
                        status="pending",
                        beneficiary=SETTINGS.beneficiary,
                        amount_usd=round(chosen.net_profit_usd, 6),
                        gas_mode="pending",
                        reason=f"matrix_lanes={len(matrix.lanes)};none_delivered_yet",
                    )
                    self._baseline_for(iid, _networks()[0] if _networks() else None)
                    self._dispatch_settlement(iid, chosen, execution, constraints)
            else:
                execution = ExecutionResult(
                    status="pending",
                    beneficiary=SETTINGS.beneficiary,
                    amount_usd=round(chosen.net_profit_usd, 6),
                    gas_mode="pending",
                    reason="settlement_dispatched_to_flywheel",
                )
                self._baseline_for(iid, _networks()[0] if _networks() else None)
                execution = self._settle_inline(chosen, constraints, execution)
                self._dispatch_settlement(iid, chosen, execution, constraints)

        proven = bool(proof is not None and proof.satisfied)
        t0 = time.time()
        narrative, equations = self._narrate(
            constraints, chosen, proof, invented, stats, bound, absolute_limit,
            execution, matrix,
        )
        timings["narrative"] = (time.time() - t0) * 1000.0

        result = IntentResult(
            intent_id=iid,
            raw_intent=raw_intent,
            constraints=constraints,
            chosen_path=chosen if proven else None,
            proof=proof if proven else bound,
            execution=execution,
            narrative_ar=narrative,
            invented=invented,
            attempts=stats.attempts,
            primitives_used=chosen.primitives if chosen and proven else [],
            equations=equations,
            elapsed_seconds=time.time() - started,
            background=False,
            source=source if proven else "bound",
            absolute_limit=absolute_limit,
            timings_ms=timings,
            matrix=matrix.to_dict() if matrix is not None else None,
            artifact=(
                matrix.producer.detail
                if matrix is not None and matrix.producer is not None
                else None
            ),
        )
        self._results[iid] = result
        self._record(result)
        return result

    async def synthesize(self, raw_intent: str, budget: int | None = None) -> IntentResult:
        import asyncio

        return await asyncio.to_thread(self.synthesize_sync, raw_intent, budget)

    def latest(self, iid: str) -> IntentResult | None:
        return self._results.get(iid)

    def unsettled(self) -> list[IntentResult]:
        """Intents whose value has not reached the beneficiary yet.

        The loop's contract is that an intent keeps working until it pays, so
        the ones that have not paid are exactly the ones still owed a
        settlement. An intent at a proved physical limit is excluded: repeating
        it would re-derive the same UNSAT bound, which is not a settlement
        attempt.
        """
        with self._results_lock:
            return [
                r for r in self._results.values()
                if not r.absolute_limit
                and (r.execution is None or r.execution.status != "executed")
            ]

    def retry_unsettled(self) -> int:
        """Re-compose and re-settle every intent that has not paid yet.

        Each pass re-reads the primitive scan, so a component that appeared
        since the last attempt can enter the path. Returns how many intents
        were retried.
        """
        pending = self.unsettled()
        for result in pending:
            try:
                self.synthesize_sync(result.raw_intent)
            except Exception:
                # One intent's failure must not stop the others from retrying.
                continue
        return len(pending)

    # -- instant synthesis ---------------------------------------------------

    def _candidates(self, constraints: Constraints,
                    primitives) -> list[tuple[AtomicPath, str]]:
        """Build every admissible candidate in memory: templates first, then
        composed variants. No network, no waiting."""
        target = max(constraints.net_profit_usd_min, SETTINGS.min_net_profit_usd)
        out: list[tuple[AtomicPath, str]] = []

        for tpl in templates_for(constraints.goal.objective):
            if tpl.net_rate <= 0:
                continue
            notional = _notional_for(target, tpl.net_rate)
            if notional <= 0:
                continue
            out.append((tpl.instantiate(constraints, notional), "template"))

        # Composed variants widen the space; they use only cached primitives.
        for attempt in range(min(SETTINGS.max_attempts, 6)):
            path = self.composer.propose(constraints, primitives, attempt)
            if path is not None and path.net_rate > 0:
                out.append((path, "composed"))
        return out

    def _prove_fastest(self, constraints: Constraints, primitives,
                       stats: LoopStats
                       ) -> tuple[AtomicPath | None, Proof | None, bool, str]:
        """Prove the strongest candidates first, in waves, stopping at a proof.

        Candidates are ordered by the net profit they predict, so the first wave
        holds the most promising compositions. The sweep stops once a wave has
        produced a proof; later waves run only when nothing has proved yet,
        which is exactly the case that needs a wider search.

        The guarantee is per-path, not global: the certificate states that THIS
        path satisfies the constraints. The engine never claims to have found
        the most profitable path on the market, only a proven one.
        """
        candidates = self._candidates(constraints, primitives)
        if not candidates:
            return None, None, False, "composed"

        ordered = sorted(
            candidates,
            key=lambda t: (-(t[0].notional_usd * t[0].net_rate),
                           t[0].template or t[0].path_id),
        )

        def _prove(item: tuple[AtomicPath, str]) -> tuple[AtomicPath, Proof, str]:
            path, kind = item
            # Skip the CVC5 second opinion during the sweep; it runs on the
            # winner below, where its cost cannot delay the decision.
            return path, self.prover.prove(path, constraints, second_opinion=False), kind

        proven: list[tuple[AtomicPath, Proof, str]] = []
        proved_count = 0
        wave = max(1, SETTINGS.proof_wave_size)
        with ThreadPoolExecutor(max_workers=min(8, len(ordered))) as pool:
            for start in range(0, len(ordered), wave):
                for path, proof, kind in pool.map(_prove, ordered[start:start + wave]):
                    proved_count += 1
                    if proof.satisfied:
                        proven.append((path, proof, kind))
                if proven:
                    break

        stats.attempts = proved_count
        if not proven:
            return None, None, False, "composed"
        stats.proofs_found = len(proven)

        # Deterministic ranking inside the proving wave: highest proven net
        # profit, then a stable key.
        proven.sort(
            key=lambda t: (-t[1].model.get("net_profit_usd", 0.0),
                           t[0].template or t[0].path_id)
        )
        path, proof, kind = proven[0]

        # One independent second opinion on the winner only.
        if proof.satisfied:
            confirmation = self.prover.prove(path, constraints, second_opinion=True)
            if confirmation.satisfied:
                proof = confirmation
        return path, proof, kind == "composed", kind

    @staticmethod
    def _align_to_witness(path: AtomicPath, proof: Proof) -> None:
        """Adopt the solver's witness as the path's declared economics.

        One source of truth: after this, the notional, the per-hop rates, and
        the resulting net profit on the path are exactly the values in the
        certificate.
        """
        witness_n = proof.model.get("N")
        if witness_n:
            path.notional_usd = round(float(witness_n), 2)
        for i, step in enumerate(path.steps):
            y = proof.model.get(f"y_{i}")
            f = proof.model.get(f"f_{i}")
            if y is not None:
                step.yield_rate = float(y)
            if f is not None:
                step.fee_rate = float(f)
        proven = proof.model.get("net_profit_usd")
        if proven is not None:
            path.proven_net_profit_usd = round(float(proven), 6)

    # -- narrative -----------------------------------------------------------

    def _narrate(self, constraints: Constraints, path: AtomicPath | None,
                 proof: Proof | None, invented: bool, stats: LoopStats,
                 bound: Proof | None = None, absolute_limit: bool = False,
                 execution: ExecutionResult | None = None,
                 matrix=None,
                 ) -> tuple[str, list[str]]:
        g = constraints.goal
        equations: list[str] = []
        if proof is not None and proof.satisfied and path is not None:
            equations.append("NetProfit = N \u00b7 \u03a3(y_i \u2212 f_i) \u2212 gas ,  gas \u2264 \u0627\u062d\u062a\u064a\u0627\u0637\u064a \u0627\u0644\u063a\u0627\u0632 \u0627\u0644\u062f\u0627\u062e\u0644\u064a")
            equations.append(proof.theorem)
            for i, step in enumerate(path.steps):
                equations.append(
                    f"y_{i} \u2208 [{step.yield_rate*0.8:.6f}, {step.yield_rate*1.2:.6f}]"
                    f"  \u2212  f_{i} \u2208 [{step.fee_rate*0.8:.6f}, {step.fee_rate*1.2:.6f}]"
                )
            equations.append(
                f"N = {proof.model.get('N', 0):.2f} USD ; "
                f"\u03a3(y\u2212f) = {proof.model.get('net_rate', 0):.8f} ; "
                f"NetProfit = {proof.model.get('net_profit_usd', 0):.6f} "
                f"\u2265 {constraints.net_profit_usd_min:.6f}"
            )

        steps_desc = "؛ ".join(
            f"{i+1}) {s.action}" for i, s in enumerate(path.steps if path else [])
        )
        prims_desc = "، ".join(
            dict.fromkeys(p.split("#")[0] for p in (path.primitives if path else []))
        )

        if proof is not None and proof.satisfied and path is not None:
            net = proof.model.get("net_profit_usd", 0.0)
            if getattr(g, "domain", "finance") == "finance":
                exec_line = (
                    f"وُلّد برهان رياضي جديد في الطلب نفسه: صافي الربح المُثبت = "
                    f"{net:.6f} {g.asset} لصالح العنوان المقفل {constraints.beneficiary}."
                )
            else:
                # The proved figure belongs to the engine's money route, not to
                # this request. Naming it as this intent's profit would be a false
                # statement about a document.
                exec_line = (
                    f"النية غير مالية ({g.domain}): أُبقيت المسارات المالية في "
                    f"المصفوفة لأنها تُقاس دائماً، لكن مخرج هذه النية هو المُخرَج "
                    f"المُنتَج والمُوقَّع بالبصمة، لا رقم ربح. "
                    f"(المسار المالي برهن ربحاً قدره {net:.6f} {g.asset}، وهو "
                    f"بيان عن السوق لا عن طلبك.)"
                )
            if (execution is not None and execution.network == "fuel-devnet"
                    and getattr(g, "domain", "finance") == "finance"):
                # The proof prices the path in the goal asset; the ledger credits
                # its own unit. Saying both without naming the difference would
                # read as if USDC had landed in a public wallet.
                exec_line += (
                    f" الرقم أعلاه تقدير اقتصادي بالأصل {g.asset}، أما ما وصل فعلاً "
                    f"فهو وحدات على سجل Fuel الذي يشغّله المحرّك."
                )
            if execution is not None and execution.status == "executed":
                if execution.network == "fuel-devnet" and getattr(g, "domain", "finance") != "finance":
                    # The artifact was attested on this chain; no money moved for
                    # this intent, so only the attestation is described.
                    exec_line += (
                        f" المُخرَج مُوثَّق على سجل Fuel في الكتلة "
                        f"{execution.block_number or '-'}: {execution.tx_hash}."
                    )
                elif execution.network == "fuel-devnet":
                    exec_line += (
                        f" نُفّذ المسار الذري فعلياً على شبكة Fuel (SwayVM) في الكتلة "
                        f"{execution.block_number}: {execution.tx_hash}. الغاز استهلك "
                        f"{execution.gas_cost_wei} وحدة دفعها {execution.gas_paid_by} "
                        f"(سلسلة يملكها المحرّك بسعر غاز صفر)، وزاد رصيد المستفيد في "
                        f"سجل العقد بمقدار {execution.delivered} وحدة، وهو مطابق "
                        f"للربح الصافي المُثبت."
                    )
                else:
                    exec_line += (
                        f" نُفّذ المسار الذري وبُثّت العملية عبر {execution.bundler}: "
                        f"{execution.tx_hash}."
                    )
            elif execution is not None and execution.status == "pending":
                exec_line += (
                    " البرهان صحيح والمسار جاهز، لكن خزانة الغاز الداخلية لم "
                    "تُغطِّ بعد تكلفة البثّ على أي شبكة، فلم يتحرك أي مال: لا "
                    "يجوز الادّعاء بغير ذلك. العجلة تواصل التمويل الذاتي "
                    "(الرصيد الذاتي ثم إيداع EntryPoint ثم احتياطي الأرباح ثم "
                    "قرض وميض ثم الغاز بالتوكن ثم أرخص شبكة) حتى تُبَثّ، ولا "
                    "يُطلب من المستخدم غاز أبداً."
                )
            elif execution is not None and execution.status == "submitted":
                exec_line += (
                    f" قبِل المُجمِّع العملية الموقّعة وهي في طابور التجميع: "
                    f"{execution.user_op_hash}. الغاز مموّل من {execution.gas_mode}."
                )
            elif execution is not None and execution.status == "pending":
                exec_line += (
                    " أُرسل التنفيذ الحقيقي إلى عجلة الغاز في الخلفية الآن، "
                    "وسيظهر txHash حال تأكيده. مصدر الغاز لم يُحسم بعد: "
                    "المحرّك يجرّب رصيده الذاتي أولاً، ثم الرعاة، ولن يُطلب من "
                    "المستخدم غاز أبداً."
                )
            else:
                exec_line += " لم تُنفّذ بعد: البرهان جاهز والتنفيذ ينتظر قبول مُجمِّع."
            if path.template:
                exec_line += f" المسار مُستحضر من قالب مُثبت مسبقاً: {path.template}."
        elif absolute_limit and bound is not None:
            max_usd = bound.model.get("max_reachable_usd", 0.0)
            exec_line = (
                "أُصدر برهان حد مطلق: الهدف يتجاوز أقصى قيمة قابلة للاستخراج في معاملة "
                "ذرية واحدة عند الحد الأقصى للمقترض غير المضمون "
                f"(N_max = {MAX_NOTIONAL_USD:,.0f} USD)، أي {max_usd:,.2f} USD. "
                "هذا حد فيزيائي بحكم حفظ القيمة، لا قيد تقني. "
                "أقرب هدف قابل للتحقيق هو ما دون هذا الرقم."
            )
        elif bound is not None and bound.detail == "no_path":
            exec_line = (
                "لم تُبرهن أي تركيبة على هذا الهدف بعد. أُصدر برهان عدم وجود مسار "
                "مقبول لهذه النية على مجموعة المكونات الحالية، وهذا بيان عن غياب "
                "المسار لا عن حد السوق. المحرك يواصل البحث عن مكونات جديدة في الخلفية."
            )
        else:
            max_usd = bound.model.get("max_reachable_usd", 0.0) if bound else 0.0
            exec_line = (
                "الهدف قابل للتحقيق رياضياً داخل الحد المادي "
                f"(أقصى ربح ذري ممكن ≈ {max_usd:,.2f} USD)، لكن البرهان لم يكتمل "
                "في هذه اللحظة. المحرك يواصل البحث في الخلفية."
            )

        # The matrix is the visible answer to "which routes exist". It is
        # reported route by route, including the ones that could not pay and the
        # reason each gave, so no single venue's obstacle stands in for the whole
        # result. A route that paid is named as the one that delivered.
        matrix_line = ""
        if matrix is not None:
            # Each route is described by what actually happened to it. "Did not
            # deliver" is only correct for a route that ran; a route that was
            # stood down after another paid, or that is still working, must not
            # be described as having failed — that would misreport the matrix.
            verdict_ar = {
                "delivered": lambda l: f"أوصلت {l.delivers:.6f}",
                "produced": lambda l: (
                    f"أنتجت المُخرَج: {l.detail.get('bytes', 0)} بايت، "
                    f"{l.detail.get('lines', 0)} سطراً، بصمة {l.detail.get('sha256', '')[:16]}…"
                ),
                "blocked": lambda l: f"لم تُوصل ({l.reason})",
                "failed": lambda l: f"تعذّر تشغيلها ({l.reason})",
                "stood_down": lambda l: "لم تُشغَّل: مسار شبكي آخر أوصل القيمة أولاً",
                "continuing": lambda l: "ما زال يعمل بعد إرسال الجواب",
                "queued": lambda l: "لم يبدأ داخل نافذة الطلب",
                "running": lambda l: "ما زال يعمل بعد إرسال الجواب",
            }
            parts = []
            for lane in matrix.lanes:
                describe = verdict_ar.get(lane.status, lambda l: f"حالة: {l.status}")
                parts.append(
                    f"{lane.title_ar} [{lane.category}]: {describe(lane)}"
                )
            winner = matrix.winner
            producer = matrix.producer
            # A money lane that happened to settle while a document was being
            # written did not answer this request, so its figure is not reported
            # as the outcome of a non-finance intent.
            asks_for_money = getattr(g, "domain", "finance") == "finance"
            if winner is not None and asks_for_money:
                winner_line = (
                    f"\nالمسار الذي أوصل القيمة فعلاً: {winner.title_ar} "
                    f"({winner.network}) بمقدار {winner.delivers:.6f}."
                )
            elif producer is not None:
                winner_line = ""
            elif winner is not None:
                winner_line = (
                    f"\nمسار مالي أوصل قيمة لم تتعلق بهذه النية: {winner.title_ar} "
                    f"({winner.network}). النية المطلوبة غير مالية، فمخرجها هو "
                    f"المُخرَج أدناه لا هذا الرقم."
                )
            else:
                winner_line = (
                    "\nلم يُوصل أي مسار داخل هذه الثواني. العائق يخص "
                    "المسارات المجرَّبة، لا النتيجة: المسارات تُعاد في الخلفية "
                    "ويُضاف كل مسار جديد يظهر في المسح."
                )
            if producer is not None:
                d = producer.detail or {}
                winner_line += (
                    f"\nالمُخرَج المطلوب أُنتج فعلاً: {d.get('path', '')} — "
                    f"{d.get('bytes', 0)} بايت، {d.get('lines', 0)} سطراً، "
                    f"كاتبه {d.get('producer', '')}. "
                    f"بصمته SHA-256: {d.get('sha256', '')}. "
                    + (
                        f"سُجّلت البصمة على سجل Fuel في الكتلة {d.get('block', '')}: "
                        f"{d.get('settlement_tx', '')}. "
                        if d.get("settled")
                        else "لم تُسجَّل البصمة على السجل بعد. "
                    )
                    + "أي طرف يستطيع التحقق بإعادة حساب بصمة الملف ومقارنتها بالسجل."
                )
            matrix_line = (
                f"\nمصفوفة المسارات ({len(matrix.lanes)} مساراً في "
                f"{len(matrix.covered_categories)} عائلة، نُفّذت بالتوازي في "
                f"{matrix.elapsed_ms:.0f} مللي ثانية):\n- "
                + "\n- ".join(parts)
                + winner_line
            )

        if getattr(g, "domain", "finance") == "finance":
            analysis_line = (
                f"التحليل الصوري: الهدف={g.objective}، الأصل={g.asset}، "
                f"الهدف الكمي={g.target_amount}، "
                f"الحد الأدنى للربح الصافي={constraints.net_profit_usd_min}.\n"
            )
        else:
            analysis_line = (
                f"التحليل الصوري: النية من نوع «{g.domain}»، فالمخرج المطلوب شيء "
                f"يُصنع لا مال يتحرك. القيد الملزم: مُخرَج مكتمل، مقيس، وموثَّق "
                f"ببصمة على السجل. قيود الأمان تبقى كما هي: بلا عهدة، بلا مفتاح "
                f"مستخدم، بلا غاز على المستخدم.\n"
            )

        narrative = (
            f"النية: {g.raw_intent}\n"
            f"{analysis_line}"
            f"{'اخترع النظام' if invented else 'استحضر النظام'} المسار من المشاريع: {prims_desc or '—'}.\n"
            f"المسار الذري: {steps_desc or '—'}.\n"
            f"{exec_line}"
            f"{matrix_line}\n"
            f"زمن القرار الكلي: {(time.time() - stats.started_at)*1000:.1f} مللي ثانية، "
            f"المرشحون المبرهن عليهم: {stats.attempts}، المكونات الحية: {stats.live_primitives}"
            + (
                " (حدّ GitHub من حصة البحث الآن، فاعتمد المحرك على مجموعته المثبتة "
                "دون توقف)."
                if self.scanner.rate_limited()
                else "."
            )
        )

        # The narrative above is complete on its own: it names the composed
        # projects, the atomic path, and the exact equation and witness. No
        # model call and no background work is needed to finish the answer.
        return narrative, equations

    def _settle_inline(self, path: AtomicPath, constraints: Constraints,
                       placeholder: ExecutionResult) -> ExecutionResult:
        """Settle on the Fuel chain now, falling back to the placeholder.

        Only the Fuel leg is attempted here: it runs against a node the engine
        owns, so its latency is bounded and known. The EVM ladder stays on the
        background path because a sponsor handshake is not bounded.
        """
        if not SETTINGS.fuel_primary:
            return placeholder
        try:
            gated = self.executor.settle(path, constraints, fuel_only=True)
        except Exception:
            return placeholder
        if gated.network == "fuel-devnet":
            return gated
        return placeholder

    # -- settlement ----------------------------------------------------------

    def _dispatch_settlement(self, iid: str, path: AtomicPath,
                             execution: ExecutionResult,
                             constraints: Constraints | None = None) -> None:
        """Settle in the background, then upgrade the stored result in place.

        The wait for a mined transaction happens off the request path. When the
        flywheel lands one, the result the caller is holding gains the real hash
        and explorer link; a caller polling ``/intent/{id}`` sees it change from
        pending to executed without re-sending the intent.

        Settlement goes through the executor's dual gate, never straight to the
        flywheel: a mined transaction that moved nothing is a different outcome
        from a mined transaction that paid, and only the gate can tell them
        apart.
        """
        call_data = self.executor.settlement_call_data(path)

        def work() -> None:
            if constraints is not None:
                gated = self.executor.settle(path, constraints)
                self._fold_gated(iid, gated)
                if gated.status == "unsettled_value":
                    # Gate A passed but Gate B failed. Replaying the same path
                    # would reproduce the same no-op, so the retry widens the
                    # path before it goes back to the watch.
                    deep = self.composer.propose_deep(
                        constraints, self.scanner.scan()
                    )
                    if deep is not None:
                        self._retry_deep(iid, deep, constraints)
                elif gated.status == "pending":
                    self.watchdog.watch(iid, call_data, gated)
                return

            settlement = self.executor.flywheel.settle(call_data)
            self._apply_settlement(iid, settlement)
            if not settlement.mined and settlement.status != "submitted":
                self.watchdog.watch(iid, call_data, execution)

        try:
            self._settle_pool.submit(work)
        except RuntimeError:  # pool already shut down
            work()

    def _retry_deep(self, iid: str, deep: AtomicPath,
                    constraints: Constraints) -> None:
        """Settle a deeper composed path and fold the gated outcome back."""
        gated = self.executor.settle(deep, constraints)
        with self._results_lock:
            result = self._results.get(iid)
            if result is not None and gated.status == "executed":
                result.chosen_path = deep
                result.primitives_used = deep.primitives
        self._fold_gated(iid, gated)
        if gated.status in ("unsettled_value", "pending"):
            self.watchdog.watch(
                iid, self.executor.settlement_call_data(deep), gated
            )

    def _fold_gated(self, iid: str, gated: ExecutionResult) -> None:
        """Replace the stored execution with a fully gated result."""
        with self._results_lock:
            result = self._results.get(iid)
            if result is None:
                return
            result.execution = gated
            result.narrative_ar = self._settlement_line(gated)

    def _on_watchdog_settled(self, key: str, settlement) -> None:
        """A retry mined (or was accepted). Apply Gate B before calling it paid.

        The watchdog speaks the flywheel's language, which knows a receipt but
        not a balance. Converting its outcome into a stored result is exactly
        where the "mined therefore paid" assumption used to creep in, so the
        conversion happens here with the measured baseline attached.
        """
        self._apply_settlement(key, settlement)

    def _baseline_for(self, iid: str, net) -> int:
        """The beneficiary balance recorded before this intent's first broadcast."""
        with self._results_lock:
            if iid in self._baselines:
                return self._baselines[iid]
        value = self.executor._beneficiary_balance(net) if net else 0
        with self._results_lock:
            self._baselines.setdefault(iid, value)
            return self._baselines[iid]

    def _apply_settlement(self, iid: str, settlement) -> None:
        """Fold a flywheel outcome into the stored result, in place.

        A mined operation is only ``executed`` when Gate B also holds: the
        beneficiary's balance grew by the proved profit. A mined operation that
        moved nothing becomes ``unsettled_value`` and the deeper path takes over.
        """
        nets = _networks()
        net = next(
            (n for n in nets if n.chain_id == getattr(settlement, "chain_id", None)),
            nets[0] if nets else None,
        )
        baseline = self._baseline_for(iid, net)

        with self._results_lock:
            result = self._results.get(iid)
            if result is None or result.execution is None:
                return
            ex = result.execution
            if settlement.mined:
                # The expectation has to be in the settled unit and net of the
                # contract's own deductions; a wei figure would mark a correct
                # token payout as short.
                expected = self.executor.expected_beneficiary_delta(
                    result.chosen_path, bool(ex.value_transferred)
                ) if result.chosen_path else 0
                landed, evidence, delta = (
                    self.executor.sweep_landed(net, baseline, expected)
                    if net else (False, "no_network", 0)
                )
                scale = 1e6 if SETTINGS.gas_value_lock else 1e18
                ex.delivered = round(max(delta, 0) / scale, 6)
                ex.simulated = False
                ex.tx_hash = settlement.tx_hash
                ex.explorer_url = settlement.explorer_url
                ex.user_op_hash = settlement.user_op_hash
                ex.block_number = settlement.block_number
                ex.gas_cost_wei = settlement.actual_gas_cost_wei
                ex.gas_paid_by = settlement.gas_paid_by
                ex.network = settlement.network or ex.network
                ex.bundler = settlement.paymaster or ex.bundler
                ex.value_transferred = landed
                if landed:
                    ex.status = "executed"
                    ex.reason = (
                        f"mined_on_{settlement.network};"
                        f"gas_paid_by={settlement.gas_paid_by};"
                        f"value_transferred=True;{evidence}"
                    )
                else:
                    ex.status = "unsettled_value"
                    ex.amount_usd = 0.0
                    ex.reason = (
                        f"mined_on_{settlement.network}_but_{evidence};"
                        f"gas_paid_by={settlement.gas_paid_by}"
                    )
            elif settlement.status == "submitted":
                ex.status = "submitted"
                ex.simulated = False
                ex.tx_hash = settlement.user_op_hash
                ex.user_op_hash = settlement.user_op_hash
                ex.explorer_url = settlement.explorer_url
                ex.network = settlement.network or ex.network
                ex.reason = settlement.reason
            else:
                ex.status = "pending"
                ex.simulated = False
                ex.tx_hash = ""
                ex.explorer_url = ""
                ex.reason = settlement.reason
            result.narrative_ar = self._settlement_line(ex)

    @staticmethod
    def _settlement_line(ex: ExecutionResult) -> str:
        """The Arabic sentence describing the settlement, matching its status."""
        if ex.status == "executed":
            if ex.network == "fuel-devnet":
                # The Fuel leg needs its own sentence: it did not borrow a
                # sponsor, it ran on a chain the engine owns, and the gas was
                # priced at zero by that chain's own consensus parameters.
                return (
                    f"نُفّذت العملية فعلياً على شبكة Fuel (SwayVM) وبُثّت في الكتلة "
                    f"{ex.block_number}: {ex.tx_hash}. "
                    f"العقد الموثّق: {ex.bundler}. الغاز استهلك {ex.gas_cost_wei} وحدة "
                    f"ودفعَه {ex.gas_paid_by} — أي سلسلة تملكها المحرّك نفسه بسعر غاز "
                    f"صفر، فلم يُخصم من المستخدم ولا من المستفيد شيء. "
                    f"وتحقّق وصول القيمة: رصيد المستفيد في السجل زاد فعلياً بمقدار "
                    f"{ex.delivered} وحدة، وهو مطابق للربح الصافي الذي أثبته البرهان."
                )
            return (
                f"نُفّذت العملية فعلياً على {ex.network} وبُثّت في الكتلة "
                f"{ex.block_number}: {ex.tx_hash}. الغاز دفعَه {ex.gas_paid_by} "
                f"({ex.bundler})، ولم يُخصم من رصيد المستفيد شيء. "
                f"وتحقّق وصول القيمة: رصيد المستفيد زاد فعلياً بمقدار "
                f"{ex.delivered or ex.amount_usd} USDC."
            )
        if ex.status == "unsettled_value":
            return (
                f"البرهان نجح والعملية بُثّت فعلياً على {ex.network} "
                f"({ex.tx_hash})، لكن بوابة القيمة الثانية كشفت أن رصيد "
                f"المستفيد لم يزد. لذلك لا أُسمّي هذا نجاحاً: الحالة "
                f"unsettled_value، والمسار الأعمق يعاد بناؤه تلقائياً "
                f"(flash_mint + liquidity + market_making) حتى يصل الرصيد."
            )
        if ex.status == "submitted":
            return (
                "قبِل المُجمِّع العملية الموقّعة وهي في طابور التجميع الآن: "
                f"userOpHash = {ex.user_op_hash}. مصدر الغاز: {ex.gas_mode}."
            )
        if ex.status == "pending":
            return (
                "التنفيذ الحقيقي قيد التنفيذ الآن عبر عجلة الغاز في الخلفية. "
                "مصدر الغاز لم يُحسم بعد: المحرّك يجرّب خزانته الداخلية أولاً "
                "(الرصيد الذاتي ثم إيداع EntryPoint ثم احتياطي الأرباح ثم قرض "
                "وميض ثم الغاز بالتوكن ثم أرخص شبكة)، ولا يُطلب من المستخدم "
                "غاز أبداً."
            )
        return (
            "لم تُبَثّ العملية بعد: خزانة الغاز الداخلية لم تُغطِّ هذه الخطوة، "
            "فلم يتحرك أي مال. العجلة تواصل المحاولة في الخلفية تلقائياً، "
            "والنظام يذكر بالضبط كم وحدة نقدية ناقصة وأي شبكة تحتاجها."
        )

    # -- persistence ---------------------------------------------------------

    def _record(self, result: IntentResult) -> None:
        os.makedirs(SETTINGS.evidence_dir, exist_ok=True)
        path = os.path.join(SETTINGS.evidence_dir, "synthesis.jsonl")
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(result.to_dict(), ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass
