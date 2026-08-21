"""The gate, watched failing.

This is the single most important module of tests in this change. A comparison
whose logic is wrong reports green through a real regression, which is worse
than having no gate at all — so every verdict it can reach is exercised here,
including the ones a passing run never produces.

Four of them decide whether the gate keeps working over time:

- a measurement above its baseline beyond the tolerance **fails**, and the line
  names the measurement and by how much;
- a measurement within the tolerance **passes**, so ordinary run-to-run noise
  does not fail an honest change;
- a measurement with no recorded figure **fails**, rather than passing because
  there was nothing to compare it to;
- a recorded figure this run did not measure **fails**, unless the benchmark
  that owns it was excluded.

No test here performs any input or output: every run and every baseline is
built in memory by `bench_support`.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import pytest
from bench_support import (
    PROFILE,
    make_baseline,
    make_benchmark,
    make_measurement,
    make_run,
)

from reachy_bench.baseline import Baseline, BaselineEntry, Profile
from reachy_bench.compare import Verdict, compare, predecessor_lines
from reachy_bench.result import BenchmarkResult, Status, Unit


def _verdicts(comparison: object) -> dict[str, Verdict]:
    """Index a comparison's verdicts by name.

    Args:
        comparison: The comparison to read.

    Returns:
        Delta name to verdict.
    """
    assert hasattr(comparison, "deltas")
    return {delta.name: delta.verdict for delta in comparison.deltas}


def test_a_measurement_within_tolerance_passes() -> None:
    """Ordinary noise is not a regression."""
    run = make_run([make_benchmark("detect", {"detect.face.threads.4": 40.0})])
    baseline = make_baseline(entries={"detect.face.threads.4": 38.0})

    comparison = compare(run, baseline)

    assert comparison.ok
    assert _verdicts(comparison)["detect.face.threads.4"] is Verdict.OK


def test_a_slower_detection_pass_fails_and_says_by_how_much() -> None:
    """REQ-071's scenario, in full.

    The pull request makes detection materially slower; the gate fails, and the
    line it prints names the measurement and the size of the regression.
    """
    run = make_run([make_benchmark("detect", {"detect.face.threads.4": 76.0})])
    baseline = make_baseline(entries={"detect.face.threads.4": 38.0})

    comparison = compare(run, baseline)

    assert not comparison.ok
    (failure,) = comparison.failures
    assert failure.name == "detect.face.threads.4"
    assert failure.verdict is Verdict.REGRESSED
    assert failure.change == pytest.approx(1.0)
    described = failure.describe()
    assert "detect.face.threads.4" in described
    assert "+100.0%" in described
    assert "76" in described
    assert "38" in described


def test_a_measurement_that_improved_is_reported_and_fails_nothing() -> None:
    """An unexpected improvement is worth seeing and is not a failure.

    It is reported because a stage that started returning early is very fast
    indeed, and that is the shape a benchmark which stopped measuring anything
    has.
    """
    run = make_run([make_benchmark("detect", {"detect.face.threads.4": 2.0})])
    baseline = make_baseline(entries={"detect.face.threads.4": 38.0})

    comparison = compare(run, baseline)

    assert comparison.ok
    assert _verdicts(comparison)["detect.face.threads.4"] is Verdict.IMPROVED


def test_a_measurement_with_no_recorded_figure_fails() -> None:
    """A benchmark added without recording its cost must not pass silently.

    This is the case that decides whether the gate keeps working: treating an
    unrecorded measurement as passing is how a suite goes quiet.
    """
    run = make_run(
        [
            make_benchmark(
                "detect",
                {"detect.face.threads.4": 38.0, "detect.face.threads.8": 41.0},
            ),
        ],
    )
    baseline = make_baseline(entries={"detect.face.threads.4": 38.0})

    comparison = compare(run, baseline)

    assert not comparison.ok
    (failure,) = comparison.failures
    assert failure.name == "detect.face.threads.8"
    assert failure.verdict is Verdict.MISSING_BASELINE
    assert "committed baseline" in failure.describe()


def test_a_recorded_figure_this_run_did_not_measure_fails() -> None:
    """A measurement that disappears looks exactly like one that is fine."""
    run = make_run([make_benchmark("detect", {"detect.face.threads.4": 38.0})])
    baseline = make_baseline(
        entries={"detect.face.threads.4": 38.0, "detect.face.threads.1": 93.0},
    )

    comparison = compare(run, baseline)

    assert not comparison.ok
    (failure,) = comparison.failures
    assert failure.name == "detect.face.threads.1"
    assert failure.verdict is Verdict.MISSING_MEASUREMENT


def test_a_recorded_figure_of_an_excluded_benchmark_is_left_alone() -> None:
    """REQ-072: an excluded benchmark reports as excluded, not as a failure.

    Its recorded figures are not missing measurements either — nothing was
    supposed to measure them on a machine with no robot attached.
    """
    run = make_run(
        [
            make_benchmark("detect", {"detect.face.threads.4": 38.0}),
            make_benchmark(
                "robot-load",
                status=Status.EXCLUDED,
                reason="needs a physical robot",
            ),
        ],
    )
    baseline = make_baseline(
        entries={
            "detect.face.threads.4": 38.0,
            "robot-load.cpu_cores": 1.52,
        },
    )

    comparison = compare(run, baseline)

    assert comparison.ok
    verdicts = _verdicts(comparison)
    assert verdicts["robot-load"] is Verdict.EXCLUDED
    assert "robot-load.cpu_cores" not in verdicts


def test_a_recorded_figure_of_a_benchmark_that_was_not_selected_is_left_alone() -> None:
    """Running one benchmark does not fail on the others' recorded figures."""
    run = make_run([make_benchmark("detect", {"detect.face.threads.4": 38.0})])
    baseline = make_baseline(
        entries={"detect.face.threads.4": 38.0, "session.round_trip": 54.0},
    )

    comparison = compare(run, baseline)

    assert comparison.ok
    assert "session.round_trip" not in _verdicts(comparison)


