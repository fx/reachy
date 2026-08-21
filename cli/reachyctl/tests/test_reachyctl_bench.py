"""`bench`: the one command that reaches the measurements needing a robot.

What is under test here is the command, not a measurement. The suite's own
tests own the statistics, the comparison and the benchmarks; what this asks is
whether naming a hardware benchmark reaches it, whether an unnamed one is
reported as excluded rather than attempted, whether a robot that refuses a
command produces a failed benchmark rather than a wrong number, and whether the
link is let go on every path out.

The robot is a fake that answers two commands, which is all the hardware
benchmarks ask of one. The filesystem is `pyfakefs`, an in-memory one that
performs no input or output, and the run context is built rather than collected
so that no test here runs `git` or reads `/proc`.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from reachyctl_robot import ROBOT, FakeRemoteAccess
from reachyctl_support import reporter_for

from reachy_bench.context import RunContext, collect_context
from reachyctl.bench import BenchPlan, execute
from reachyctl.errors import ConfigurationError, UnreachableError
from reachyctl.exits import ExitCode
from reachyctl.robot import CommandOutcome, RobotAccessError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pyfakefs.fake_filesystem import FakeFilesystem

_OUTPUT: Final = Path("/work/bench-results.json")

# Two readings of a robot's cumulative processor time, four hundred busy jiffies
# apart out of a thousand elapsed — two fifths of the machine.
_STAT_FIRST: Final = "cpu  1000 0 1000 8000 0 0 0 0 0 0\nintr 1\n"
_STAT_SECOND: Final = "cpu  1200 0 1200 8600 0 0 0 0 0 0\nintr 2\n"


class BenchRobot(FakeRemoteAccess):
    """A robot that answers the two commands the hardware benchmarks ask.

    Everything but `run` is the shared fake's — the link lifecycle, the command
    log, the upload and stream surfaces the `RemoteAccess` protocol declares —
    so this adds the two answers `robot-load` needs and nothing else.

    Attributes:
        refuse: The first word of a command to refuse, or an empty string.
    """

    def __init__(self, refuse: str = "") -> None:
        """Build a robot.

        Args:
            refuse: The first word of a command to refuse, standing in for a
                robot that will not answer.
        """
        super().__init__()
        self.refuse = refuse
        self._stats = [_STAT_FIRST, _STAT_SECOND]

    async def run(self, command: Sequence[str]) -> CommandOutcome:
        """Answer one command.

        Args:
            command: What to run.

        Returns:
            What it did.
        """
        self.connected = True
        self.commands.append(list(command))
        rendered = " ".join(command)
        if self.refuse and command[0] == self.refuse:
            return CommandOutcome(
                command=rendered,
                exit_status=1,
                stdout="",
                stderr="permission denied",
            )
        if command[0] == "nproc":
            return CommandOutcome(
                command=rendered,
                exit_status=0,
                stdout="4\n",
                stderr="",
            )
        return CommandOutcome(
            command=rendered,
            exit_status=0,
            stdout=self._stats.pop(0) if self._stats else _STAT_SECOND,
            stderr="",
        )


class UnreachableRobot(BenchRobot):
    """A robot that cannot be reached at all."""

    async def connect(self) -> None:
        """Refuse to open the link.

        Raises:
            RobotAccessError: Always.
        """
        message = "the robot did not answer"
        raise RobotAccessError(message)


def _context(network: str = "an example network") -> RunContext:
    """Build a run context without reading this machine.

    Args:
        network: How the link behaved, recorded with the run.

    Returns:
        The context.
    """
    return collect_context(
        profile="example-class",
        network=network,
        cpu_count=4,
        cpuinfo="model name\t: Example Processor 1000\n",
        meminfo="MemTotal:       16384000 kB\n",
        run_command=lambda _argv: "0" * 40,
        version_of=lambda _name: "1.2.3",
        now=lambda: datetime(2026, 8, 21, tzinfo=UTC),
    )


def _plan(*benchmarks: str) -> BenchPlan:
    """Build a plan that writes into the in-memory filesystem.

    The sampling interval is the default rather than a short one, and no test
    here reaches it: `robot-load` refuses before it would wait, so nothing in
    this module sleeps. Timing two samples an interval apart is the suite's own
    test, where the wait is injected.

    Args:
        benchmarks: The names to run.

    Returns:
        The plan.
    """
    return BenchPlan(
        benchmarks=benchmarks,
        repository=Path("/nowhere"),
        models_dir=Path("/nowhere/.models"),
        output=_OUTPUT,
        network="an example network",
        observations_ms=(),
        frame_rate=10.0,
        sample_seconds=10.0,
    )


#:= docs/specs/benchmarks/index.md#req-072-benchmarks-requiring-hardware-are-opt-in
#:% Any benchmark that requires a physical robot MUST be excluded from the default
#:% suite and selectable explicitly.
def test_naming_a_hardware_benchmark_selects_it_and_it_fails_without_its_input(
    fs: FakeFilesystem,
) -> None:
    """Naming it is what selects it — the second half of REQ-072.

    And with no observations it reports a failure rather than a number, because
    there is no automated stimulus and a figure this benchmark invented would
    be worse than none. The exclusion half of REQ-072 is covered by the suite's
    own registry tests, over the default selection.

    Args:
        fs: The in-memory filesystem.
    """
    fs.create_dir("/work")
    reporter, _streams = reporter_for()

    code = execute(_plan("photon-to-head"), None, reporter, context=_context())

    assert code == ExitCode.FAILURE
    document = json.loads(_OUTPUT.read_text(encoding="utf-8"))
    (benchmark,) = document["benchmarks"]
    assert benchmark["benchmark"] == "photon-to-head"
    assert benchmark["status"] == "failed"
    assert "no observations were given" in benchmark["reason"]


def test_naming_a_hardware_benchmark_reaches_it_against_the_robot(
    fs: FakeFilesystem,
) -> None:
    """The commands go over the link the tool opened, and their answers come back.

    The robot refuses the second command, and that is what makes this a test of
    the runner's success path as well as its failure one: reaching `/proc/stat`
    at all means the core count that came back from `nproc` was read and
    accepted. The arithmetic over two samples is the suite's own test, where the
    interval between them is injected and no test sleeps.

    Args:
        fs: The in-memory filesystem.
    """
    fs.create_dir("/work")
    robot = BenchRobot(refuse="cat")
    reporter, _streams = reporter_for()

    execute(
        _plan("robot-load"),
        robot,
        reporter,
        close=robot.aclose,
        context=_context(),
    )

    assert robot.connected
    assert robot.commands == [["nproc"], ["cat", "/proc/stat"]]


def test_the_manual_photon_observations_reach_the_measurement(
    fs: FakeFilesystem,
) -> None:
    """There is no automated stimulus, so the operator's numbers arrive here.

    Args:
        fs: The in-memory filesystem.
    """
    fs.create_dir("/work")
    reporter, _streams = reporter_for()
    plan = BenchPlan(
        benchmarks=("photon-to-head",),
        repository=Path("/nowhere"),
        models_dir=None,
        output=_OUTPUT,
        network="2.4 GHz WLAN",
        observations_ms=(150.0, 200.0, 250.0),
        frame_rate=10.0,
        sample_seconds=10.0,
    )

    code = execute(plan, None, reporter, context=_context())

    assert code == ExitCode.OK
    document = json.loads(_OUTPUT.read_text(encoding="utf-8"))
    (measurement,) = document["benchmarks"][0]["measurements"]
    assert measurement["name"] == "photon-to-head.stimulus_to_motion"
    assert measurement["value"] == pytest.approx(200.0)


def test_a_robot_that_refuses_a_command_fails_the_benchmark_not_the_run(
    fs: FakeFilesystem,
) -> None:
    """A refusal must not be parsed as a measurement.

    Args:
        fs: The in-memory filesystem.
    """
    fs.create_dir("/work")
    robot = BenchRobot(refuse="cat")
    reporter, streams = reporter_for()

    code = execute(
        _plan("robot-load"),
        robot,
        reporter,
        close=robot.aclose,
        context=_context(),
    )

    assert code == ExitCode.FAILURE
    assert robot.closed
    document = json.loads(_OUTPUT.read_text(encoding="utf-8"))
    assert document["benchmarks"][0]["status"] == "failed"
    assert "permission denied" in document["benchmarks"][0]["reason"]
    assert "could not measure" in streams.out.getvalue()


def test_a_robot_that_cannot_be_reached_is_reported_as_unreachable(
    fs: FakeFilesystem,
) -> None:
    """Not being able to reach a robot is not a benchmark that measured badly.

    Args:
        fs: The in-memory filesystem.
    """
    fs.create_dir("/work")
    robot = UnreachableRobot()
    reporter, _streams = reporter_for()

    with pytest.raises(UnreachableError) as raised:
        execute(
            _plan("robot-load"),
            robot,
            reporter,
            close=robot.aclose,
            context=_context(),
        )

    assert raised.value.exit_code == ExitCode.UNREACHABLE
    assert robot.closed


def test_a_benchmark_name_that_is_not_one_is_refused_before_anything_is_opened(
    fs: FakeFilesystem,
) -> None:
    """A typo that selected nothing would produce an empty run that passed.

    Args:
        fs: The in-memory filesystem.
    """
    fs.create_dir("/work")
    robot = BenchRobot()
    reporter, _streams = reporter_for()

    with pytest.raises(ConfigurationError, match="no such benchmark"):
        execute(_plan("robot-lode"), robot, reporter, context=_context())

    assert not robot.connected


def test_the_report_names_the_host_class_and_what_was_excluded(
    fs: FakeFilesystem,
) -> None:
    """Reading a result should not require opening the document.

    Args:
        fs: The in-memory filesystem.
    """
    from reachyctl.output import OutputFormat

    fs.create_dir("/work")
    reporter, streams = reporter_for(output_format=OutputFormat.JSON)

    execute(_plan("photon-to-head"), None, reporter, context=_context())

    document = json.loads(streams.out.getvalue())
    assert document["command"] == "bench"
    assert document["data"]["host_profile"] == "example-class"
    assert document["data"]["network"] == "an example network"
    assert document["data"]["results"] == str(_OUTPUT)


def test_the_command_surface_opens_a_link_when_a_robot_is_named() -> None:
    """The two hardware benchmarks need one, and this is where it comes from.

    Driven through the real command rather than through `execute`, because what
    is under test is the wiring between the robot options every robot-facing
    command shares and the plan the suite is handed. The benchmark named does
    not exist, so nothing is measured and nothing is written — the link is built
    and the run is refused before either.
    """
    from typer.testing import CliRunner

    from reachyctl.cli import app
    from reachyctl.credentials import CREDENTIAL_VARIABLE

    result = CliRunner().invoke(
        app,
        ["bench", "--benchmark", "nope", "--robot", ROBOT],
        env={CREDENTIAL_VARIABLE: "example-credential"},
    )

    assert result.exit_code == ExitCode.CONFIGURATION
    assert "no such benchmark: nope" in result.stdout
