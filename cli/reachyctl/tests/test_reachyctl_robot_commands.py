"""`deploy`, `config` and `app` as an operator types them.

Everything here goes through Click's runner, so argument parsing, the rendering
and the exit status are the real ones. The robot is not: `cli._build_access` is
the one seam replaced, which is what lets a whole deploy run against a robot that
does not exist — and, more to the point, lets a test assert that a command
*never built a link at all*.

That last assertion is reachyctl REQ-053's scenario. The requirement is not that
an invalid value is reported; it is that it is reported without the robot being
contacted, and the only way to test "was not contacted" is to watch the thing
that would have done the contacting.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

import pytest
from reachyctl_fixture_wheel import FIXTURE_DISTRIBUTION, FIXTURE_VERSION, fixture_wheel
from reachyctl_robot import DROP_IN, FakeRemoteAccess, FakeRobot
from typer.testing import CliRunner

from reachyctl import cli
from reachyctl.credentials import ENV_PREFIX
from reachyctl.exits import ExitCode
from reachyctl.managed import parse_region, render_region
from reachyctl.robot import RobotAccessError

if TYPE_CHECKING:
    from pathlib import Path

    from reachyctl.robot import RemoteAccess, RobotTarget

runner = CliRunner()

# RFC 5737 TEST-NET-1 and a placeholder account — see the root AGENTS.md.
ROBOT: Final = "operator@192.0.2.10"

URL: Final = "REACHY_GROUNDSTATION_URL"
LEVEL: Final = "REACHY_SATELLITE_LOG_LEVEL"
INTERVAL: Final = "REACHY_SATELLITE_FRAME_INTERVAL_MS"
ENDPOINT: Final = "ws://192.0.2.10:8000/v1/session"

APPLICATION: Final = ["--application", FIXTURE_DISTRIBUTION]


class Watcher:
    """Records whether a link was ever built, and hands out one when it is.

    Attributes:
        built: One entry per time a command asked for a link. A command that
            refused its arguments locally leaves this empty.
        access: The link that was handed out, once one was.
    """

    def __init__(self, robot: FakeRobot | None = None) -> None:
        """Prepare a watcher over a robot.

        Args:
            robot: The state any link will operate on.
        """
        self.robot = robot if robot is not None else FakeRobot()
        self.built: list[RobotTarget] = []
        self.access: FakeRemoteAccess | None = None

    @property
    def contacted(self) -> bool:
        """Whether anything was ever actually asked of the robot.

        Building a link asks nothing — the connection is opened by the first
        command that needs it — so this is the property that says the robot was
        left alone, and it is true of a command that built a link and then
        refused its arguments before using it.

        Returns:
            True when a command reached the robot.
        """
        return self.access is not None and self.access.connected

    def __call__(self, target: RobotTarget) -> RemoteAccess:
        """Hand out a link, recording that one was asked for.

        Args:
            target: Where the robot is.

        Returns:
            The link.
        """
        self.built.append(target)
        self.access = FakeRemoteAccess(self.robot)
        return self.access


class _Refusing:
    """A link to a robot that is never there.

    One of these rather than a `FakeRemoteAccess` with a flag, because what is
    being modelled is a link that could not be opened at all: nothing runs, and
    every command has to turn that into the same exit status. It carries no
    `stream`, and that is the assertion rather than an omission — every command
    opens the link before it asks anything, so nothing here is ever reached.
    """

    async def connect(self) -> None:
        """Fail to open the link.

        Raises:
            RobotAccessError: Always.
        """
        message = f"cannot reach the robot at {ROBOT}:22"
        raise RobotAccessError(message)

    async def run(self, command: object) -> object:
        """Fail to run anything.

        Args:
            command: Ignored.

        Returns:
            Never.

        Raises:
            RobotAccessError: Always.
        """
        del command
        message = f"cannot reach the robot at {ROBOT}:22"
        raise RobotAccessError(message)

    async def aclose(self) -> None:
        """Let nothing go."""


@pytest.fixture
def watcher(monkeypatch: pytest.MonkeyPatch) -> Watcher:
    """Replace the one seam that reaches a robot.

    Args:
        monkeypatch: How the seam is replaced.

    Returns:
        The watcher, so a test can assert on what was and was not built.
    """
    watching = Watcher()
    monkeypatch.setattr(cli, "_build_access", watching)
    return watching


def _declaration(directory: Path, settings: dict[str, str]) -> Path:
    """Write a declaration document.

    Args:
        directory: Where to write it.
        settings: What it declares.

    Returns:
        Where it is.
    """
    path = directory / "intent.json"
    path.write_text(json.dumps({"configuration": settings}), encoding="utf-8")
    return path


def test_the_help_lists_every_command_the_spec_names() -> None:
    """A tool whose help describes a smaller thing than the spec is a tool nobody finds."""
    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == ExitCode.OK
    for command in ("deploy", "config", "app", "doctor", "probe", "bench"):
        assert command in result.stdout


@pytest.mark.parametrize("group", ["config", "app"])
def test_a_group_with_no_verb_shows_its_verbs(group: str) -> None:
    """Rather than doing something, which is what a group with a default would do.

    Args:
        group: Which group to ask.
    """
    result = runner.invoke(cli.app, [group])

    assert result.exit_code == ExitCode.USAGE


#:= docs/specs/reachyctl/index.md#req-053-configuration-values-are-validated-before-they-are-sent
#:% The tool MUST reject a configuration value that the receiving component would
#:% not accept, before applying it to the robot.
def test_an_out_of_range_value_is_refused_locally_with_no_connection_attempted(
    watcher: Watcher,
) -> None:
    """REQ-053's scenario, both halves.

    The constraint is stated, and nothing was contacted — asserted by watching
    the seam that would have built the link rather than by trusting that it was
    not used.

    Args:
        watcher: Records whether a link was built.
    """
    result = runner.invoke(
        cli.app,
        ["config", "set", f"{INTERVAL}=12000", "--robot", ROBOT],
    )

    assert result.exit_code == ExitCode.CONFIGURATION
    assert INTERVAL in result.stdout
    assert "from 20 to 1000" in result.stdout
    assert watcher.built == []
    assert watcher.access is None
    assert watcher.contacted is False


def test_a_setting_nothing_declares_is_refused_locally(watcher: Watcher) -> None:
    """A typo costs no round trip, and the message lists what is declared."""
    result = runner.invoke(
        cli.app,
        ["config", "set", "REACHY_SATELLITE_FRAME_INTERVAL=100", "--robot", ROBOT],
    )

    assert result.exit_code == ExitCode.CONFIGURATION
    assert INTERVAL in result.stdout
    assert watcher.built == []


def test_an_argument_that_is_not_an_assignment_is_refused_locally(
    watcher: Watcher,
) -> None:
    """`config set LEVEL debug` is the shape people type first."""
    result = runner.invoke(cli.app, ["config", "set", LEVEL, "--robot", ROBOT])

    assert result.exit_code == ExitCode.CONFIGURATION
    assert "NAME=VALUE" in result.stdout
    assert watcher.built == []


def test_the_same_setting_assigned_twice_is_refused_locally(watcher: Watcher) -> None:
    """One of the two would silently win, and nobody could tell which."""
    result = runner.invoke(
        cli.app,
        ["config", "set", f"{LEVEL}=info", f"{LEVEL}=debug", "--robot", ROBOT],
    )

    assert result.exit_code == ExitCode.CONFIGURATION
    assert "assigned twice" in result.stdout
    assert watcher.built == []


def test_a_command_with_no_robot_says_which_option_names_one(
    watcher: Watcher,
) -> None:
    """And no address is defaulted, because an address belongs to whoever runs this."""
    result = runner.invoke(cli.app, ["config", "get"], env={})

    assert result.exit_code == ExitCode.CONFIGURATION
    assert "--robot" in result.stdout
    assert watcher.built == []


def test_an_address_that_is_not_one_is_refused_before_anything_is_built(
    watcher: Watcher,
) -> None:
    """The parse happens above the link, so a typo costs nothing."""
    result = runner.invoke(cli.app, ["config", "get", "--robot", "192.0.2.10"])

    assert result.exit_code == ExitCode.CONFIGURATION
    assert watcher.built == []


def test_the_robot_can_come_from_the_environment(watcher: Watcher) -> None:
    """So a session against one robot does not repeat the address on every line."""
    result = runner.invoke(
        cli.app,
        ["config", "get"],
        env={f"{ENV_PREFIX}ROBOT": ROBOT},
    )

    assert result.exit_code == ExitCode.OK, result.stdout
    assert watcher.built[0].host == "192.0.2.10"


def test_a_deploy_with_neither_a_member_nor_a_wheel_says_so(watcher: Watcher) -> None:
    """There is nothing to send, and nothing was contacted to find that out."""
    result = runner.invoke(cli.app, ["deploy", "--robot", ROBOT])

    assert result.exit_code == ExitCode.CONFIGURATION
    assert "--member" in result.stdout
    assert watcher.built == []


@pytest.mark.filesystem  # `tmp_path` is a real directory; not a unit test
def test_a_deploy_given_both_a_member_and_a_wheel_refuses_to_choose(
    watcher: Watcher,
    tmp_path: Path,
) -> None:
    """A deploy that quietly ignored half of what it was told ends by asserting a version.

    Args:
        watcher: Records whether a link was built.
        tmp_path: Somewhere to name a wheel.
    """
    result = runner.invoke(
        cli.app,
        [
            "deploy",
            "--robot",
            ROBOT,
            "--member",
            "reachyctl",
            "--wheel",
            str(tmp_path / "a.whl"),
        ],
    )

    assert result.exit_code == ExitCode.CONFIGURATION
    assert "exactly one" in result.stdout
    assert watcher.built == []


@pytest.mark.filesystem  # writes a wheel for the command to read; not a unit test
def test_a_deploy_of_the_fixture_wheel_runs_and_verifies_the_running_version(
    watcher: Watcher,
    tmp_path: Path,
) -> None:
    """The whole command, end to end, against a robot with no application on it.

    Args:
        watcher: The seam that hands out the link.
        tmp_path: Where the fixture wheel is written.
    """
    name, content = fixture_wheel()
    (tmp_path / name).write_bytes(content)

    result = runner.invoke(
        cli.app,
        [
            "--output",
            "json",
            "deploy",
            "--robot",
            ROBOT,
            "--wheel",
            str(tmp_path / name),
            *APPLICATION,
        ],
    )

    document = json.loads(result.stdout)
    assert result.exit_code == ExitCode.OK, result.stdout
    assert document["data"]["running_version"] == FIXTURE_VERSION
    assert watcher.robot.packages[FIXTURE_DISTRIBUTION] == FIXTURE_VERSION
    assert watcher.access is not None
    assert watcher.access.closed is True


@pytest.mark.filesystem  # writes a wheel for the command to read; not a unit test
def test_a_deploy_that_did_not_take_effect_exits_failure_and_names_the_version(
    watcher: Watcher,
    tmp_path: Path,
) -> None:
    """REQ-051's scenario as an operator meets it: a status and a sentence.

    Args:
        watcher: The seam that hands out the link.
        tmp_path: Where the fixture wheel is written.
    """
    watcher.robot.install_takes_effect = False
    watcher.robot.packages[FIXTURE_DISTRIBUTION] = "0.9.0"
    name, content = fixture_wheel()
    (tmp_path / name).write_bytes(content)

    result = runner.invoke(
        cli.app,
        [
            "deploy",
            "--robot",
            ROBOT,
            "--wheel",
            str(tmp_path / name),
            *APPLICATION,
        ],
    )

    assert result.exit_code == ExitCode.FAILURE
    assert "0.9.0" in result.stdout
    assert FIXTURE_VERSION in result.stdout


@pytest.mark.filesystem  # writes a wheel for the command to read; not a unit test
def test_a_wheel_that_is_not_one_is_refused_before_the_robot_is_contacted(
    watcher: Watcher,
    tmp_path: Path,
) -> None:
    """The cheapest checks first, so a mistake costs nothing over a slow link.

    Args:
        watcher: Records whether a link was built.
        tmp_path: Where the file is written.
    """
    (tmp_path / "broken-1.0-py3-none-any.whl").write_bytes(b"not a zip")

    result = runner.invoke(
        cli.app,
        [
            "deploy",
            "--robot",
            ROBOT,
            "--wheel",
            str(tmp_path / "broken-1.0-py3-none-any.whl"),
        ],
    )

    assert result.exit_code == ExitCode.CONFIGURATION
    assert "not a readable zip" in result.stdout
    # The link object exists by this point and has been used for nothing: the
    # wheel is read by the deploy's first step, which is above every step that
    # asks the robot anything.
    assert watcher.contacted is False


@pytest.mark.filesystem  # writes a declaration for the command to read; not a unit test
def test_applying_a_declaration_writes_the_region_and_verifies_it(
    watcher: Watcher,
    tmp_path: Path,
) -> None:
    """The document `doctor --intent` reads, applied by `config apply`.

    Args:
        watcher: The seam that hands out the link.
        tmp_path: Where the declaration is written.
    """
    declaration = _declaration(tmp_path, {URL: ENDPOINT, LEVEL: "info"})

    result = runner.invoke(
        cli.app,
        ["config", "apply", "--robot", ROBOT, "--declaration", str(declaration)],
    )

    assert result.exit_code == ExitCode.OK, result.stdout
    assert parse_region(watcher.robot.managed_region) == {URL: ENDPOINT, LEVEL: "info"}


@pytest.mark.filesystem  # writes a declaration for the command to read; not a unit test
def test_previewing_an_apply_leaves_the_region_byte_identical(
    watcher: Watcher,
    tmp_path: Path,
) -> None:
    """Through the command surface, with the after-state as the assertion.

    Args:
        watcher: The seam that hands out the link.
        tmp_path: Where the declaration is written.
    """
    watcher.robot.files[DROP_IN] = render_region({URL: ENDPOINT, INTERVAL: "100"})
    watcher.robot.environment = {URL: ENDPOINT, INTERVAL: "100"}
    before = watcher.robot.managed_region
    declaration = _declaration(tmp_path, {URL: ENDPOINT, LEVEL: "info"})

    result = runner.invoke(
        cli.app,
        [
            "--output",
            "json",
            "config",
            "apply",
            "--robot",
            ROBOT,
            "--declaration",
            str(declaration),
            "--preview",
        ],
    )

    document = json.loads(result.stdout)
    assert result.exit_code == ExitCode.OK, result.stdout
    assert watcher.robot.managed_region == before
    assert watcher.robot.environment == {URL: ENDPOINT, INTERVAL: "100"}
    assert document["data"]["to_remove"] == [INTERVAL]
    assert document["data"]["to_add"] == [LEVEL]


def test_an_apply_with_no_declaration_says_which_option_names_one(
    watcher: Watcher,
) -> None:
    """And nothing is contacted to find it out."""
    result = runner.invoke(cli.app, ["config", "apply", "--robot", ROBOT], env={})

    assert result.exit_code == ExitCode.CONFIGURATION
    assert "--declaration" in result.stdout
    assert watcher.built == []


@pytest.mark.filesystem  # writes a declaration for the command to read; not a unit test
def test_a_declaration_holding_a_value_the_robot_would_refuse_is_refused_locally(
    watcher: Watcher,
    tmp_path: Path,
) -> None:
    """A rejection partway through a multi-step apply leaves partial state.

    Args:
        watcher: Records whether a link was built.
        tmp_path: Where the declaration is written.
    """
    declaration = _declaration(tmp_path, {INTERVAL: "12000"})

    result = runner.invoke(
        cli.app,
        ["config", "apply", "--robot", ROBOT, "--declaration", str(declaration)],
    )

    assert result.exit_code == ExitCode.CONFIGURATION
    assert "from 20 to 1000" in result.stdout
    assert watcher.built == []


@pytest.mark.filesystem  # writes a declaration for the command to read; not a unit test
def test_a_diff_against_a_robot_that_differs_exits_failure(
    watcher: Watcher,
    tmp_path: Path,
) -> None:
    """Which is what makes it usable as a gate, the way `doctor` is.

    Args:
        watcher: The seam that hands out the link.
        tmp_path: Where the declaration is written.
    """
    declaration = _declaration(tmp_path, {LEVEL: "info"})

    result = runner.invoke(
        cli.app,
        ["config", "diff", "--robot", ROBOT, "--declaration", str(declaration)],
    )

    assert result.exit_code == ExitCode.FAILURE
    assert LEVEL in result.stdout
    assert watcher.access is not None
    assert watcher.access.closed is True


def test_setting_one_value_leaves_the_others_where_they_were(
    watcher: Watcher,
) -> None:
    """`set` merges; `apply` is the verb that removes."""
    watcher.robot.files[DROP_IN] = render_region({URL: ENDPOINT})
    watcher.robot.environment = {URL: ENDPOINT}

    result = runner.invoke(
        cli.app,
        ["config", "set", f"{LEVEL}=debug", "--robot", ROBOT],
    )

    assert result.exit_code == ExitCode.OK, result.stdout
    assert parse_region(watcher.robot.managed_region) == {URL: ENDPOINT, LEVEL: "debug"}


def test_reading_a_robots_configuration_reports_what_is_in_force(
    watcher: Watcher,
) -> None:
    """The effective environment, which is the question an operator has."""
    watcher.robot.environment = {URL: ENDPOINT}

    result = runner.invoke(
        cli.app,
        ["--output", "json", "config", "get", "--robot", ROBOT],
    )

    document = json.loads(result.stdout)
    assert result.exit_code == ExitCode.OK, result.stdout
    assert document["rows"][0]["in_force"] == ENDPOINT


def test_starting_and_stopping_the_application_through_the_command_surface(
    watcher: Watcher,
) -> None:
    """Both verbs, so neither is wired to the other's implementation."""
    started = runner.invoke(cli.app, ["app", "start", "--robot", ROBOT, *APPLICATION])
    assert started.exit_code == ExitCode.OK, started.stdout
    assert watcher.robot.app_running is True

    stopped = runner.invoke(cli.app, ["app", "stop", "--robot", ROBOT, *APPLICATION])
    assert stopped.exit_code == ExitCode.OK, stopped.stdout
    assert watcher.robot.app_running is False


