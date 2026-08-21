"""What the daemon client asks the robot, and what it makes of the answers.

This is the adapter the shared checks were written against and left unbound in
change 0008, so it is tested against the shape of those checks rather than
against itself: `ping`, `installed_application`, `application_state`,
`effective_configuration` and `announced_identity` are what `reachy_checks`
calls, and each is exercised in the state where the check passes and in the
state where it fails.

Two of these tests are the ones the whole change turns on. One asserts that the
interpreter is taken from the daemon's own unit rather than from a configured
path — installing into a path this tool assumed and then verifying against the
same assumption would agree with itself no matter which environment the daemon
was really using. The other asserts that nothing is cached: a deploy's
verification must be able to see a value change under it, and a client that
remembered the first answer would report the version that was true before the
restart.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import PurePosixPath

import pytest
from reachyctl_robot import (
    DAEMON_DISTRIBUTION,
    DROP_IN,
    FakeRemoteAccess,
    FakeRobot,
    daemon_for,
)

from reachyctl.daemon import DaemonClient, DaemonControlError
from reachyctl.managed import MalformedRegionError, render_region
from reachyctl.robot import (
    DEFAULT_APPLICATION,
    DEFAULT_STAGING,
    CommandOutcome,
    RobotAccessError,
    RobotLayout,
)


@pytest.mark.asyncio
async def test_a_healthy_daemon_answers_with_its_version() -> None:
    """The version comes from the environment the daemon runs, not from the unit."""
    daemon, _access = daemon_for()

    info = await daemon.ping()

    assert info.responding is True
    assert info.version == "4.5.6"


@pytest.mark.asyncio
async def test_a_unit_that_is_not_installed_is_a_different_fault_from_one_that_is_stopped() -> (
    None
):
    """An operator sent to the wrong question loses an afternoon."""
    missing, _one = daemon_for(FakeRobot(load_state="not-found"))
    stopped, _two = daemon_for(FakeRobot(active=False))

    absent = await missing.ping()
    inactive = await stopped.ping()

    assert "not installed on this robot" in absent.complaint
    assert "inactive (dead)" in inactive.complaint
    assert absent.responding is False
    assert inactive.responding is False


@pytest.mark.asyncio
async def test_the_interpreter_is_the_one_the_daemon_actually_runs() -> None:
    """Asking rather than assuming is what makes verification mean anything.

    The layout carries a different path from the one the unit declares. If this
    client preferred the configured one, an install and its verification would
    agree with each other while both looked somewhere the daemon does not.
    """
    robot = FakeRobot(exec_start="/opt/other/venv/bin/python")
    daemon, _access = daemon_for(robot, layout=RobotLayout(python="/usr/bin/python3"))

    assert await daemon.interpreter() == "/opt/other/venv/bin/python"


@pytest.mark.asyncio
async def test_the_configured_interpreter_is_used_only_when_the_unit_cannot_be_read() -> (
    None
):
    """A fallback that is never reached is a fallback nobody can trust."""
    robot = FakeRobot(exec_start="")
    daemon, _access = daemon_for(robot, layout=RobotLayout(python="/usr/bin/python3"))

    assert await daemon.interpreter() == "/usr/bin/python3"


@pytest.mark.asyncio
async def test_an_application_that_is_not_installed_says_so_rather_than_erroring() -> (
    None
):
    """Reporting it as not installed is the check's job; raising would be an accident."""
    daemon, _access = daemon_for()

    installed = await daemon.installed_application()

    assert installed.installed is False
    assert "is not installed" in installed.complaint


@pytest.mark.asyncio
async def test_an_installed_application_reports_the_version_the_environment_holds() -> (
    None
):
    """One round trip answers for every distribution the caller named."""
    robot = FakeRobot(
        packages={DAEMON_DISTRIBUTION: "4.5.6", DEFAULT_APPLICATION: "2.0"}
    )
    daemon, access = daemon_for(robot)

    installed = await daemon.installed_application()

    assert installed.installed is True
    assert installed.version == "2.0"
    assert sum(1 for command in access.commands if "-c" in command) == 1


@pytest.mark.asyncio
async def test_nothing_is_cached_so_a_version_can_change_under_the_client() -> None:
    """The failure this change exists to catch is an answer that was true a moment ago."""
    robot = FakeRobot(packages={DEFAULT_APPLICATION: "1.0"})
    daemon, _access = daemon_for(robot)

    before = await daemon.installed_application()
    robot.packages[DEFAULT_APPLICATION] = "2.0"
    after = await daemon.installed_application()

    assert before.version == "1.0"
    assert after.version == "2.0"


