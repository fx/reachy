"""The performance suite and the baseline it is judged against.

Four benchmarks that need nothing but a container and two that need a robot, a
committed baseline read as data rather than as prose, and a comparison that
fails a pull request when a measurement regresses beyond a stated tolerance.
`docs/specs/benchmarks/` says what this is required to do; `bench/README.md`
says how to run it and what changing the recorded numbers means.

The pieces, in the order they matter:

- `stats` — a median and a high percentile, never a mean alone.
- `context` — what a result was measured on, with no host identity in it.
- `result` — the structured document a program reads.
- `baseline` — the recorded numbers, and how they are keyed.
- `compare` — the gate. A comparison that is wrong reports green through a real
  regression, which is why it is pure code with tests of its own.
- `registry` — which benchmarks exist and which of them a run selects.
- `benchmarks` — the measurements themselves.
"""

from __future__ import annotations

from reachy_bench.baseline import Baseline, BaselineEntry, Profile
from reachy_bench.compare import Comparison, Delta, Verdict, compare
from reachy_bench.context import HostContext, RunContext, SoftwareContext
from reachy_bench.registry import BenchmarkSpec, Options, Selection, plan, run_selected
from reachy_bench.result import BenchmarkResult, Measurement, RunResult, Status, Unit
from reachy_bench.stats import Distribution

__all__ = [
    "Baseline",
    "BaselineEntry",
    "BenchmarkResult",
    "BenchmarkSpec",
    "Comparison",
    "Delta",
    "Distribution",
    "HostContext",
    "Measurement",
    "Options",
    "Profile",
    "RunContext",
    "RunResult",
    "Selection",
    "SoftwareContext",
    "Status",
    "Unit",
    "Verdict",
    "compare",
    "plan",
    "run_selected",
]