def test_a_benchmark_that_could_not_measure_fails_the_run() -> None:
    """A benchmark that was selected and failed is a broken run."""
    run = make_run(
        [
            BenchmarkResult.failed("session", "the groundstation did not start"),
        ],
    )

    comparison = compare(run, make_baseline())

    assert not comparison.ok
    (failure,) = comparison.failures
    assert failure.name == "session"
    assert failure.verdict is Verdict.FAILED
    assert "did not start" in failure.describe()


def test_a_size_is_judged_against_the_flat_artifact_set() -> None:
    """A byte count does not depend on the machine that weighed it."""
    run = make_run(
        [
            make_benchmark(
                "footprint",
                {"footprint.image.cpu.linux-amd64": 458_535_567.0},
                unit=Unit.BYTES,
            ),
        ],
    )
    baseline = make_baseline(
        artifacts={"footprint.image.cpu.linux-amd64": 458_535_567.0},
    )

    comparison = compare(run, baseline)

    assert _verdicts(comparison)["footprint.image.cpu.linux-amd64"] is Verdict.OK


def test_a_size_is_gated_even_on_a_machine_with_no_recorded_profile() -> None:
    """REQ-073 holds on a class of machine nobody has timed before.

    This is why sizes are a flat set rather than part of a profile: the timing
    half of the gate has nothing to compare against on a new runner, and the
    size half still fires.
    """
    run = make_run(
        [
            make_benchmark(
                "footprint",
                {"footprint.image.cpu.linux-amd64": 900_000_000.0},
                unit=Unit.BYTES,
            ),
        ],
        profile="linux-x86_64-64c",
    )
    baseline = make_baseline(
        artifacts={"footprint.image.cpu.linux-amd64": 458_535_567.0},
    )

    comparison = compare(run, baseline)

    assert not comparison.ok
    failures = {delta.name: delta.verdict for delta in comparison.failures}
    assert failures == {"footprint.image.cpu.linux-amd64": Verdict.REGRESSED}


