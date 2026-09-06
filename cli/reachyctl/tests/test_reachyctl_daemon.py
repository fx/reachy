"""What the daemon client asks the robot, and what it makes of the answers.

This is the adapter the shared checks were written against and left unbound in
change 0008, so it is tested against the shape of those checks rather than
against itself: `ping`, `installed_application`, `application_state`,
`effective_configuration` and `announced_identity` are what `reachy_checks`
calls, and each is exercised in the state where the check passes and in the
state where it fails.

Two of these tests are the ones change 0008 turned on. One asserts that the
interpreter is resolved from the robot rather than from a configured path —
installing into a path this tool assumed and then verifying against the same
assumption would agree with itself no matter which environment the daemon was
really using. The other asserts that nothing is cached: a deploy's verification
must be able to see a value change under it, and a client that remembered the
first answer would report the version that was true before the restart.

The interpreter tests then say what change 0021 made true. `reachyctl.daemon`
used to read the unit's start program as an interpreter, and on the stock image
that program is a shell launcher — running it with Python arguments started a
second daemon that contended with the first for its port, its serial device and
its camera. The fake robot models exactly that: anything sent to a start program
that is not an interpreter lands in `wrapper_runs`, so REQ-106 is asserted
against the robot's state rather than against the arguments a tool assembled.
`test_reachyctl_interpreters.py` covers the derivation on its own.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import PurePosixPath

import pytest
from reachyctl_robot import (
    DAEMON_DISTRIBUTION,
    DAEMON_INTERPRETER,
    DROP_IN,
    STOCK_APPLICATIONS,
    STOCK_INTERPRETER,
    STOCK_LAUNCHER,
    FakeRemoteAccess,
    FakeRobot,
    daemon_for,
)
from reachyctl_support import CREDENTIAL, reporter_for

from reachyctl.configure import run_apply
from reachyctl.daemon import (
    DaemonClient,
    DaemonControlError,
    InterpreterResolutionError,
    _json_kind,
)
from reachyctl.errors import CommandError
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

    Nothing is configured, so every part of the answer came from the robot. If
    this client preferred a path of its own, an install and its verification
    would agree with each other while both looked somewhere the daemon does not.
    """
    robot = FakeRobot(
        exec_start="/opt/other/venv/bin/python",
        interpreters={"/opt/other/venv/bin/python": "3.12.3"},
    )
    daemon, _access = daemon_for(robot)

    assert await daemon.interpreter() == "/opt/other/venv/bin/python"


@pytest.mark.asyncio
async def test_a_unit_that_starts_a_wrapper_resolves_the_environment_around_it() -> (
    None
):
    """The stock image, and the reason this resolution exists.

    ReachyMiniOS v0.2.3 starts a shell launcher out of the daemon's own virtual
    environment. Reading that path as an interpreter and handing it Python
    source ran the launcher, which started a second daemon.
    """
    robot = FakeRobot(
        exec_start=STOCK_LAUNCHER,
        interpreters={STOCK_INTERPRETER: "3.12.3"},
    )
    daemon, _access = daemon_for(robot)

    assert await daemon.interpreter() == STOCK_INTERPRETER
    assert robot.wrapper_runs == []


@pytest.mark.asyncio
async def test_no_python_source_ever_reaches_the_unit_s_start_program() -> None:
    """REQ-106, asserted against the robot rather than against an argument list.

    Every question this client asks of the daemon's environment goes through
    the interpreter it resolved, and the fake starts a second daemon the moment
    anything runs the launcher. An empty `wrapper_runs` is that daemon never
    having been started.
    """
    robot = FakeRobot(
        exec_start=STOCK_LAUNCHER,
        interpreters={STOCK_INTERPRETER: "3.12.3"},
        packages={DAEMON_DISTRIBUTION: "4.5.6", DEFAULT_APPLICATION: "2.0"},
    )
    daemon, access = daemon_for(robot)

    await daemon.ping()
    await daemon.installed_application()
    await daemon.application_state()

    assert robot.wrapper_runs == []
    assert not any(command[0] == STOCK_LAUNCHER for command in access.commands)


@pytest.mark.asyncio
async def test_the_environment_the_unit_declares_answers_before_anything_is_derived() -> (
    None
):
    """A unit that says which environment it means is a unit that has answered."""
    robot = FakeRobot(
        exec_start="/usr/lib/reachy/launch",
        environment={"VIRTUAL_ENV": "/venvs/declared/"},
        interpreters={"/venvs/declared/bin/python": "3.12.3"},
    )
    daemon, _access = daemon_for(robot)

    assert await daemon.interpreter() == "/venvs/declared/bin/python"


