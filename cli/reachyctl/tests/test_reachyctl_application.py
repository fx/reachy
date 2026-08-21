"""What `app start`, `app stop` and `app logs` do, and what decides their answer.

The lifecycle verbs share `deploy`'s reasoning: a control command that exited
zero is not evidence that the application is running, so each verb asks the
shared `application.running` check afterwards and reports what the robot said.
The cases that matter are therefore the ones where the two disagree.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import json
from typing import Final

import pytest
from reachyctl_robot import FakeRobot, daemon_for
from reachyctl_support import reporter_for

from reachy_session_client import REDACTED
from reachyctl.application import execute_logs, execute_start, execute_stop
from reachyctl.daemon import DaemonControlError
from reachyctl.exits import ExitCode
from reachyctl.output import OutputFormat
from reachyctl.robot import RobotAccessError

# RFC 5737 TEST-NET-1 and a placeholder account — see the root AGENTS.md.
ROBOT: Final = "operator@192.0.2.10:22"


def _rows(output: str) -> dict[str, dict[str, object]]:
    """Read a structured run's steps, keyed by step.

    Args:
        output: What the command wrote to standard output.

    Returns:
        Each row, by its `step` field.
    """
    return {str(row["step"]): row for row in json.loads(output)["rows"]}


def test_starting_an_application_that_is_stopped_starts_it_and_confirms() -> None:
    """Inspect, control, verify — the same shape as a deploy, three steps long."""
    robot = FakeRobot(app_running=False)
    daemon, access = daemon_for(robot)
    reporter, streams = reporter_for(output_format=OutputFormat.JSON)

    code = execute_start(daemon, reporter, ROBOT, preview=False, close=access.aclose)

    rows = _rows(streams.result)
    assert code is ExitCode.OK
    assert robot.app_running is True
    assert [row["status"] for row in rows.values()] == ["done", "done", "done"]
    assert access.closed is True


def test_starting_an_application_that_is_already_running_changes_nothing() -> None:
    """Skipped rather than done: a script gating on one must not be told the other."""
    robot = FakeRobot(app_running=True, app_detail="active")
    daemon, access = daemon_for(robot)
    reporter, streams = reporter_for(output_format=OutputFormat.JSON)

    code = execute_start(daemon, reporter, ROBOT, preview=False)

    rows = _rows(streams.result)
    assert code is ExitCode.OK
    assert rows["control"]["status"] == "skipped"
    assert not any("start" in command for command in access.commands)


def test_a_start_the_daemon_accepted_that_did_not_take_fails() -> None:
    """The robot's state decides it, which is what makes this verb worth running."""
    daemon, _access = daemon_for(FakeRobot(start_succeeds=False))
    reporter, streams = reporter_for(output_format=OutputFormat.JSON)

    code = execute_start(daemon, reporter, ROBOT, preview=False)

    rows = _rows(streams.result)
    assert code is ExitCode.FAILURE
    assert rows["control"]["status"] == "done"
    assert rows["verify"]["status"] == "failed"


def test_a_control_that_complained_while_the_application_started_is_a_success() -> None:
    """The robot's state decides it; a control command's exit status is not evidence.

    The complaint is recorded rather than swallowed, and it is a warning rather
    than a failure — otherwise a robot that did exactly what was asked would be
    reported as a failed command.
    """
    daemon, _access = daemon_for(FakeRobot(control_succeeds=False))
    reporter, streams = reporter_for(output_format=OutputFormat.JSON)

    code = execute_start(daemon, reporter, ROBOT, preview=False)

    rows = _rows(streams.result)
    assert code is ExitCode.OK
    assert rows["control"]["status"] == "warned"
    assert rows["verify"]["status"] == "done"


def test_a_control_that_complained_and_did_not_start_it_fails() -> None:
    """And it is the verification that failed the run, not the control."""
    daemon, _access = daemon_for(
        FakeRobot(control_succeeds=False, start_succeeds=False),
    )
    reporter, streams = reporter_for(output_format=OutputFormat.JSON)

    code = execute_start(daemon, reporter, ROBOT, preview=False)

    rows = _rows(streams.result)
    assert code is ExitCode.FAILURE
    assert rows["control"]["status"] == "warned"
    assert rows["verify"]["status"] == "failed"


