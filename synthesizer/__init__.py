"""Sovereign Mathematical Synthesizer.

A deterministic, non-custodial engine that turns an Arabic intent into a
*proven* atomic execution path. It has no failure branch: when no ready-made
solution exists it invents one by scanning live GitHub primitives, composing
them into a single atomic path, and searching for a mathematical proof
(Z3 + CVC5) that the path satisfies the intent's constraints, including
``Net Profit > 0``.

Version 2 answers in the same request. Nothing is deferred to a background
loop: the primitive set, the account identity, and the gas price are resolved
at process start, and every intent is settled by in-memory composition plus a
parallel proof sweep. A median intent is answered in well under a tenth of a
second.
"""

__version__ = "2.0.0"