@pytest.mark.asyncio
async def test_a_console_script_resolves_only_the_environment_the_unit_declares() -> (
    None
):
    """A `bin` directory alone is not an environment, so nothing is derived from one.

    `/usr/local/bin/python` would exist and answer `-V` on many robots and be
    nothing to do with the daemon's packages. What resolves this unit is the
    unit saying which environment it means.
    """
    interpreters = {STOCK_INTERPRETER: "3.12.3"}
    guessing = FakeRobot(
        exec_start="/venvs/mini_daemon/bin/reachy-mini-daemon",
        interpreters=interpreters,
    )
    declaring = FakeRobot(
        exec_start="/venvs/mini_daemon/bin/reachy-mini-daemon",
        environment={"VIRTUAL_ENV": "/venvs/mini_daemon"},
        interpreters=interpreters,
    )
    unguided, _one = daemon_for(guessing)
    guided, _two = daemon_for(declaring)

    with pytest.raises(InterpreterResolutionError):
        await unguided.interpreter()

    assert await guided.interpreter() == STOCK_INTERPRETER
    assert guessing.wrapper_runs == []


@pytest.mark.asyncio
async def test_the_configured_interpreter_is_the_answer_rather_than_a_last_resort() -> (
    None
):
    """An operator who knows which interpreter it is should not have to be right twice.

    The unit declares one and the robot has both. What `--python` names is what
    is used, because it is an answer to the question rather than help with a
    guess.
    """
    robot = FakeRobot(
        exec_start="/opt/other/venv/bin/python",
        interpreters={
            "/opt/other/venv/bin/python": "3.12.3",
            "/usr/bin/python3": "3.12.3",
        },
    )
    daemon, _access = daemon_for(robot, layout=RobotLayout(python="/usr/bin/python3"))

    assert await daemon.interpreter() == "/usr/bin/python3"


@pytest.mark.asyncio
async def test_an_environment_with_no_interpreter_in_it_is_named_not_guessed_at() -> (
    None
):
    """A path that might not be an interpreter is what started the second daemon."""
    robot = FakeRobot(exec_start=STOCK_LAUNCHER, interpreters={})
    daemon, _access = daemon_for(robot)

    with pytest.raises(InterpreterResolutionError) as raised:
        await daemon.interpreter()

    assert STOCK_INTERPRETER in str(raised.value)
    assert "--python" in str(raised.value)
    assert "not run" in str(raised.value)
    assert robot.wrapper_runs == []


@pytest.mark.parametrize(
    "answer",
    [
        "",
        "3",
        "3.12",
        "3.12.3 — wrapper usage: --help for options",
        "3.12.3\nstarting daemon",
    ],
)
@pytest.mark.asyncio
async def test_a_program_that_says_one_word_more_than_a_version_is_not_one(
    answer: str,
) -> None:
    """The probe establishes what everything after it assumes, so it cannot be lax.

    `-V` makes CPython print a complete version and nothing else, so anything
    that prints more is something else — a banner, a wrapper's usage line, a
    launcher announcing what it is about to start — and anything that prints
    LESS is something else too. `Python 3` is eight characters a wrapper can
    emit by accident, and admitting it would hand that wrapper
    `-c '<python source>'` next. The next candidate is tried instead, which
    here is the environment the launcher is installed in.

    Args:
        answer: What the impostor writes after the word.
    """
    robot = FakeRobot(
        exec_start=STOCK_LAUNCHER,
        interpreters={
            "/venvs/impostor/bin/python": answer,
            STOCK_INTERPRETER: "3.12.3",
        },
        environment={"VIRTUAL_ENV": "/venvs/impostor"},
    )
    daemon, _access = daemon_for(robot)

    assert await daemon.interpreter() == STOCK_INTERPRETER


@pytest.mark.asyncio
async def test_a_declared_interpreter_that_is_not_there_is_reported_as_tried() -> None:
    """The unit's start program was a candidate on its name and still had to answer.

    Nothing was withheld here — the message says so rather than accusing the
    unit of starting something it was not allowed to run, which would send an
    operator looking for a wrapper that does not exist.
    """
    robot = FakeRobot(exec_start="/opt/gone/bin/python", interpreters={})
    daemon, _access = daemon_for(robot)

    with pytest.raises(InterpreterResolutionError) as raised:
        await daemon.interpreter()

    assert "starts /opt/gone/bin/python." in str(raised.value)
    assert "not run" not in str(raised.value)
    assert "the interpreter the unit's ExecStart names" in str(raised.value)


