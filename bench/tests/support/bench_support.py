"""The builders that the benchmark suite's tests share, so none builds a run by hand.

Every test here is about the harness rather than about a measurement, so what it
needs is a result document with particular numbers in it — not a real one. These
builders make one in a line, which is what keeps a comparison test about the
comparison instead of about twenty lines of scaffolding.

Nothing here reads a clock, a file or a socket.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from reachy_bench.baseline import Baseline, BaselineEntry, Profile
from reachy_bench.context import HostContext, RunContext, SoftwareContext
from reachy_bench.result import BenchmarkResult, Measurement, RunResult, Status, Unit
from reachy_bench.stats import Distribution

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "PROFILE",
    "make_baseline",
    "make_benchmark",
    "make_context",
    "make_distribution",
    "make_measurement",
    "make_run",
]

# The host class every built run claims to have been measured on.
PROFILE = "linux-x86_64-4c"


def make_context(profile: str = PROFILE) -> RunContext:
    """Build a run context with no real host behind it.

    Args:
        profile: The host class to claim.

    Returns:
        The context.
    """
    return RunContext(
        host=HostContext(
            profile=profile,
            system="Linux",
            release="6.0.0",
            machine="x86_64",
            cpu_model="Example Processor 1000",
            cpu_count=4,
            memory_mib=16384,
        ),
        software=SoftwareContext(
            python="3.12.0",
            commit="0" * 40,
            versions={"numpy": "2.1.0"},
        ),
        started_at="2026-08-21T00:00:00+00:00",
    )


def make_distribution(median_ms: float) -> Distribution:
    """Build a distribution centred on one figure.

    Args:
        median_ms: The median, which is the figure a comparison reads.

    Returns:
        The distribution.
    """
    return Distribution.of_seconds(
        [median_ms / 1000.0, median_ms / 1000.0, median_ms / 1000.0],
    )


def make_measurement(
    name: str,
    value: float,
    unit: Unit = Unit.MILLISECONDS,
) -> Measurement:
    """Build one measurement.

    Args:
        name: What it measures.
        value: The figure.
        unit: What the figure is counted in.

    Returns:
        The measurement.
    """
    if unit is Unit.MILLISECONDS:
        return Measurement.timing(name, make_distribution(value))
    return Measurement(name=name, unit=unit, value=value)


def make_benchmark(
    name: str,
    measurements: Mapping[str, float] | None = None,
    *,
    unit: Unit = Unit.MILLISECONDS,
    status: Status = Status.MEASURED,
    reason: str = "",
) -> BenchmarkResult:
    """Build one benchmark's result.

    Args:
        name: The benchmark's name.
        measurements: Measurement name to figure.
        unit: What the figures are counted in.
        status: Whether it measured, was excluded, or failed.
        reason: Why it was excluded or failed.

    Returns:
        The result.
    """
    return BenchmarkResult(
        benchmark=name,
        status=status,
        measurements=tuple(
            make_measurement(key, value, unit)
            for key, value in (measurements or {}).items()
        ),
        reason=reason,
    )


def make_run(
    benchmarks: Sequence[BenchmarkResult],
    profile: str = PROFILE,
) -> RunResult:
    """Build a whole run.

    Args:
        benchmarks: The benchmark results in it.
        profile: The host class it claims to have been measured on.

    Returns:
        The run.
    """
    return RunResult(context=make_context(profile), benchmarks=tuple(benchmarks))


def make_baseline(
    *,
    entries: Mapping[str, float] | None = None,
    artifacts: Mapping[str, float] | None = None,
    tolerances: Mapping[Unit, float] | None = None,
    profile: str = PROFILE,
    gated: bool = True,
    profiles: Mapping[str, Profile] | None = None,
) -> Baseline:
    """Build a baseline.

    Args:
        entries: Timing figures for the profile below.
        artifacts: Size figures, in bytes.
        tolerances: Unit to the fraction a measurement may drift by. Defaults to
            ten per cent for every unit, which is a round number a test's
            arithmetic can be read against.
        profile: The host class the timings are recorded for.
        gated: Whether that profile is judged against.
        profiles: Whole profiles to use instead of building one.

    Returns:
        The baseline.
    """
    built = (
        dict(profiles)
        if profiles is not None
        else {
            profile: Profile(
                name=profile,
                gated=gated,
                description="an example machine",
                entries={
                    name: BaselineEntry(value=value, unit=Unit.MILLISECONDS)
                    for name, value in (entries or {}).items()
                },
            ),
        }
    )
    return Baseline(
        tolerances=(
            dict(tolerances) if tolerances is not None else dict.fromkeys(Unit, 0.10)
        ),
        artifacts={
            name: BaselineEntry(value=value, unit=Unit.BYTES)
            for name, value in (artifacts or {}).items()
        },
        profiles=built,
    )
