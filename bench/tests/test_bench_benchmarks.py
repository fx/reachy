"""The benchmarks' own logic, with the measuring itself replaced by a fake.

Each benchmark is a thin assembly around one function that does the expensive
thing — opens a model, starts a server, reads a robot's processor time. That
function is an argument with a real default, so everything else is exercised
here: the sweep, the knee, the notes that keep an absent model from reading as a
fast one, the parsing of the size documents the producing workflows publish, and
the refusals the hardware benchmarks give when there is no robot.

The expensive functions themselves are not unit-tested and say so where they are
defined: a unit test of them would be a unit test of ONNX Runtime, uvicorn or
the operating system. They are exercised by `just bench`, which the benchmark
workflow runs on every pull request.

No test here performs any input or output.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from bench_support import make_distribution

from reachy_bench.benchmarks import detect, footprint, photon_to_head, pipeline, session
from reachy_bench.benchmarks import robot_load as load
from reachy_bench.benchmarks.pipeline import Stages
from reachy_bench.registry import Options
from reachy_bench.result import Status, Unit

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from reachy_bench.result import Detail
    from reachy_bench.stats import Distribution

_OPTIONS = Options(repository=Path("/nowhere"), thread_counts=(1, 2, 4))


# --- detect ------------------------------------------------------------------


def _curve(*medians: float) -> detect.ThreadMeasure:
    """Build a thread sweep that reports a known curve.

    Args:
        medians: The median for each thread count in turn.

    Returns:
        Something the sweep can call.
    """
    remaining = list(medians)

    def _measure(options: Options, threads: int) -> detect.ThreadOutcome:
        """Report the next median in the curve.

        Args:
            options: Unused.
            threads: Unused.

        Returns:
            The distribution and one face found.
        """
        del options, threads
        return make_distribution(remaining.pop(0)), 1

    return _measure


def test_the_sweep_reports_one_measurement_per_thread_count() -> None:
    """The curve is reproduced, not a single configured value measured."""
    result = detect.build(_OPTIONS, measure_threads=_curve(93.0, 60.0, 51.0))

    assert [one.name for one in result.measurements] == [
        "detect.face.threads.1",
        "detect.face.threads.2",
        "detect.face.threads.4",
    ]
    assert [one.value for one in result.measurements] == [93.0, 60.0, 51.0]


def test_every_sweep_measurement_records_the_thread_count_it_ran_at() -> None:
    """REQ-068's scenario reads the thread count out of the result itself."""
    result = detect.build(_OPTIONS, measure_threads=_curve(93.0, 60.0, 51.0))

    assert [one.detail["threads"] for one in result.measurements] == [1, 2, 4]


def test_the_knee_is_the_fastest_thread_count() -> None:
    """It is reported as a note, because a knee that moves is not a regression."""
    result = detect.build(_OPTIONS, measure_threads=_curve(93.0, 51.0, 55.0))

    assert "knee on this host is 2 thread(s)" in result.notes[0]


def test_a_sweep_over_no_thread_counts_has_no_knee() -> None:
    """An empty sweep must not raise looking for the minimum of nothing."""
    assert detect.knee_of([]) is None


def test_a_sweep_that_detected_nothing_says_so() -> None:
    """A detector finding nothing is very fast, and would read as a win."""

    def _finds_nothing(options: Options, threads: int) -> detect.ThreadOutcome:
        """Report a fast pass that detected no face.

        Args:
            options: Unused.
            threads: Unused.

        Returns:
            The distribution and no faces.
        """
        del options, threads
        return make_distribution(0.1), 0

    result = detect.build(
        Options(repository=Path("/nowhere"), thread_counts=(4,)),
        measure_threads=_finds_nothing,
        describe_frame=lambda _options: "640x480",
    )

    assert any("no face was detected" in note for note in result.notes)


