"""Which benchmarks exist, which of them a run selects, and what runs them.

Benchmarks REQ-072 is the whole of this module's shape. A benchmark that needs a
physical robot is not in the default selection, and a run that leaves it out
says so — an excluded benchmark appears in the result document with its reason,
because a suite that simply omitted it would look exactly like a suite that had
lost it.

**Naming a benchmark is what selects it.** There is no `--hardware` switch,
because the requirement is that a hardware benchmark be "selectable explicitly"
and an explicit name is that. `reachy_bench run photon-to-head` runs it;
`reachy_bench run` does not and reports why.

**A benchmark that raises is a failed benchmark, not a failed run.** The other
measurements in the run are still worth having, and the comparison fails on the
failure rather than the process dying in the middle of the suite with nothing
written. What is *not* caught is a cancellation or an interrupt: a run somebody
stopped is not a benchmark that failed.

Each benchmark's measurements are named with the benchmark as their first dotted
segment. That is not decoration — `reachy_bench.compare` attributes a recorded
figure back to the benchmark that would have taken it, so an excluded
benchmark's recorded figures are left alone rather than reported as missing.
`benchmark_name_problems` holds the suite to it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from reachy_bench.result import BenchmarkResult, RunResult, Status

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from reachy_bench.context import RunContext

__all__ = [
    "DEFAULT_FRAME",
    "DEFAULT_FRAME_RATE",
    "DEFAULT_ITERATIONS",
    "DEFAULT_SAMPLE_SECONDS",
    "DEFAULT_THREAD_COUNTS",
    "DEFAULT_WARMUP",
    "BenchmarkSpec",
    "Options",
    "Selection",
    "benchmark_name_problems",
    "plan",
    "run_selected",
]

# The committed fixture a detection benchmark runs over unless told otherwise.
#
# One of the perception fixtures change 0005 generates — reused rather than
# duplicated, so the frames the benchmark measures are the frames the perception
# tests assert on, and there is one provenance question rather than two. They are
# drawn by `scripts/generate_perception_fixtures.py` from seeded random draws
# rather than photographed, so their licence is this repository's and there is
# nothing to check.
#
# `scene_full.jpg` and not one of the smaller ones, because it is 640 by 480 —
# the resolution the predecessor captured at, and therefore the only one at
# which a face pass here and the recorded 38 ms are measurements of the same
# amount of work.
DEFAULT_FRAME: Final = "scene_full.jpg"

# The run's defaults, named here rather than left as dataclass field defaults.
# `Options` is a slots dataclass, so its defaults are not readable as class
# attributes, and the command surface needs to print them in its help.
#
# Fifty timed passes and five untimed ones: enough that a median is not one
# scheduling accident, few enough that the whole hardware-free suite is a couple
# of minutes rather than a coffee break.
DEFAULT_ITERATIONS: Final = 50
DEFAULT_WARMUP: Final = 5

# One, two, four, six and eight. The predecessor's curve was measured at one,
# four and six, so those three are here to be comparable with it; two and eight
# are here because a knee at either would be invisible otherwise.
DEFAULT_THREAD_COUNTS: Final = (1, 2, 4, 6, 8)

# The frame rate the recorded robot-load figure was taken at, and how long to
# sample the robot's processors for.
DEFAULT_FRAME_RATE: Final = 10.0
DEFAULT_SAMPLE_SECONDS: Final = 10.0


@dataclass(frozen=True, slots=True, kw_only=True)
class Options:
    """Everything a benchmark needs that is not the code it measures.

    Every field is here rather than read from the environment inside a
    benchmark, so a result's `configuration` block can be assembled from what
    the run was actually told — which is the half of REQ-068 that differs
    between two benchmarks in the same run.

    Attributes:
        repository: The repository root, which is where the committed fixtures
            and the baseline are found.
        models_dir: Where the pinned model files are. `just models` puts them
            there.
        iterations: How many timed passes each timing measurement takes.
        warmup: How many untimed passes precede them, so the first pass's
            allocation and kernel planning are not in the distribution.
        thread_counts: The inference thread counts the detection sweep walks.
            A sweep rather than one value: the knee moves with the host.
        frame: Which committed fixture to measure over.
        artifact_sizes: Paths to the JSON documents `just image-size` and
            `just wheel-size` emit. Sizes are collected from the change that
            produces each artifact rather than rebuilt here — see REQ-073 and
            the change document.
        network: How the link behaved, in the operator's words, for the runs
            that cross one.
        robot: How to run a command on the robot, for the hardware benchmarks.
            `None` off the robot, which is what makes them refuse rather than
            invent a number.
        frame_rate: The frame rate `robot-load` measures the robot under.
        sample_seconds: How long `robot-load` samples for.
        observations_ms: Manually recorded photon-to-head timings, in
            milliseconds. There is no automated stimulus — that is this
            change's stated non-goal — so the measurement is an operator's and
            this is where it arrives.
    """

    repository: Path
    models_dir: Path = Path(".models")
    iterations: int = DEFAULT_ITERATIONS
    warmup: int = DEFAULT_WARMUP
    thread_counts: tuple[int, ...] = DEFAULT_THREAD_COUNTS
    frame: str = DEFAULT_FRAME
    artifact_sizes: tuple[Path, ...] = ()
    network: str = ""
    robot: Callable[[Sequence[str]], str] | None = None
    frame_rate: float = DEFAULT_FRAME_RATE
    sample_seconds: float = DEFAULT_SAMPLE_SECONDS
    observations_ms: tuple[float, ...] = ()

    @property
    def fixtures(self) -> Path:
        """Where the committed perception fixtures are.

        Returns:
            The directory change 0005's generator writes, under the repository
            root this run was pointed at.
        """
        return (
            self.repository
            / "services"
            / "groundstation"
            / "tests"
            / "fixtures"
            / "perception"
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchmarkSpec:
    """One benchmark: what it is called, what it needs, and how to run it.

    Attributes:
        name: How it is selected and the first segment of every measurement it
            produces.
        summary: One line for `reachy_bench list`.
        requires_hardware: Whether it needs a physical robot. The default
            selection leaves these out and reports them as excluded.
        run: What takes the measurements.
    """

    name: str
    summary: str
    requires_hardware: bool
    run: Callable[[Options], BenchmarkResult]


@dataclass(frozen=True, slots=True, kw_only=True)
class Selection:
    """One benchmark and whether this run will actually take it.

    Attributes:
        spec: The benchmark.
        selected: Whether it runs.
        reason: Why it does not, when it does not.
    """

    spec: BenchmarkSpec
    selected: bool
    reason: str = ""


# What an unselected hardware benchmark reports. Written once so the default
# run's document and the `list` output cannot describe the same exclusion two
# different ways.
_HARDWARE_REASON: Final = (
    "needs a physical robot, so it is not in the default selection; "
    "run it deliberately by naming it, through `reachyctl bench` against a "
    "live installation"
)


#:= docs/specs/benchmarks/index.md#req-072-benchmarks-requiring-hardware-are-opt-in
#:% Any benchmark that requires a physical robot MUST be excluded from the default
#:% suite and selectable explicitly.
def plan(
    specs: Sequence[BenchmarkSpec],
    requested: Sequence[str] = (),
) -> tuple[Selection, ...]:
    """Decide what a run will take and what it will merely report.

    Args:
        specs: Every benchmark the suite knows about.
        requested: The names asked for. Empty means the default selection:
            everything that needs no hardware, with the rest reported as
            excluded.

    Returns:
        One selection per benchmark in scope, in the order the suite declares
        them.

    Raises:
        ValueError: If a requested name is not a benchmark. A typo that
            silently selected nothing would produce an empty run that passed.
    """
    known = {spec.name: spec for spec in specs}
    if requested:
        unknown = [name for name in requested if name not in known]
        if unknown:
            message = (
                f"no such benchmark: {', '.join(sorted(unknown))}. "
                f"Known: {', '.join(sorted(known))}"
            )
            raise ValueError(message)
        # Naming a benchmark is what selects it, hardware or not: REQ-072 asks
        # that a hardware benchmark be selectable explicitly, and this is that.
        wanted = set(requested)
        return tuple(
            Selection(spec=spec, selected=True) for spec in specs if spec.name in wanted
        )
    return tuple(
        Selection(
            spec=spec,
            selected=not spec.requires_hardware,
            reason=_HARDWARE_REASON if spec.requires_hardware else "",
        )
        for spec in specs
    )


def run_selected(
    selections: Sequence[Selection],
    options: Options,
    context: RunContext,
) -> RunResult:
    """Take every selected measurement and report every benchmark either way.

    Args:
        selections: What `plan` decided.
        options: What the benchmarks are configured with.
        context: The machine and the versions this run happened on.

    Returns:
        The run, with one result per benchmark in the plan.
    """
    results: list[BenchmarkResult] = []
    for selection in selections:
        if not selection.selected:
            results.append(
                BenchmarkResult.excluded(selection.spec.name, selection.reason),
            )
            continue
        try:
            results.append(selection.spec.run(options))
        except Exception as error:
            # Every ordinary failure, because the other benchmarks' numbers are
            # still worth having and a run that died mid-suite would write
            # nothing at all. `BaseException` is deliberately not caught: an
            # interrupt is somebody stopping the run, which is not a benchmark
            # that failed.
            results.append(
                BenchmarkResult.failed(
                    selection.spec.name,
                    f"{type(error).__name__}: {error}",
                ),
            )
    return RunResult(context=context, benchmarks=tuple(results))


def benchmark_name_problems(run: RunResult) -> tuple[str, ...]:
    """List every measurement whose name does not start with its benchmark.

    `reachy_bench.compare` reads the leading segment of a measurement name to
    decide which benchmark owns a recorded figure, so a measurement named
    outside its own namespace would make an excluded benchmark's recorded
    figures look like measurements that had gone missing. This is the check that
    makes the convention hold; a test runs it over the real suite.

    Args:
        run: A run to inspect.

    Returns:
        One message per offending measurement, empty when the suite is
        consistent.
    """
    problems: list[str] = []
    for benchmark in run.benchmarks:
        if benchmark.status is not Status.MEASURED:
            continue
        prefix = f"{benchmark.benchmark}."
        problems.extend(
            f"{benchmark.benchmark} measured {measurement.name!r}, which is "
            f"outside its own {prefix!r} namespace"
            for measurement in benchmark.measurements
            if not measurement.name.startswith(prefix)
        )
    return tuple(problems)
