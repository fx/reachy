"""`deploy`: put a wheel on the robot, and then find out whether it took.

Six steps — build, reach, transfer, install, restart, start — and a seventh that
decides whether any of it worked. reachyctl REQ-051 says the command must
confirm the intended version is installed *and running* before it reports
success, and the reason is a specific failure this stack has already had: a
package that installs successfully into an environment the running daemon is not
using. Every step of that deploy exits zero. `pip` is happy, `systemctl` is
happy, and the robot goes on running the previous version.

So the last step does not read the install's exit status at all. It asks the
daemon, through the interpreter the daemon itself runs, what version of the
application is there now, and compares it with the version in the wheel that was
sent. A mismatch is a failed deploy that **names the version actually running**,
because "deploy failed" sends an operator to the logs and "the robot is running
0.1.0, not 0.2.0" sends them to the right question.

**The application is the one the wheel carries.** Not a configured name: a
deploy that installed one distribution and verified another would report success
whenever the other happened to be at the version the wheel declares — the same
"looks identical to success" failure by a different door. `--application`
overrides it, for a robot whose daemon knows the distribution by another name,
and overriding it means saying which name is verified.

**The checks are the shared ones.** Reachability, installation and running state
are `reachy_checks` declarations, run here exactly as `doctor` runs them, which
is what stops a deploy having a private opinion about whether a robot is healthy
— reachyctl REQ-056. If verification needs a question the registry cannot ask,
the answer is a check in the registry, not a probe in this file.

**Every run that reached the robot ends by asking it what it is running.** A
step that fails does not end the sequence; it stops the steps that would make
things worse — there is nothing to restart over a failed install — and the
verification still runs, because it only reads. Two reasons, and the second is
the one that matters. An operator whose install failed still needs to know what
the robot is running now. And a command that exits non-zero may still have taken
effect, so its own status is never the last word: that is the whole thesis of
this change, and applying it only to the happy path would be applying it
nowhere.

**A running application is warned about, not refused.** The change document
records this as an open question and it resolves here: restarting the daemon
interrupts whatever the robot is doing, possibly a conversation, and the command
says so loudly before it does it. Refusing would need application state the
daemon does not expose — "is somebody talking to it right now" is not a thing
that can be asked — so the alternative to warning is not safety, it is a
`--force` flag that everybody types by reflex.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from reachy_checks import (
    APPLICATION_INSTALLED,
    APPLICATION_RUNNING,
    DAEMON_REACHABLE,
    CheckContext,
    check_by_identifier,
    run_check,
)
from reachyctl.configure import guard_robot_secrets
from reachyctl.output import Report
from reachyctl.robot import closing
from reachyctl.steps import StepLog

if TYPE_CHECKING:
    from collections.abc import Callable

    from reachy_checks import CheckResult
    from reachyctl.daemon import DaemonClient
    from reachyctl.exits import ExitCode
    from reachyctl.output import Reporter
    from reachyctl.robot import Closer
    from reachyctl.wheels import Wheel

__all__ = [
    "RESTART_WARNING",
    "DeployPlan",
    "execute",
    "report_for",
    "run_deploy",
]

# Said before the restart happens, every time, whether or not anything is
# running. An operator who reads it after the fact has been told what happened;
# an operator who reads it before has been given the half-second in which
# interrupting a deploy is still possible.
RESTART_WARNING: Final = (
    "the daemon is about to be restarted, which interrupts whatever the robot "
    "is doing — including a conversation in progress"
)

_BUILD: Final = "build"
_REACH: Final = "reach"
_TRANSFER: Final = "transfer"
_INSTALL: Final = "install"
_RESTART: Final = "restart"
_START: Final = "start"
_VERIFY: Final = "verify"


@dataclass(frozen=True, slots=True, kw_only=True)
class DeployPlan:
    """What one deploy was asked to do.

    Attributes:
        obtain: How to get the wheel — building a named workspace member, or
            reading one off this machine. A callable rather than a path,
            because building is the one thing a deploy does locally and a
            command surface that had already built it could not report the
            build as a step.
        origin: Where the wheel came from, for the report.
        application: The distribution the daemon knows this by, when the
            operator named one. `None` — the ordinary case — takes it from the
            wheel, which is the only name that cannot be wrong about what was
            installed.
        preview: Whether to report the changes and make none of them.
    """

    obtain: Callable[[], Wheel]
    origin: str
    application: str | None = None
    preview: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class DeployOutcome:
    """What a deploy did, and what it found afterwards.

    Attributes:
        steps: Every step, in order.
        wheel: What was deployed, once it is known.
        running_version: The version the daemon reports after the restart, or
            an empty string when nothing is installed or the run never got
            there. This is the field REQ-051's scenario is about.
        preview: Whether this was a preview.
    """

    steps: StepLog
    wheel: Wheel | None
    running_version: str
    preview: bool

    @property
    def ok(self) -> bool:
        """Whether the deploy succeeded.

        Returns:
            True when no step failed.
        """
        return self.steps.ok


def _version_of(result: CheckResult) -> str:
    """Pull the application's version out of the installation check's finding.

    Args:
        result: What the check found.

    Returns:
        The version the daemon reported, or an empty string when it reported
        none. The field is on the finding whether the check passed or failed,
        which is what makes a mismatch reportable rather than merely
        detectable.
    """
    version = result.detail.get("application_version")
    return version if isinstance(version, str) else ""


async def run_deploy(
    plan: DeployPlan,
    daemon: DaemonClient,
    reporter: Reporter,
) -> DeployOutcome:
    """Run the step sequence, or report what it would do.

    Args:
        plan: What the deploy was asked to do.
        daemon: The robot.
        reporter: Where progress goes as it happens.

    Returns:
        Every step and what the robot ended up running.

    Raises:
        CommandError: If the wheel cannot be obtained, or the link fails part
            way through. A link that failed has told us nothing about the robot,
            so it costs `UNREACHABLE` rather than being reported as a robot that
            is unhealthy.
    """
    steps = StepLog(reporter=reporter)
    steps.begin(_BUILD, f"obtaining the wheel {plan.origin}")
    wheel = plan.obtain()
    steps.done(_BUILD, wheel.describe())

    # From here on, the application is the one the WHEEL carries — unless the
    # operator named one, which is how a robot whose daemon knows it by another
    # name is reached. Verifying a name that came from anywhere other than the
    # thing being installed is how a deploy reports success because something
    # else happens to be at the version the wheel declares.
    daemon = daemon.for_application(plan.application or wheel.distribution)
    context = CheckContext(daemon=daemon)
    steps.begin(_REACH, "asking the robot whether its daemon is answering")
    # Above the check on purpose: a link that is not there has told us nothing
    # about the robot, and `run_check` would turn it into a failed diagnosis.
    await daemon.connect()
    # And before anything the robot wrote is rendered — a systemd complaint, a
    # traceback from the daemon's control — because a redactor cannot remove a
    # value it was never given. See `reachyctl.configure.guard_robot_secrets`.
    await guard_robot_secrets(daemon, reporter)
    reachable = await run_check(check_by_identifier(DAEMON_REACHABLE), context)
    if reachable.failed:
        steps.failed(_REACH, reachable.summary)
        return DeployOutcome(
            steps=steps,
            wheel=wheel,
            running_version="",
            preview=plan.preview,
        )
    steps.done(_REACH, reachable.summary)

    before = await run_check(check_by_identifier(APPLICATION_INSTALLED), context)
    running = await run_check(check_by_identifier(APPLICATION_RUNNING), context)
    if not running.failed:
        # The open question, resolved: warn, do not refuse. See the module
        # documentation.
        reporter.note(
            f"the application is running on the robot ({running.summary}); "
            f"deploying will interrupt it",
        )

    if plan.preview:
        _preview(steps, wheel, _version_of(before))
        return DeployOutcome(
            steps=steps,
            wheel=wheel,
            running_version=_version_of(before),
            preview=True,
        )

    steps.begin(_TRANSFER, f"sending {wheel.size_bytes} bytes to the robot")
    staged = await daemon.stage(wheel.content, wheel.file_name)
    steps.done(_TRANSFER, f"the wheel is at {staged}")

    steps.begin(_INSTALL, "installing into the environment the daemon runs")
    try:
        installed = await daemon.install_wheel(staged)
    finally:
        # The robot has little room and this change retains no versions, so the
        # transferred wheel is removed whether or not the install worked.
        await daemon.discard(staged)
    if installed.ok:
        steps.done(_INSTALL, "the install reported success, which proves nothing yet")

        reporter.note(RESTART_WARNING)
        steps.begin(_RESTART, f"restarting {daemon.layout.daemon_unit}")
        restarted = await daemon.restart_daemon()
        if restarted.ok:
            steps.done(_RESTART, "the daemon restarted")
            steps.begin(
                _START,
                f"asking the daemon to start {daemon.layout.application}",
            )
            started = await daemon.start_application()
            if started.ok:
                steps.done(_START, "the daemon accepted the start")
            else:
                # Recorded and not decisive. The verification below asks the
                # robot what is actually running, and an application that
                # started despite a control command complaining is a working
                # robot — where treating this as the answer would be trusting
                # an exit status again.
                steps.warned(_START, started.complaint())
        else:
            steps.failed(_RESTART, restarted.complaint())
            steps.skipped(
                _START, "the daemon did not restart, so there is nothing to start"
            )
    else:
        steps.failed(_INSTALL, installed.complaint())
        steps.skipped(
            _RESTART,
            "the install failed, so restarting would interrupt the robot for nothing",
        )
        steps.skipped(_START, "nothing new was installed to start")

    return await _verify(steps, wheel, context)


def _preview(steps: StepLog, wheel: Wheel, installed: str) -> None:
    """Record what a deploy would do, having done none of it.

    Args:
        steps: Where to record them.
        wheel: What would be deployed.
        installed: What the robot has now, or an empty string.
    """
    current = installed or "nothing"
    steps.planned(_TRANSFER, f"would send {wheel.size_bytes} bytes to the robot")
    steps.planned(
        _INSTALL,
        f"would install {wheel.distribution} {wheel.version} over {current}",
    )
    steps.planned(_RESTART, f"would restart the daemon — {RESTART_WARNING}")
    steps.planned(_START, f"would start {wheel.distribution}")
    steps.planned(
        _VERIFY,
        f"would then require the robot to report {wheel.version} running",
    )


#:= docs/specs/reachyctl/index.md#req-051-deployment-verifies-its-own-result
#:% The deploy command MUST confirm that the intended version is installed and
#:% running before it reports success.
async def _verify(
    steps: StepLog,
    wheel: Wheel,
    context: CheckContext,
) -> DeployOutcome:
    """Ask the robot what it is running now, and judge the deploy on the answer.

    Args:
        steps: Where to record the verification.
        wheel: What was sent.
        context: What the checks run against. The same context the run started
            with, and nothing in it is cached — `reachyctl.daemon` memoises
            nothing, precisely so that this call cannot return what was true
            before the restart.

    Returns:
        The whole outcome, with the version the robot reported.
    """
    steps.begin(_VERIFY, "asking the robot what version it is running now")
    after = await run_check(check_by_identifier(APPLICATION_INSTALLED), context)
    running = await run_check(check_by_identifier(APPLICATION_RUNNING), context)
    version = _version_of(after)
    if after.failed:
        steps.failed(_VERIFY, after.summary)
    elif version != wheel.version:
        steps.failed(
            _VERIFY,
            f"the robot is running {wheel.distribution} "
            f"{version or 'an unnamed build'}, not the {wheel.version} that was "
            f"just installed; the install went somewhere the daemon is not "
            f"reading",
        )
    elif running.failed:
        steps.failed(
            _VERIFY,
            f"{wheel.distribution} {version} is installed but is not running: "
            f"{running.summary}",
        )
    else:
        steps.done(_VERIFY, f"the robot is running {wheel.distribution} {version}")
    return DeployOutcome(
        steps=steps,
        wheel=wheel,
        running_version=version,
        preview=False,
    )


#:= docs/specs/reachyctl/index.md#req-058-output-is-machine-readable-on-request
#:% Every command that reports results MUST offer a structured output format
#:% suitable for consumption by another program.
def report_for(outcome: DeployOutcome, robot: str) -> Report:
    """Shape a deploy into the thing every rendering is built from.

    Args:
        outcome: What the deploy did.
        robot: How the robot was addressed.

    Returns:
        The report to emit.
    """
    wheel = outcome.wheel
    data: dict[str, object] = {
        "robot": robot,
        "preview": outcome.preview,
        "application": None if wheel is None else wheel.distribution,
        "version": None if wheel is None else wheel.version,
        "wheel_bytes": None if wheel is None else wheel.size_bytes,
        # Always present, and it is the field REQ-051 is about: a script gating
        # on a deploy compares this with `version` rather than reading a
        # sentence.
        "running_version": outcome.running_version or None,
    }
    return Report(
        command="deploy",
        ok=outcome.ok,
        summary=_summary(outcome),
        data=data,
        columns=("step", "status", "detail"),
        rows=outcome.steps.rows,
    )


def _summary(outcome: DeployOutcome) -> str:
    """Say in one line what the deploy did.

    Args:
        outcome: What it did.

    Returns:
        The line, naming the failing step when one failed.
    """
    failures = [result for result in outcome.steps.results if result.failed]
    if failures:
        first = failures[0]
        # What the robot is running is appended only when the failing step is
        # not the one that already said it. An operator whose install failed
        # needs to know the robot is still on the old version; an operator whose
        # verification failed has just been told that in the same sentence.
        running = (
            ""
            if first.name == _VERIFY or not outcome.running_version
            else f"; the robot is running {outcome.running_version}"
        )
        return f"the deploy failed at {first.name}: {first.detail}{running}"
    if outcome.preview:
        return "nothing was changed: this was a preview"
    wheel = outcome.wheel
    named = "nothing" if wheel is None else f"{wheel.distribution} {wheel.version}"
    return f"the robot is running {named}, verified after the restart"


def execute(
    plan: DeployPlan,
    daemon: DaemonClient,
    reporter: Reporter,
    robot: str,
    close: Closer | None = None,
) -> ExitCode:
    """Deploy, and report what the robot ended up running.

    Args:
        plan: What the deploy was asked to do.
        daemon: The robot.
        reporter: Where everything is written.
        robot: How the robot was addressed, for the report.
        close: Awaited when the run is over, whatever happened, to let the link
            go. Inside the same event loop the run used, because a connection
            opened on one loop cannot be closed from another.

    Returns:
        The exit status. A verification that found the wrong version is
        `FAILURE`: the command ran and its answer was negative, which is a
        different thing from not having reached the robot at all.

    Raises:
        CommandError: If the wheel could not be obtained or the link failed.
    """
    outcome = asyncio.run(closing(run_deploy(plan, daemon, reporter), close))
    return reporter.emit(report_for(outcome, robot))