@pytest.mark.asyncio
async def test_an_environment_that_cannot_be_asked_is_a_fault_not_an_empty_answer() -> (
    None
):
    """Answering "nothing is installed" would be the exact failure REQ-051 detects.

    A deploy's verification asks this and fails when the version is not there.
    An environment that did not answer, reported as an environment holding
    nothing, would make every deploy against an unreachable interpreter report a
    version mismatch that never happened.
    """
    daemon, _access = daemon_for(FakeRobot(failing={"/opt/reachy/venv/bin/python"}))

    with pytest.raises(RobotAccessError, match="what it has installed"):
        await daemon.installed_versions("anything")


@pytest.mark.asyncio
async def test_an_empty_environment_is_an_answer_and_an_unreadable_one_is_not() -> None:
    """The distinction the whole module turns on, at the one method that reads a version.

    An environment that answered and holds nothing has told us something. One
    that answered with nonsense has not, and saying "nothing is installed" for
    it would be a wrong answer presented as a successful read.
    """
    empty = FakeRobot()
    empty.packages = {}
    installed, _one = daemon_for(empty)
    unreadable, _two = daemon_for(FakeRobot(metadata_stdout="not json at all"))
    wrong_shape, _three = daemon_for(FakeRobot(metadata_stdout="[1, 2, 3]"))

    assert await installed.installed_versions("absent") == {"absent": ""}
    with pytest.raises(RobotAccessError, match="not JSON"):
        await unreadable.installed_versions("absent")
    with pytest.raises(RobotAccessError, match="rather than an object"):
        await wrong_shape.installed_versions("absent")


@pytest.mark.asyncio
async def test_the_effective_environment_is_read_from_systemd_and_shell_quoted() -> (
    None
):
    """A value with a space in it is one item, not two."""
    robot = FakeRobot(
        environment={"A_SETTING": "one two", "B_SETTING": "three"},
    )
    daemon, _access = daemon_for(robot)

    assert await daemon.effective_configuration() == {
        "A_SETTING": "one two",
        "B_SETTING": "three",
    }


@pytest.mark.asyncio
async def test_an_environment_that_cannot_be_read_is_a_fault_not_an_empty_one() -> None:
    """An empty mapping would be a different robot from one that did not answer.

    `config diff` would report every declared setting as missing, and an
    apply's verification would fail a change that had worked.
    """
    daemon, _access = daemon_for(FakeRobot(failing={"systemctl"}))

    with pytest.raises(RobotAccessError, match="could not read"):
        await daemon.effective_configuration()


@pytest.mark.asyncio
async def test_the_announced_identity_is_read_out_of_the_environment() -> None:
    """It is a setting, so it comes from where settings come from."""
    robot = FakeRobot(environment={"REACHY_HOME_ASSISTANT_IDENTITY": "reachy-example"})
    daemon, _access = daemon_for(robot)

    assert await daemon.announced_identity() == "reachy-example"


@pytest.mark.asyncio
async def test_a_robot_announcing_nothing_answers_with_an_empty_identity() -> None:
    """Which the identity check reads as "announces none" rather than as a fault."""
    daemon, _access = daemon_for()

    assert await daemon.announced_identity() == ""


@pytest.mark.asyncio
async def test_the_application_state_comes_from_the_daemons_own_control() -> None:
    """Both states, because a lifecycle command has to be able to see either."""
    running, _one = daemon_for(FakeRobot(app_running=True, app_detail="active"))
    stopped, _two = daemon_for(FakeRobot(app_running=False, app_detail="inactive"))

    assert (await running.application_state()).running is True
    assert (await stopped.application_state()).running is False
    assert (await stopped.application_state()).detail == "inactive"


@pytest.mark.asyncio
async def test_a_control_that_answers_with_something_unreadable_names_the_module() -> (
    None
):
    """The likeliest cause is a daemon spelling it differently, which is an option away."""
    daemon, _access = daemon_for(
        FakeRobot(control_stdout="the application is fine, thanks")
    )

    with pytest.raises(DaemonControlError, match=r"reachy_mini\.apps"):
        await daemon.application_state()


@pytest.mark.asyncio
async def test_a_control_that_could_not_be_run_is_a_fault_not_a_stopped_application() -> (
    None
):
    """Reporting "not running" for it would make `app stop` succeed over silence.

    The command would find the application already stopped, do nothing, and
    exit zero, having learned nothing about it at all.
    """
    daemon, _access = daemon_for(FakeRobot(failing={"/opt/reachy/venv/bin/python"}))

    with pytest.raises(DaemonControlError, match="could not be run"):
        await daemon.application_state()