@pytest.mark.asyncio
async def test_a_unit_that_is_not_installed_suggests_nothing_and_says_so() -> None:
    """An empty `ExecStart` is what systemd reports for a unit that is not there."""
    robot = FakeRobot(exec_start="", interpreters={})
    daemon, _access = daemon_for(robot)

    with pytest.raises(InterpreterResolutionError) as raised:
        await daemon.interpreter()

    assert "declares no start program" in str(raised.value)
    assert "Nothing on this robot suggested one" in str(raised.value)


@pytest.mark.asyncio
async def test_naming_the_launcher_with_python_still_does_not_run_it() -> None:
    """The one route an operator can open by mistake, closed from the inside.

    `--python` is an answer, and the answer it may not give is the unit's own
    start program. Naming it does not run it: the second daemon is the same
    second daemon whoever asked for it.
    """
    robot = FakeRobot(exec_start=STOCK_LAUNCHER, interpreters={})
    daemon, _access = daemon_for(robot, layout=RobotLayout(python=STOCK_LAUNCHER))

    with pytest.raises(InterpreterResolutionError):
        await daemon.interpreter()

    assert robot.wrapper_runs == []


@pytest.mark.asyncio
async def test_a_configured_path_that_is_not_an_interpreter_is_refused() -> None:
    """The override is an answer, and an answer is still checked before it is trusted."""
    robot = FakeRobot(
        exec_start=STOCK_LAUNCHER,
        interpreters={STOCK_INTERPRETER: "3.12.3"},
    )
    daemon, _access = daemon_for(
        robot,
        layout=RobotLayout(python="/usr/bin/python-there-is-nothing-here"),
    )

    assert await daemon.interpreter() == STOCK_INTERPRETER


@pytest.mark.asyncio
async def test_a_configured_path_no_interpreter_is_named_says_why_it_was_refused() -> (
    None
):
    """An operator whose own answer was refused must not have to guess at it."""
    robot = FakeRobot(exec_start=STOCK_LAUNCHER, interpreters={})
    daemon, _access = daemon_for(robot, layout=RobotLayout(python="/opt/tools/py312"))

    with pytest.raises(InterpreterResolutionError) as raised:
        await daemon.interpreter()

    assert "/opt/tools/py312, was not run either" in str(raised.value)
    assert "a link to it named python" in str(raised.value)


@pytest.mark.parametrize(
    ("version", "also"),
    [
        ("3.12.3", "reachy-mini launcher: starting\n"),
        ("3.", "12.3"),
    ],
)
@pytest.mark.asyncio
async def test_a_program_that_speaks_on_both_streams_is_not_an_interpreter(
    version: str,
    also: str,
) -> None:
    """`-V` puts a bare version on one stream and nothing at all on the other.

    Preferring whichever stream is non-empty would admit a launcher that prints
    the version and then announces itself; concatenating the two would admit a
    version split across them. Neither is what an interpreter does, and either
    would be handed `-c '<python source>'` next.

    Args:
        version: What the impostor writes after the word on standard output.
        also: What it writes to standard error.
    """
    robot = FakeRobot(
        exec_start=STOCK_LAUNCHER,
        interpreters={
            "/venvs/impostor/bin/python": version,
            STOCK_INTERPRETER: "3.12.3",
        },
        noisy_interpreters={"/venvs/impostor/bin/python": also},
        environment={"VIRTUAL_ENV": "/venvs/impostor"},
    )
    daemon, _access = daemon_for(robot)

    assert await daemon.interpreter() == STOCK_INTERPRETER


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
    daemon, _access = daemon_for(FakeRobot(failing={DAEMON_INTERPRETER}))

    with pytest.raises(RobotAccessError, match="what it has installed"):
        await daemon.installed_versions(DAEMON_INTERPRETER, "anything")


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

    assert await installed.installed_versions(DAEMON_INTERPRETER, "absent") == {
        "absent": ""
    }
    with pytest.raises(RobotAccessError, match="not JSON"):
        await unreadable.installed_versions(DAEMON_INTERPRETER, "absent")
    with pytest.raises(RobotAccessError, match="rather than an object"):
        await wrong_shape.installed_versions(DAEMON_INTERPRETER, "absent")


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
    daemon, _access = daemon_for(FakeRobot(failing={DAEMON_INTERPRETER}))

    with pytest.raises(DaemonControlError, match="could not be asked"):
        await daemon.application_state()


