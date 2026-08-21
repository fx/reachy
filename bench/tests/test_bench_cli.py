"""The command surface: what each verb decides, and what it exits with.

The verbs that matter here are `compare` and `record`. `compare` is what a
continuous integration job runs, so its exit status is the gate; `record` is
what makes adopting a class of machine a reviewable diff, so what matters is
that it writes nothing and prints the block to paste.

`run` is exercised only through `photon-to-head`, which needs no model, no
socket and no robot: with no observations it refuses, and with some it reports
them. Everything else `run` can select takes real measurements, which is what
`just bench` is for. Its argument parsing is tested regardless, because a thread
sweep read wrongly is a sweep measured wrongly.

The filesystem is `pyfakefs`, which is an in-memory one and performs no input or
output — so these are ordinary unit tests and carry no marker. See the root
`AGENTS.md` on where the line is.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from bench_support import PROFILE, make_baseline, make_benchmark, make_run

from reachy_bench.cli import default_repository, main
from reachy_bench.context import RunContext, collect_context

if TYPE_CHECKING:
    from pyfakefs.fake_filesystem import FakeFilesystem

_RESULTS = "/work/results.json"
_BASELINE = "/work/baseline.json"


def _write(fs: FakeFilesystem, path: str, text: str) -> None:
    """Put a file into the in-memory filesystem.

    Args:
        fs: The fake filesystem.
        path: Where to write it.
        text: What to write.
    """
    fs.create_file(path, contents=text)


def test_a_run_within_tolerance_exits_zero(
    fs: FakeFilesystem,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The gate passes, and says what it judged the run against.

    Args:
        fs: The in-memory filesystem.
        capsys: What the command wrote.
    """
    run = make_run([make_benchmark("detect", {"detect.face.threads.4": 39.0})])
    baseline = make_baseline(entries={"detect.face.threads.4": 38.0})
    _write(fs, _RESULTS, run.as_json())
    _write(fs, _BASELINE, json.dumps(baseline.as_document()))

    status = main(["compare", "--results", _RESULTS, "--baseline", _BASELINE])

    assert status == 0
    assert PROFILE in capsys.readouterr().out