def test_reading_the_application_log_through_the_command_surface(
    watcher: Watcher,
) -> None:
    """The result is the lines, and the run's document after them."""
    watcher.robot.journal = ["a line the application wrote"]

    result = runner.invoke(
        cli.app,
        ["app", "logs", "--robot", ROBOT, "--lines", "5", "--since", "-1h"],
    )

    assert result.exit_code == ExitCode.OK, result.stdout
    assert "a line the application wrote" in result.stdout


def test_a_robot_that_cannot_be_reached_costs_an_unreachable_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not a diagnosis: nothing has been learned about the robot.

    Args:
        monkeypatch: How the seam is replaced.
    """
    monkeypatch.setattr(cli, "_build_access", lambda _target: _Refusing())

    result = runner.invoke(cli.app, ["config", "get", "--robot", ROBOT])

    assert result.exit_code == ExitCode.UNREACHABLE
    assert "cannot reach the robot" in result.stdout


def test_diagnosing_a_robot_runs_the_daemon_side_checks(watcher: Watcher) -> None:
    """`doctor` gained a robot in this change, so its skipped checks now run."""
    watcher.robot.packages["reachy-mini-ha-satellite"] = "1.0"
    watcher.robot.app_running = True

    result = runner.invoke(
        cli.app,
        ["--output", "json", "doctor", "--robot", ROBOT],
    )

    document = json.loads(result.stdout)
    rows = {row["check"]: row for row in document["rows"]}
    assert rows["daemon.reachable"]["status"] == "passed"
    assert rows["application.installed"]["status"] == "passed"
    assert rows["application.running"]["status"] == "passed"
    # The resolved target, not the option's text: the same value `deploy`,
    # `config` and `app` report, so one robot is named one way whichever
    # command a script is reading.
    assert document["data"]["robot"] == f"{ROBOT}:22"
    assert watcher.access is not None
    assert watcher.access.closed is True


def test_a_set_with_no_assignment_is_a_usage_error(watcher: Watcher) -> None:
    """Click answers this one, and its status is the one reserved for it.

    `USAGE` is listed in `reachyctl.exits` precisely so nothing else claims the
    number, and a command that re-answered it with a `CONFIGURATION` status
    would be a command a script has to special-case.
    """
    result = runner.invoke(cli.app, ["config", "set", "--robot", ROBOT])

    assert result.exit_code == ExitCode.USAGE
    assert watcher.built == []


@pytest.mark.parametrize(
    "command",
    [
        ["config", "get"],
        ["config", "set", "REACHY_SATELLITE_LOG_LEVEL=info"],
        ["app", "start"],
        ["app", "stop"],
        ["app", "logs"],
    ],
)
def test_every_robot_command_reports_an_unreachable_robot_the_same_way(
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
) -> None:
    """One handler per command, and a command that grew its own would say something else.

    Args:
        monkeypatch: How the seam is replaced.
        command: The command to run.
    """
    monkeypatch.setattr(cli, "_build_access", lambda _target: _Refusing())

    result = runner.invoke(cli.app, [*command, "--robot", ROBOT])

    assert result.exit_code == ExitCode.UNREACHABLE, result.stdout
    assert "cannot reach the robot" in result.stdout


def test_doctor_reports_an_unreachable_robot_as_a_diagnosis_rather_than_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one command for which this is deliberately different.

    Every command above was asked to *do* something and could not, so nothing
    was learned and the status says so. `doctor` was asked to find out whether
    the robot is there; being told it is not is the answer, exactly as it is for
    a groundstation that is down.

    Args:
        monkeypatch: How the seam is replaced.
    """
    monkeypatch.setattr(cli, "_build_access", lambda _target: _Refusing())

    result = runner.invoke(
        cli.app,
        ["--output", "json", "doctor", "--robot", ROBOT],
    )

    rows = {row["check"]: row for row in json.loads(result.stdout)["rows"]}
    assert result.exit_code == ExitCode.FAILURE
    assert rows["daemon.reachable"]["status"] == "failed"
    assert "cannot reach the robot" in str(rows["daemon.reachable"]["detail"])