@pytest.mark.asyncio
async def test_reading_a_region_that_was_never_written_is_absent_not_empty() -> None:
    """A robot nothing has been applied to is not a robot in a bad state.

    `None` rather than `""`: the two are different robots, and the fuller
    three-state case is below.
    """
    daemon, _access = daemon_for()

    assert await daemon.read_managed_region() is None
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
    robot = FakeRobot(
        exec_start="/opt/other/venv/bin/python",
        interpreters={"/opt/other/venv/bin/python": "3.12.3"},
    )
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


@pytest.mark.asyncio
async def test_the_three_states_of_the_managed_drop_in_are_three_answers() -> None:
    """Absent, present, and present-but-not-ours. Collapsing any two loses a file.

    A region read as ours is a region the next apply rewrites, so the safety of
    owning it wholesale rests entirely on telling "nothing has been applied"
    from "somebody else's file is sitting here". This is the seam that can see
    the difference: `parse_region` is handed a string and cannot.
    """
    absent, _one = daemon_for(FakeRobot())
    written = render_region({"A_SETTING": "1"})
    present, _two = daemon_for(FakeRobot(files={DROP_IN: written}))
    blanked, _three = daemon_for(FakeRobot(files={DROP_IN: ""}))

    assert await absent.read_managed_region() is None
    assert await absent.read_managed_settings() == {}

    assert await present.read_managed_region() == written
    assert await present.read_managed_settings() == {"A_SETTING": "1"}

    # There, and empty. Not "never written": this format never writes an empty
    # file, so something else emptied it.
    assert await blanked.read_managed_region() == ""
    with pytest.raises(MalformedRegionError) as raised:
        await blanked.read_managed_settings()

    # And the operator is told which file to go and look at.
    assert DROP_IN in str(raised.value)


@pytest.mark.asyncio
async def test_a_blanked_drop_in_is_never_silently_overwritten() -> None:
    """The consequence the distinction exists to prevent, asserted end to end."""
    robot = FakeRobot(files={DROP_IN: ""})
    daemon, _access = daemon_for(robot)
    reporter, _streams = reporter_for()

    with pytest.raises(CommandError):
        await run_apply(daemon, {"A_SETTING": "1"}, reporter, preview=False)

    assert robot.files[DROP_IN] == ""


@pytest.mark.asyncio
async def test_an_application_in_the_sibling_environment_is_not_reported_absent() -> (
    None
):
    """The false negative hardware found, and the only thing worse than the error.

    The satellite is installed and running on a stock robot; asking the
    daemon's own environment says it is not there. An operator acting on that
    installs something they already have.
    """
    robot = FakeRobot(
        exec_start=STOCK_LAUNCHER,
        interpreters={STOCK_INTERPRETER: "3.12.3", STOCK_APPLICATIONS: "3.12.3"},
        packages={DAEMON_DISTRIBUTION: "1.9.0"},
        environments={STOCK_APPLICATIONS: {DEFAULT_APPLICATION: "2.0"}},
    )
    daemon, _access = daemon_for(robot)

    installed = await daemon.installed_application()
    reported = await daemon.ping()

    assert installed.installed is True
    assert installed.version == "2.0"
    # The daemon's own version still comes from the daemon's own environment.
    assert reported.version == "1.9.0"
    assert robot.wrapper_runs == []


@pytest.mark.asyncio
async def test_an_application_in_neither_environment_says_where_it_looked() -> None:
    """Absent is only a useful answer beside a statement of where it was sought."""
    robot = FakeRobot(
        exec_start=STOCK_LAUNCHER,
        interpreters={STOCK_INTERPRETER: "3.12.3", STOCK_APPLICATIONS: "3.12.3"},
        environments={STOCK_APPLICATIONS: {}},
    )
    daemon, _access = daemon_for(robot)

    installed = await daemon.installed_application()

    assert installed.installed is False
    assert STOCK_APPLICATIONS in installed.complaint