def test_a_regression_exits_non_zero_and_names_the_measurement(
    fs: FakeFilesystem,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """What a continuous integration job turns red on.

    Args:
        fs: The in-memory filesystem.
        capsys: What the command wrote.
    """
    run = make_run([make_benchmark("detect", {"detect.face.threads.4": 380.0})])
    baseline = make_baseline(entries={"detect.face.threads.4": 38.0})
    _write(fs, _RESULTS, run.as_json())
    _write(fs, _BASELINE, json.dumps(baseline.as_document()))

    status = main(["compare", "--results", _RESULTS, "--baseline", _BASELINE])

    assert status == 1
    printed = capsys.readouterr().out
    assert "regressed: detect.face.threads.4" in printed
    assert "+900.0%" in printed


def test_a_missing_result_file_is_a_failure_rather_than_a_pass(
    fs: FakeFilesystem,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A run whose document never arrived must not read as a run that passed.

    Args:
        fs: The in-memory filesystem.
        capsys: What the command wrote.
    """
    _write(fs, _BASELINE, json.dumps(make_baseline().as_document()))

    status = main(["compare", "--results", _RESULTS, "--baseline", _BASELINE])

    assert status == 1
    assert "results.json" in capsys.readouterr().err


def test_a_missing_baseline_is_a_failure_rather_than_a_pass(
    fs: FakeFilesystem,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A comparison against nothing passes everything.

    Args:
        fs: The in-memory filesystem.
        capsys: What the command wrote.
    """
    _write(fs, _RESULTS, make_run([]).as_json())

    status = main(["compare", "--results", _RESULTS, "--baseline", _BASELINE])

    assert status == 1
    assert "baseline.json" in capsys.readouterr().err


def test_requiring_a_profile_is_a_flag_the_job_passes(
    fs: FakeFilesystem,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A runner class that is recorded should never silently stop being.

    Args:
        fs: The in-memory filesystem.
        capsys: What the command wrote.
    """
    run = make_run(
        [make_benchmark("detect", {"detect.face.threads.4": 38.0})],
        profile="linux-aarch64-4c",
    )
    _write(fs, _RESULTS, run.as_json())
    _write(fs, _BASELINE, json.dumps(make_baseline().as_document()))

    assert main(["compare", "--results", _RESULTS, "--baseline", _BASELINE]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "compare",
                "--results",
                _RESULTS,
                "--baseline",
                _BASELINE,
                "--require-profile",
            ],
        )
        == 1
    )


def test_the_predecessors_figures_are_printed_beside_the_comparison(
    fs: FakeFilesystem,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A reviewer should see what the rebuild is accountable to.

    Args:
        fs: The in-memory filesystem.
        capsys: What the command wrote.
    """
    from reachy_bench.baseline import Baseline, BaselineEntry, Profile
    from reachy_bench.result import Unit

    run = make_run([make_benchmark("detect", {"detect.face.threads.4": 1.9})])
    baseline = Baseline(
        tolerances={Unit.MILLISECONDS: 1.0},
        artifacts={},
        profiles={
            "predecessor": Profile(
                name="predecessor",
                gated=False,
                description="",
                entries={
                    "detect.face.threads.4": BaselineEntry(
                        value=38.0,
                        unit=Unit.MILLISECONDS,
                    ),
                },
            ),
        },
    )
    _write(fs, _RESULTS, run.as_json())
    _write(fs, _BASELINE, json.dumps(baseline.as_document()))

    main(["compare", "--results", _RESULTS, "--baseline", _BASELINE])

    assert "predecessor 38 ms" in capsys.readouterr().out


def test_recording_prints_the_block_and_writes_nothing(
    fs: FakeFilesystem,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Changing the recorded numbers is a pull request, not a command.

    Args:
        fs: The in-memory filesystem.
        capsys: What the command wrote.
    """
    run = make_run([make_benchmark("detect", {"detect.face.threads.4": 1.9})])
    _write(fs, _RESULTS, run.as_json())
    _write(fs, _BASELINE, json.dumps(make_baseline().as_document()))
    before = Path(_BASELINE).read_text(encoding="utf-8")

    status = main(["record", "--results", _RESULTS, "--description", "a machine"])

    assert status == 0
    printed = capsys.readouterr()
    block = json.loads(printed.out)
    assert block[PROFILE]["description"] == "a machine"
    assert "Nothing was written" in printed.err
    assert Path(_BASELINE).read_text(encoding="utf-8") == before


def test_recording_describes_the_machine_when_nobody_did(
    fs: FakeFilesystem,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A profile nobody described is one nobody can decide they match.

    Args:
        fs: The in-memory filesystem.
        capsys: What the command wrote.
    """
    _write(fs, _RESULTS, make_run([]).as_json())

    main(["record", "--results", _RESULTS])

    block = json.loads(capsys.readouterr().out)
    assert "Example Processor 1000" in block[PROFILE]["description"]


def test_recording_from_a_document_that_is_not_one_fails(
    fs: FakeFilesystem,
) -> None:
    """A truncated result must not produce a profile of invented numbers.

    Args:
        fs: The in-memory filesystem.
    """
    _write(fs, _RESULTS, "{")

    assert main(["record", "--results", _RESULTS]) == 1


def test_listing_names_every_benchmark_and_what_it_needs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """REQ-072 made visible before anybody runs anything.

    Args:
        capsys: What the command wrote.
    """
    assert main(["list"]) == 0

    printed = capsys.readouterr().out
    assert "detect" in printed
    assert "photon-to-head   [needs a robot]" in printed
    assert "no hardware" in printed


def test_a_thread_sweep_is_read_as_a_list_of_counts() -> None:
    """A sweep read wrongly is a sweep measured wrongly."""
    from reachy_bench.cli import _thread_counts

    assert _thread_counts("1,2,4, 6") == (1, 2, 4, 6)


@pytest.mark.parametrize("text", ["", "0", "four", "1,,2", "-1"])
def test_a_thread_sweep_that_is_not_one_is_refused(text: str) -> None:
    """Argparse turns this into a usage error rather than a silent default.

    Args:
        text: What was written on the command line.
    """
    import argparse

    from reachy_bench.cli import _thread_counts

    with pytest.raises(argparse.ArgumentTypeError):
        _thread_counts(text)


def test_the_repository_root_is_found_by_the_task_surface(
    fs: FakeFilesystem,
) -> None:
    """`just bench` runs from the root; a subdirectory should still work.

    Args:
        fs: The in-memory filesystem.
    """
    fs.create_file("/checkout/Justfile", contents="")
    fs.create_dir("/checkout/bench/src")

    assert default_repository(Path("/checkout/bench/src")) == Path("/checkout")


def test_a_directory_that_is_not_a_checkout_is_reported_as_itself(
    fs: FakeFilesystem,
) -> None:
    """A better message comes from the first missing fixture than from here.

    Args:
        fs: The in-memory filesystem.
    """
    fs.create_dir("/elsewhere")

    assert default_repository(Path("/elsewhere")) == Path("/elsewhere")


def _measured_context() -> RunContext:
    """Build a run context without reading this machine.

    Returns:
        The context.
    """
    return collect_context(
        profile=PROFILE,
        cpu_count=4,
        cpuinfo="model name\t: Example Processor 1000\n",
        meminfo="MemTotal:       16384000 kB\n",
        run_command=lambda _argv: "0" * 40,
        version_of=lambda _name: "1.2.3",
        now=lambda: datetime(2026, 8, 21, tzinfo=UTC),
    )


def test_a_run_writes_the_document_and_reports_a_benchmark_that_failed(
    fs: FakeFilesystem,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`photon-to-head` with no observations, which needs no model and no robot.

    Naming it is what selects it, so this also exercises the half of REQ-072
    that says a hardware benchmark is selectable explicitly.

    Args:
        fs: The in-memory filesystem.
        capsys: What the command wrote.
    """
    fs.create_dir("/work")
    output = "/work/run.json"

    status = main(
        ["run", "photon-to-head", "--output", output],
        context=_measured_context(),
    )

    assert status == 1
    document = json.loads(Path(output).read_text(encoding="utf-8"))
    assert document["benchmarks"][0]["status"] == "failed"
    printed = capsys.readouterr()
    assert "photon-to-head: failed" in printed.out
    assert f"results written to {output}" in printed.out
    assert "could not measure" in printed.err


def test_a_run_summarises_the_host_and_every_measurement(
    fs: FakeFilesystem,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The summary is derived from the document a program reads.

    Args:
        fs: The in-memory filesystem.
        capsys: What the command wrote.
    """
    fs.create_dir("/work")

    main(
        [
            "run",
            "photon-to-head",
            "--output",
            "/work/run.json",
            "--observation",
            "180",
            "--observation",
            "220",
        ],
        context=_measured_context(),
    )

    printed = capsys.readouterr().out
    assert "Example Processor 1000" in printed
    assert "photon-to-head.stimulus_to_motion = 200 ms" in printed
    assert "p95 220 ms, n=2" in printed


def test_a_size_document_is_judged_without_measuring_anything(
    fs: FakeFilesystem,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """What the image and release workflows run: REQ-073 on its own.

    Args:
        fs: The in-memory filesystem.
        capsys: What the command wrote.
    """
    fs.create_file(
        "/work/image.json",
        contents=json.dumps(
            {
                "image": "example:dev",
                "variant": "cpu",
                "platform": "linux/amd64",
                "size_bytes": 458_535_567,
                "size_mib": 437.3,
            },
        ),
    )
    _write(
        fs,
        _BASELINE,
        json.dumps(
            make_baseline(
                artifacts={"footprint.image.cpu.linux-amd64": 458_535_567.0},
            ).as_document(),
        ),
    )

    status = main(
        [
            "sizes",
            "--artifact-size",
            "/work/image.json",
            "--baseline",
            _BASELINE,
            "--output",
            "/work/sizes.json",
        ],
        context=_measured_context(),
    )

    assert status == 0
    printed = capsys.readouterr().out
    assert "458,535,567 bytes (437.3 MiB)" in printed
    assert "PASS" in printed
    assert json.loads(Path("/work/sizes.json").read_text(encoding="utf-8"))


def test_a_grown_artifact_fails_the_size_command(
    fs: FakeFilesystem,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """REQ-073's scenario, through the command the workflows call.

    Args:
        fs: The in-memory filesystem.
        capsys: What the command wrote.
    """
    fs.create_file(
        "/work/wheel.json",
        contents=json.dumps(
            {
                "artifact": "reachyctl",
                "wheel": "reachyctl-0.1.0-py3-none-any.whl",
                "version": "0.1.0",
                "size_bytes": 400_000,
                "size_kib": 390.6,
            },
        ),
    )
    _write(
        fs,
        _BASELINE,
        json.dumps(
            make_baseline(
                artifacts={"footprint.wheel.reachyctl": 97_451.0}
            ).as_document(),
        ),
    )

    status = main(
        ["sizes", "--artifact-size", "/work/wheel.json", "--baseline", _BASELINE],
        context=_measured_context(),
    )

    assert status == 1
    assert "regressed: footprint.wheel.reachyctl" in capsys.readouterr().out


def test_a_size_document_that_is_not_one_fails_rather_than_reporting_nothing(
    fs: FakeFilesystem,
) -> None:
    """A gate that skipped what it could not read would pass silently.

    Args:
        fs: The in-memory filesystem.
    """
    fs.create_file("/work/broken.json", contents='{"size_bytes": 1}')
    _write(fs, _BASELINE, json.dumps(make_baseline().as_document()))

    status = main(
        ["sizes", "--artifact-size", "/work/broken.json", "--baseline", _BASELINE],
        context=_measured_context(),
    )

    assert status == 1


def test_a_benchmark_name_that_is_not_one_fails_the_run(
    fs: FakeFilesystem,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A typo that selected nothing would produce an empty run that passed.

    Args:
        fs: The in-memory filesystem.
        capsys: What the command wrote.
    """
    fs.create_dir("/work")

    status = main(
        ["run", "detct", "--output", "/work/run.json"], context=_measured_context()
    )

    assert status == 1
    assert "no such benchmark: detct" in capsys.readouterr().err
