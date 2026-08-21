"""What `deploy` does, in what order, and what makes it call itself successful.

REQ-051's scenario is the one this module exists for: a deploy where the package
installs and the daemon goes on running the previous version. Every step of that
deploy exits zero, so a test that only asserted "the steps ran" would pass on the
broken case. What is asserted instead is the version the robot reports afterwards
and the fact that the command names it.

The wheel is the in-memory fixture from `reachyctl_fixture_wheel`, with no
application in it at all, which is what makes this change testable before the
satellite exists.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

import pytest
from reachyctl_fixture_wheel import FIXTURE_DISTRIBUTION, FIXTURE_VERSION, fixture_wheel
from reachyctl_robot import DROP_IN, FakeRobot, daemon_for
from reachyctl_support import reporter_for

from reachyctl.deploy import (
    RESTART_WARNING,
    DeployOutcome,
    DeployPlan,
    execute,
    report_for,
    run_deploy,
)
from reachyctl.exits import ExitCode
from reachyctl.output import OutputFormat
from reachyctl.robot import RobotLayout
from reachyctl.steps import StepLog
from reachyctl.wheels import Wheel, describe_wheel

if TYPE_CHECKING:
    from collections.abc import Sequence

    from reachyctl.steps import StepResult

# The robot as an operator addressed it. RFC 5737 TEST-NET-1 and a placeholder
# account — see the root AGENTS.md.
ROBOT: Final = "operator@192.0.2.10:22"

LAYOUT: Final = RobotLayout(application=FIXTURE_DISTRIBUTION)


def _wheel(version: str = FIXTURE_VERSION) -> Wheel:
    """Build the fixture wheel and read it as `deploy` would.

    Args:
        version: The version to build.

    Returns:
        The wheel.
    """
    return describe_wheel(*fixture_wheel(version=version))


def _plan(version: str = FIXTURE_VERSION, *, preview: bool = False) -> DeployPlan:
    """Build a plan that sends the fixture wheel.

    Args:
        version: The version to send.
        preview: Whether this is a preview.

    Returns:
        The plan.
    """
    return DeployPlan(
        obtain=lambda: _wheel(version),
        origin="from this suite's fixture",
        preview=preview,
    )


def _named(results: Sequence[StepResult]) -> dict[str, StepResult]:
    """Index a run's steps by name.

    Args:
        results: What happened.

    Returns:
        The steps by name.
    """
    return {result.name: result for result in results}


@pytest.mark.asyncio
async def test_a_deploy_runs_its_whole_step_sequence_and_verifies_the_result() -> None:
    """Build, reach, transfer, install, restart, start, verify — in that order."""
    robot = FakeRobot()
    daemon, access = daemon_for(robot, layout=LAYOUT)
    reporter, _streams = reporter_for()

    outcome = await run_deploy(_plan(), daemon, reporter)

    assert [result.name for result in outcome.steps.results] == [
        "build",
        "reach",
        "transfer",
        "install",
        "restart",
        "start",
        "verify",
    ]
    assert outcome.ok is True
    assert outcome.running_version == FIXTURE_VERSION
    assert robot.packages[FIXTURE_DISTRIBUTION] == FIXTURE_VERSION
    assert robot.app_running is True
    assert access.commands


#:= docs/specs/reachyctl/index.md#req-051-deployment-verifies-its-own-result
#:% The deploy command MUST confirm that the intended version is installed and
#:% running before it reports success.
@pytest.mark.asyncio
async def test_an_install_that_did_not_take_effect_fails_and_names_what_is_running() -> (
    None
):
    """REQ-051's scenario, exactly.

    The install exits zero, the restart exits zero, and the daemon goes on
    running the previous version. Nothing before the verification step can tell
    this apart from a successful deploy, which is why the verification asks the
    robot rather than reading a status.
    """
    robot = FakeRobot(
        packages={FIXTURE_DISTRIBUTION: "0.9.0"},
        install_takes_effect=False,
    )
    daemon, _access = daemon_for(robot, layout=LAYOUT)
    reporter, _streams = reporter_for()

    outcome = await run_deploy(_plan(), daemon, reporter)

    steps = _named(outcome.steps.results)
    assert steps["install"].outcome.value == "done"
    assert steps["restart"].outcome.value == "done"
    assert steps["verify"].failed is True
    assert "0.9.0" in steps["verify"].detail
    assert FIXTURE_VERSION in steps["verify"].detail
    assert outcome.running_version == "0.9.0"
    assert outcome.ok is False


@pytest.mark.asyncio
async def test_a_deploy_onto_a_robot_with_nothing_installed_fails_and_says_so() -> None:
    """The version is absent rather than wrong, and the check's own words carry it."""
    robot = FakeRobot(install_takes_effect=False)
    daemon, _access = daemon_for(robot, layout=LAYOUT)
    reporter, _streams = reporter_for()

    outcome = await run_deploy(_plan(), daemon, reporter)

    assert outcome.ok is False
    assert "not installed" in _named(outcome.steps.results)["verify"].detail