@pytest.mark.asyncio
async def test_a_single_environment_image_reads_and_installs_where_it_always_did() -> (
    None
):
    """No sibling to find, so the daemon's own environment is the answer."""
    robot = FakeRobot(packages={DEFAULT_APPLICATION: "2.0"})
    daemon, access = daemon_for(robot)
    await daemon.stage(b"not really a wheel", "thing.whl")

    installed = await daemon.installed_application()
    await daemon.install_wheel(PurePosixPath(DEFAULT_STAGING) / "thing.whl")

    assert installed.version == "2.0"
    installs = [command for command in access.commands if "pip" in command]
    assert installs[0][:3] == ["sudo", "-n", DAEMON_INTERPRETER]


@pytest.mark.asyncio
async def test_a_wheel_is_installed_where_the_version_is_read_back_from() -> None:
    """Installing into one environment and verifying another is REQ-051's failure."""
    robot = FakeRobot(
        exec_start=STOCK_LAUNCHER,
        interpreters={STOCK_INTERPRETER: "3.12.3", STOCK_APPLICATIONS: "3.12.3"},
        environments={STOCK_APPLICATIONS: {}},
    )
    daemon, access = daemon_for(robot)
    await daemon.stage(b"not really a wheel", "thing.whl")

    await daemon.install_wheel(PurePosixPath(DEFAULT_STAGING) / "thing.whl")

    installs = [command for command in access.commands if "pip" in command]
    assert installs[0][:3] == ["sudo", "-n", STOCK_APPLICATIONS]


@pytest.mark.asyncio
async def test_the_daemon_s_own_api_answers_the_application_state() -> None:
    """The interface a stock robot actually serves, and no flag to reach it."""
    robot = FakeRobot(daemon_api=True, app_running=True)
    daemon, _access = daemon_for(robot)

    state = await daemon.application_state()

    assert state.running is True
    assert state.detail == "running"


@pytest.mark.asyncio
async def test_a_daemon_running_nothing_is_not_a_daemon_running_this() -> None:
    """The endpoint answers about the current application, and there may be none."""
    robot = FakeRobot(daemon_api=True, app_running=False)
    daemon, _access = daemon_for(robot)

    state = await daemon.application_state()

    assert state.running is False
    assert "running no application" in state.detail


@pytest.mark.asyncio
async def test_a_daemon_running_something_else_says_which() -> None:
    """An operator whose satellite was displaced needs to be told that."""
    robot = FakeRobot(daemon_api=True, app_running=True, current_app="another-app")
    daemon, _access = daemon_for(robot)

    state = await daemon.application_state()

    assert state.running is False
    assert "another-app" in state.detail


@pytest.mark.asyncio
async def test_an_application_that_is_still_starting_is_not_running_yet() -> None:
    """`starting` is one of five states and only one of them is up."""
    robot = FakeRobot(daemon_api=True, app_running=True, app_state="starting")
    daemon, _access = daemon_for(robot)

    state = await daemon.application_state()

    assert state.running is False
    assert state.detail == "starting"


@pytest.mark.asyncio
async def test_an_image_with_no_api_is_asked_through_the_control_module() -> None:
    """The container target the provisioning gate runs against is that image."""
    robot = FakeRobot(daemon_api=False, app_running=True, app_detail="active")
    daemon, access = daemon_for(robot)

    state = await daemon.application_state()

    assert state.running is True
    assert any("-m" in command for command in access.commands)


@pytest.mark.asyncio
async def test_an_api_that_answered_and_refused_is_not_retried_elsewhere() -> None:
    """Replacing its reason with another interface's would debug the wrong thing."""
    robot = FakeRobot(daemon_api=True, control_succeeds=False)
    daemon, access = daemon_for(robot)

    outcome = await daemon.start_application()

    assert outcome.ok is False
    assert "400" in outcome.stderr
    assert not any("-m" in command for command in access.commands)


@pytest.mark.asyncio
async def test_starting_and_stopping_go_through_whichever_interface_answers() -> None:
    """One seam, two implementations, and the robot decides which."""
    served = FakeRobot(daemon_api=True)
    module = FakeRobot(daemon_api=False)
    over_api, _one = daemon_for(served)
    over_module, _two = daemon_for(module)

    await over_api.start_application()
    await over_module.start_application()

    # Asserted as a pair rather than one at a time: a narrowing type checker
    # reads the second half of this test as unreachable otherwise.
    assert (served.app_running, module.app_running) == (True, True)

    await over_api.stop_application()
    await over_module.stop_application()

    assert (served.app_running, module.app_running) == (False, False)