@pytest.mark.filesystem  # writes a declaration for the command to read; not a unit test
@pytest.mark.parametrize("verb", ["diff", "apply"])
def test_the_declaring_commands_report_an_unreachable_robot_the_same_way(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    verb: str,
) -> None:
    """The two verbs that read a declaration, which the parametrisation above cannot.

    Args:
        monkeypatch: How the seam is replaced.
        tmp_path: Where the declaration is written.
        verb: Which verb to run.
    """
    monkeypatch.setattr(cli, "_build_access", lambda _target: _Refusing())
    declaration = _declaration(tmp_path, {LEVEL: "info"})

    result = runner.invoke(
        cli.app,
        ["config", verb, "--robot", ROBOT, "--declaration", str(declaration)],
    )

    assert result.exit_code == ExitCode.UNREACHABLE, result.stdout
    assert "cannot reach the robot" in result.stdout


@pytest.mark.filesystem  # writes a wheel for the command to read; not a unit test
def test_a_deploy_against_an_unreachable_robot_reports_it_the_same_way(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The seventh command, which needs a wheel before it can get that far.

    Args:
        monkeypatch: How the seam is replaced.
        tmp_path: Where the wheel is written.
    """
    monkeypatch.setattr(cli, "_build_access", lambda _target: _Refusing())
    name, content = fixture_wheel()
    (tmp_path / name).write_bytes(content)

    result = runner.invoke(
        cli.app,
        ["deploy", "--robot", ROBOT, "--wheel", str(tmp_path / name)],
    )

    assert result.exit_code == ExitCode.UNREACHABLE, result.stdout
    assert "cannot reach the robot" in result.stdout


def test_a_secret_setting_is_refused_as_an_argument(watcher: Watcher) -> None:
    """The tool's own rule, and it does not stop being true because it is a setting.

    An argument is visible in the process list to every user on the machine and
    lands in the shell history. `--credential-file` exists for exactly this
    reason, and the declaration document is the equivalent for a setting.
    """
    result = runner.invoke(
        cli.app,
        [
            "config",
            "set",
            "REACHY_GROUNDSTATION_CREDENTIAL=example-not-a-real-secret",
            "--robot",
            ROBOT,
        ],
    )

    assert result.exit_code == ExitCode.CONFIGURATION
    assert "config apply --declaration" in result.stdout
    assert "example-not-a-real-secret" not in result.stdout
    assert watcher.built == []


def test_a_malformed_argument_is_reported_by_its_position_not_its_text(
    watcher: Watcher,
) -> None:
    """`=hunter2` partitions into no name and a value, and quoting it would print it.

    Nothing has seeded the redactor at that point — it is seeded from the
    settings this parse produces — so a message carrying the argument would
    carry the whole of it.
    """
    result = runner.invoke(
        cli.app,
        [
            "config",
            "set",
            "REACHY_SATELLITE_LOG_LEVEL=info",
            "=hunter2",
            "--robot",
            ROBOT,
        ],
    )

    assert result.exit_code == ExitCode.CONFIGURATION
    assert "argument 2" in result.stdout
    assert "hunter2" not in result.stdout
    assert watcher.built == []


def test_an_unmanaged_setting_is_reported_by_presence_rather_than_by_value(
    watcher: Watcher,
) -> None:
    """The effective environment carries values this tool never wrote.

    A token another tool set on the robot would otherwise be printed in full,
    and the redactor was never given it. Unclassified is treated as secret.
    """
    watcher.robot.environment = {
        "SOMETHING_ELSE_PUT_THIS_HERE": "a-value-this-tool-cannot-vouch-for",
        LEVEL: "info",
    }

    result = runner.invoke(
        cli.app,
        ["--output", "json", "config", "get", "--robot", ROBOT],
    )

    rows = {row["setting"]: row for row in json.loads(result.stdout)["rows"]}
    assert rows["SOMETHING_ELSE_PUT_THIS_HERE"]["in_force"] == "set"
    assert "a-value-this-tool-cannot-vouch-for" not in result.stdout
    # A setting the vocabulary declares is still shown, because a configuration
    # command that cannot say what a setting holds has not done its job.
    assert rows[LEVEL]["in_force"] == "info"


#:= docs/specs/reachyctl/index.md#req-058-output-is-machine-readable-on-request
#:% Every command that reports results MUST offer a structured output format
#:% suitable for consumption by another program.
@pytest.mark.filesystem  # writes a wheel for the command to read; not a unit test
def test_an_overridden_application_is_what_the_structured_output_names(
    watcher: Watcher,
    tmp_path: Path,
) -> None:
    """The report names the application that was controlled, not the wheel's.

    `--application` is how a robot whose daemon knows the distribution by
    another name is reached, and from that point the deploy restarts, starts
    and version-checks *that* name. A report that named the wheel's
    distribution instead would be asserting a verification it did not perform
    against the thing it names — REQ-051's own failure wearing a different hat,
    reaching a consumer through the field REQ-058 exists to make readable.

    Args:
        watcher: The seam that hands out the link.
        tmp_path: Where the fixture wheel is written.
    """
    name, content = fixture_wheel()
    (tmp_path / name).write_bytes(content)
    watcher.robot.packages["known-by-another-name"] = FIXTURE_VERSION

    result = runner.invoke(
        cli.app,
        [
            "--output",
            "json",
            "deploy",
            "--robot",
            ROBOT,
            "--wheel",
            str(tmp_path / name),
            "--application",
            "known-by-another-name",
        ],
    )

    document = json.loads(result.stdout)
    assert document["data"]["application"] == "known-by-another-name"
    assert document["data"]["application"] != FIXTURE_DISTRIBUTION
    # And it is the name the run actually controlled, not merely the name it
    # was handed: the daemon's control was asked about this one.
    assert watcher.access is not None
    assert any(
        "known-by-another-name" in " ".join(command)
        for command in watcher.access.commands
    )
    assert FIXTURE_DISTRIBUTION not in document["summary"]


@pytest.mark.filesystem  # writes a wheel for the command to read; not a unit test
@pytest.mark.usefixtures("watcher")
def test_without_an_override_the_wheels_own_distribution_is_named(
    tmp_path: Path,
) -> None:
    """The ordinary case, so the override test above is a difference from something.

    Args:
        tmp_path: Where the fixture wheel is written.
    """
    name, content = fixture_wheel()
    (tmp_path / name).write_bytes(content)

    result = runner.invoke(
        cli.app,
        [
            "--output",
            "json",
            "deploy",
            "--robot",
            ROBOT,
            "--wheel",
            str(tmp_path / name),
        ],
    )

    assert json.loads(result.stdout)["data"]["application"] == FIXTURE_DISTRIBUTION


@pytest.mark.usefixtures("watcher")
def test_every_command_names_one_robot_the_same_way() -> None:
    """A script reading `data.robot` must not get two spellings for one robot.

    `doctor` used to report the text of the option while the others reported
    the resolved target, so a bare IPv6 address came back two ways — and the
    one `doctor` printed was not the address the run had used.
    """
    address = "operator@2001:db8::1"
    commands = {
        "doctor": ["--output", "json", "doctor", "--robot", address],
        "config get": ["--output", "json", "config", "get", "--robot", address],
        "app start": ["--output", "json", "app", "start", "--robot", address],
    }

    reported = {}
    for name, arguments in commands.items():
        result = runner.invoke(cli.app, arguments)
        assert result.exit_code in {ExitCode.OK, ExitCode.FAILURE}, result.stdout
        reported[name] = json.loads(result.stdout)["data"]["robot"]

    assert set(reported.values()) == {"operator@[2001:db8::1]:22"}, reported
