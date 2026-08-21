"""`bench`: run the benchmark suite against a live installation.

This is the command the benchmarks spec reserves for the two measurements that
need a robot — `photon-to-head` and `robot-load` — and it is deliberately the
only way to reach them from an operator's machine. The suite's own module entry
point runs the hardware-free four; what this adds is the robot: an SSH link the
tool already knows how to open, handed to the benchmarks as something that runs
a command there.

**The suite is an optional import, and that is a packaging decision.**
`reachy-bench` is a workspace member that is never published — it holds the
committed baseline and imports the groundstation — so making it a requirement of
`reachyctl` would put an unpublishable distribution in the dependency set of the
one wheel this repository does release, and `just wheel-verify` would fail
installing it. So it is imported inside the command, and an installation without
it is told so in a sentence rather than in an ImportError.

**Nothing here re-implements a measurement.** The plan, the run and the result
document are the suite's; this module resolves the options, builds the robot
runner, and turns the run into the tool's own report. A benchmark that measured
something slightly different when invoked through the CLI would be a second
suite.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final

from reachyctl.errors import ConfigurationError, UnreachableError
from reachyctl.exits import ExitCode
from reachyctl.output import Report
from reachyctl.robot import RobotAccessError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    # Under `TYPE_CHECKING` only, and that is the whole of how the optional
    # dependency is expressed: the type checker resolves these in a checkout,
    # where the member is installed, and nothing imports them at run time in an
    # installation that has not got it. The run-time imports are inside
    # `execute`, where their absence is answered with a sentence.
    from reachy_bench.context import RunContext
    from reachy_bench.result import RunResult
    from reachyctl.output import Reporter
    from reachyctl.robot import Closer, RemoteAccess

__all__ = ["BenchPlan", "execute", "report_for"]

# What the suite is called when it is not installed. Named once so the message
# and this module's docstring cannot drift apart.
_DISTRIBUTION: Final = "reachy-bench"

_MISSING = (
    f"the benchmark suite is not installed. It ships as this repository's "
    f"{_DISTRIBUTION} workspace member, which is deliberately never published — "
    f"it carries the committed baseline and imports the groundstation — so "
    f"`bench` runs from a checkout: `just sync`, then `reachyctl bench`, or "
    f"`just bench` for the measurements that need no robot"
)


class BenchPlan:
    """What one benchmark run was asked to do.

    A plain class rather than a frozen dataclass because every field is optional
    and the command surface fills them in one at a time; there is nothing here
    to compare two of.

    Attributes:
        benchmarks: The names to run. Empty means the default selection, which
            leaves out anything needing a robot and reports it as excluded.
        repository: The checkout the fixtures and the baseline are read from.
        models_dir: Where the pinned model files are.
        output: Where to write the result document.
        network: How the link behaved, in the operator's own words.
        observations_ms: Manually recorded photon-to-head intervals.
        frame_rate: The frame rate `robot-load` measures the robot under.
        sample_seconds: How long `robot-load` samples for.
    """

    def __init__(
        self,
        *,
        benchmarks: tuple[str, ...],
        repository: Path | None,
        models_dir: Path | None,
        output: Path,
        network: str,
        observations_ms: tuple[float, ...],
        frame_rate: float,
        sample_seconds: float,
    ) -> None:
        """Record what the run was asked to do.

        Args:
            benchmarks: The names to run.
            repository: The checkout to read from.
            models_dir: Where the pinned model files are.
            output: Where to write the result document.
            network: How the link behaved.
            observations_ms: Manually recorded photon-to-head intervals.
            frame_rate: The frame rate to measure the robot under.
            sample_seconds: How long to sample for.
        """
        self.benchmarks = benchmarks
        self.repository = repository
        self.models_dir = models_dir
        self.output = output
        self.network = network
        self.observations_ms = observations_ms
        self.frame_rate = frame_rate
        self.sample_seconds = sample_seconds


def _robot_runner(
    access: RemoteAccess,
    loop: asyncio.AbstractEventLoop,
) -> Callable[[Sequence[str]], str]:
    """Build the thing the hardware benchmarks run commands on the robot with.

    The suite asks for something synchronous — it samples a file twice with a
    wait in between — and the robot link is asynchronous, so the call is driven
    on the loop this command already owns.

    Args:
        access: The open link to the robot.
        loop: The loop the link was opened on.

    Returns:
        A callable taking an argument vector and returning what the robot wrote
        to standard output.
    """

    def _run(command: Sequence[str]) -> str:
        """Run one command on the robot.

        Args:
            command: The arguments to run.

        Returns:
            What it wrote to standard output.

        Raises:
            RuntimeError: If the robot refused the command. Raised rather than
                returned so the benchmark fails with the robot's own reason
                instead of parsing an error message as a measurement.
        """
        outcome = loop.run_until_complete(access.run(list(command)))
        if not outcome.ok:
            message = (
                f"the robot refused `{outcome.command}` with status "
                f"{outcome.exit_status}: {outcome.stderr.strip() or 'no detail'}"
            )
            raise RuntimeError(message)
        return outcome.stdout

    return _run


#:= docs/specs/benchmarks/index.md#req-072-benchmarks-requiring-hardware-are-opt-in
#:% Any benchmark that requires a physical robot MUST be excluded from the default
#:% suite and selectable explicitly.
def report_for(run: RunResult, plan: BenchPlan) -> Report:
    """Shape a run into the report this tool emits.

    Args:
        run: The suite's `RunResult`.
        plan: What the run was asked to do.

    Returns:
        The report. Every benchmark appears, whatever became of it — an
        excluded one is reported as excluded rather than being absent, which is
        REQ-072's second half.
    """
    from reachy_bench.result import Status

    rows: list[dict[str, object]] = []
    for benchmark in run.benchmarks:
        if not benchmark.measurements:
            rows.append(
                {
                    "benchmark": benchmark.benchmark,
                    "measurement": "",
                    "value": "",
                    "unit": "",
                    "status": benchmark.status.value,
                },
            )
            continue
        rows.extend(
            {
                "benchmark": benchmark.benchmark,
                "measurement": one.name,
                "value": round(one.value, 3),
                "unit": one.unit.value,
                "status": benchmark.status.value,
            }
            for one in benchmark.measurements
        )
    statuses = run.statuses()
    failed = [name for name, status in statuses.items() if status is Status.FAILED]
    excluded = [name for name, status in statuses.items() if status is Status.EXCLUDED]
    return Report(
        command="bench",
        ok=not failed,
        summary=(
            f"{len(failed)} benchmark(s) could not measure: {', '.join(failed)}"
            if failed
            else f"{len(rows)} measurement(s) written to {plan.output}"
        ),
        data={
            "host_profile": run.context.host.profile,
            "commit": run.context.software.commit,
            "results": str(plan.output),
            "excluded": tuple(excluded),
            "network": run.context.network,
        },
        columns=("benchmark", "measurement", "value", "unit", "status"),
        rows=tuple(rows),
    )


def execute(
    plan: BenchPlan,
    access: RemoteAccess | None,
    reporter: Reporter,
    close: Closer | None = None,
    context: RunContext | None = None,
) -> ExitCode:
    """Run the suite and report what it measured.

    Args:
        plan: What the run was asked to do.
        access: The robot, when one was named. `None` runs the hardware-free
            benchmarks and reports the rest as excluded.
        reporter: Where everything is written.
        close: What to call to let the robot link go.
        context: What to record the run as having happened on. Collected from
            this machine when omitted; an argument so that a test of this
            command does not have to run `git` and read `/proc` to exercise it.

    Returns:
        The exit status.

    Raises:
        ConfigurationError: If the benchmark suite is not installed, or if a
            named benchmark is not one. Neither has contacted anything.
        UnreachableError: If the robot could not be reached at all, which is
            not a benchmark that measured badly and is not reported as one.
    """
    try:
        from reachy_bench.benchmarks import SUITE
        from reachy_bench.cli import default_repository
        from reachy_bench.context import collect_context
        from reachy_bench.registry import Options, run_selected
        from reachy_bench.registry import plan as build_plan
    except ImportError as error:
        raise ConfigurationError(_MISSING) from error

    repository = plan.repository or default_repository()
    try:
        selections = build_plan(SUITE, plan.benchmarks)
    except ValueError as error:
        raise ConfigurationError(str(error)) from error

    reporter.detail(f"benchmarking from {repository}")
    loop = asyncio.new_event_loop()
    try:
        runner = None
        if access is not None:
            try:
                loop.run_until_complete(access.connect())
            except RobotAccessError as error:
                raise UnreachableError(str(error)) from error
            runner = _robot_runner(access, loop)
        options = Options(
            repository=repository,
            models_dir=plan.models_dir or (repository / ".models"),
            network=plan.network,
            robot=runner,
            frame_rate=plan.frame_rate,
            sample_seconds=plan.sample_seconds,
            observations_ms=plan.observations_ms,
        )
        run = run_selected(
            selections,
            options,
            context if context is not None else collect_context(network=plan.network),
        )
    finally:
        # The link is let go on every path out, including the one where a
        # benchmark raised: `run_selected` contains an ordinary failure, but a
        # cancellation or an interrupt comes through here.
        if close is not None:
            loop.run_until_complete(close())
        loop.close()

    plan.output.write_text(run.as_json(), encoding="utf-8")
    return reporter.emit(report_for(run, plan))