def test_a_dependency_that_grows_the_image_fails_the_size_tolerance() -> None:
    """REQ-073's scenario: growth is reported and prompts a decision."""
    run = make_run(
        [
            make_benchmark(
                "footprint",
                {"footprint.image.cpu.linux-amd64": 700_000_000.0},
                unit=Unit.BYTES,
            ),
        ],
    )
    baseline = make_baseline(
        artifacts={"footprint.image.cpu.linux-amd64": 458_535_567.0},
        tolerances={Unit.BYTES: 0.02},
    )

    comparison = compare(run, baseline)

    assert not comparison.ok
    (failure,) = comparison.failures
    assert failure.verdict is Verdict.REGRESSED
    assert "+52.7%" in failure.describe()


def test_an_entry_may_state_a_tolerance_of_its_own() -> None:
    """Run-to-run variance is not uniform, so a figure may state its own bar."""
    run = make_run([make_benchmark("pipeline", {"pipeline.emit": 0.006})])
    baseline = make_baseline(
        profiles={
            PROFILE: Profile(
                name=PROFILE,
                gated=True,
                description="an example machine",
                entries={
                    "pipeline.emit": BaselineEntry(
                        value=0.004,
                        unit=Unit.MILLISECONDS,
                        tolerance=1.0,
                        note="clock granularity dominates a four-microsecond stage",
                    ),
                },
            ),
        },
        tolerances={Unit.MILLISECONDS: 0.10},
    )

    comparison = compare(run, baseline)

    assert comparison.ok
    (delta,) = [one for one in comparison.deltas if one.name == "pipeline.emit"]
    assert delta.tolerance == 1.0


def test_a_baseline_recorded_in_another_unit_is_not_compared() -> None:
    """Milliseconds against bytes would still produce a ratio."""
    run = make_run(
        [make_benchmark("footprint", {"footprint.x": 10.0}, unit=Unit.BYTES)],
    )
    baseline = Baseline(
        tolerances={Unit.BYTES: 0.10},
        # Recorded in milliseconds, measured in bytes. The values are equal, so
        # a comparison that only divided would report a perfect match.
        artifacts={
            "footprint.x": BaselineEntry(value=10.0, unit=Unit.MILLISECONDS),
        },
        profiles={PROFILE: Profile(name=PROFILE, gated=True, description="")},
    )

    comparison = compare(run, baseline)

    assert not comparison.ok
    (failure,) = comparison.failures
    assert failure.verdict is Verdict.MISSING_BASELINE
    assert "not the same quantity" in failure.describe()


def test_a_baseline_of_zero_treats_any_measurement_as_growth() -> None:
    """There is no fraction of zero, and dividing would raise."""
    run = make_run(
        [make_benchmark("footprint", {"footprint.x": 1.0}, unit=Unit.BYTES)],
    )
    baseline = make_baseline(artifacts={"footprint.x": 0.0})

    comparison = compare(run, baseline)

    assert not comparison.ok
    (failure,) = comparison.failures
    assert failure.verdict is Verdict.REGRESSED
    assert failure.change is None


def test_a_machine_with_no_recorded_profile_is_reported_rather_than_failed() -> None:
    """A class of machine nobody has measured is a fact, not a regression."""
    run = make_run(
        [make_benchmark("detect", {"detect.face.threads.4": 38.0})],
        profile="linux-aarch64-4c",
    )
    baseline = make_baseline(entries={"detect.face.threads.4": 38.0})

    comparison = compare(run, baseline)

    assert comparison.ok
    assert comparison.profile == ""
    verdicts = _verdicts(comparison)
    assert verdicts["linux-aarch64-4c"] is Verdict.UNBASELINED
    assert verdicts["detect.face.threads.4"] is Verdict.UNBASELINED


def test_requiring_a_profile_turns_an_unmeasured_machine_into_a_failure() -> None:
    """A job whose runner class is recorded passes `require_profile`.

    That is what stops the timing half of the gate quietly becoming advisory if
    the class label ever moves.
    """
    run = make_run(
        [make_benchmark("detect", {"detect.face.threads.4": 38.0})],
        profile="linux-aarch64-4c",
    )
    baseline = make_baseline(entries={"detect.face.threads.4": 38.0})

    comparison = compare(run, baseline, require_profile=True)

    assert not comparison.ok
    assert _verdicts(comparison)["linux-aarch64-4c"] is Verdict.MISSING_BASELINE


