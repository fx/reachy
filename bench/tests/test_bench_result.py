"""The result document: what it carries, and that it survives a round trip.

REQ-067 asks that a comparison tool read two runs without screen-scraping, so
the thing that has to hold is that everything the comparison needs comes back
out of the document unchanged. These tests write a run to JSON, read it back and
compare the two — which is the only way to catch a field that serialises and
does not deserialise, the failure that makes a gate compare against a default.

They also hold the document to the shapes the rest of the suite depends on: a
timing's headline figure is its median, a benchmark that did not run still
appears with its reason, and two benchmarks cannot both claim one measurement
name.

No test here performs any input or output: the document is a string in memory.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import json

import pytest
from bench_support import make_benchmark, make_context, make_distribution, make_run

from reachy_bench.result import (
    SCHEMA_VERSION,
    BenchmarkResult,
    Measurement,
    RunResult,
    Status,
    Unit,
)


def test_a_timings_headline_figure_is_its_median() -> None:
    """The gate compares medians, and this is where that is made true."""
    distribution = make_distribution(12.0)

    measurement = Measurement.timing("detect.face.threads.4", distribution)

    assert measurement.value == distribution.median_ms
    assert measurement.unit is Unit.MILLISECONDS


def test_a_run_survives_being_written_and_read_back() -> None:
    """A field that serialises and does not deserialise breaks the gate.

    The two documents are compared rather than the two objects, and that is not
    a weaker check: the document is what a comparison reads, and the figures in
    it are rounded to the microsecond on the way out, so an object built from
    unrounded floats is not expected to come back bit for bit. Re-rendering what
    was read is exactly the property that matters — anything the document does
    not carry disappears here.
    """
    run = make_run(
        [
            make_benchmark("detect", {"detect.face.threads.4": 1.9}),
            make_benchmark(
                "footprint",
                {"footprint.image.cpu.linux-amd64": 458_535_567.0},
                unit=Unit.BYTES,
            ),
            BenchmarkResult.excluded("robot-load", "needs a physical robot"),
            BenchmarkResult.failed("session", "the groundstation did not start"),
        ],
    )

    restored = RunResult.from_json(run.as_json())

    assert restored.as_json() == run.as_json()
    assert restored.context == run.context
    assert restored.statuses() == run.statuses()
    assert restored.benchmarks[2].reason == "needs a physical robot"
    assert restored.by_name()["footprint.image.cpu.linux-amd64"].unit is Unit.BYTES


def test_the_document_carries_a_schema_and_the_context() -> None:
    """A reader that meets a later shape has to be able to say so."""
    document = json.loads(make_run([]).as_json())

    assert document["schema"] == SCHEMA_VERSION
    assert document["context"]["host"]["profile"]
    assert document["context"]["software"]["python"]


def test_a_document_from_another_schema_is_refused() -> None:
    """Reading past a changed shape would compare numbers that had moved."""
    document = json.loads(make_run([]).as_json())
    document["schema"] = SCHEMA_VERSION + 1

    with pytest.raises(ValueError, match="schema"):
        RunResult.from_document(document)


def test_a_document_that_is_not_json_is_refused() -> None:
    """A truncated file must not read as an empty run that passed."""
    with pytest.raises(ValueError, match="is JSON"):
        RunResult.from_json("{not json")


def test_a_json_array_is_not_a_result_document() -> None:
    """The top level is an object, and a list is not one."""
    with pytest.raises(ValueError, match="JSON object"):
        RunResult.from_json("[]")


def test_a_document_missing_its_context_is_refused() -> None:
    """Half a document would gate on the half that parsed."""
    with pytest.raises(ValueError, match="context"):
        RunResult.from_document({"schema": SCHEMA_VERSION, "benchmarks": []})


def test_a_measurement_without_a_name_is_refused() -> None:
    """The name is the key a baseline is written against."""
    with pytest.raises(ValueError, match="not a measurement"):
        Measurement.from_document({"unit": "ms", "value": 1.0})


def test_a_measurement_in_an_unknown_unit_is_refused() -> None:
    """A unit this build cannot read is a document from another version."""
    with pytest.raises(ValueError, match="not a measurement"):
        Measurement.from_document({"name": "x", "unit": "furlongs", "value": 1.0})


def test_a_benchmark_result_without_a_status_is_refused() -> None:
    """Excluded, measured and failed are three different outcomes."""
    with pytest.raises(ValueError, match="not a benchmark result"):
        BenchmarkResult.from_document({"benchmark": "detect"})


def test_an_excluded_benchmark_appears_with_its_reason() -> None:
    """REQ-072: reported as excluded, which is not the same as absent."""
    result = BenchmarkResult.excluded("photon-to-head", "needs a robot and a person")

    document = result.as_document()

    assert document["status"] == "excluded"
    assert document["measurements"] == []
    assert document["reason"] == "needs a robot and a person"


def test_two_benchmarks_measuring_one_name_is_refused() -> None:
    """A duplicate would make the gate read whichever came last."""
    run = make_run(
        [
            make_benchmark("detect", {"shared": 1.0}),
            make_benchmark("pipeline", {"shared": 2.0}),
        ],
    )

    with pytest.raises(ValueError, match="both measured"):
        run.by_name()


def test_the_run_reports_what_became_of_every_selected_benchmark() -> None:
    """The comparison reads this to decide what a missing figure means."""
    run = make_run(
        [
            make_benchmark("detect", {"detect.x": 1.0}),
            BenchmarkResult.excluded("robot-load", "needs a physical robot"),
        ],
    )

    assert run.statuses() == {
        "detect": Status.MEASURED,
        "robot-load": Status.EXCLUDED,
    }


def test_walking_the_measurements_carries_the_benchmark_beside_each() -> None:
    """A consumer reading a figure needs the configuration it was taken under."""
    run = RunResult(
        context=make_context(),
        benchmarks=(
            BenchmarkResult(
                benchmark="detect",
                status=Status.MEASURED,
                configuration={"threads": 4},
                measurements=(Measurement.timing("detect.x", make_distribution(1.0)),),
            ),
        ),
    )

    (benchmark, measurement), *rest = run.measurements()

    assert not rest
    assert benchmark.configuration["threads"] == 4
    assert measurement.name == "detect.x"


def test_a_measurements_detail_travels_with_it() -> None:
    """The thread count and the model are what REQ-068's scenario reads."""
    run = make_run(
        [
            BenchmarkResult(
                benchmark="detect",
                status=Status.MEASURED,
                measurements=(
                    Measurement.timing(
                        "detect.face.threads.4",
                        make_distribution(1.9),
                        threads=4,
                        faces=1,
                    ),
                ),
            ),
        ],
    )

    restored = RunResult.from_json(run.as_json())

    (measurement,) = restored.benchmarks[0].measurements
    assert measurement.detail == {"threads": 4, "faces": 1}


def test_the_json_ends_in_a_newline_so_two_runs_diff_line_by_line() -> None:
    """A reviewer compares recorded numbers by reading a diff."""
    text = make_run([]).as_json()

    assert text.endswith("\n")
    assert "\n  " in text
