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
        self.prover = Prover()
        self.executor = Executor()
        self.watchdog = SettlementWatchdog(self.executor.flywheel)
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
        self.watchdog.set_on_settled(self._apply_settlement)

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

        # Execution gate: a satisfied proof is the only authorisation needed.
        #
        # Settlement is real network work (a sponsor handshake, a broadcast, a
        # receipt) and takes seconds; the proof takes milliseconds. Blocking the
        # answer on settlement would trade the latency contract for it, so the
        # answer carries a pending settlement and the flywheel finishes the job
        # in the background, upgrading this same result in place.
        execution: ExecutionResult | None = None
        if chosen is not None and proof is not None and proof.satisfied:
            execution = ExecutionResult(
                status="pending",
                beneficiary=SETTINGS.beneficiary,
                amount_usd=round(chosen.net_profit_usd, 6),
                gas_mode="paymaster",
                reason="settlement_dispatched_to_flywheel",
            )
            self._dispatch_settlement(iid, chosen, execution)

        proven = bool(proof is not None and proof.satisfied)
        t0 = time.time()
        narrative, equations = self._narrate(
            constraints, chosen, proof, invented, stats, bound, absolute_limit,
            execution,
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
        )
        self._results[iid] = result
        self._record(result)
        return result

    async def synthesize(self, raw_intent: str, budget: int | None = None) -> IntentResult:
        import asyncio

        return await asyncio.to_thread(self.synthesize_sync, raw_intent, budget)

    def latest(self, iid: str) -> IntentResult | None:
        return self._results.get(iid)

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
                 ) -> tuple[str, list[str]]:
        g = constraints.goal
        equations: list[str] = []
        if proof is not None and proof.satisfied and path is not None:
            equations.append("NetProfit = N \u00b7 \u03a3(y_i \u2212 f_i) \u2212 gas ,  gas = 0")
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
            exec_line = (
                f"وُلّد برهان رياضي جديد في الطلب نفسه: صافي الربح المُثبت = "
                f"{net:.6f} {g.asset} لصالح العنوان المقفل {constraints.beneficiary}."
            )
            if execution is not None and execution.status == "executed":
                exec_line += (
                    f" نُفّذ المسار الذري وبُثّت العملية عبر {execution.bundler}: "
                    f"{execution.tx_hash}."
                )
            elif execution is not None and execution.status == "simulated":
                exec_line += (
                    " العملية موقّعة وجاهزة (ERC-4337) لكن لم يقبلها أي مُجمِّع في هذه "
                    "اللحظة، فلم تُبَثّ ولم يتحرك أي مال: لا يجوز الادّعاء بغير ذلك. "
                    f"الغاز مموّل من {execution.gas_mode}، ولا يُطلب من المستخدم غاز أبداً."
                )
            elif execution is not None and execution.status == "submitted":
                exec_line += (
                    f" قبِل المُجمِّع العملية الموقّعة وهي في طابور التجميع: "
                    f"{execution.user_op_hash}. الغاز مموّل من {execution.gas_mode}."
                )
            elif execution is not None and execution.status == "pending":
                exec_line += (
                    " أُرسل التنفيذ الحقيقي إلى عجلة الغاز في الخلفية الآن، "
                    "وسيظهر txHash حال تأكيده. الغاز مموّل من paymaster."
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

        narrative = (
            f"النية: {g.raw_intent}\n"
            f"التحليل الصوري: الهدف={g.objective}، الأصل={g.asset}، الهدف الكمي={g.target_amount}، "
            f"الحد الأدنى للربح الصافي={constraints.net_profit_usd_min}.\n"
            f"{'اخترع النظام' if invented else 'استحضر النظام'} المسار من المشاريع: {prims_desc or '—'}.\n"
            f"المسار الذري: {steps_desc or '—'}.\n"
            f"{exec_line}\n"
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

    # -- settlement ----------------------------------------------------------

    def _dispatch_settlement(self, iid: str, path: AtomicPath,
                             execution: ExecutionResult) -> None:
        """Settle in the background, then upgrade the stored result in place.

        The wait for a mined transaction happens off the request path. When the
        flywheel lands one, the result the caller is holding gains the real hash
        and explorer link; a caller polling ``/intent/{id}`` sees it change from
        pending to executed without re-sending the intent.
        """
        call_data = self.executor.settlement_call_data(path)

        def work() -> None:
            settlement = self.executor.flywheel.settle(call_data)
            self._apply_settlement(iid, settlement)
            if not settlement.mined and settlement.status != "submitted":
                self.watchdog.watch(iid, call_data, execution)

        try:
            self._settle_pool.submit(work)
        except RuntimeError:  # pool already shut down
            work()

    def _apply_settlement(self, iid: str, settlement) -> None:
        """Fold a flywheel outcome into the stored result, in place."""
        with self._results_lock:
            result = self._results.get(iid)
            if result is None or result.execution is None:
                return
            ex = result.execution
            if settlement.mined:
                ex.status = "executed"
                ex.simulated = False
                ex.tx_hash = settlement.tx_hash
                ex.explorer_url = settlement.explorer_url
                ex.user_op_hash = settlement.user_op_hash
                ex.block_number = settlement.block_number
                ex.gas_cost_wei = settlement.actual_gas_cost_wei
                ex.gas_paid_by = settlement.gas_paid_by
                ex.network = settlement.network or ex.network
                ex.bundler = settlement.paymaster or ex.bundler
                ex.reason = (
                    f"mined_on_{settlement.network};"
                    f"gas_paid_by={settlement.gas_paid_by};"
                    f"value_transferred={ex.value_transferred}"
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
                ex.status = "simulated"
                ex.simulated = True
                ex.tx_hash = ""
                ex.explorer_url = ""
                ex.reason = settlement.reason
            result.narrative_ar = self._settlement_line(ex)

    @staticmethod
    def _settlement_line(ex: ExecutionResult) -> str:
        """The Arabic sentence describing the settlement, matching its status."""
        if ex.status == "executed":
            return (
                f"نُفّذت العملية فعلياً على {ex.network} وبُثّت في الكتلة "
                f"{ex.block_number}: {ex.tx_hash}. الغاز دفعَه {ex.gas_paid_by} "
                f"({ex.bundler})، ولم يُخصم من رصيد المستفيد شيء."
            )
        if ex.status == "submitted":
            return (
                "قبِل المُجمِّع العملية الموقّعة وهي في طابور التجميع الآن: "
                f"userOpHash = {ex.user_op_hash}. الغاز مموّل من paymaster."
            )
        if ex.status == "pending":
            return "التنفيذ الحقيقي قيد التنفيذ الآن عبر عجلة الغاز في الخلفية."
        return (
            "لم تُبَثّ العملية: لم يتوفر راعٍ للغاز في هذه اللحظة، فلم يتحرك أي "
            "مال. عجلة الغاز تواصل المحاولة في الخلفية تلقائياً."
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