@pytest.mark.asyncio
async def test_a_robot_serving_neither_control_interface_names_both() -> None:
    """The released image has no control module, and an image may have no API.

    Told only about the module, an operator would go looking for a module. The
    reason the API was not there is the other half of the answer.
    """
    robot = FakeRobot(daemon_api=False, control_runs=False)
    daemon, _access = daemon_for(robot)

    with pytest.raises(DaemonControlError) as raised:
        await daemon.application_state()

    assert "could not be reached" in str(raised.value)
    assert "control module could not be run" in str(raised.value)


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("not json at all", "not JSON"),
        ("[1, 2, 3]", "rather than an object"),
    ],
)
@pytest.mark.asyncio
async def test_an_api_answering_with_something_unreadable_is_a_fault(
    answer: str,
    expected: str,
) -> None:
    """Reporting "not running" for it would be a guess dressed as a reading.

    Args:
        answer: What the API wrote instead of a status document.
        expected: What the complaint has to say about it.
    """
    robot = FakeRobot(daemon_api=True, api_stdout=answer)
    daemon, _access = daemon_for(robot)

    with pytest.raises(DaemonControlError, match=expected):
        await daemon.application_state()


@pytest.mark.asyncio
async def test_an_api_answering_with_an_error_status_is_a_fault() -> None:
    """It has an API and it refused; that reason is the one worth reporting."""
    robot = FakeRobot(daemon_api=True, api_refuses=True)
    daemon, _access = daemon_for(robot)

    with pytest.raises(DaemonControlError, match="refused the request"):
        await daemon.application_state()


@pytest.mark.asyncio
async def test_a_status_document_missing_its_state_is_read_for_what_it_has() -> None:
    """A daemon that named the application but not its state has still answered."""
    robot = FakeRobot(
        daemon_api=True,
        api_stdout=(
            '{"info": {"name": "' + DEFAULT_APPLICATION + '"}, '
            '"state": null, "error": "boom"}'
        ),
    )
    daemon, _access = daemon_for(robot)

    state = await daemon.application_state()

    assert state.running is False
    assert "did not name" in state.detail
    assert "boom" in state.detail


@pytest.mark.parametrize(
    ("answer", "field"),
    [
        ('{"state": "running"}', "info"),
        ('{"info": null, "state": "running"}', "info"),
        ('{"info": "reachy", "state": "running"}', "info"),
        ('{"info": [], "state": "running"}', "info"),
        ('{"info": {}, "state": "running"}', "info.name"),
        ('{"info": {"name": null}, "state": "running"}', "info.name"),
        ('{"info": {"name": 7}, "state": "running"}', "info.name"),
        ('{"info": {"name": ""}, "state": "running"}', "info.name"),
    ],
)
@pytest.mark.asyncio
async def test_an_answer_that_names_no_application_is_not_a_different_one(
    answer: str,
    field: str,
) -> None:
    """An incomplete answer is a different fault from a displaced application.

    We know the daemon is running something and we know it did not say what.
    We do NOT know it is another application, and telling an operator it is
    sends them hunting a rogue one that may not exist — a different problem
    with a different fix. A JSON API can put `null`, a number or an empty
    string in that field, and none of them is a name.

    Args:
        answer: The status document the daemon returned.
        field: The part of it the message has to point at.
    """
    robot = FakeRobot(daemon_api=True, api_stdout=answer)
    daemon, _access = daemon_for(robot)

    state = await daemon.application_state()

    assert state.running is False
    assert "did not say which application" in state.detail
    assert field in state.detail
    assert "instead" not in state.detail


@pytest.mark.asyncio
async def test_an_environment_that_changed_under_the_client_is_named() -> None:
    """Nothing here is memoised, so an answer can stop being true mid-question.

    The daemon's interpreter answers the first question and is gone by the
    second. The application environment then resolves to nothing, and the
    client says so rather than reaching for a path it invented.
    """
    robot = FakeRobot(
        exec_start=STOCK_LAUNCHER,
        interpreters={STOCK_INTERPRETER: "3.12.3"},
        version_answers=1,
    )
    daemon, _access = daemon_for(robot)

    with pytest.raises(InterpreterResolutionError) as raised:
        await daemon.application_interpreter()

    assert "runs applications from" in str(raised.value)
    assert "--python" in str(raised.value)