@pytest.mark.asyncio
async def test_reading_a_region_that_was_never_written_is_empty() -> None:
    """A robot nothing has been applied to is not a robot in a bad state."""
    daemon, _access = daemon_for()

    assert await daemon.read_managed_region() == ""
    assert await daemon.read_managed_settings() == {}


@pytest.mark.asyncio
async def test_reading_a_region_something_else_wrote_is_refused() -> None:
    """Rewriting it regardless is how two tools start reverting each other."""
    robot = FakeRobot(files={DROP_IN: "[Service]\nEnvironment=A=1\n"})
    daemon, _access = daemon_for(robot)

    with pytest.raises(MalformedRegionError):
        await daemon.read_managed_settings()


@pytest.mark.asyncio
async def test_writing_a_region_stages_it_installs_it_and_reloads_systemd() -> None:
    """A half-written drop-in is a daemon that will not start, so it is not written in place."""
    robot = FakeRobot()
    daemon, access = daemon_for(robot)
    content = render_region({"A_SETTING": "1"})

    await daemon.write_managed_region(content)

    assert robot.files[DROP_IN] == content
    verbs = [command for command in access.commands if command[0] != "<upload>"]
    assert ["sudo", "-n", "systemctl", "daemon-reload"] in verbs
    assert any("install" in command for command in verbs)


@pytest.mark.asyncio
async def test_a_write_that_could_not_be_installed_says_which_step_failed() -> None:
    """Saying only that an apply failed sends an operator to the logs; a step name does not."""
    daemon, _access = daemon_for(FakeRobot(failing={"install"}))

    with pytest.raises(RobotAccessError, match="could not install the managed drop-in"):
        await daemon.write_managed_region(render_region({"A_SETTING": "1"}))


@pytest.mark.asyncio
async def test_a_write_that_could_not_make_its_directory_says_so() -> None:
    """The other failing step, named separately for the same reason."""
    daemon, _access = daemon_for(FakeRobot(failing={"mkdir"}))

    with pytest.raises(RobotAccessError, match="staging directory"):
        await daemon.write_managed_region(render_region({"A_SETTING": "1"}))


@pytest.mark.asyncio
async def test_installing_a_wheel_uses_the_interpreter_the_daemon_runs() -> None:
    """Which is the whole difference between installing and installing somewhere useful."""
    robot = FakeRobot(exec_start="/opt/other/venv/bin/python")
    daemon, access = daemon_for(robot)
    await daemon.stage(b"not really a wheel", "thing.whl")

    outcome = await daemon.install_wheel(
        PurePosixPath(DEFAULT_STAGING) / "thing.whl",
    )

    assert outcome.ok is False  # not a wheel, and the fake says so like pip would
    installs = [command for command in access.commands if "pip" in command]
    assert installs[0][:4] == ["sudo", "-n", "/opt/other/venv/bin/python", "-m"]


@pytest.mark.asyncio
async def test_a_journal_read_filters_by_the_application_and_the_unit() -> None:
    """A search of the unit's text would include the daemon's line about starting it."""
    robot = FakeRobot(journal=["first line", "second line"])
    daemon, access = daemon_for(robot)

    lines = [line async for line in daemon.journal(lines=10, follow=False, since="-1h")]

    assert lines == ["first line", "second line"]
    command = access.commands[-1]
    assert f"SYSLOG_IDENTIFIER={DEFAULT_APPLICATION}" in command
    assert "--unit" in command
    assert "--since" in command
    assert "--follow" not in command


@pytest.mark.asyncio
async def test_following_the_journal_asks_for_it() -> None:
    """The other branch of the same command, because `--follow` is the point of it."""
    daemon, access = daemon_for()

    assert [line async for line in daemon.journal(lines=5, follow=True)] == []
    assert "--follow" in access.commands[-1]


@pytest.mark.asyncio
async def test_an_account_that_is_already_root_sends_no_sudo() -> None:
    """`sudo` may not even be installed on the robot's image."""
    access = FakeRemoteAccess(FakeRobot())
    daemon = DaemonClient(access, RobotLayout(), elevate=False)

    await daemon.restart_daemon()

    assert access.commands[-1][0] == "systemctl"


@pytest.mark.asyncio
async def test_a_unit_systemd_will_not_report_on_is_a_fault_not_a_stopped_daemon() -> (
    None
):
    """`systemctl` refusing is a robot that told us nothing, not one that is down.

    A unit that is not installed answers with empty properties rather than
    failing, so an empty answer means what it says and this one does not.
    """
    daemon, _access = daemon_for(FakeRobot(failing={"systemctl"}))

    with pytest.raises(RobotAccessError, match="could not read"):
        await daemon.ping()