def test_a_robot_whose_state_cannot_be_read_is_unreachable_not_already_stopped() -> (
    None
):
    """The failure the inspect step asks the daemon directly to avoid.

    A `stop` that read a failed check as "not running" would report the
    application as already stopped and exit zero, having learned nothing about
    it at all.
    """
    daemon, _access = daemon_for(FakeRobot(control_stdout="not json at all"))
    reporter, _streams = reporter_for()

    with pytest.raises(DaemonControlError):
        execute_stop(daemon, reporter, ROBOT, preview=False)


def test_stopping_an_application_that_is_running_stops_it_and_confirms() -> None:
    """The other verb, through the same three steps."""
    robot = FakeRobot(app_running=True, app_detail="active")
    daemon, _access = daemon_for(robot)
    reporter, streams = reporter_for(output_format=OutputFormat.JSON)

    code = execute_stop(daemon, reporter, ROBOT, preview=False)

    assert code is ExitCode.OK
    assert robot.app_running is False
    assert _rows(streams.result)["verify"]["status"] == "done"


def test_stopping_an_application_that_is_already_stopped_changes_nothing() -> None:
    """Idempotence, for the same reason `config apply` has it."""
    robot = FakeRobot(app_running=False)
    daemon, access = daemon_for(robot)
    reporter, streams = reporter_for(output_format=OutputFormat.JSON)

    code = execute_stop(daemon, reporter, ROBOT, preview=False)

    assert code is ExitCode.OK
    assert _rows(streams.result)["control"]["status"] == "skipped"
    assert not any("stop" in command for command in access.commands)


#:= docs/specs/reachyctl/index.md#req-052-configuration-changes-can-be-previewed-without-being-applied
#:% Every command that modifies robot state MUST support a mode that reports the
#:% changes it would make and makes none of them.
def test_previewing_a_stop_leaves_the_application_running() -> None:
    """Stopping is the modification an operator most wants to think about first.

    The after-state is what is asserted, not the printed plan: a command that
    printed a perfect plan and stopped the application anyway would pass that.
    """
    robot = FakeRobot(app_running=True, app_detail="active")
    daemon, access = daemon_for(robot)
    reporter, streams = reporter_for(output_format=OutputFormat.JSON)

    code = execute_stop(daemon, reporter, ROBOT, preview=True)

    rows = _rows(streams.result)
    assert code is ExitCode.OK
    assert robot.app_running is True
    assert robot.app_detail == "active"
    assert rows["control"]["status"] == "planned"
    assert rows["verify"]["status"] == "planned"
    assert not any("stop" in command for command in access.commands)
    assert "nothing was changed" in json.loads(streams.result)["summary"]


def test_previewing_a_start_leaves_the_application_stopped() -> None:
    """The other verb's preview, which is a different branch of the same code."""
    robot = FakeRobot(app_running=False)
    daemon, access = daemon_for(robot)
    reporter, _streams = reporter_for()

    execute_start(daemon, reporter, ROBOT, preview=True)

    assert robot.app_running is False
    assert not any("start" in command for command in access.commands)


def test_reading_the_journal_streams_what_the_application_wrote() -> None:
    """The result is the lines themselves, with the run's document after them."""
    robot = FakeRobot(journal=["first line", "second line"])
    daemon, access = daemon_for(robot)
    reporter, streams = reporter_for()

    code = execute_logs(
        daemon,
        reporter,
        ROBOT,
        lines=10,
        follow=False,
        close=access.aclose,
    )

    written = streams.result.splitlines()
    assert code is ExitCode.OK
    assert written[:2] == ["first line", "second line"]
    assert "2 line(s)" in streams.result
    assert access.closed is True


def test_a_structured_journal_run_is_one_json_object_per_line() -> None:
    """Which is what a stream of JSON is, and the run's result is the last object."""
    robot = FakeRobot(journal=["first line"])
    daemon, _access = daemon_for(robot)
    reporter, streams = reporter_for(output_format=OutputFormat.JSON)

    execute_logs(daemon, reporter, ROBOT, lines=10, follow=False)

    lines = streams.result.splitlines()
    assert json.loads(lines[0]) == {"line": "first line"}
    # The run's own document follows the stream, so a consumer reading a line
    # at a time ends with a result rather than with a truncated stream.
    result = json.loads("\n".join(lines[1:]))
    assert result["command"] == "app logs"
    assert result["ok"] is True