def test_an_ungated_profile_is_not_compared_against() -> None:
    """The predecessor's numbers describe a machine that no longer exists."""
    run = make_run([make_benchmark("detect", {"detect.face.threads.4": 400.0})])
    baseline = make_baseline(
        entries={"detect.face.threads.4": 38.0},
        gated=False,
    )

    comparison = compare(run, baseline)

    assert comparison.ok
    assert comparison.profile == ""
    assert _verdicts(comparison)["detect.face.threads.4"] is Verdict.UNBASELINED


def test_two_benchmarks_measuring_the_same_name_is_refused() -> None:
    """A duplicate would gate on whichever of the two came last."""
    run = make_run(
        [
            make_benchmark("detect", {"shared": 1.0}),
            make_benchmark("pipeline", {"shared": 2.0}),
        ],
    )

    with pytest.raises(ValueError, match="both measured"):
        compare(run, make_baseline())


def test_the_report_names_the_profile_and_says_whether_it_passed() -> None:
    """The gate's output is what a reviewer reads out of a job log."""
    run = make_run([make_benchmark("detect", {"detect.face.threads.4": 38.0})])
    baseline = make_baseline(entries={"detect.face.threads.4": 38.0})

    report = compare(run, baseline).report()

    assert f"timings against {PROFILE}" in report
    assert "PASS" in report

    regressed = compare(
        make_run([make_benchmark("detect", {"detect.face.threads.4": 380.0})]),
        baseline,
    ).report()
    assert "FAIL: 1 measurement(s)" in regressed


def test_the_predecessors_figures_are_printed_beside_the_run() -> None:
    """The rebuild is accountable to them, and nothing gates on them."""
    run = make_run(
        [
            make_benchmark("detect", {"detect.face.threads.4": 1.9}),
            make_benchmark(
                "footprint",
                {"footprint.image.cpu.linux-amd64": 458_535_567.0},
                unit=Unit.BYTES,
            ),
        ],
    )
    baseline = make_baseline(
        profiles={
            "predecessor": Profile(
                name="predecessor",
                gated=False,
                description="the stack this one replaces",
                entries={
                    "detect.face.threads.4": BaselineEntry(
                        value=38.0,
                        unit=Unit.MILLISECONDS,
                        note="inference runtime, four threads",
                    ),
                    "footprint.image.cpu.linux-amd64": BaselineEntry(
                        value=483_000_000.0,
                        unit=Unit.BYTES,
                    ),
                },
            ),
        },
    )

    lines = predecessor_lines(run, baseline)

    # Sorted by measurement name, so the detection figure comes first.
    assert len(lines) == 2
    assert "detect.face.threads.4" in lines[0]
    assert "predecessor 38" in lines[0]
    assert "-95%" in lines[0]
    assert "inference runtime, four threads" in lines[0]
    assert "footprint.image.cpu.linux-amd64" in lines[1]


def test_nothing_is_printed_beside_a_baseline_with_no_predecessor() -> None:
    """The comparison is optional; its absence is not an error."""
    run = make_run([make_benchmark("detect", {"detect.face.threads.4": 1.9})])

    assert predecessor_lines(run, make_baseline()) == ()


def test_a_measurement_the_predecessor_did_not_record_is_not_invented() -> None:
    """Only the figures both sides have are printed beside each other."""
    run = make_run([make_benchmark("session", {"session.reconnect": 1.0})])
    baseline = make_baseline(
        profiles={
            "predecessor": Profile(
                name="predecessor",
                gated=False,
                description="",
                entries={
                    "session.connect": BaselineEntry(
                        value=378.0,
                        unit=Unit.MILLISECONDS,
                    ),
                },
            ),
        },
    )

    assert predecessor_lines(run, baseline) == ()


def test_a_measurement_naming_no_benchmark_is_still_compared() -> None:
    """A name with no dot has no benchmark prefix, and must not be skipped."""
    run = make_run([make_benchmark("detect", {"loose": 100.0})])
    baseline = make_baseline(entries={"loose": 10.0})

    comparison = compare(run, baseline)

    assert not comparison.ok
    assert _verdicts(comparison)["loose"] is Verdict.REGRESSED