def test_the_detect_configuration_records_the_model_and_the_frame() -> None:
    """The configuration half of REQ-068, on the benchmark that needs it most."""
    result = detect.build(
        _OPTIONS,
        measure_threads=_curve(93.0, 60.0, 51.0),
        describe_frame=lambda _options: "640x480",
    )

    assert result.configuration["model"] == "face_detection_yunet"
    assert result.configuration["frame_size"] == "640x480"
    assert result.configuration["score_threshold"] == 0.6


# --- pipeline ----------------------------------------------------------------


def _stages(**medians: float) -> pipeline.StageMeasure:
    """Build a stage measurement that reports known figures.

    Args:
        medians: Stage suffix to median.

    Returns:
        Something `pipeline.build` can call.
    """

    def _measure(options: Options) -> tuple[Stages, Mapping[str, Detail]]:
        """Report the stages.

        Args:
            options: Unused.

        Returns:
            The distributions and a configuration.
        """
        del options
        stages = Stages()
        for stage, median in medians.items():
            stages[stage.replace("__", ".")] = make_distribution(median)
        return stages, {"model": "face_detection_yunet"}

    return _measure


def test_every_stage_is_reported_separately_and_so_is_the_whole() -> None:
    """REQ-070: the stage responsible is identifiable without another run."""
    result = pipeline.build(
        _OPTIONS,
        measure_stages=_stages(
            decode=0.46,
            capability__face=2.16,
            emit=0.004,
            end_to_end=2.92,
        ),
    )

    assert [one.name for one in result.measurements] == [
        "pipeline.decode",
        "pipeline.capability.face",
        "pipeline.emit",
        "pipeline.end_to_end",
    ]


def test_no_gesture_timing_is_reported_and_the_result_says_why() -> None:
    """An absent model must not read as a three-order improvement.

    This build wires no gesture model, which is the perception spec's recorded
    decision, so the capability answers in microseconds. Reporting that beside
    the predecessor's 5 ms would claim a win that is really an absence.
    """
    result = pipeline.build(_OPTIONS, measure_stages=_stages(decode=0.46))

    assert not any("gesture" in one.name for one in result.measurements)
    assert any("no gesture model" in note for note in result.notes)


def test_the_stage_sum_is_reported_beside_the_end_to_end_figure() -> None:
    """Two measurements of overlapping work, and the difference is worth reading."""
    result = pipeline.build(
        _OPTIONS,
        measure_stages=_stages(decode=1.0, emit=1.0, end_to_end=3.0),
    )

    assert any(
        "the stages sum to 2.00 ms" in note and "3.00 ms" in note
        for note in result.notes
    )


def test_a_pipeline_run_with_no_end_to_end_figure_reports_no_comparison() -> None:
    """The comparison needs both halves, and says nothing without them."""
    result = pipeline.build(_OPTIONS, measure_stages=_stages(decode=1.0))

    assert not any("the stages sum to" in note for note in result.notes)


# --- footprint ---------------------------------------------------------------

_IMAGE_DOCUMENT: Mapping[str, Any] = {
    "image": "reachy-groundstation:dev",
    "variant": "cpu",
    "platform": "linux/amd64",
    "size_bytes": 458535567,
    "size_mib": 437.3,
}

_WHEEL_DOCUMENT: Mapping[str, Any] = {
    "artifact": "reachyctl",
    "wheel": "reachyctl-0.1.0-py3-none-any.whl",
    "version": "0.1.0",
    "size_bytes": 97451,
    "size_kib": 95.2,
}


def test_an_image_size_document_becomes_a_measurement_named_by_variant() -> None:
    """The name has to be stable across builds of the same variant."""
    measurement = footprint.size_measurement(_IMAGE_DOCUMENT)

    assert measurement.name == "footprint.image.cpu.linux-amd64"
    assert measurement.unit is Unit.BYTES
    assert measurement.value == 458535567.0
    assert measurement.detail["size_mib"] == pytest.approx(437.3)


