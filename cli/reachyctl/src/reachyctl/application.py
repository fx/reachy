"""`app`: start it, stop it, and read what it is saying.

The three verbs of the application's own lifecycle, as distinct from the
daemon's. Starting and stopping go through the daemon, because the application
is a child of it and the daemon is what knows how to launch one; reading the log
goes through the robot's journal, filtered to the application.

**The filter is a journal field, not a search.** `journalctl` is asked for the
daemon's unit and for entries whose syslog identifier is the application, so
what comes back is what the application wrote — rather than every line that
happens to mention its name, which is what a `grep` of the unit's log would
give and which quietly includes the daemon's own line saying it is starting the
thing.

**Start and stop support preview, and that is not ceremony.** REQ-052 asks for a
preview mode on every command that modifies robot state, and stopping the
application is a modification an operator may well want to think about first —
it is the one that ends a conversation. A preview reports what the application
is doing now and what the verb would do to it, and issues no control command at
all.

**Both verbs verify.** The same reasoning as `deploy`: a control command that
exits zero is not evidence that the application is running, so the verb asks the
shared `application.running` check afterwards and reports what it found. A
control command that reported a *failure* is not evidence either, so it is
recorded as a warning and the verification still decides.

**A state that could not be read is not a state.** The step that decides whether
there is anything to do asks the daemon directly rather than reading the shared
check's result, and the difference is not stylistic. `reachy_checks.run_check`
turns anything a probe raises into a *failed* check, and a failed
`application.running` reads as "it is not running" — so `app stop` against a
robot whose control could not be reached would report the application as already
stopped and exit zero, having learned nothing about it. Asked directly, that
same fault is a `DaemonControlError` and costs `UNREACHABLE`. The registry check
is still what verifies the end state, which is the question REQ-056 is about.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final

from reachy_checks import (
    APPLICATION_RUNNING,
    CheckContext,
    check_by_identifier,
    run_check,
)
from reachyctl.output import Report
from reachyctl.robot import closing
from reachyctl.steps import StepLog

if TYPE_CHECKING:
    from reachyctl.daemon import DaemonClient
    from reachyctl.exits import ExitCode
    from reachyctl.output import Reporter
    from reachyctl.robot import Closer

__all__ = ["execute_logs", "execute_start", "execute_stop"]

_INSPECT: Final = "inspect"
_CONTROL: Final = "control"
_VERIFY: Final = "verify"


async def _lifecycle(
    daemon: DaemonClient,
    reporter: Reporter,
    *,
    start: bool,
    preview: bool,
) -> StepLog:
    """Start or stop the application, or report what that would do.

    Args:
        daemon: The robot.
        reporter: Where progress goes.
        start: Whether the verb is `start` rather than `stop`.
        preview: Whether to report the change and make none of it.

    Returns:
        Every step, in order.
    """
    verb = "start" if start else "stop"
    steps = StepLog(reporter=reporter)
    context = CheckContext(daemon=daemon)

    steps.begin(_INSPECT, f"asking whether {daemon.layout.application} is running")
    await daemon.connect()
    # Asked directly rather than through the registry, and the module
    # documentation says why: a check that could not run reads as a negative
    # answer, and this is the answer the "nothing to do" branch turns on.
    state = await daemon.application_state()
    running = state.running
    steps.done(
        _INSPECT,
        f"{daemon.layout.application} is "
        f"{'running' if running else 'not running'}"
        f"{f' ({state.detail})' if state.detail else ''}",
    )

    if running is start:
        # Already in the state that was asked for. Reported as skipped rather
        # than done, because "the daemon accepted a start" and "it was already
        # running" are different facts and a script gating on one should not be
        # told the other.
        steps.skipped(
            _CONTROL,
            f"{daemon.layout.application} is already "
            f"{'running' if start else 'stopped'}",
        )
        steps.skipped(_VERIFY, "nothing changed, so there is nothing to verify")
        return steps

    if preview:
        steps.planned(
            _CONTROL,
            f"would ask the daemon to {verb} {daemon.layout.application}",
        )
        steps.planned(
            _VERIFY,
            f"would then require the robot to report it "
            f"{'running' if start else 'stopped'}",
        )
        return steps

    steps.begin(_CONTROL, f"asking the daemon to {verb} {daemon.layout.application}")
    outcome = await (daemon.start_application() if start else daemon.stop_application())
    if outcome.ok:
        steps.done(_CONTROL, f"the daemon accepted the {verb}")
    else:
        # Recorded and not decisive. What decides the answer is the state
        # afterwards, not what the control command said about itself — so this
        # is a warning rather than a failure, or a robot that did as it was
        # asked would be reported as a failed command.
        steps.warned(_CONTROL, outcome.complaint())

    steps.begin(_VERIFY, "asking the robot what the application is doing now")
    after = await run_check(check_by_identifier(APPLICATION_RUNNING), context)
    if (not after.failed) is start:
        steps.done(_VERIFY, after.summary)
    else:
        steps.failed(
            _VERIFY,
            f"the daemon was asked to {verb} {daemon.layout.application} and "
            f"the robot reports: {after.summary}",
        )
    return steps


def _report_for(
    command: str,
    steps: StepLog,
    robot: str,
    application: str,
    *,
    preview: bool,
) -> Report:
    """Shape a lifecycle run into the thing every rendering is built from.

    Args:
        command: Which command produced it.
        steps: What happened.
        robot: How the robot was addressed.
        application: Which application it was about.
        preview: Whether this was a preview.

    Returns:
        The report to emit.
    """
    failures = [result for result in steps.results if result.failed]
    if failures:
        summary = f"{command} failed at {failures[0].name}: {failures[0].detail}"
    elif preview:
        summary = "nothing was changed: this was a preview"
    else:
        summary = steps.results[-1].detail if steps.results else "nothing happened"
    return Report(
        command=command,
        ok=steps.ok,
        summary=summary,
        data={"robot": robot, "application": application, "preview": preview},
        columns=("step", "status", "detail"),
        rows=steps.rows,
    )


def execute_start(
    daemon: DaemonClient,
    reporter: Reporter,
    robot: str,
    *,
    preview: bool,
    close: Closer | None = None,
) -> ExitCode:
    """Start the application and confirm it is running.

    Args:
        daemon: The robot.
        reporter: Where everything is written.
        robot: How the robot was addressed.
        preview: Whether to report the change and make none of it.
        close: How to let the link go.

    Returns:
        The exit status.

    Raises:
        CommandError: If the robot could not be reached.
    """
    steps = asyncio.run(
        closing(_lifecycle(daemon, reporter, start=True, preview=preview), close),
    )
    return reporter.emit(
        _report_for(
            "app start",
            steps,
            robot,
            daemon.layout.application,
            preview=preview,
        ),
    )


def execute_stop(
    daemon: DaemonClient,
    reporter: Reporter,
    robot: str,
    *,
    preview: bool,
    close: Closer | None = None,
) -> ExitCode:
    """Stop the application and confirm it has stopped.

    Args:
        daemon: The robot.
        reporter: Where everything is written.
        robot: How the robot was addressed.
        preview: Whether to report the change and make none of it.
        close: How to let the link go.

    Returns:
        The exit status.

    Raises:
        CommandError: If the robot could not be reached.
    """
    steps = asyncio.run(
        closing(_lifecycle(daemon, reporter, start=False, preview=preview), close),
    )
    return reporter.emit(
        _report_for(
            "app stop",
            steps,
            robot,
            daemon.layout.application,
            preview=preview,
        ),
    )


def execute_logs(
    daemon: DaemonClient,
    reporter: Reporter,
    robot: str,
    *,
    lines: int,
    follow: bool,
    since: str = "",
    close: Closer | None = None,
) -> ExitCode:
    """Stream the robot's journal, filtered to the application.

    Args:
        daemon: The robot.
        reporter: Where everything is written.
        robot: How the robot was addressed.
        lines: How many past lines to show first.
        follow: Whether to keep the stream open.
        since: A journal time expression to start from, or empty for none.
        close: How to let the link go.

    Returns:
        The exit status. `OK` when the journal could be read, whatever it says:
        a log full of errors is a log that was read successfully, and a command
        that failed on the content of a log would be unusable for the one thing
        it is for.

    Raises:
        CommandError: If the robot could not be reached.
    """

    async def _read() -> int:
        """Stream the journal until it ends or the operator stops it.

        Returns:
            How many lines were shown.
        """
        await daemon.connect()
        shown = 0
        async for line in daemon.journal(lines=lines, follow=follow, since=since):
            reporter.stream_line(line)
            shown += 1
        return shown

    try:
        shown = asyncio.run(closing(_read(), close))
    except KeyboardInterrupt:
        # `--follow` is meant to be ended this way, so ending it that way is a
        # successful run rather than a traceback. The count is unknown by then;
        # what matters is that the result document still arrives, so a
        # structured consumer's last line is a result rather than a truncated
        # stream.
        return reporter.emit(
            Report(
                command="app logs",
                ok=True,
                summary="stopped following the journal",
                data={
                    "robot": robot,
                    "application": daemon.layout.application,
                    # The same keys as the ordinary path. Ending `--follow` at
                    # the keyboard is how this command is meant to stop, so a
                    # consumer reading `lines` must not fail on the normal
                    # termination path; the count is genuinely unknown, and
                    # `None` says that rather than claiming a number.
                    "lines": None,
                    "followed": follow,
                },
            ),
        )
    return reporter.emit(
        Report(
            command="app logs",
            ok=True,
            summary=f"{shown} line(s) from {daemon.layout.application}",
            data={
                "robot": robot,
                "application": daemon.layout.application,
                "lines": shown,
                "followed": follow,
            },
        ),
    )
