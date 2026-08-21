"""The benchmarks themselves, and the suite that declares them in order.

Four measure with nothing but a container and are the default selection; two
need a physical robot and are reported as excluded unless they are named. See
`reachy_bench.registry` for how that selection is made and the benchmarks spec's
table for what each one is for.
"""

from __future__ import annotations

from reachy_bench.benchmarks.detect import DETECT
from reachy_bench.benchmarks.footprint import FOOTPRINT
from reachy_bench.benchmarks.photon_to_head import PHOTON_TO_HEAD
from reachy_bench.benchmarks.pipeline import PIPELINE
from reachy_bench.benchmarks.robot_load import ROBOT_LOAD
from reachy_bench.benchmarks.session import SESSION
from reachy_bench.registry import BenchmarkSpec

__all__ = [
    "DETECT",
    "FOOTPRINT",
    "PHOTON_TO_HEAD",
    "PIPELINE",
    "ROBOT_LOAD",
    "SESSION",
    "SUITE",
]

# Every benchmark, in the order a run reports them: the hardware-free four
# first, because they are what a pull request sees, and the two that need a
# robot last.
SUITE: tuple[BenchmarkSpec, ...] = (
    DETECT,
    PIPELINE,
    SESSION,
    FOOTPRINT,
    PHOTON_TO_HEAD,
    ROBOT_LOAD,
)