def test_a_wheel_size_document_becomes_a_measurement_named_by_artifact() -> None:
    """The version changes every release; the artifact does not."""
    measurement = footprint.size_measurement(_WHEEL_DOCUMENT)

    assert measurement.name == "footprint.wheel.reachyctl"
    assert measurement.value == 97451.0
    assert measurement.detail["version"] == "0.1.0"


def test_a_document_with_no_size_is_refused() -> None:
    """A size gate that skipped what it could not read would pass silently."""
    with pytest.raises(ValueError, match="no size_bytes"):
        footprint.size_measurement({"artifact": "reachyctl"})


def test_a_document_that_is_neither_shape_is_refused() -> None:
    """`size_bytes` alone does not say what was weighed."""
    with pytest.raises(ValueError, match="image variant or a wheel artifact"):
        footprint.size_measurement({"size_bytes": 1})


def test_two_documents_describing_one_artifact_are_refused() -> None:
    """A duplicate would gate on whichever came last."""
    with pytest.raises(ValueError, match="same artifact"):
        footprint.size_measurements([_WHEEL_DOCUMENT, dict(_WHEEL_DOCUMENT)])


def test_the_footprint_reports_resident_memory_in_mebibytes() -> None:
    """The predecessor's figure is a resident set, and so is this one."""
    result = footprint.build(
        _OPTIONS,
        read_documents=lambda _paths: [],
        measure_memory=lambda _options: (119, "reachy_groundstation, once ready"),
    )

    (memory,) = result.measurements
    assert memory.name == "footprint.resident_memory"
    assert memory.unit is Unit.MEBIBYTES
    assert memory.value == 119.0
    assert memory.detail["process"] == "reachy_groundstation, once ready"


def test_a_run_given_no_size_documents_says_none_were_reported() -> None:
    """Sizes come from the change that produces each artifact, not from here."""
    result = footprint.build(
        _OPTIONS,
        read_documents=lambda _paths: [],
        measure_memory=lambda _options: (119, "reachy_groundstation"),
    )

    assert any("no artifact sizes were given" in note for note in result.notes)


def test_the_sizes_that_were_given_are_reported_beside_the_memory() -> None:
    """One benchmark, both quantities, so one document carries both."""
    result = footprint.build(
        Options(
            repository=Path("/nowhere"),
            artifact_sizes=(Path("/nowhere/sizes"),),
        ),
        read_documents=lambda _paths: [_IMAGE_DOCUMENT, _WHEEL_DOCUMENT],
        measure_memory=lambda _options: (119, "reachy_groundstation"),
    )

    assert [one.name for one in result.measurements] == [
        "footprint.resident_memory",
        "footprint.image.cpu.linux-amd64",
        "footprint.wheel.reachyctl",
    ]


# --- photon-to-head ----------------------------------------------------------


def test_photon_to_head_refuses_to_report_without_observations() -> None:
    """There is no automated stimulus, and inventing a number would be worse."""
    result = photon_to_head.build(_OPTIONS)

    assert result.status is Status.FAILED
    assert "no observations were given" in result.reason
    assert "--observation" in result.reason


@pytest.mark.parametrize("value", [0.0, -1.0, float("nan"), float("inf")])
def test_an_observation_that_is_not_a_duration_is_refused(value: float) -> None:
    """A transcription mistake would move a median nobody could explain.

    Args:
        value: The observation to refuse.
    """
    (problem,) = photon_to_head.observation_problems([value])

    assert "not a duration" in problem


def test_manually_recorded_observations_are_reported_as_a_distribution() -> None:
    """What the benchmark owns is the reporting, not the stimulus."""
    result = photon_to_head.build(
        Options(
            repository=Path("/nowhere"),
            observations_ms=(150.0, 180.0, 210.0, 250.0),
            network="2.4 GHz WLAN",
        ),
    )

    assert result.status is Status.MEASURED
    (measurement,) = result.measurements
    assert measurement.name == "photon-to-head.stimulus_to_motion"
    assert measurement.value == pytest.approx(195.0)
    assert result.configuration["observations"] == 4
    assert result.configuration["network"] == "2.4 GHz WLAN"