def test_a_journal_line_is_written_as_the_robot_wrote_it() -> None:
    """A log is read by a person; escaping it would make the log unreadable."""
    robot = FakeRobot(journal=["a line\twith a tab"])
    daemon, _access = daemon_for(robot)
    reporter, streams = reporter_for()

    execute_logs(daemon, reporter, ROBOT, lines=10, follow=False)

    assert "a line\twith a tab" in streams.result


def test_an_empty_journal_is_a_successful_read() -> None:
    """A log with nothing in it is a log that was read, not a command that failed."""
    daemon, _access = daemon_for(FakeRobot())
    reporter, streams = reporter_for(output_format=OutputFormat.JSON)

    code = execute_logs(daemon, reporter, ROBOT, lines=10, follow=True)

    assert code is ExitCode.OK
    document = json.loads(streams.result)
    assert document["data"]["lines"] == 0
    assert document["data"]["followed"] is True


def test_ending_a_followed_journal_at_the_keyboard_is_a_successful_run() -> None:
    """`--follow` is meant to be ended that way, so it is not a traceback.

    The result document still arrives, which is what keeps a structured
    consumer's last line a result rather than a truncated stream.
    """
    robot = FakeRobot(journal=["a line"], journal_interrupts=True)
    daemon, access = daemon_for(robot)
    reporter, streams = reporter_for(output_format=OutputFormat.JSON)

    code = execute_logs(
        daemon,
        reporter,
        ROBOT,
        lines=10,
        follow=True,
        close=access.aclose,
    )

    lines = streams.result.splitlines()
    assert code is ExitCode.OK
    assert json.loads(lines[0]) == {"line": "a line"}
    result = json.loads("\n".join(lines[1:]))
    assert result["summary"] == "stopped following the journal"
    assert access.closed is True


def test_both_exit_paths_of_a_log_run_report_the_same_fields() -> None:
    """Ending `--follow` at the keyboard is how this command is meant to stop.

    A consumer reading `data["lines"]` must not fail on the ordinary
    termination path, so the keys are the same on both and the count that is
    genuinely unknown says so rather than claiming a number.
    """
    ended, _one = daemon_for(FakeRobot(journal=["a line"]))
    interrupted, _two = daemon_for(
        FakeRobot(journal=["a line"], journal_interrupts=True),
    )
    reporter, streams = reporter_for(output_format=OutputFormat.JSON)
    execute_logs(ended, reporter, ROBOT, lines=5, follow=True)
    normal = json.loads("\n".join(streams.result.splitlines()[1:]))["data"]

    reporter, streams = reporter_for(output_format=OutputFormat.JSON)
    execute_logs(interrupted, reporter, ROBOT, lines=5, follow=True)
    stopped = json.loads("\n".join(streams.result.splitlines()[1:]))["data"]

    assert sorted(normal) == sorted(stopped)
    assert normal["lines"] == 1
    assert stopped["lines"] is None
    assert stopped["followed"] is True


#:= docs/specs/reachyctl/index.md#req-059-secrets-are-never-written-to-output
#:% The tool MUST NOT write credentials to its output, its logs, or its error
#:% messages.
def test_a_credential_the_robot_logged_is_scrubbed_out_of_the_journal() -> None:
    """A journal is the likeliest place for a credential to appear in text nobody wrote.

    The redactor cannot remove a value it was never given, so the command reads
    the robot's configuration first and learns its secret values before it
    forwards a single line.
    """
    awkward = "example\\secret\twith\na-newline"
    robot = FakeRobot(
        environment={"REACHY_GROUNDSTATION_CREDENTIAL": awkward},
        journal=[f"opening a session with {awkward} configured"],
    )
    daemon, _access = daemon_for(robot)
    reporter, streams = reporter_for()

    code = execute_logs(daemon, reporter, ROBOT, lines=10, follow=False)

    written = streams.result + streams.diagnostics
    assert code is ExitCode.OK
    assert awkward not in written
    assert "a-newline" not in written
    assert REDACTED in written


def test_a_robot_whose_configuration_cannot_be_read_will_not_have_its_log_shown() -> (
    None
):
    """Proceeding would mean rendering its output unable to promise it is clean."""
    daemon, _access = daemon_for(FakeRobot(failing={"systemctl"}, journal=["a line"]))
    reporter, streams = reporter_for()

    with pytest.raises(RobotAccessError, match="would go out unscrubbed"):
        execute_logs(daemon, reporter, ROBOT, lines=10, follow=False)

    assert "a line" not in streams.result
