"""`probe` against a real groundstation, run as the command an operator types.

Every test here opens a socket, and every test here says so with
`@pytest.mark.enable_socket`. The reason is reachyctl REQ-057: the probe is
required to establish its session with the same protocol implementation the
robot application uses, and the only evidence for that is real traffic. A real
uvicorn server runs in-process on the loopback interface with an ephemeral port,
the real `reachy_groundstation` application answers, and the command is invoked
through Click's runner so that argument parsing, rendering and the exit status
are all the real ones too.

Real files are written and read for the same reason: a directory of recorded
frames is the input `probe --frames` takes, and a fake filesystem here would be
testing the fake rather than the command. That is what the `filesystem` marker
on each test declares — a real one, unlike the `pyfakefs` unit tests elsewhere
in this suite, which perform no input or output and carry no marker.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Final

import pytest
from reachyctl_server import (
    CREDENTIAL,
    CountingFace,
    SlowFace,
    StaticRegistry,
    serving,
    write_frames,
)
from typer.testing import CliRunner

from reachyctl.cli import app
from reachyctl.credentials import CREDENTIAL_VARIABLE
from reachyctl.exits import ExitCode

if TYPE_CHECKING:
    from pathlib import Path

FRAMES: Final = 4

CONFIGURED: Final = {CREDENTIAL_VARIABLE: CREDENTIAL}

runner = CliRunner()


def probe_arguments(url: str, directory: Path, *extra: str) -> list[str]:
    """Build a probe invocation against a running groundstation.

    Args:
        url: Where the groundstation is listening.
        directory: The recorded frames to send.
        extra: Anything else to pass.

    Returns:
        The argument list.
    """
    return [
        "probe",
        "--url",
        url,
        "--frames",
        str(directory),
        "--count",
        str(FRAMES),
        "--interval",
        "0",
        "--timeout",
        "20",
        *extra,
    ]


#:= docs/specs/reachyctl/index.md#req-057-the-probe-exercises-the-real-session-protocol
#:% The probe command MUST establish a session using the same protocol
#:% implementation the robot application uses.
@pytest.mark.enable_socket  # a real server and the real client; see the module docstring
@pytest.mark.filesystem  # and a real directory of frames; see the module docstring
def test_the_probe_exchanges_real_frames_for_real_results(tmp_path: Path) -> None:
    """One command, one session, and one answer per frame sent.

    Args:
        tmp_path: Where the recorded frames are written.
    """
    write_frames(tmp_path, FRAMES)
    capability = CountingFace()

    with serving(StaticRegistry(capability)) as url:
        result = runner.invoke(app, probe_arguments(url, tmp_path), env=CONFIGURED)

    assert result.exit_code == ExitCode.OK, result.stdout
    assert capability.seen == list(range(FRAMES))
    lines = result.stdout.splitlines()
    assert lines[0].split("\t")[0] == "sequence"
    assert lines[-1].startswith("probe\tok")


#:= docs/specs/reachyctl/index.md#req-058-output-is-machine-readable-on-request
#:% Every command that reports results MUST offer a structured output format
#:% suitable for consumption by another program.
@pytest.mark.enable_socket  # a real server and the real client; see the module docstring
@pytest.mark.filesystem  # and a real directory of frames; see the module docstring
def test_the_structured_report_carries_a_row_per_result_with_its_timing(
    tmp_path: Path,
) -> None:
    """What a script reads: parsed, not scraped, with the exit status agreeing.

    Args:
        tmp_path: Where the recorded frames are written.
    """
    write_frames(tmp_path, FRAMES)

    with serving(StaticRegistry(CountingFace())) as url:
        result = runner.invoke(
            app,
            ["--output", "json", *probe_arguments(url, tmp_path)],
            env=CONFIGURED,
        )

    document = json.loads(result.stdout)
    assert result.exit_code == ExitCode.OK
    assert document["ok"] is True
    assert document["data"]["agreed"] == ["face"]
    assert document["data"]["frames_submitted"] == FRAMES
    assert document["data"]["results_applied"] == FRAMES
    assert document["data"]["errors_received"] == 0
    assert [row["sequence"] for row in document["rows"]] == list(range(FRAMES))
    timings = [row["round_trip_ms"] for row in document["rows"]]
    # Asserted separately, so a row that carried no measurement fails by
    # naming the missing measurement rather than as a TypeError.
    assert all(timing is not None for timing in timings), timings
    assert all(timing >= 0 for timing in timings)


#:= docs/specs/robot-link/index.md#req-013-an-empty-result-is-a-valid-result
#:% A result message carrying no detections MUST be treated as a successful result
#:% for that frame.
@pytest.mark.enable_socket  # a real server and the real client; see the module docstring
@pytest.mark.filesystem  # and a real directory of frames; see the module docstring
def test_a_frame_that_yielded_nothing_is_reported_as_a_successful_result(
    tmp_path: Path,
) -> None:
    """Over the real wire: zero detections, and no error counter moves.

    Args:
        tmp_path: Where the recorded frames are written.
    """
    write_frames(tmp_path, FRAMES)

    with serving(StaticRegistry(CountingFace())) as url:
        result = runner.invoke(
            app,
            ["--output", "json", *probe_arguments(url, tmp_path)],
            env=CONFIGURED,
        )

    document = json.loads(result.stdout)
    # The capability answers frame N with N faces, so the first frame's result
    # carries none at all — and the run is a success anyway.
    assert document["rows"][0]["detections"] == 0
    assert document["rows"][1]["detections"] == 1
    assert document["ok"] is True
    assert document["data"]["errors_received"] == 0


#:= docs/specs/robot-link/index.md#req-016-results-return-the-capture-timestamp-unaltered
#:% Every result MUST carry the capture timestamp of the frame it derives from,
#:% byte-for-byte as the capturing side supplied it, so that the capturing side can
#:% compute the result's age against the same clock that produced it.
@pytest.mark.enable_socket  # a real server and the real client; see the module docstring
@pytest.mark.filesystem  # and a real directory of frames; see the module docstring
def test_the_round_trip_is_measured_against_the_clock_that_stamped_the_frame(
    tmp_path: Path,
) -> None:
    """A measurable timing at all is the proof: the token came back intact.

    Args:
        tmp_path: Where the recorded frames are written.
    """
    write_frames(tmp_path, FRAMES)

    with serving(StaticRegistry(CountingFace())) as url:
        result = runner.invoke(
            app,
            ["--output", "json", *probe_arguments(url, tmp_path)],
            env=CONFIGURED,
        )

    document = json.loads(result.stdout)
    fastest = document["data"]["round_trip_ms_fastest"]
    slowest = document["data"]["round_trip_ms_slowest"]
    assert fastest is not None
    assert 0 <= fastest <= slowest


#:= docs/specs/reachyctl/index.md#req-059-secrets-are-never-written-to-output
#:% The tool MUST NOT write credentials to its output, its logs, or its error
#:% messages.
@pytest.mark.enable_socket  # a real server and the real client; see the module docstring
@pytest.mark.filesystem  # and a real directory of frames; see the module docstring
def test_a_credential_the_groundstation_refuses_appears_nowhere_in_the_failure(
    tmp_path: Path,
) -> None:
    """The forced failure, which is where a credential actually escapes.

    A rejected credential is the one path where every layer has a reason to
    quote the value: the client built a message out of it, the groundstation
    refused it, and the error travels back through a renderer that was only
    trying to be helpful. Verbose is on, so the path that says what the tool is
    doing is checked as well as the path that says what went wrong.

    Args:
        tmp_path: Where the recorded frames are written.
    """
    wrong = "example-credential-that-is-not-the-configured-one"
    write_frames(tmp_path, FRAMES)

    with serving(StaticRegistry(CountingFace())) as url:
        result = runner.invoke(
            app,
            ["--verbose", *probe_arguments(url, tmp_path)],
            env={CREDENTIAL_VARIABLE: wrong},
        )

    assert result.exit_code == ExitCode.UNREACHABLE
    assert "unauthenticated" in result.stdout
    assert wrong not in result.stdout
    assert wrong not in result.stderr
    assert "<redacted>" in result.stderr


#:= docs/specs/reachyctl/index.md#req-059-secrets-are-never-written-to-output
#:% The tool MUST NOT write credentials to its output, its logs, or its error
#:% messages.
@pytest.mark.enable_socket  # a real client against nothing; see the module docstring
@pytest.mark.filesystem  # and a real directory of frames; see the module docstring
def test_a_groundstation_that_is_not_there_is_reported_without_the_credential(
    tmp_path: Path,
) -> None:
    """The other forced failure: nothing answered, and nothing was quoted.

    Args:
        tmp_path: Where the recorded frames are written.
    """
    write_frames(tmp_path, FRAMES)

    with serving(StaticRegistry(CountingFace())) as url:
        # A port that was listening a moment ago and is not now, which is what
        # a stopped groundstation looks like — and an address on the loopback
        # interface rather than anybody's network.
        pass

    result = runner.invoke(
        app,
        ["--verbose", *probe_arguments(url, tmp_path)],
        env=CONFIGURED,
    )

    assert result.exit_code == ExitCode.UNREACHABLE
    assert CREDENTIAL not in result.stdout
    assert CREDENTIAL not in result.stderr


#:= docs/specs/robot-link/index.md#req-012-capabilities-are-negotiated-at-session-start
#:% Both sides MUST exchange the set of capabilities they support, each with a
#:% version, before any capability-specific message is sent.
@pytest.mark.enable_socket  # a real server and the real client; see the module docstring
@pytest.mark.filesystem  # and a real directory of frames; see the module docstring
def test_a_groundstation_that_agrees_to_nothing_is_reported_rather_than_waited_on(
    tmp_path: Path,
) -> None:
    """Nothing would answer a frame, so the run says so instead of timing out.

    Args:
        tmp_path: Where the recorded frames are written.
    """
    write_frames(tmp_path, FRAMES)

    with serving(StaticRegistry(CountingFace())) as url:
        result = runner.invoke(
            app,
            [
                "--output",
                "json",
                *probe_arguments(url, tmp_path, "--capability", "gesture"),
            ],
            env=CONFIGURED,
        )

    document = json.loads(result.stdout)
    assert result.exit_code == ExitCode.FAILURE
    assert document["data"]["agreed"] == []
    assert "agreed to none" in document["summary"]


@pytest.mark.enable_socket  # a real server and the real client; see the module docstring
@pytest.mark.filesystem  # and a real directory of frames; see the module docstring
def test_only_as_many_frames_are_sent_as_were_asked_for(tmp_path: Path) -> None:
    """A directory longer than `--count` is a recording, not an instruction.

    The interval is non-zero here, because pacing frames is what a probe against
    a live groundstation actually does and a run that sent them all at once
    would not exercise it.

    Args:
        tmp_path: Where the recorded frames are written.
    """
    write_frames(tmp_path, FRAMES * 2)
    capability = CountingFace()

    with serving(StaticRegistry(capability)) as url:
        result = runner.invoke(
            app,
            ["--output", "json", *probe_arguments(url, tmp_path, "--interval", "0.01")],
            env=CONFIGURED,
        )

    document = json.loads(result.stdout)
    assert result.exit_code == ExitCode.OK
    assert document["data"]["frames_submitted"] == FRAMES
    assert capability.seen == list(range(FRAMES))


@pytest.mark.enable_socket  # a real server and the real client; see the module docstring
@pytest.mark.filesystem  # and a real directory of frames; see the module docstring
def test_results_that_do_not_arrive_in_time_are_reported_as_missing(
    tmp_path: Path,
) -> None:
    """A probe that reported success on half the answers would be worse than none.

    Args:
        tmp_path: Where the recorded frames are written.
    """
    write_frames(tmp_path, FRAMES)

    with serving(StaticRegistry(SlowFace())) as url:
        result = runner.invoke(
            app,
            [
                "--output",
                "json",
                *probe_arguments(url, tmp_path, "--staleness", "0.1"),
            ],
            env=CONFIGURED,
        )

    document = json.loads(result.stdout)
    assert result.exit_code == ExitCode.FAILURE
    assert document["ok"] is False
    assert "expected results arrived" in document["summary"]


@pytest.mark.enable_socket  # a real server and the real client; see the module docstring
@pytest.mark.filesystem  # and a real directory of frames; see the module docstring
def test_a_recording_shorter_than_the_count_is_not_a_run_that_fell_short(
    tmp_path: Path,
) -> None:
    """What is owed is one answer per frame that went out, not per frame asked for.

    Args:
        tmp_path: Where the recorded frames are written.
    """
    write_frames(tmp_path, FRAMES)

    with serving(StaticRegistry(CountingFace())) as url:
        result = runner.invoke(
            app,
            [
                "--output",
                "json",
                *probe_arguments(url, tmp_path, "--count", str(FRAMES * 3)),
            ],
            env=CONFIGURED,
        )

    document = json.loads(result.stdout)
    assert result.exit_code == ExitCode.OK
    assert document["data"]["frames_submitted"] == FRAMES
    assert len(document["rows"]) == FRAMES


#:= docs/specs/robot-link/index.md#req-017-stale-results-stop-being-acted-on
#:% A consumer MUST stop acting on results once none has arrived within a configured
#:% staleness window.
@pytest.mark.enable_socket  # a real server and the real client; see the module docstring
@pytest.mark.filesystem  # and a real directory of frames; see the module docstring
def test_the_frames_running_out_mid_wait_still_bounds_the_run_by_one_window(
    tmp_path: Path,
) -> None:
    """The window has to start when the frames stop, not when the loop next looks.

    The producer finishes while the loop is already waiting for a result, and
    it is that wait — entered with the frames still flowing, and therefore with
    the whole of `--timeout` as its budget — that has to be cut short. A loop
    which only narrows its budget on the next time round never gets a next time
    round when the missing result is the thing it is waiting for, so it sits
    out the full timeout instead of one staleness window.

    The capability here answers far later than the window allows, so the run is
    bounded by the window or by nothing. Wall time is the assertion because it
    is the symptom: the outcome was already correct before this was fixed, it
    just took twenty seconds to say so.

    Args:
        tmp_path: Where the recorded frames are written.
    """
    write_frames(tmp_path, FRAMES)

    with serving(StaticRegistry(SlowFace(delay=1.5))) as url:
        started = time.monotonic()
        result = runner.invoke(
            app,
            ["--output", "json", *probe_arguments(url, tmp_path, "--staleness", "0.1")],
            env=CONFIGURED,
        )
        elapsed = time.monotonic() - started

    document = json.loads(result.stdout)
    assert result.exit_code == ExitCode.FAILURE
    assert document["ok"] is False
    # Comfortably below the capability's own delay, which is what the run would
    # otherwise have waited for, and an order of magnitude below `--timeout`.
    assert elapsed < 1.0