def test_the_photon_measurement_records_the_method_it_was_taken_by() -> None:
    """Two runs are only comparable when the same method produced both."""
    result = photon_to_head.build(
        Options(repository=Path("/nowhere"), observations_ms=(200.0,)),
    )

    assert "manual" in str(result.configuration["method"])


# --- robot-load --------------------------------------------------------------

_STAT_FIRST = """cpu  1000 0 1000 8000 0 0 0 0 0 0
cpu0 250 0 250 2000 0 0 0 0 0 0
intr 12345
"""

_STAT_SECOND = """cpu  1200 0 1200 8600 0 0 0 0 0 0
cpu0 300 0 300 2150 0 0 0 0 0 0
intr 12999
"""


def test_the_aggregate_processor_line_is_read_out_of_the_dump() -> None:
    """The per-core lines and the interrupt counts are not it."""
    sample = load.parse_proc_stat(_STAT_FIRST)

    assert sample.total == 10000
    assert sample.idle == 8000


def test_a_dump_with_no_aggregate_line_is_refused() -> None:
    """A sample that half-parsed would produce a load nobody could account for."""
    with pytest.raises(ValueError, match="no aggregate cpu line"):
        load.parse_proc_stat("cpu0 1 2 3 4 5 6 7\n")


def test_a_dump_whose_fields_are_not_numbers_is_refused() -> None:
    """A command that answered with an error message is not a sample."""
    with pytest.raises(ValueError, match="not numeric"):
        load.parse_proc_stat("cpu  permission denied\n")


def test_a_dump_that_is_too_short_is_refused() -> None:
    """The idle and iowait fields are the third and fourth."""
    with pytest.raises(ValueError, match="too short"):
        load.parse_proc_stat("cpu  1 2 3\n")


def test_the_busy_fraction_is_the_work_over_the_whole_interval() -> None:
    """Jiffies cancel, so the tick rate never enters it.

    Four hundred busy jiffies out of a thousand elapsed is two fifths of the
    machine.
    """
    first = load.parse_proc_stat(_STAT_FIRST)
    second = load.parse_proc_stat(_STAT_SECOND)

    assert load.busy_fraction(first, second) == pytest.approx(0.4)


def test_two_identical_samples_are_refused() -> None:
    """One sample read twice would report zero load on a busy robot."""
    sample = load.parse_proc_stat(_STAT_FIRST)

    with pytest.raises(ValueError, match="same sample read twice"):
        load.busy_fraction(sample, sample)


def test_robot_load_reports_cores_busy_out_of_the_cores_the_robot_has() -> None:
    """The recorded figure is "1.52 of 4 cores", so this reports both."""
    answers = iter(["4\n", _STAT_FIRST, _STAT_SECOND])

    def _run(argv: Sequence[str]) -> str:
        """Answer a command on the robot.

        Args:
            argv: The command.

        Returns:
            The next canned answer.
        """
        del argv
        return next(answers)

    result = load.build(
        Options(
            repository=Path("/nowhere"),
            robot=_run,
            frame_rate=10.0,
            sample_seconds=5.0,
        ),
        sleep=lambda _seconds: None,
    )

    assert result.status is Status.MEASURED
    (measurement,) = result.measurements
    assert measurement.name == "robot-load.cpu_cores"
    assert measurement.unit is Unit.CORES
    assert measurement.value == pytest.approx(1.6)
    assert measurement.detail["cores_available"] == 4
    assert result.configuration["frame_rate"] == 10.0