@pytest.mark.asyncio
async def test_a_control_answering_with_a_list_rather_than_an_object_is_refused() -> (
    None
):
    """It is JSON, and it is still not the answer this tool knows how to read."""
    daemon, _access = daemon_for(FakeRobot(control_stdout="[1, 2, 3]"))

    with pytest.raises(DaemonControlError, match="rather than an object"):
        await daemon.application_state()


@pytest.mark.asyncio
async def test_the_staging_directory_is_narrowed_to_the_connecting_account() -> None:
    """The managed region passes through it, and a setting is where a credential lives.

    `chmod` runs on every call rather than only on creation, because
    `mkdir --parents` leaves an existing directory's mode alone.
    """
    robot = FakeRobot()
    daemon, _access = daemon_for(robot)

    await daemon.stage(b"something", "thing")

    assert robot.modes[DEFAULT_STAGING] == "0700"


@pytest.mark.asyncio
async def test_the_staged_region_is_removed_after_it_is_installed() -> None:
    """`install` copies, so without this the whole region is left on the robot."""
    robot = FakeRobot()
    daemon, _access = daemon_for(robot)

    await daemon.write_managed_region(render_region({"A_SETTING": "1"}))

    assert f"{DEFAULT_STAGING}/managed.conf" not in robot.files
    assert robot.files[DROP_IN]


@pytest.mark.asyncio
async def test_the_staged_region_is_removed_even_when_the_install_failed() -> None:
    """The path that fails is exactly the one that would otherwise leave it there."""
    robot = FakeRobot(failing={"install"})
    daemon, _access = daemon_for(robot)

    with pytest.raises(RobotAccessError):
        await daemon.write_managed_region(render_region({"A_SETTING": "1"}))

    assert f"{DEFAULT_STAGING}/managed.conf" not in robot.files


@pytest.mark.asyncio
async def test_a_staged_file_that_could_not_be_removed_is_said_out_loud() -> None:
    """Best effort, and not silent: a file left on the robot is worth knowing about."""
    said: list[str] = []
    access = FakeRemoteAccess(FakeRobot(failing={"rm"}))
    daemon = DaemonClient(access, RobotLayout(), elevate=True, complain=said.append)

    await daemon.discard(PurePosixPath(DEFAULT_STAGING) / "thing")

    assert said
    assert "could not remove" in said[0]


@pytest.mark.asyncio
async def test_a_drop_in_that_is_there_and_unreadable_is_a_fault() -> None:
    """Treating it as never written would overwrite whatever is actually in it."""
    daemon, _access = daemon_for(FakeRobot(failing={"cat"}))

    with pytest.raises(RobotAccessError, match="could not read"):
        await daemon.read_managed_region()


@pytest.mark.asyncio
async def test_the_configuration_read_quotes_nothing_the_robot_wrote() -> None:
    """It is the read that teaches the redactor what to scrub, so nothing can scrub it.

    Every other message may quote the robot verbatim, because by the time one is
    produced the redactor knows the robot's secret values. This one is produced
    while learning them, so it says the command and the status and withholds the
    output — and says that it is withholding it.
    """
    robot = FakeRobot(
        failing={"systemctl"},
        environment={"REACHY_GROUNDSTATION_CREDENTIAL": "example-not-a-real-secret"},
        leaky=True,
    )
    daemon, _access = daemon_for(robot)

    with pytest.raises(RobotAccessError) as raised:
        await daemon.effective_configuration()

    message = str(raised.value)
    assert "withheld" in message
    assert "exited 1" in message
    assert "this robot was told to refuse" not in message


@pytest.mark.asyncio
async def test_a_cleanup_over_a_broken_link_complains_rather_than_raising() -> None:
    """It is called from the `finally` of a step that may already be failing.

    Letting the link failure out would replace the reason a deploy failed with a
    message about tidying up.
    """
    said: list[str] = []

    class Broken(FakeRemoteAccess):
        """A link that has gone while a step was in flight."""

        async def run(self, command: Sequence[str]) -> CommandOutcome:
            """Fail every command.

            Args:
                command: Ignored.

            Returns:
                Never.

            Raises:
                RobotAccessError: Always.
            """
            del command
            message = "the link to the robot failed"
            raise RobotAccessError(message)

    daemon = DaemonClient(
        Broken(FakeRobot()),
        RobotLayout(),
        elevate=True,
        complain=said.append,
    )

    await daemon.discard(PurePosixPath(DEFAULT_STAGING) / "thing")

    assert said
    assert "could not remove" in said[0]
