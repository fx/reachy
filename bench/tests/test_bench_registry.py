"""Selection and containment: what runs, what is reported, what is not attempted.

REQ-072 is the requirement under test. A benchmark that needs a physical robot
is not in the default selection and is reported as excluded; naming it is what
selects it. The scenario — a continuous integration runner with no robot — is
the first test below, and what it asserts is that the run completes, attempts no
hardware measurement, and reports no skip as a failure.

The other half is containment. A benchmark that raises must cost its own
measurements and nothing else, because the remaining numbers are still worth
having and a run that died mid-suite writes no document at all.

No test here performs any input or output: every benchmark is a function that
returns a result.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from bench_support import make_benchmark, make_context, make_run

from reachy_bench.benchmarks import SUITE
from reachy_bench.registry import (
    BenchmarkSpec,
    Options,
    Selection,
    benchmark_name_problems,
    plan,
    run_selected,
)
from reachy_bench.result import BenchmarkResult, Status

if TYPE_CHECKING:
    from collections.abc import Sequence

_OPTIONS = Options(repository=Path("/nowhere"))


def _spec(name: str, *, hardware: bool = False) -> BenchmarkSpec:
    """Build a benchmark that measures one thing instantly.

    Args:
        name: Its name.
        hardware: Whether it claims to need a robot.

    Returns:
        The specification.
    """
    return BenchmarkSpec(
        name=name,
        summary=f"the {name} benchmark",
        requires_hardware=hardware,
        run=lambda _options: make_benchmark(name, {f"{name}.figure": 1.0}),
    )


def _names(selections: Sequence[Selection]) -> list[str]:
    """List the benchmarks a plan will actually run.

    Args:
        selections: What `plan` decided.

    Returns:
        The names, in order.
    """
    return [one.spec.name for one in selections if one.selected]


def test_the_default_selection_leaves_out_everything_that_needs_a_robot() -> None:
    """REQ-072's scenario: a runner with no robot runs the rest."""
    specs = (_spec("detect"), _spec("robot-load", hardware=True))

    selections = plan(specs)

    assert _names(selections) == ["detect"]
    assert len(selections) == 2


def test_an_excluded_benchmark_is_reported_rather_than_skipped_silently() -> None:
    """A suite that omitted it would look like one that had lost it."""
    specs = (_spec("detect"), _spec("robot-load", hardware=True))

    run = run_selected(plan(specs), _OPTIONS, make_context())

    assert run.statuses() == {
        "detect": Status.MEASURED,
        "robot-load": Status.EXCLUDED,
    }
    excluded = next(one for one in run.benchmarks if one.status is Status.EXCLUDED)
    assert "physical robot" in excluded.reason
    assert excluded.measurements == ()


def test_naming_a_hardware_benchmark_is_what_selects_it() -> None:
    """Being selectable explicitly means a name, not a second switch."""
    specs = (_spec("detect"), _spec("robot-load", hardware=True))

    assert _names(plan(specs, ["robot-load"])) == ["robot-load"]


def test_naming_benchmarks_runs_only_those() -> None:
    """A narrowed run is narrowed, and the rest are out of scope entirely."""
    specs = (_spec("detect"), _spec("pipeline"), _spec("session"))

    selections = plan(specs, ["session", "detect"])

    assert [one.spec.name for one in selections] == ["detect", "session"]


def test_a_name_that_is_not_a_benchmark_is_refused() -> None:
    """A typo that selected nothing would produce an empty run that passed."""
    with pytest.raises(ValueError, match="no such benchmark: detct"):
        plan((_spec("detect"),), ["detct"])


def test_a_benchmark_that_raises_costs_its_own_measurements_and_no_others() -> None:
    """The rest of the run is still worth having."""

    def _explodes(options: Options) -> BenchmarkResult:
        """Fail the way a benchmark with a missing model fails.

        Args:
            options: Unused.

        Returns:
            Nothing; it always raises.

        Raises:
            FileNotFoundError: Always.
        """
        del options
        message = "the model file is not there"
        raise FileNotFoundError(message)

    specs = (
        _spec("detect"),
        BenchmarkSpec(
            name="pipeline",
            summary="",
            requires_hardware=False,
            run=_explodes,
        ),
    )

    run = run_selected(plan(specs), _OPTIONS, make_context())

    assert run.statuses() == {
        "detect": Status.MEASURED,
        "pipeline": Status.FAILED,
    }
    failed = next(one for one in run.benchmarks if one.status is Status.FAILED)
    assert "FileNotFoundError" in failed.reason
    assert "the model file is not there" in failed.reason


def test_an_interrupted_run_is_not_reported_as_a_failed_benchmark() -> None:
    """Somebody stopping a run is not a benchmark that could not measure."""

    def _interrupted(options: Options) -> BenchmarkResult:
        """Stand in for the run being interrupted.

        Args:
            options: Unused.

        Returns:
            Nothing; it always raises.

        Raises:
            KeyboardInterrupt: Always.
        """
        del options
        raise KeyboardInterrupt

    specs = (
        BenchmarkSpec(
            name="detect",
            summary="",
            requires_hardware=False,
            run=_interrupted,
        ),
    )

    with pytest.raises(KeyboardInterrupt):
        run_selected(plan(specs), _OPTIONS, make_context())


def test_a_measurement_outside_its_benchmarks_namespace_is_reported() -> None:
    """The comparison reads the leading segment to attribute a figure.

    A measurement named outside its own namespace would make an excluded
    benchmark's recorded figures look like measurements that had gone missing.
    """
    run = make_run([make_benchmark("detect", {"pipeline.decode": 1.0})])

    (problem,) = benchmark_name_problems(run)

    assert "detect measured 'pipeline.decode'" in problem


def test_a_benchmark_that_measured_nothing_has_no_naming_problem() -> None:
    """An excluded benchmark has no measurements to be misnamed."""
    run = make_run(
        [BenchmarkResult.excluded("robot-load", "needs a physical robot")],
    )

    assert benchmark_name_problems(run) == ()


def test_the_real_suite_declares_two_hardware_benchmarks_and_four_others() -> None:
    """The split the benchmarks spec's table describes, held to."""
    hardware = {spec.name for spec in SUITE if spec.requires_hardware}
    hardware_free = {spec.name for spec in SUITE if not spec.requires_hardware}

    assert hardware == {"photon-to-head", "robot-load"}
    assert hardware_free == {"detect", "pipeline", "session", "footprint"}


def test_every_benchmark_in_the_real_suite_has_a_distinct_name() -> None:
    """A duplicate would make one of the two unselectable."""
    names = [spec.name for spec in SUITE]

    assert len(names) == len(set(names))


def test_the_real_default_selection_attempts_nothing_that_needs_a_robot() -> None:
    """REQ-072 over the suite as it actually is, not over a fixture."""
    selections = plan(SUITE)

    assert _names(selections) == ["detect", "pipeline", "session", "footprint"]


def test_the_options_find_the_committed_fixtures_under_the_repository() -> None:
    """The detection benchmarks reuse change 0005's perception fixtures."""
    options = Options(repository=Path("/example/checkout"))

    assert options.fixtures == Path(
        "/example/checkout/services/groundstation/tests/fixtures/perception",
    )
