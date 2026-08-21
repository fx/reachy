"""The gate: a run against the committed baseline, measurement by measurement.

This is the module the whole change turns on. **A comparison that is wrong
reports green through a real regression**, which is worse than no gate at all,
so it is ordinary pure code with ordinary tests and it takes no arguments it
could get wrong: a parsed run, a parsed baseline, and nothing else.

What it decides, and why each decision is the one it is.

**A measurement above its baseline by more than the stated tolerance is a
regression, and the report names it and by how much.** That is REQ-071's
scenario in one sentence.

**A measurement below its baseline by more than the tolerance is reported as an
improvement and fails nothing.** It is still reported, because an improvement
that nobody expected is the shape a benchmark that stopped measuring anything
has — a stage that started returning early is very fast indeed.

**A measurement with no baseline entry fails.** This is the case that decides
whether the gate keeps working: a benchmark added without recording what it
costs is a measurement nothing will ever compare, and treating it as passing is
exactly how a suite goes quiet without anybody noticing.

**A recorded timing with no measurement fails too**, unless the benchmark that
owns it was excluded or was not selected. A measurement that disappears is
otherwise indistinguishable from one that is fine.

**A recorded timing is only checked when the run took timings at all.** A
size-only run — which is what the image and release workflows produce — has not
lost a measurement, it never took one.

**A recorded size with no measurement does not fail either**, and the asymmetry
against timings is deliberate. Sizes are collected from the change that produces each artifact, so
a run is normally given one of them: the image workflow weighs the variant its
matrix entry built and knows nothing about the other two, and the release
workflow weighs the wheels. A completeness check here would fail every one of
those runs for not having measured somebody else's artifact. What keeps the
recorded set honest instead is a contract test over this repository's own build
definitions — `bench/tests/test_bench_baseline.py` reads the image workflow's
matrix and the `wheels` recipe and holds `artifacts` to exactly the artifacts
they produce — so an entry for something nothing builds any more is a red run
there rather than a gate that quietly stopped covering it.

**An excluded benchmark is neither.** REQ-072 requires the hardware benchmarks
be reported as excluded rather than as passing *or* as failing, so they carry
their own verdict and contribute nothing to the outcome.

**Sizes are compared against the flat `artifacts` set and timings against the
profile for the host class the run happened on.** A byte count does not depend
on the machine that measured it and a millisecond does; see
`reachy_bench.baseline`.

Every quantity this suite measures is one where less is better, which is what
lets a single signed comparison serve all of them. A measurement where more was
better would need a direction recorded beside it, and there is not one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from reachy_bench.baseline import PREDECESSOR_PROFILE
from reachy_bench.result import Status, Unit

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from reachy_bench.baseline import Baseline, BaselineEntry, Profile
    from reachy_bench.result import Measurement, RunResult

__all__ = ["Comparison", "Delta", "Verdict", "compare", "predecessor_lines"]


class Verdict(StrEnum):
    """What the comparison concluded about one measurement or benchmark.

    Attributes:
        OK: Within tolerance of the recorded figure.
        REGRESSED: Worse than the recorded figure by more than the tolerance.
            The only measurement verdict that fails a run.
        IMPROVED: Better by more than the tolerance. Reported, never failed.
        MISSING_BASELINE: Measured, with nothing recorded to compare it to.
        MISSING_MEASUREMENT: Recorded, and this run did not measure it.
        EXCLUDED: The benchmark was deliberately not run.
        FAILED: The benchmark was selected and could not measure.
        UNBASELINED: Nothing has been recorded for this run's class of machine,
            so its timings have nothing comparable to be judged against.
    """

    OK = "ok"
    REGRESSED = "regressed"
    IMPROVED = "improved"
    MISSING_BASELINE = "missing-baseline"
    MISSING_MEASUREMENT = "missing-measurement"
    EXCLUDED = "excluded"
    FAILED = "failed"
    UNBASELINED = "unbaselined"


# The verdicts that fail a run. Written as a set rather than as a chain of
# comparisons in the loop, so "what fails the gate" is one line a reviewer can
# read and a test can assert against.
_FAILING: frozenset[Verdict] = frozenset(
    {
        Verdict.REGRESSED,
        Verdict.MISSING_BASELINE,
        Verdict.MISSING_MEASUREMENT,
        Verdict.FAILED,
    },
)


@dataclass(frozen=True, slots=True, kw_only=True)
class Delta:
    """One line of the comparison.

    Attributes:
        name: The measurement, or the benchmark for a verdict about a whole one.
        verdict: What was concluded.
        measured: What this run got, or `None` when it measured nothing.
        baseline: What is recorded, or `None` when nothing is.
        unit: What both are counted in, or `None` when there is nothing to
            count.
        tolerance: The fraction the measurement was allowed to drift by.
        detail: Why, in a sentence, when the verdict needs one.
    """

    name: str
    verdict: Verdict
    measured: float | None = None
    baseline: float | None = None
    unit: Unit | None = None
    tolerance: float = 0.0
    detail: str = ""

    @property
    def change(self) -> float | None:
        """How far the measurement moved, as a fraction of the baseline.

        Returns:
            Positive for worse and negative for better, or `None` when there is
            no pair to compare — including when the baseline is zero, where a
            fraction of it is not a number anybody can act on.
        """
        if self.measured is None or self.baseline is None or self.baseline == 0.0:
            return None
        return (self.measured - self.baseline) / self.baseline

    @property
    def failed(self) -> bool:
        """Whether this line fails the run.

        Returns:
            True for the verdicts in the failing set.
        """
        return self.verdict in _FAILING

    def describe(self) -> str:
        """Say what happened, in one line, naming the measurement and by how much.

        Returns:
            The line the gate prints. REQ-071's scenario asks the check to name
            the measurement that regressed and by how much, and this is where
            that sentence is built.
        """
        change = self.change
        if self.measured is not None and self.baseline is not None:
            unit = "" if self.unit is None else f" {self.unit.value}"
            movement = (
                "" if change is None else f", {change * 100:+.1f}% against baseline"
            )
            allowed = f" (tolerance {self.tolerance * 100:.0f}%)"
            line = (
                f"{self.verdict.value}: {self.name} measured "
                f"{self.measured:.3g}{unit} against a recorded "
                f"{self.baseline:.3g}{unit}{movement}{allowed}"
            )
        elif self.measured is not None:
            unit = "" if self.unit is None else f" {self.unit.value}"
            line = (
                f"{self.verdict.value}: {self.name} measured {self.measured:.3g}{unit}"
            )
        else:
            line = f"{self.verdict.value}: {self.name}"
        return f"{line} — {self.detail}" if self.detail else line


@dataclass(frozen=True, slots=True, kw_only=True)
class Comparison:
    """What the gate concluded about a whole run.

    Attributes:
        profile: The host class the run's timings were judged against, or an
            empty string when no timing was judged at all — either because
            nothing is recorded for this class of machine, or because the run
            took no timings, which is what a size-only run from the image or
            release workflow is.
        deltas: One line per measurement, per recorded figure that went
            unmeasured, and per benchmark that was excluded or failed.
    """

    profile: str
    deltas: tuple[Delta, ...]

    @property
    def ok(self) -> bool:
        """Whether the run passes the gate.

        Returns:
            True when nothing failed.
        """
        return not any(delta.failed for delta in self.deltas)

    @property
    def failures(self) -> tuple[Delta, ...]:
        """Every line that fails the run.

        Returns:
            The failing deltas, in the order they were produced.
        """
        return tuple(delta for delta in self.deltas if delta.failed)

    def report(self) -> str:
        """Render the whole comparison for a log and a job summary.

        Returns:
            One line per delta, with a closing line saying what the run is
            judged against and whether it passed.
        """
        against = f", timings against {self.profile}" if self.profile else ""
        lines = [f"benchmark comparison against the committed baseline{against}", ""]
        lines.extend(f"  {delta.describe()}" for delta in self.deltas)
        lines.append("")
        if self.ok:
            lines.append("PASS: nothing regressed beyond its stated tolerance.")
        else:
            lines.append(f"FAIL: {len(self.failures)} measurement(s) below the bar.")
        return "\n".join(lines)


def _benchmark_of(name: str) -> str:
    """Name the benchmark a measurement belongs to.

    Measurement names are dotted and the first segment is the benchmark, which
    is what lets a recorded figure be attributed back to the benchmark that
    would have taken it — and therefore lets an excluded benchmark's recorded
    figures be left alone rather than reported as missing.

    Args:
        name: The measurement name.

    Returns:
        The leading segment.
    """
    return name.partition(".")[0]


def _judge(
    name: str,
    measurement: Measurement,
    entry: BaselineEntry,
    tolerance: float,
) -> Delta:
    """Compare one measurement against one recorded figure.

    Args:
        name: The measurement name.
        measurement: What this run got.
        entry: What is recorded.
        tolerance: The fraction it may drift by.

    Returns:
        The delta.
    """
    if measurement.unit is not entry.unit:
        return Delta(
            name=name,
            verdict=Verdict.MISSING_BASELINE,
            measured=measurement.value,
            unit=measurement.unit,
            detail=(
                f"the recorded figure is in {entry.unit.value} and this run "
                f"measured {measurement.unit.value}; they are not the same "
                f"quantity"
            ),
        )
    if not math.isfinite(measurement.value) or not math.isfinite(entry.value):
        # The one direction a gate must never fail in. `nan` compares false
        # against every bound, so a measurement carrying one would be reported
        # as within tolerance however wrong it is; an infinity would be reported
        # as a regression of an amount nobody can read. The readers refuse both
        # at the door, and this is the backstop for a value built in memory
        # rather than read from a document.
        return Delta(
            name=name,
            verdict=Verdict.REGRESSED,
            measured=measurement.value,
            baseline=entry.value,
            unit=entry.unit,
            tolerance=tolerance,
            detail=(
                "one of these is not a finite number, so nothing about them can "
                "be compared; a gate that passed on that would fail open"
            ),
        )
    if entry.value == 0.0:
        # A baseline of zero has no fraction to be within. Anything above it is
        # growth against a figure that recorded none, and the honest answer is
        # to say so rather than to divide.
        verdict = Verdict.REGRESSED if measurement.value > 0.0 else Verdict.OK
        return Delta(
            name=name,
            verdict=verdict,
            measured=measurement.value,
            baseline=entry.value,
            unit=entry.unit,
            tolerance=tolerance,
            detail=(
                "the recorded figure is zero, so any measurement above it is growth"
                if verdict is Verdict.REGRESSED
                else ""
            ),
        )
    change = (measurement.value - entry.value) / entry.value
    if change > tolerance:
        verdict = Verdict.REGRESSED
    elif change < -tolerance:
        verdict = Verdict.IMPROVED
    else:
        verdict = Verdict.OK
    return Delta(
        name=name,
        verdict=verdict,
        measured=measurement.value,
        baseline=entry.value,
        unit=entry.unit,
        tolerance=tolerance,
    )


def _entry_for(
    name: str,
    measurement: Measurement,
    baseline: Baseline,
    profile: Profile | None,
) -> BaselineEntry | None:
    """Find the recorded figure a measurement is judged against.

    Args:
        name: The measurement name.
        measurement: What this run got, whose unit decides where to look.
        baseline: The committed baseline.
        profile: The profile for this run's class of machine, if there is one.

    Returns:
        The entry, or `None` when nothing is recorded for it.
    """
    if measurement.unit is Unit.BYTES:
        return baseline.artifacts.get(name)
    return None if profile is None else profile.entries.get(name)


def _measurement_deltas(
    run: RunResult,
    baseline: Baseline,
    profile: Profile | None,
    measurements: Mapping[str, Measurement],
) -> list[Delta]:
    """Judge every measurement the run produced.

    Args:
        run: The run.
        baseline: The committed baseline.
        profile: The profile for this run's class of machine, if there is one.
        measurements: The run's measurements, indexed by name.

    Returns:
        One delta per measurement.
    """
    del run
    deltas: list[Delta] = []
    for name, measurement in sorted(measurements.items()):
        entry = _entry_for(name, measurement, baseline, profile)
        if entry is None:
            deltas.append(
                Delta(
                    name=name,
                    verdict=(
                        Verdict.UNBASELINED
                        if profile is None and measurement.unit is not Unit.BYTES
                        else Verdict.MISSING_BASELINE
                    ),
                    measured=measurement.value,
                    unit=measurement.unit,
                    detail=(
                        "nothing is recorded for this class of machine; run "
                        "`just bench-record` and commit the profile it prints"
                        if profile is None and measurement.unit is not Unit.BYTES
                        else "nothing is recorded for it, so nothing can be "
                        "compared; add the figure to the committed baseline in "
                        "this pull request"
                    ),
                ),
            )
            continue
        deltas.append(_judge(name, measurement, entry, baseline.tolerance(entry)))
    return deltas


def _unmeasured_deltas(
    entries: Mapping[str, BaselineEntry],
    measurements: Mapping[str, Measurement],
    statuses: Mapping[str, Status],
) -> list[Delta]:
    """Report every recorded timing this run did not measure.

    A recorded figure whose benchmark was excluded or was not selected at all is
    left alone: neither is a measurement that went missing. A recorded figure
    whose benchmark ran and did not produce it is the case this exists for.

    Args:
        entries: The recorded figures in scope.
        measurements: What the run measured, indexed by name.
        statuses: What became of each selected benchmark.

    Returns:
        One delta per recorded figure that should have been measured and was
        not.
    """
    deltas: list[Delta] = []
    for name, entry in sorted(entries.items()):
        if name in measurements:
            continue
        status = statuses.get(_benchmark_of(name))
        if status is not Status.MEASURED:
            continue
        deltas.append(
            Delta(
                name=name,
                verdict=Verdict.MISSING_MEASUREMENT,
                baseline=entry.value,
                unit=entry.unit,
                detail=(
                    f"the {_benchmark_of(name)} benchmark ran and did not "
                    f"produce it; a measurement that disappears is otherwise "
                    f"indistinguishable from one that is fine"
                ),
            ),
        )
    return deltas


#:= docs/specs/benchmarks/index.md#req-071-regression-is-judged-against-a-baseline-recorded-in-the-repository
#:% Continuous integration MUST compare benchmark results against a baseline stored
#:% in version control and fail when a measurement regresses beyond a stated
#:% tolerance.
#
#:= docs/specs/benchmarks/index.md#req-073-artifact-size-is-measured-as-a-tracked-quantity
#:% The suite MUST record the size of each published artifact and treat growth
#:% beyond a stated tolerance as a regression.
def compare(
    run: RunResult,
    baseline: Baseline,
    *,
    require_profile: bool = False,
) -> Comparison:
    """Judge a run against the committed baseline.

    Args:
        run: The run to judge.
        baseline: The committed baseline.
        require_profile: Whether a class of machine with nothing recorded for it
            fails the run. False by default, because a machine nobody has
            measured yet is a fact about the fleet rather than a regression, and
            the run prints the profile to commit. A continuous integration job
            whose runner class *is* recorded passes True, which is what stops
            the timing half of the gate quietly becoming advisory if the class
            label ever moves.

    Returns:
        What the gate concluded.

    Raises:
        ValueError: If two benchmarks measured the same name, which would make
            the comparison gate on whichever came last.
    """
    measurements = run.by_name()
    statuses = run.statuses()
    profile = baseline.profile(run.context.host.profile)
    if profile is not None and not profile.gated:
        # A profile recorded for accountability rather than for gating — the
        # predecessor's, whose host is gone. Treated as absent rather than as a
        # comparison target, because comparing against it would dress the
        # difference between two unrelated machines up as a regression.
        profile = None

    deltas: list[Delta] = []
    # A run that measured only sizes needs no timing profile and is not
    # reported as unbaselined for lacking one: the image and release workflows
    # weigh an artifact without timing anything, and telling them their runner
    # class is unrecorded would be noise about a comparison they never make.
    needs_profile = any(
        measurement.unit is not Unit.BYTES for measurement in measurements.values()
    )
    if profile is None and needs_profile:
        deltas.append(
            Delta(
                name=run.context.host.profile,
                verdict=(
                    Verdict.MISSING_BASELINE if require_profile else Verdict.UNBASELINED
                ),
                detail=(
                    "no timing baseline is recorded for this class of machine; "
                    "`just bench-record` prints the profile to commit"
                ),
            ),
        )

    deltas.extend(_measurement_deltas(run, baseline, profile, measurements))
    # Deliberately not over `baseline.artifacts`: see the module docstring on
    # why a recorded size this run did not weigh is an ordinary partial run
    # rather than a measurement that went missing.
    if profile is not None and needs_profile:
        # `needs_profile` again, and for the same reason: a run that measured
        # nothing but sizes has not lost a timing, it never took one.
        deltas.extend(
            _unmeasured_deltas(profile.entries, measurements, statuses),
        )
    deltas.extend(_benchmark_deltas(run))
    return Comparison(
        profile="" if profile is None or not needs_profile else profile.name,
        deltas=tuple(deltas),
    )


def _benchmark_deltas(run: RunResult) -> Sequence[Delta]:
    """Report the benchmarks that measured nothing, and why.

    Args:
        run: The run.

    Returns:
        One delta per excluded and per failed benchmark. A benchmark that
        measured contributes nothing here: its measurements have already been
        judged one by one.
    """
    deltas: list[Delta] = []
    for benchmark in run.benchmarks:
        if benchmark.status is Status.EXCLUDED:
            deltas.append(
                Delta(
                    name=benchmark.benchmark,
                    verdict=Verdict.EXCLUDED,
                    detail=benchmark.reason,
                ),
            )
        elif benchmark.status is Status.FAILED:
            deltas.append(
                Delta(
                    name=benchmark.benchmark,
                    verdict=Verdict.FAILED,
                    detail=benchmark.reason,
                ),
            )
    return deltas


def predecessor_lines(run: RunResult, baseline: Baseline) -> tuple[str, ...]:
    """Report this run beside the predecessor's hand-measured figures.

    Not a gate and never one: the predecessor's host is gone, so the difference
    between the two numbers is a difference between two machines as much as
    between two implementations. It is printed because it is what the rebuild is
    accountable to, and reading it beside a run is the whole reason the spec
    records it.

    Args:
        run: The run.
        baseline: The committed baseline, holding the predecessor's profile.

    Returns:
        One line per figure the predecessor recorded that this run also
        measured, and an empty tuple when the two sets do not overlap.
    """
    profile = baseline.profiles.get(PREDECESSOR_PROFILE)
    if profile is None:
        return ()
    measurements = run.by_name()
    lines: list[str] = []
    for name, entry in sorted(profile.entries.items()):
        measurement = measurements.get(name)
        if measurement is None or measurement.unit is not entry.unit:
            continue
        movement = (
            ""
            if entry.value == 0.0
            else f" ({(measurement.value - entry.value) / entry.value * 100:+.0f}%)"
        )
        lines.append(
            f"  {name}: this run {measurement.value:.3g} {entry.unit.value}, "
            f"predecessor {entry.value:.3g} {entry.unit.value}{movement}"
            + (f" — {entry.note}" if entry.note else ""),
        )
    return tuple(lines)