@pytest.mark.parametrize("version", ["3.12.3", "3.13.0rc1", "3.13.0rc1+"])
@pytest.mark.asyncio
async def test_a_complete_version_is_accepted_including_a_pre_release(
    version: str,
) -> None:
    """The pattern is the shape `-V` produces, and these are all of them.

    A pre-release carries a release-level suffix and one built from a checkout
    carries a `+`. Narrower than what CPython prints would refuse a real
    interpreter, which costs an operator a link and a `--python` for no safety
    at all.

    Args:
        version: What the interpreter answers with.
    """
    robot = FakeRobot(
        exec_start=STOCK_LAUNCHER,
        interpreters={STOCK_INTERPRETER: version},
    )
    daemon, _access = daemon_for(robot)

    assert await daemon.interpreter() == STOCK_INTERPRETER


@pytest.mark.asyncio
async def test_an_empty_body_is_not_a_daemon_running_nothing() -> None:
    """Two different robots, and substituting one for the other invented a fact.

    A daemon that answers `null` is running no application. A daemon that
    answers with no body at all has told us nothing, and reporting that as "no
    application is running" is a false sentence about a real robot.
    """
    robot = FakeRobot(daemon_api=True, api_stdout="")
    daemon, _access = daemon_for(robot)

    with pytest.raises(DaemonControlError, match="not JSON"):
        await daemon.application_state()


@pytest.mark.parametrize(
    ("failure", "kind"),
    [
        ("7", "a number"),
        ('{"code": 5}', "an object"),
        ('["a", "b"]', "an array"),
        ("true", "a boolean"),
    ],
)
@pytest.mark.asyncio
async def test_an_error_that_is_not_a_message_is_reported_by_type(
    failure: str,
    kind: str,
) -> None:
    """Silently dropping it leaves an operator with nothing to go on.

    Its presence and its type are reported, and its content is not — see the
    test below for why. The type is named in JSON's vocabulary, because what
    the operator is looking at is an API answer rather than this process.

    Args:
        failure: What the daemon put in its `error` field.
        kind: What the detail has to call it.
    """
    robot = FakeRobot(
        daemon_api=True,
        api_stdout=(
            '{"info": {"name": "' + DEFAULT_APPLICATION + '"}, '
            '"state": "error", "error": ' + failure + "}"
        ),
    )
    daemon, _access = daemon_for(robot)

    state = await daemon.application_state()

    assert state.running is False
    assert kind in state.detail
    assert "withheld" in state.detail
    assert "daemon's own log" in state.detail


@pytest.mark.asyncio
async def test_no_part_of_an_unreadable_error_reaches_the_output() -> None:
    """The reason the content is withheld, asserted rather than described.

    Rendering it would mean re-encoding it, and escaping a quote, a backslash
    or a newline is exactly what stops a redactor seeded with a raw secret from
    matching it. This field carries whatever went wrong, which includes
    whatever was being sent at the time.
    """
    robot = FakeRobot(
        daemon_api=True,
        api_stdout=(
            '{"info": {"name": "' + DEFAULT_APPLICATION + '"}, '
            '"state": "error", "error": {"sent": {"credential": "' + CREDENTIAL + '"}}}'
        ),
    )
    daemon, _access = daemon_for(robot)

    state = await daemon.application_state()

    assert CREDENTIAL not in state.detail
    assert "credential" not in state.detail
    assert "sent" not in state.detail
    assert "an object" in state.detail


@pytest.mark.asyncio
async def test_an_error_the_daemon_wrote_is_quoted_verbatim() -> None:
    """A string is the daemon's own message and reaches the redactor unaltered."""
    robot = FakeRobot(
        daemon_api=True,
        api_stdout=(
            '{"info": {"name": "' + DEFAULT_APPLICATION + '"}, '
            '"state": "error", "error": "it fell over"}'
        ),
    )
    daemon, _access = daemon_for(robot)

    state = await daemon.application_state()

    assert state.detail == "error: it fell over"


def test_a_value_json_cannot_produce_still_gets_an_honest_name() -> None:
    """The one branch a status document cannot reach, and it must not lie either.

    `json.loads` yields exactly seven kinds and the caller has already dealt
    with two of them, so nothing a daemon sends arrives here. Naming this case
    "an array" to save a line would be the same defect as every other one on
    this branch: a sentence true of the values the author had in mind and false
    of one the code reaches. Called directly, because a robot cannot produce it.
    """
    assert _json_kind(object()) == (
        "neither a message nor anything else this tool can name"
    )