@pytest.mark.asyncio
async def test_a_version_that_installed_and_will_not_run_fails_the_deploy() -> None:
    """Installed and running are two claims, and REQ-051 asks for both.

    The daemon accepts the start and the application is not running afterwards,
    which is what a crash loop looks like from the other end of a link. Nothing
    in the step statuses says so; only asking the robot does.
    """
    robot = FakeRobot(start_succeeds=False)
    daemon, _access = daemon_for(robot, layout=LAYOUT)
    reporter, _streams = reporter_for()

    outcome = await run_deploy(_plan(), daemon, reporter)

    steps = _named(outcome.steps.results)
    assert steps["start"].outcome.value == "done"
    assert steps["verify"].failed is True
    assert "is installed but is not running" in steps["verify"].detail
    assert outcome.running_version == FIXTURE_VERSION
    assert outcome.ok is False


@pytest.mark.asyncio
async def test_a_restart_that_failed_stops_before_the_application_is_started() -> None:
    """There is nothing to start, and the failing step is the one worth naming."""
    daemon, _access = daemon_for(FakeRobot(restart_succeeds=False), layout=LAYOUT)
    reporter, _streams = reporter_for()

    outcome = await run_deploy(_plan(), daemon, reporter)

    steps = _named(outcome.steps.results)
    assert steps["restart"].failed is True
    assert "start" not in steps
    assert outcome.ok is False


@pytest.mark.asyncio
async def test_a_control_that_refused_the_start_does_not_decide_the_deploy() -> None:
    """The robot's state decides it. A control command's status is not evidence."""
    daemon, _access = daemon_for(
        FakeRobot(control_succeeds=False),
        layout=LAYOUT,
    )
    reporter, _streams = reporter_for()

    outcome = await run_deploy(_plan(), daemon, reporter)

    steps = _named(outcome.steps.results)
    assert steps["start"].failed is True
    assert steps["verify"].failed is True
    assert outcome.ok is False


@pytest.mark.asyncio
async def test_a_robot_whose_daemon_is_not_answering_stops_before_it_transfers() -> (
    None
):
    """Sending a wheel to a robot that cannot install it wastes the slow part."""
    robot = FakeRobot(active=False)
    daemon, access = daemon_for(robot, layout=LAYOUT)
    reporter, _streams = reporter_for()

    outcome = await run_deploy(_plan(), daemon, reporter)

    assert [result.name for result in outcome.steps.results] == ["build", "reach"]
    assert outcome.ok is False
    assert not any(command[0] == "<upload>" for command in access.commands)


@pytest.mark.asyncio
async def test_an_install_that_failed_stops_before_the_restart() -> None:
    """Restarting a daemon over a failed install interrupts the robot for nothing."""
    daemon, _access = daemon_for(FakeRobot(install_succeeds=False), layout=LAYOUT)
    reporter, _streams = reporter_for()

    outcome = await run_deploy(_plan(), daemon, reporter)

    steps = _named(outcome.steps.results)
    assert steps["install"].failed is True
    assert "restart" not in steps
    assert outcome.ok is False


@pytest.mark.asyncio
async def test_the_restart_warning_is_written_before_the_restart_happens() -> None:
    """The point of the warning is the moment in which it can still be acted on.

    Asserting that the line was printed proves nothing about when. So the robot
    records what had already been written to standard error at the moment each
    command arrived, and the assertion is that the warning was already there when
    the restart was sent.
    """
    reporter, streams = reporter_for()
    seen_at_restart: list[str] = []

    def watch(command: Sequence[str]) -> None:
        """Snapshot the diagnostics stream as each command is sent.

        Args:
            command: What is about to run.
        """
        if "restart" in command:
            seen_at_restart.append(streams.diagnostics)

    daemon, _access = daemon_for(FakeRobot(), observer=watch, layout=LAYOUT)

    await run_deploy(_plan(), daemon, reporter)

    assert seen_at_restart, "the restart was never sent"
    assert RESTART_WARNING in seen_at_restart[0]


@pytest.mark.asyncio
async def test_a_running_application_is_warned_about_rather_than_refused() -> None:
    """The change document's open question, resolved: warn, do not refuse."""
    robot = FakeRobot(app_running=True, app_detail="active")
    daemon, _access = daemon_for(robot, layout=LAYOUT)
    reporter, streams = reporter_for()

    outcome = await run_deploy(_plan(), daemon, reporter)

    assert "deploying will interrupt it" in streams.diagnostics
    assert outcome.ok is True