def test_a_measurement_carries_its_unit_into_the_line_it_prints() -> None:
    """A reader should not have to guess whether 458 is bytes or milliseconds."""
    run = make_run(
        [
            make_benchmark(
                "footprint",
                {"footprint.resident_memory": 119.0},
                unit=Unit.MEBIBYTES,
            ),
        ],
    )
    baseline = make_baseline(
        profiles={
            PROFILE: Profile(
                name=PROFILE,
                gated=True,
                description="",
                entries={
                    "footprint.resident_memory": BaselineEntry(
                        value=119.0,
                        unit=Unit.MEBIBYTES,
                    ),
                },
            ),
        },
    )

    (delta,) = [
        one
        for one in compare(run, baseline).deltas
        if one.name == "footprint.resident_memory"
    ]
    assert "MiB" in delta.describe()


def test_a_measurement_with_no_recorded_figure_still_reports_what_it_measured() -> None:
    """The failing line has to carry the number, or nobody can record it."""
    run = make_run([make_benchmark("detect", {"detect.face.threads.8": 41.5})])
    baseline = make_baseline(entries={})

    (failure,) = [
        one
        for one in compare(run, baseline).failures
        if one.name == "detect.face.threads.8"
    ]
    assert "41.5" in failure.describe()


def test_a_measurement_object_can_be_built_without_a_distribution() -> None:
    """A size is read once rather than sampled, and carries no distribution."""
    measurement = make_measurement("footprint.x", 10.0, Unit.BYTES)

    assert measurement.distribution is None


def test_a_recorded_size_this_run_did_not_weigh_is_not_a_failure() -> None:
    """Sizes come from the change that produces each artifact, one at a time.

    The image workflow weighs the variant its matrix entry built and knows
    nothing about the other two. A completeness check here would fail every one
    of those runs; what keeps the recorded set honest is the contract test over
    the build definitions in `test_bench_baseline.py`.
    """
    run = make_run(
        [
            make_benchmark(
                "footprint",
                {"footprint.image.cpu.linux-amd64": 458_535_567.0},
                unit=Unit.BYTES,
            ),
        ],
    )
    baseline = make_baseline(
        artifacts={
            "footprint.image.cpu.linux-amd64": 458_535_567.0,
            "footprint.image.cuda.linux-amd64": 3_659_931_059.0,
        },
    )

    comparison = compare(run, baseline)

    assert comparison.ok
    assert "footprint.image.cuda.linux-amd64" not in _verdicts(comparison)


def test_a_size_only_run_is_not_reported_as_having_lost_its_timings() -> None:
    """The image and release workflows weigh an artifact and time nothing.

    Telling them a timing went missing would fail every such run for not having
    been a benchmark run.
    """
    run = make_run(
        [
            make_benchmark(
                "footprint",
                {"footprint.image.cpu.linux-amd64": 458_535_567.0},
                unit=Unit.BYTES,
            ),
        ],
    )
    baseline = make_baseline(
        entries={"footprint.resident_memory": 119.0},
        artifacts={"footprint.image.cpu.linux-amd64": 458_535_567.0},
    )

    comparison = compare(run, baseline)

    assert comparison.ok
    assert "footprint.resident_memory" not in _verdicts(comparison)


def test_a_size_only_run_on_an_unrecorded_machine_says_nothing_about_profiles() -> None:
    """It makes no timing comparison, so its runner class is beside the point."""
    run = make_run(
        [
            make_benchmark(
                "footprint",
                {"footprint.image.cpu.linux-amd64": 458_535_567.0},
                unit=Unit.BYTES,
            ),
        ],
        profile="linux-aarch64-4c",
    )
    baseline = make_baseline(
        artifacts={"footprint.image.cpu.linux-amd64": 458_535_567.0},
    )

    comparison = compare(run, baseline)

    assert comparison.ok
    assert "linux-aarch64-4c" not in _verdicts(comparison)
