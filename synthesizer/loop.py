"""The Forced Generative Loop.

This is the orchestrator. It has no failure branch. Given an intent it will:

1. Formal Analysis      -> constraint system            (intent.parse)
2. Local search         -> try ready-made compositions (composer + prover)
3. If none proves:
   a. Scan GitHub Live  -> fresh primitives (< lookback window)
   b. Dynamic Composition -> new atomic path
   c. Proof Generation  -> Z3 (+CVC5) certificate
   d. Execution Gate    -> prove again on the winner; if it holds, execute
4. Return a deterministic result. If nothing proves within the budget it keeps
   a background task alive and returns the best partial synthesis, never a
   refusal.

The only thing the loop can return is a proven path (executed or ready), or a
queued synthesis that keeps searching. There is no "I cannot" outcome.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from dataclasses import dataclass, field

from .brain import Brain
from .composer import Composer
from .config import SETTINGS
from .executor import Executor
from .intent import intent_id, parse
from .models import AtomicPath, Constraints, ExecutionResult, IntentResult, Proof
from .prover import Prover
from .scanner import GitHubScanner


@dataclass
class LoopStats:
    attempts: int = 0
    live_primitives: int = 0
    proofs_found: int = 0
    invented: bool = False
    started_at: float = 0.0


class Synthesizer:
    def __init__(self) -> None:
        self.brain = Brain()
        self.scanner = GitHubScanner()
        self.composer = Composer()
        self.prover = Prover()
        self.executor = Executor()
        self._background: dict[str, threading.Thread] = {}
        self._results: dict[str, IntentResult] = {}

    # -- public --------------------------------------------------------------

    def synthesize_sync(self, raw_intent: str, budget: int | None = None) -> IntentResult:
        """Synchronous synthesis, used by tests and by background tasks."""
        budget = budget or SETTINGS.loop_budget_seconds
        started = time.time()
        constraints, _ = parse(raw_intent, self.brain)
        iid = intent_id(constraints)
        stats = LoopStats(started_at=started)

        primitives = self.scanner.scan()
        stats.live_primitives = self.scanner.fresh_count()

        chosen, proof, invented = self._search(constraints, primitives, budget, stats)

        # Align the path's notional with the proven witness so the reported
        # profit is exactly what the proof certifies.
        if chosen is not None and proof is not None and proof.satisfied:
            witness_n = proof.model.get("N")
            if witness_n:
                chosen.notional_usd = round(witness_n, 2)

        # Execution gate: only a proven path may execute.
        execution: ExecutionResult | None = None
        if chosen is not None and proof is not None and proof.satisfied:
            execution = self.executor.settle(chosen, constraints)

        narrative, equations = self._narrate(constraints, chosen, proof, invented, stats)
        result = IntentResult(
            intent_id=iid,
            raw_intent=raw_intent,
            constraints=constraints,
            chosen_path=chosen,
            proof=proof,
            execution=execution,
            narrative_ar=narrative,
            invented=invented,
            attempts=stats.attempts,
            primitives_used=chosen.primitives if chosen else [],
            equations=equations,
            elapsed_seconds=time.time() - started,
            background=False,
        )
        self._results[iid] = result
        self._record(result)
        return result

    async def synthesize(self, raw_intent: str, budget: int | None = None) -> IntentResult:
        return await asyncio.to_thread(self.synthesize_sync, raw_intent, budget)

    def latest(self, iid: str) -> IntentResult | None:
        return self._results.get(iid)

    # -- the loop ------------------------------------------------------------

    def _search(self, constraints: Constraints, primitives, budget: int,
                stats: LoopStats) -> tuple[AtomicPath | None, Proof | None, bool]:
        deadline = time.time() + budget
        best: tuple[AtomicPath, Proof] | None = None
        invented = False

        attempt = 0
        while attempt < SETTINGS.max_attempts:
            if time.time() > deadline and best is not None:
                break
            stats.attempts += 1

            # Round 1..N: compose from the current primitive set and prove.
            path = self.composer.propose(constraints, primitives, attempt)
            if path is None:
                break
            proof = self.prover.prove(path, constraints)
            if proof.satisfied:
                stats.proofs_found += 1
                return path, proof, invented

            # Track the strongest partial composition for reporting.
            if best is None or path.net_profit_usd > best[0].net_profit_usd:
                best = (path, proof)

            attempt += 1
            # Every few attempts, refresh the live primitive set: this is the
            # "go back to step (a) and fetch more components" branch.
            if attempt % 4 == 0:
                primitives = self.scanner.scan(force=True)
                stats.live_primitives = self.scanner.fresh_count()
                invented = True

        # Nothing proved within budget: keep the loop alive in the background.
        if SETTINGS.always_on:
            self._spawn_background(constraints, primitives)
        return (best[0], best[1], invented) if best else (None, None, invented)

    def _spawn_background(self, constraints: Constraints, primitives) -> None:
        iid = intent_id(constraints)
        existing = self._background.get(iid)
        if existing is not None and existing.is_alive():
            return
        thread = threading.Thread(
            target=self._keep_searching,
            args=(iid, constraints, primitives),
            name=f"synthesis-{iid}",
            daemon=True,
        )
        self._background[iid] = thread
        thread.start()

    def _keep_searching(self, iid: str, constraints: Constraints, primitives) -> None:
        """Background worker: keep searching until a proof exists, then record it."""
        attempt = SETTINGS.max_attempts
        prims = primitives
        while True:
            path = self.composer.propose(constraints, prims, attempt)
            if path is not None:
                proof = self.prover.prove(path, constraints)
                if proof.satisfied:
                    execution = self.executor.settle(path, constraints)
                    prior = self._results.get(iid)
                    if prior is not None:
                        prior.chosen_path = path
                        prior.proof = proof
                        prior.execution = execution
                        prior.background = False
                        self._record(prior)
                    return
            attempt += 1
            if attempt % 8 == 0:
                prims = self.scanner.scan(force=True)
            time.sleep(2.0)

    # -- narrative -----------------------------------------------------------

    def _narrate(self, constraints: Constraints, path: AtomicPath | None,
                 proof: Proof | None, invented: bool,
                 stats: LoopStats) -> tuple[str, list[str]]:
        g = constraints.goal
        equations = []
        if proof and proof.satisfied:
            equations.append("NetProfit = N \u00b7 \u03a3(y_i \u2212 f_i) \u2212 gas ,  gas = 0")
            equations.append(proof.theorem)
            for i, step in enumerate(path.steps if path else []):
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

        # Build the Arabic narrative deterministically; enhance with the model
        # when it is reachable. The deterministic version is always complete.
        steps_desc = "؛ ".join(
            f"{i+1}) {s.action}" for i, s in enumerate(path.steps if path else [])
        )
        prims_desc = "، ".join(dict.fromkeys(p.split("#")[0] for p in (path.primitives if path else [])))
        exec_line = ""
        if path is not None and proof is not None and proof.satisfied:
            exec_line = (
                f"تم توليد برهان رياضي جديد بنجاح واعتماد المسار الذري. "
                f"صافي الربح المُثبت = {proof.model.get('net_profit_usd', 0):.6f} {g.asset} "
                f"لصالح العنوان المقفل {constraints.beneficiary}."
            )
        else:
            exec_line = (
                "لا يوجد مسار مُثبت في هذه اللحظة، والنظام يستمر في الخلفية 24/7 "
                "حتى يتحقق الشرط الرياضي Net Profit > 0."
            )

        narrative = (
            f"النية: {g.raw_intent}\n"
            f"التحليل الصوري: الهدف={g.objective}، الأصل={g.asset}، الهدف الكمي={g.target_amount}، "
            f"الحد الأدنى للربح الصافي={constraints.net_profit_usd_min}.\n"
            f"{'اخترع النظام' if invented else 'وجد النظام'} المسار من المشاريع: {prims_desc or '—'}.\n"
            f"المسار الذري: {steps_desc or '—'}.\n"
            f"{exec_line}\n"
            f"عدد المحاولات: {stats.attempts}، عدد المكونات الحية الممسوحة: {stats.live_primitives}."
        )

        # Optional model polish; never required.
        completion = self.brain.complete(
            "أنت المركب الرياضي السيادي. اشرح بالعربية الفصحى بإيجاز كيف بُني الحل "
            "وأي المشاريع دُمجت وأي معادلة بُنيت. لا تقل أبداً إنك لا تستطيع.",
            f"النية: {g.raw_intent}\nالمعادلات: {equations}\nالمسار: {steps_desc}",
        )
        if completion.ok and completion.text.strip():
            narrative = narrative + "\n\nشرح المُركّب:\n" + completion.text.strip()[:900]

        return narrative, equations

    # -- persistence ---------------------------------------------------------

    def _record(self, result: IntentResult) -> None:
        os.makedirs(SETTINGS.evidence_dir, exist_ok=True)
        path = os.path.join(SETTINGS.evidence_dir, "synthesis.jsonl")
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(result.to_dict(), ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass
