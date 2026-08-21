"""What `config` reports, what it changes, and what it never prints.

Three of these are the change's load-bearing scenarios.

**Preview is proved by the after-state.** The robot's managed region is
snapshotted before a preview run and compared byte for byte afterwards, and the
commands the link received are checked for the absence of every mutating one.
Asserting that a diff was printed would pass on a command that printed a perfect
diff and then applied it anyway.

**A withdrawn setting is removed.** The region is owned in full — provisioning
REQ-063 — so a declaration that no longer names a setting takes it off the robot
rather than leaving it behind.

**A credential never reaches any rendering.** The value used carries a
backslash, a tab and a newline, because each of those is rewritten by the
escaping the plain rendering does and by the `repr` a nested value gets. A
redactor shown a value only after one of those transformations matches nothing
and the secret goes out in transformed form, so the assertions cover the escaped
spellings as well as the raw one.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

import pytest
from reachyctl_robot import DROP_IN, FakeRobot, daemon_for
from reachyctl_support import reporter_for

from reachy_session_client import REDACTED
from reachyctl.configure import (
    RESTART_WARNING,
    ConfigurationConflictError,
    compare,
    execute_apply,
    execute_diff,
    execute_get,
    merge_settings,
    run_apply,
)
from reachyctl.exits import ExitCode
from reachyctl.managed import parse_region, render_region
from reachyctl.output import OutputFormat

if TYPE_CHECKING:
    from collections.abc import Sequence

    from reachyctl.steps import StepResult

# RFC 5737 TEST-NET-1 and a placeholder account — see the root AGENTS.md.
ROBOT: Final = "operator@192.0.2.10:22"

URL: Final = "REACHY_GROUNDSTATION_URL"
CREDENTIAL: Final = "REACHY_GROUNDSTATION_CREDENTIAL"
LEVEL: Final = "REACHY_SATELLITE_LOG_LEVEL"
INTERVAL: Final = "REACHY_SATELLITE_FRAME_INTERVAL_MS"

ENDPOINT: Final = "ws://192.0.2.10:8000/v1/session"

# A placeholder credential carrying every character the renderings transform on
# their way out. Never anybody's — see the root AGENTS.md.
AWKWARD_CREDENTIAL: Final = "example\\secret\twith\na-newline"

DECLARED: Final = {URL: ENDPOINT, LEVEL: "info"}


def _named(results: Sequence[StepResult]) -> dict[str, StepResult]:
    """Index a run's steps by name.

    Args:
        results: What happened.

    Returns:
        The steps by name.
    """
    return {result.name: result for result in results}


def _provisioned(settings: dict[str, str]) -> FakeRobot:
    """Build a robot that already has a managed region in force.

    Args:
        settings: What it carries.

    Returns:
        The robot.
    """
    return FakeRobot(
        files={DROP_IN: render_region(settings)},
        environment=dict(settings),
    )


def test_a_comparison_names_what_would_be_added_changed_and_removed() -> None:
    """The whole of what an apply then does, worked out with nothing contacted."""
    difference = compare(
        {URL: ENDPOINT, LEVEL: "debug"},
        {LEVEL: "info", INTERVAL: "100"},
        {LEVEL: "info", INTERVAL: "100"},
    )

    assert difference.added == (URL,)
    assert difference.changed == (LEVEL,)
    assert difference.removed == (INTERVAL,)
    assert difference.unchanged == ()
    assert difference.changes is True


def test_a_comparison_of_a_matching_robot_says_it_matches() -> None:
    """Which is what makes a second apply a run with nothing in it."""
    difference = compare(DECLARED, DECLARED, DECLARED)

    assert difference.changes is False
    assert difference.not_in_force == ()
    assert "already matches" in difference.summary()


def test_a_setting_in_the_region_and_not_in_force_is_reported() -> None:
    """The silently-inert configuration the spec's background names."""
    difference = compare(DECLARED, DECLARED, {URL: ENDPOINT})

    assert difference.changes is False
    assert difference.not_in_force == (LEVEL,)
    assert "not in force" in difference.summary()


def test_settings_the_robot_has_and_nothing_declares_are_reported_not_touched() -> None:
    """This tool owns one drop-in, not the unit."""
    difference = compare(DECLARED, DECLARED, {**DECLARED, "PATH": "/usr/bin"})

    assert difference.unmanaged == ("PATH",)
    assert difference.changes is False


@pytest.mark.asyncio
async def test_applying_writes_the_region_restarts_and_verifies_it_is_in_force() -> (
    None
):
    """Three things, and the last is what makes the first two mean anything."""
    robot = FakeRobot()
    daemon, _access = daemon_for(robot)
    reporter, _streams = reporter_for()

    steps, difference = await run_apply(daemon, DECLARED, reporter, preview=False)

    assert parse_region(robot.managed_region) == DECLARED
    assert robot.environment == DECLARED
    assert [result.name for result in steps.results] == [
        "read",
        "write",
        "restart",
        "verify",
    ]
    assert steps.ok is True
    assert difference.added == tuple(sorted(DECLARED))


#:= docs/specs/provisioning/index.md#req-063-the-managed-configuration-is-fully-owned
#:% Configuration under provisioning's control MUST converge to exactly what is
#:% declared, including removing values that were previously declared and no longer
#:% are.
@pytest.mark.asyncio
async def test_a_setting_withdrawn_from_the_declaration_is_removed_from_the_robot() -> (
    None
):
    """Appending works perfectly until the first time somebody deletes a setting."""
    robot = _provisioned({URL: ENDPOINT, LEVEL: "info", INTERVAL: "100"})
    daemon, _access = daemon_for(robot)
    reporter, _streams = reporter_for()

    steps, difference = await run_apply(daemon, DECLARED, reporter, preview=False)

    assert difference.removed == (INTERVAL,)
    assert INTERVAL not in parse_region(robot.managed_region)
    assert INTERVAL not in robot.environment
    assert steps.ok is True


@pytest.mark.asyncio
async def test_applying_the_same_declaration_twice_changes_nothing_the_second_time() -> (
    None
):
    """Idempotence comes out of the comparison, not out of a second code path."""
    robot = FakeRobot()
    daemon, access = daemon_for(robot)
    reporter, _streams = reporter_for()

    await run_apply(daemon, DECLARED, reporter, preview=False)
    after_first = robot.managed_region
    sent = len(access.commands)

    steps, difference = await run_apply(daemon, DECLARED, reporter, preview=False)

    assert robot.managed_region == after_first
    assert difference.changes is False
    assert _named(steps.results)["write"].outcome.value == "skipped"
    assert _named(steps.results)["restart"].outcome.value == "skipped"
    # Two reads and nothing else: no write, no reload, no restart.
    assert len(access.commands) - sent <= 3


#:= docs/specs/reachyctl/index.md#req-052-configuration-changes-can-be-previewed-without-being-applied
#:% Every command that modifies robot state MUST support a mode that reports the
#:% changes it would make and makes none of them.
@pytest.mark.asyncio
async def test_a_preview_leaves_the_robot_byte_identical() -> None:
    """The guarantee is that nothing happened, and only an after-state tests that."""
    robot = _provisioned({URL: ENDPOINT, LEVEL: "info", INTERVAL: "100"})
    daemon, access = daemon_for(robot)
    reporter, _streams = reporter_for()
    before_region = robot.managed_region
    before_environment = dict(robot.environment)

    steps, difference = await run_apply(daemon, DECLARED, reporter, preview=True)

    assert robot.managed_region == before_region
    assert robot.environment == before_environment
    assert not any("install" in command for command in access.commands)
    assert not any("restart" in command for command in access.commands)
    assert not any("daemon-reload" in command for command in access.commands)
    assert not any(command[0] == "<upload>" for command in access.commands)
    assert difference.removed == (INTERVAL,)
    assert _named(steps.results)["write"].outcome.value == "planned"
    assert steps.ok is True


@pytest.mark.asyncio
async def test_a_preview_says_what_it_would_do_without_saying_any_value() -> None:
    """Names are safe to print; a value is exactly where a credential ends up."""
    robot = _provisioned({CREDENTIAL: "example-not-a-real-secret"})
    daemon, _access = daemon_for(robot)
    reporter, _streams = reporter_for()

    steps, _difference = await run_apply(daemon, DECLARED, reporter, preview=True)

    planned = _named(steps.results)["write"].detail
    assert CREDENTIAL in planned
    assert "example-not-a-real-secret" not in planned


@pytest.mark.asyncio
async def test_the_restart_warning_is_written_before_the_restart_happens() -> None:
    """The point of a warning is the moment in which it can still be acted on."""
    reporter, streams = reporter_for()
    seen_at_restart: list[str] = []

    def watch(command: Sequence[str]) -> None:
        """Snapshot the diagnostics stream as each command is sent.

        Args:
            command: What is about to run.
        """
        if "restart" in command:
            seen_at_restart.append(streams.diagnostics)

    daemon, _access = daemon_for(FakeRobot(), observer=watch)

    await run_apply(daemon, DECLARED, reporter, preview=False)

    assert seen_at_restart, "the restart was never sent"
    assert RESTART_WARNING in seen_at_restart[0]


@pytest.mark.asyncio
async def test_a_restart_that_failed_is_still_followed_by_the_verification() -> None:
    """A declaration written and not re-read is not a declaration in force.

    The run does not end at the failed restart: the verification only reads, a
    restart that reported a failure may still have taken effect, and either way
    the operator needs to know what is in force now.
    """
    daemon, _access = daemon_for(FakeRobot(restart_succeeds=False))
    reporter, _streams = reporter_for()

    steps, _difference = await run_apply(daemon, DECLARED, reporter, preview=False)

    results = _named(steps.results)
    assert results["restart"].failed is True
    assert results["verify"].failed is True
    assert URL in results["verify"].detail
    assert steps.ok is False


@pytest.mark.asyncio
async def test_a_region_written_and_not_in_force_fails_the_verification() -> None:
    """Which is the whole reason the verification reads the environment, not the file."""
    daemon, _access = daemon_for(FakeRobot(honours_restart=False))
    reporter, _streams = reporter_for()

    steps, _difference = await run_apply(daemon, DECLARED, reporter, preview=False)

    verify = _named(steps.results)["verify"]
    assert verify.failed is True
    assert URL in verify.detail
    assert steps.ok is False


@pytest.mark.asyncio
async def test_an_empty_declaration_removes_everything_and_verifies_nothing() -> None:
    """Emptying the region is a legitimate apply; there is then nothing to assert."""
    robot = _provisioned(DECLARED)
    daemon, _access = daemon_for(robot)
    reporter, _streams = reporter_for()

    steps, difference = await run_apply(daemon, {}, reporter, preview=False)

    assert parse_region(robot.managed_region) == {}
    assert difference.removed == tuple(sorted(DECLARED))
    assert _named(steps.results)["verify"].outcome.value == "skipped"


@pytest.mark.asyncio
async def test_a_region_something_else_wrote_stops_the_command() -> None:
    """Overwriting it regardless is how two tools start reverting each other."""
    robot = FakeRobot(files={DROP_IN: "[Service]\nEnvironment=A=1\n"})
    daemon, _access = daemon_for(robot)
    reporter, _streams = reporter_for()

    with pytest.raises(ConfigurationConflictError):
        await run_apply(daemon, DECLARED, reporter, preview=False)

    assert robot.managed_region == "[Service]\nEnvironment=A=1\n"


def test_setting_merges_into_the_region_rather_than_replacing_it() -> None:
    """`apply` is the verb that removes; `set` changes what it was asked to."""
    assert merge_settings({URL: ENDPOINT}, {LEVEL: "debug"}) == {
        URL: ENDPOINT,
        LEVEL: "debug",
    }


@pytest.mark.asyncio
async def test_setting_one_value_leaves_the_others_where_they_were() -> None:
    """The region is still written whole, from the merged desired state."""
    robot = _provisioned({URL: ENDPOINT, LEVEL: "info"})
    daemon, _access = daemon_for(robot)
    reporter, _streams = reporter_for()

    _steps, difference = await run_apply(
        daemon,
        {LEVEL: "debug"},
        reporter,
        preview=False,
        merge=True,
    )

    assert parse_region(robot.managed_region) == {URL: ENDPOINT, LEVEL: "debug"}
    assert difference.removed == ()
    assert difference.changed == (LEVEL,)


def test_reading_a_robots_configuration_reports_what_is_in_force() -> None:
    """And says which settings this tool put there and which arrived some other way."""
    robot = _provisioned({URL: ENDPOINT})
    robot.environment["PATH"] = "/usr/bin"
    daemon, access = daemon_for(robot)
    reporter, streams = reporter_for(output_format=OutputFormat.JSON)

    code = execute_get(daemon, [], reporter, ROBOT, access.aclose)

    document = json.loads(streams.result)
    rows = {row["setting"]: row for row in document["rows"]}
    assert code is ExitCode.OK
    assert rows[URL]["in_force"] == ENDPOINT
    assert rows[URL]["managed"] is True
    assert rows["PATH"]["managed"] is False
    assert access.closed is True


def test_reading_a_named_setting_that_is_not_set_is_a_negative_answer() -> None:
    """Asking about one thing and being told nothing is not the same as asking about all."""
    daemon, _access = daemon_for(FakeRobot())
    reporter, streams = reporter_for(output_format=OutputFormat.JSON)

    code = execute_get(daemon, [LEVEL], reporter, ROBOT)

    document = json.loads(streams.result)
    assert code is ExitCode.FAILURE
    assert document["data"]["absent"] == [LEVEL]


def test_reading_a_robot_whose_region_something_else_wrote_still_reports() -> None:
    """`get` reads; a region it will not overwrite is not a reason to show nothing."""
    robot = FakeRobot(
        files={DROP_IN: "[Service]\nEnvironment=A=1\n"},
        environment={URL: ENDPOINT},
    )
    daemon, _access = daemon_for(robot)
    reporter, streams = reporter_for(output_format=OutputFormat.JSON)

    code = execute_get(daemon, [], reporter, ROBOT)

    assert code is ExitCode.OK
    assert json.loads(streams.result)["rows"][0]["setting"] == URL


def test_a_diff_against_a_matching_robot_exits_zero() -> None:
    """Which is what makes `config diff` usable as a gate the way `doctor` is."""
    daemon, _access = daemon_for(_provisioned(DECLARED))
    reporter, _streams = reporter_for()

    assert execute_diff(daemon, DECLARED, reporter, ROBOT) is ExitCode.OK


def test_a_diff_against_a_robot_that_differs_exits_failure() -> None:
    """A gate that passed on a robot that does not match would gate nothing."""
    daemon, _access = daemon_for(FakeRobot())
    reporter, streams = reporter_for(output_format=OutputFormat.JSON)

    code = execute_diff(daemon, DECLARED, reporter, ROBOT)

    document = json.loads(streams.result)
    assert code is ExitCode.FAILURE
    assert sorted(document["data"]["to_add"]) == sorted(DECLARED)


def test_a_secret_setting_is_reported_as_set_rather_than_by_value() -> None:
    """REVIEW.md: a self-reporting configuration surface never reports one by value."""
    robot = FakeRobot(environment={CREDENTIAL: AWKWARD_CREDENTIAL})
    daemon, _access = daemon_for(robot)
    reporter, streams = reporter_for(output_format=OutputFormat.JSON)

    execute_get(daemon, [], reporter, ROBOT)

    rows = {row["setting"]: row for row in json.loads(streams.result)["rows"]}
    assert rows[CREDENTIAL]["in_force"] == "set"


#:= docs/specs/reachyctl/index.md#req-059-secrets-are-never-written-to-output
#:% The tool MUST NOT write credentials to its output, its logs, or its error
#:% messages.
@pytest.mark.parametrize(
    "output_format",
    [OutputFormat.TEXT, OutputFormat.JSON],
)
@pytest.mark.parametrize("terminal", [False, True])
def test_a_credential_on_the_robot_reaches_no_rendering_of_a_failure(
    output_format: OutputFormat,
    *,
    terminal: bool,
) -> None:
    """Every rendering, of a run that fails while holding the credential.

    The value carries a backslash, a tab and a newline. Each of those is
    rewritten by the escaping the plain rendering does and by the `repr` a
    nested value gets, so the escaped spellings are asserted absent too: a
    redactor shown a value after one of those transformations matches nothing
    and reports success while the secret goes out transformed.

    Args:
        output_format: Which rendering to check.
        terminal: Whether to render as though attached to a terminal.
    """
    robot = FakeRobot(
        environment={CREDENTIAL: AWKWARD_CREDENTIAL},
        restart_succeeds=False,
        leaky=True,
    )
    daemon, _access = daemon_for(robot)
    reporter, streams = reporter_for(output_format=output_format, terminal=terminal)

    code = execute_apply(
        "config apply",
        daemon,
        DECLARED,
        reporter,
        ROBOT,
        preview=False,
    )

    written = streams.result + streams.diagnostics
    assert code is ExitCode.FAILURE, written
    assert AWKWARD_CREDENTIAL not in written
    for escaped in (
        AWKWARD_CREDENTIAL.replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\n", "\\n"),
        json.dumps(AWKWARD_CREDENTIAL)[1:-1],
        repr(AWKWARD_CREDENTIAL)[1:-1],
    ):
        assert escaped not in written
    assert "a-newline" not in written
    # And the value really was on that path: a test that passed because the
    # robot never quoted anything would prove nothing at all.
    assert REDACTED in written


def test_an_apply_reports_its_steps_and_the_settings_it_moved() -> None:
    """One report, with the fields a script reads and the line a person reads."""
    robot = _provisioned({URL: ENDPOINT, INTERVAL: "100"})
    daemon, access = daemon_for(robot)
    reporter, streams = reporter_for(output_format=OutputFormat.JSON)

    code = execute_apply(
        "config apply",
        daemon,
        DECLARED,
        reporter,
        ROBOT,
        preview=False,
        close=access.aclose,
    )

    document = json.loads(streams.result)
    assert code is ExitCode.OK
    assert document["data"]["to_remove"] == [INTERVAL]
    assert document["data"]["to_add"] == [LEVEL]
    assert [row["step"] for row in document["rows"]][-1] == "verify"
    assert access.closed is True


def test_a_preview_run_says_it_changed_nothing() -> None:
    """A summary that read like an apply would be read like one."""
    daemon, _access = daemon_for(FakeRobot())
    reporter, streams = reporter_for(output_format=OutputFormat.JSON)

    code = execute_apply(
        "config apply",
        daemon,
        DECLARED,
        reporter,
        ROBOT,
        preview=True,
    )

    assert code is ExitCode.OK
    assert "nothing was changed" in json.loads(streams.result)["summary"]


def test_an_apply_against_a_robot_that_already_matches_says_there_was_nothing_to_do() -> (
    None
):
    """The summary a second run of the same declaration produces."""
    daemon, _access = daemon_for(_provisioned(DECLARED))
    reporter, streams = reporter_for(output_format=OutputFormat.JSON)

    code = execute_apply(
        "config apply",
        daemon,
        DECLARED,
        reporter,
        ROBOT,
        preview=False,
    )

    assert code is ExitCode.OK
    assert "nothing to do" in json.loads(streams.result)["summary"]