#:= docs/specs/reachyctl/index.md#req-052-configuration-changes-can-be-previewed-without-being-applied
#:% Every command that modifies robot state MUST support a mode that reports the
#:% changes it would make and makes none of them.
@pytest.mark.asyncio
async def test_a_preview_leaves_the_robot_exactly_as_it_found_it() -> None:
    """The guarantee is that nothing happened, so the after-state is what is asserted.

    Not that a plan was printed: a command that printed a perfect plan and then
    deployed anyway would pass that test.
    """
    robot = FakeRobot(
        packages={FIXTURE_DISTRIBUTION: "0.9.0"},
        files={DROP_IN: "left alone"},
    )
    daemon, access = daemon_for(robot, layout=LAYOUT)
    reporter, _streams = reporter_for()
    before = (dict(robot.packages), dict(robot.files), robot.app_running)

    outcome = await run_deploy(_plan(preview=True), daemon, reporter)

    assert (dict(robot.packages), dict(robot.files), robot.app_running) == before
    assert not any(command[0] == "<upload>" for command in access.commands)
    assert not any("pip" in command for command in access.commands)
    assert not any("restart" in command for command in access.commands)
    assert [
        result.outcome.value
        for result in outcome.steps.results
        if result.name in {"transfer", "install", "restart", "start", "verify"}
    ] == ["planned"] * 5
    assert outcome.ok is True


@pytest.mark.asyncio
async def test_a_preview_says_what_would_replace_what() -> None:
    """A plan an operator cannot read is a plan they will not read."""
    robot = FakeRobot(packages={FIXTURE_DISTRIBUTION: "0.9.0"})
    daemon, _access = daemon_for(robot, layout=LAYOUT)
    reporter, _streams = reporter_for()

    outcome = await run_deploy(_plan(preview=True), daemon, reporter)

    steps = _named(outcome.steps.results)
    assert "0.9.0" in steps["install"].detail
    assert FIXTURE_VERSION in steps["install"].detail
    assert RESTART_WARNING in steps["restart"].detail


def test_the_structured_report_carries_the_version_that_is_running() -> None:
    """A script gating on a deploy compares two fields, not two sentences."""
    reporter, streams = reporter_for(output_format=OutputFormat.JSON)
    robot = FakeRobot(
        packages={FIXTURE_DISTRIBUTION: "0.9.0"},
        install_takes_effect=False,
    )
    daemon, _access = daemon_for(robot, layout=LAYOUT)

    code = execute(_plan(), daemon, reporter, ROBOT)

    document = json.loads(streams.result)
    assert code is ExitCode.FAILURE
    assert document["data"]["version"] == FIXTURE_VERSION
    assert document["data"]["running_version"] == "0.9.0"
    assert document["data"]["robot"] == ROBOT
    assert document["ok"] is False
    assert [row["step"] for row in document["rows"]][-1] == "verify"


def test_a_successful_deploy_exits_zero_and_says_what_is_running() -> None:
    """The other end of the same path, through the same rendering."""
    reporter, streams = reporter_for(output_format=OutputFormat.JSON)
    daemon, access = daemon_for(FakeRobot(), layout=LAYOUT)

    code = execute(_plan(), daemon, reporter, ROBOT, access.aclose)

    document = json.loads(streams.result)
    assert code is ExitCode.OK
    assert document["data"]["running_version"] == FIXTURE_VERSION
    assert "verified after the restart" in document["summary"]
    assert access.closed is True


def test_a_report_for_a_deploy_that_never_got_a_wheel_still_renders() -> None:
    """The wheel is obtained by the first step, so a run can fail before there is one."""
    reporter, _streams = reporter_for()
    steps = StepLog(reporter=reporter)
    steps.failed("build", "the wheel could not be obtained")

    report = report_for(
        DeployOutcome(steps=steps, wheel=None, running_version="", preview=False),
        ROBOT,
    )

    assert report.data["application"] is None
    assert report.data["version"] is None
    assert report.ok is False
    assert "failed at build" in report.summary


def test_a_previewed_deploy_says_it_changed_nothing() -> None:
    """A summary that read like a deploy would be read like one."""
    reporter, streams = reporter_for(output_format=OutputFormat.JSON)
    daemon, _access = daemon_for(FakeRobot(), layout=LAYOUT)

    code = execute(_plan(preview=True), daemon, reporter, ROBOT)

    document = json.loads(streams.result)
    assert code is ExitCode.OK
    assert document["data"]["preview"] is True
    assert "nothing was changed" in document["summary"]