def test_robot_load_refuses_to_measure_the_machine_it_is_running_on() -> None:
    """A groundstation's processors reported as a robot's would be a lie."""
    result = load.build(_OPTIONS, sleep=lambda _seconds: None)

    assert result.status is Status.FAILED
    assert "no robot" in result.reason
    assert "reachyctl bench --robot" in result.reason


def test_a_robot_that_reports_no_processors_is_refused() -> None:
    """Multiplying a fraction by a core count needs a core count."""

    def _run(argv: Sequence[str]) -> str:
        """Answer the core-count command with something that is not one.

        Args:
            argv: The command.

        Returns:
            An unusable answer.
        """
        del argv
        return "not a number\n"

    with pytest.raises(ValueError, match="processors"):
        load.measure_load(_run, 5.0, lambda _seconds: None)


def test_a_sampling_interval_that_is_not_positive_is_refused() -> None:
    """Two samples are taken an interval apart."""

    def _run(argv: Sequence[str]) -> str:
        """Answer nothing, because the interval is refused first.

        Args:
            argv: The command.

        Returns:
            Nothing; it always raises.

        Raises:
            AssertionError: If it is ever called.
        """
        del argv
        message = "the robot must not be contacted before the interval is checked"
        raise AssertionError(message)

    with pytest.raises(ValueError, match="interval apart"):
        load.measure_load(_run, 0.0, lambda _seconds: None)


# --- session -----------------------------------------------------------------


def test_the_session_benchmark_reports_establishing_using_and_reconnecting() -> None:
    """Three numbers, because the link's design turns on the split between them."""

    def _measure(
        options: Options,
    ) -> tuple[Mapping[str, Distribution], Mapping[str, Detail]]:
        """Report the three timings.

        Args:
            options: Unused.

        Returns:
            The distributions and a configuration.
        """
        del options
        return (
            {
                "connect": make_distribution(1.05),
                "round_trip": make_distribution(4.21),
                "reconnect": make_distribution(0.96),
            },
            {"transport": "websocket over the loopback interface"},
        )

    result = session.build(_OPTIONS, measure_session=_measure)

    assert [one.name for one in result.measurements] == [
        "session.connect",
        "session.round_trip",
        "session.reconnect",
    ]
    assert result.configuration["transport"] == (
        "websocket over the loopback interface"
    )


def test_the_session_result_says_what_network_it_did_not_cross() -> None:
    """A loopback figure against a WLAN one is a comparison of two networks."""

    def _measure(
        options: Options,
    ) -> tuple[Mapping[str, Distribution], Mapping[str, Detail]]:
        """Report one timing.

        Args:
            options: Unused.

        Returns:
            The distribution and an empty configuration.
        """
        del options
        return {"round_trip": make_distribution(4.21)}, {}

    plain = session.build(_OPTIONS, measure_session=_measure)
    described = session.build(
        Options(repository=Path("/nowhere"), network="2.4 GHz WLAN, 120 ms idle"),
        measure_session=_measure,
    )

    assert any("loopback interface" in note for note in plain.notes)
    assert not any("as reported by the operator" in note for note in plain.notes)
    assert any("2.4 GHz WLAN, 120 ms idle" in note for note in described.notes)


def test_a_virtualised_robots_guest_time_is_not_counted_twice() -> None:
    """The kernel counts `guest` inside `user` and `guest_nice` inside `nice`.

    Summing every field would therefore inflate the total, and an inflated
    total makes the busy fraction — and the reported load — smaller than it
    really is. That is the direction that hides load, which is why it is worth
    a test of its own.
    """
    fields = "cpu  1000 0 1000 8000 0 0 0 0 500 0\n"
    sample = load.parse_proc_stat(fields)

    assert sample.total == 10000
    assert sample.idle == 8000


def test_an_aggregate_line_without_an_iowait_field_is_refused() -> None:
    """`idle` and `iowait` are the fourth and fifth, so four fields is short."""
    with pytest.raises(ValueError, match="too short"):
        load.parse_proc_stat("cpu  1 2 3 4\n")
