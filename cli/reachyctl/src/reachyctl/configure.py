"""`config`: the robot's configuration as a desired state, compared and converged.

Four verbs — `get`, `diff`, `set`, `apply` — and one idea underneath all of them.
The declaration is a desired state, the robot has a current state, and every
verb is a different amount of the same comparison: `get` reports the current
state, `diff` reports the comparison, `set` and `apply` make the current state
match. Preview and idempotence fall out of that rather than being two behaviours
somebody has to keep working — a preview is the comparison with the converge
step not taken, and a second apply finds nothing to change because the
comparison says so.

**The managed region is owned in full.** `apply` makes the region exactly the
declaration, so a setting removed from the declaration is removed from the
robot. That is provisioning REQ-063, and it is the half of configuration
management that is easy to get wrong invisibly: appending settings works
perfectly until the first time somebody deletes one, and then the file that is
supposed to describe the robot no longer does. `reachyctl.managed` holds the
region's exact shape, and change 0010's Ansible role writes the same bytes.

**Validation happens before the robot is contacted.** REQ-053, and the reason is
the link: a round trip that ends in a rejection is slow, and a rejection partway
through a multi-step apply leaves a half-written region. `reachy_contracts`
declares what each setting accepts and the command surface checks the whole
declaration against it before it builds anything that could open a connection.

**A value this tool cannot vouch for is reported as set or unset, never shown.**
Two kinds qualify: a setting the vocabulary marks `secret`, and a setting the
vocabulary does not declare at all. The second is the one that is easy to miss —
the effective environment is read off the unit and whatever else drops into it,
so it carries values this tool never wrote, and a token another tool set would
otherwise be printed in full. Everything the vocabulary *does* declare and does
not mark secret is shown, because a configuration command whose output cannot
tell an operator what a setting is set to has not done its job. Every declared
secret value is additionally handed to the reporter's redactor before anything
is rendered, so a value that reaches some other path is scrubbed there too.

**Applying restarts the daemon, and the command says so first.** The warning is
written before the restart rather than beside it, because the point of it is the
moment in which an operator can still press control-C.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from reachy_checks import (
    CONFIGURATION_EFFECTIVE,
    CheckContext,
    Intent,
    check_by_identifier,
    run_check,
)
from reachy_contracts import ROBOT_SETTINGS
from reachyctl.errors import CommandError
from reachyctl.exits import ExitCode
from reachyctl.managed import MalformedRegionError, render_region
from reachyctl.output import Report
from reachyctl.robot import RobotAccessError, closing
from reachyctl.steps import StepLog

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from reachyctl.daemon import DaemonClient
    from reachyctl.output import Reporter
    from reachyctl.robot import Closer

__all__ = [
    "RESTART_WARNING",
    "Difference",
    "compare",
    "execute_apply",
    "execute_diff",
    "execute_get",
    "guard_robot_secrets",
    "guard_secrets",
    "report_for_difference",
    "run_apply",
    "secret_setting_names",
]

# Said before the restart happens. An environment change is only in force once
# the daemon has re-read it, and re-reading it means restarting it.
RESTART_WARNING: Final = (
    "applying this requires restarting the daemon, which interrupts whatever "
    "the robot is doing — including a conversation in progress"
)

_SET: Final = "set"
_UNSET: Final = "unset"

_READ: Final = "read"
_WRITE: Final = "write"
_RESTART: Final = "restart"
_VERIFY: Final = "verify"

_SECRET_NAMES: Final = frozenset(
    setting.name for setting in ROBOT_SETTINGS if setting.secret
)

_DECLARED_NAMES: Final = frozenset(setting.name for setting in ROBOT_SETTINGS)


def _unclassified(name: str) -> bool:
    """Say whether a setting's value must not be rendered.

    Two kinds of setting qualify, and the second is the one that is easy to
    miss. A setting the vocabulary marks `secret` obviously must not be
    printed. So must a setting the vocabulary does not declare **at all**: the
    effective environment is read off the unit and whatever else drops into it,
    so it carries values this tool never wrote and cannot classify. A token
    another tool set on the robot would otherwise be printed in full, and the
    redactor was never given it. Unclassified is treated as secret rather than
    as safe.

    Args:
        name: The setting's name.

    Returns:
        True when its value must be reported as set or unset rather than shown.
    """
    return name in _SECRET_NAMES or name not in _DECLARED_NAMES


def _shown(name: str, value: str | None) -> str:
    """Render one setting's value for a report.

    Args:
        name: The setting's name.
        value: What it holds, or `None` when it is not set at all.

    Returns:
        The value, or `set`/`unset` when the vocabulary marks the setting
        secret. See `REVIEW.md`: a self-reporting configuration surface reports
        a secret as set or unset, never by value.
    """
    if _unclassified(name):
        return _UNSET if not value else _SET
    return "" if value is None else value


async def guard_robot_secrets(daemon: DaemonClient, reporter: Reporter) -> None:
    """Learn the robot's secret values before rendering anything it wrote.

    Every command that operates a robot renders text the robot produced: a
    journal line, a systemd complaint, a traceback from the daemon's control.
    Any of it can carry a configured credential, and a redactor cannot remove a
    string it was never given — so each of those commands reads the robot's
    effective environment first and hands the values of the settings marked
    secret to the reporter. That is reachyctl REQ-059 on the paths nobody
    controls, and it costs one round trip.

    A robot whose environment cannot be read fails the command rather than
    proceeding, because proceeding would mean rendering its output while unable
    to promise a credential is not in it.

    The one thing this cannot cover is its own failure message, which carries
    `systemctl`'s complaint about the read that did not happen. There is no
    order of operations that fixes that: the values are what would scrub it, and
    they are exactly what could not be obtained.

    Args:
        daemon: The robot.
        reporter: Where everything is written.

    Raises:
        RobotAccessError: If the robot's environment could not be read.
    """
    try:
        effective = await daemon.effective_configuration()
    except RobotAccessError as error:
        message = (
            f"{error}. Nothing this robot wrote can be rendered until its "
            f"configuration has been read, because a credential in its output "
            f"would go out unscrubbed"
        )
        raise RobotAccessError(message) from error
    guard_secrets(reporter, effective)


def guard_secrets(reporter: Reporter, *settings: Mapping[str, str]) -> None:
    """Teach the reporter every secret value in play, before anything renders.

    The rendering above already declines to print a secret. This is the second
    guard, and it is the one that covers the paths nobody controls: the text of
    an exception raised inside a library, a robot's own complaint quoted back,
    a verbose line written by a future edit to this file. A redactor cannot
    remove a string it was never given — see `REVIEW.md` on why seeding one is
    the legitimate reveal.

    Only the values of settings the vocabulary marks `secret` are guarded. A
    value this tool cannot classify is not rendered at all — see `_unclassified`
    — and seeding the redactor with every environment value the robot happens
    to carry would replace ordinary words like a log level or a path with a
    placeholder throughout the output.

    Args:
        reporter: Where everything is written.
        settings: Any number of mappings that may hold a secret value.
    """
    for mapping in settings:
        for name, value in mapping.items():
            if name in _SECRET_NAMES and value:
                reporter.redactor.guard(value)


@dataclass(frozen=True, slots=True, kw_only=True)
class Difference:
    """What a declaration would change on a robot.

    Attributes:
        desired: What the declaration says should be in force.
        managed: What the managed region carries now.
        effective: What the daemon is actually running with, whichever drop-in
            or unit file put it there.
        added: Settings the declaration has and the region does not.
        changed: Settings both have, with different values.
        removed: Settings the region has and the declaration does not. These
            are the ones provisioning REQ-063 is about: withdrawing a setting
            takes it off the robot rather than leaving it behind.
        unchanged: Settings that already match.
        not_in_force: Settings whose declared value is not what the daemon is
            actually running with, whatever the region says. A setting can be
            in the region, unchanged, and silently inert because something else
            overrode it — which is one of the two failures the reachyctl spec's
            background names.
        unmanaged: Settings the daemon carries that neither the declaration nor
            the region mentions. Reported and never touched: this tool owns one
            drop-in, not the unit.
    """

    desired: Mapping[str, str]
    managed: Mapping[str, str]
    effective: Mapping[str, str]
    added: tuple[str, ...]
    changed: tuple[str, ...]
    removed: tuple[str, ...]
    unchanged: tuple[str, ...]
    not_in_force: tuple[str, ...]
    unmanaged: tuple[str, ...]

    @property
    def changes(self) -> bool:
        """Whether applying this would change the region at all.

        Returns:
            True when something would be added, changed or removed.
        """
        return bool(self.added or self.changed or self.removed)

    def summary(self) -> str:
        """Say in one line what would change.

        Returns:
            The counts, or that nothing would change. Names, never values.
        """
        if not self.changes:
            inert = (
                ""
                if not self.not_in_force
                else (
                    f", but {len(self.not_in_force)} declared setting(s) are not "
                    f"in force: {', '.join(self.not_in_force)}"
                )
            )
            return f"the robot already matches the declaration{inert}"
        return (
            f"{len(self.added)} to add, {len(self.changed)} to change, "
            f"{len(self.removed)} to remove, {len(self.unchanged)} already "
            f"in place"
        )


def compare(
    desired: Mapping[str, str],
    managed: Mapping[str, str],
    effective: Mapping[str, str],
) -> Difference:
    """Work out what a declaration would change.

    Args:
        desired: What the declaration says should be in force.
        managed: What the managed region carries now.
        effective: What the daemon is actually running with.

    Returns:
        The comparison. Every tuple is in name order, so two runs against the
        same robot produce the same report.
    """
    added = tuple(sorted(set(desired) - set(managed)))
    removed = tuple(sorted(set(managed) - set(desired)))
    shared = sorted(set(desired) & set(managed))
    changed = tuple(name for name in shared if desired[name] != managed[name])
    unchanged = tuple(name for name in shared if desired[name] == managed[name])
    not_in_force = tuple(
        name for name in sorted(desired) if effective.get(name) != desired[name]
    )
    unmanaged = tuple(sorted(set(effective) - set(desired) - set(managed)))
    return Difference(
        desired=dict(desired),
        managed=dict(managed),
        effective=dict(effective),
        added=added,
        changed=changed,
        removed=removed,
        unchanged=unchanged,
        not_in_force=not_in_force,
        unmanaged=unmanaged,
    )


class ConfigurationConflictError(CommandError):
    """The robot's managed region is not one this tool wrote.

    `FAILURE` rather than `CONFIGURATION`: the command ran, reached the robot,
    and found something. What it found is a robot in a state this tool will not
    overwrite.
    """

    exit_code: ExitCode = ExitCode.FAILURE


async def read_difference(
    daemon: DaemonClient,
    declared: Mapping[str, str],
    reporter: Reporter,
    *,
    merge: bool = False,
) -> Difference:
    """Ask the robot where it stands against a declaration.

    Args:
        daemon: The robot.
        declared: What was asked for.
        reporter: Where everything is written. Seeded with the robot's secret
            values as soon as they are known and before anything the robot
            wrote can reach a message.
        merge: Whether `declared` is a set of changes to fold into the region
            rather than the whole of it. The fold happens here, against the
            region as it was just read, so `set` never has to guess what else
            is on the robot.

    Returns:
        The comparison.

    Raises:
        CommandError: If the managed region on the robot was written by
            something other than this tooling. Rewriting it regardless is how
            two tools start reverting each other, so the command stops and says
            what it found.
    """
    await daemon.connect()
    # The effective environment first, and the redactor seeded from it before
    # the region is read — because reading the region can fail with the robot's
    # own words in the message, and a redactor cannot remove a value it was
    # never given. See `guard_robot_secrets`.
    effective = await daemon.effective_configuration()
    guard_secrets(reporter, effective)
    try:
        managed = await daemon.read_managed_settings()
    except MalformedRegionError as error:
        raise ConfigurationConflictError(str(error)) from error
    desired = merge_settings(managed, declared) if merge else declared
    guard_secrets(reporter, desired, managed)
    return compare(desired, managed, effective)


#:= docs/specs/reachyctl/index.md#req-052-configuration-changes-can-be-previewed-without-being-applied
#:% Every command that modifies robot state MUST support a mode that reports the
#:% changes it would make and makes none of them.
async def run_apply(
    daemon: DaemonClient,
    declared: Mapping[str, str],
    reporter: Reporter,
    *,
    preview: bool,
    merge: bool = False,
) -> tuple[StepLog, Difference]:
    """Make the managed region exactly the declaration, or report what that would do.

    Args:
        daemon: The robot.
        declared: What was asked for. Already validated: this function writes
            values, it does not judge them.
        reporter: Where progress goes as it happens.
        preview: Whether to report the changes and make none of them. In
            preview nothing is written, nothing is reloaded and nothing is
            restarted — the only commands that run are the two reads.
        merge: Whether `declared` is a set of changes to fold into what the
            region already carries (`set`) rather than the whole of what should
            be in force (`apply`). Merging still writes the region whole, from
            the merged desired state; what it does not do is remove what is
            absent from `declared`.

    Returns:
        Every step, and the comparison the steps were decided from.

    Raises:
        CommandError: If the region cannot be read or written.
    """
    steps = StepLog(reporter=reporter)
    steps.begin(_READ, "reading the managed region and the effective environment")
    difference = await read_difference(daemon, declared, reporter, merge=merge)
    desired = difference.desired
    steps.done(_READ, difference.summary())

    if not difference.changes:
        # Idempotence, and it is the comparison that produces it rather than a
        # second code path: provisioning REQ-060 asks that a repeated run
        # change nothing, and there is nothing here that could.
        steps.skipped(_WRITE, "the managed region already says this")
        steps.skipped(_RESTART, "nothing changed, so nothing needs re-reading")
    elif preview:
        steps.planned(_WRITE, _would_write(difference))
        steps.planned(_RESTART, f"would restart the daemon — {RESTART_WARNING}")
        steps.planned(
            _VERIFY,
            "would then require the robot to report the declaration in force",
        )
        return steps, difference
    else:
        # Before the first mutation, not merely before the restart. The write is
        # what makes the restart necessary, so an operator told afterwards has
        # been told about something that already happened.
        reporter.note(RESTART_WARNING)
        steps.begin(_WRITE, f"writing {daemon.layout.drop_in}")
        await daemon.write_managed_region(render_region(desired))
        steps.done(_WRITE, _would_write(difference).replace("would ", "", 1))

        steps.begin(_RESTART, f"restarting {daemon.layout.daemon_unit}")
        restarted = await daemon.restart_daemon()
        if restarted.ok:
            steps.done(_RESTART, "the daemon restarted and re-read its environment")
        else:
            # Recorded, and the run does not end here. The verification only
            # reads, a restart that reported a failure may still have taken
            # effect, and either way the operator needs to know what is in force
            # now rather than only that a command exited non-zero.
            steps.failed(_RESTART, restarted.complaint())

    await _verify(steps, daemon, desired, difference.removed)
    return steps, difference


def _would_write(difference: Difference) -> str:
    """Say what writing the region would do to it.

    Args:
        difference: The comparison.

    Returns:
        One line naming the settings, never their values.
    """
    parts = []
    if difference.added:
        parts.append(f"add {', '.join(difference.added)}")
    if difference.changed:
        parts.append(f"change {', '.join(difference.changed)}")
    if difference.removed:
        parts.append(f"remove {', '.join(difference.removed)}")
    return f"would {'; '.join(parts)}"


async def _verify(
    steps: StepLog,
    daemon: DaemonClient,
    desired: Mapping[str, str],
    removed: tuple[str, ...],
) -> None:
    """Ask the robot whether the declaration is actually in force now.

    Two halves, because full ownership has two halves. What is declared is
    asserted with the same check `doctor` runs, from the same registry, rather
    than a second comparison written here — reachyctl REQ-056. What was
    *withdrawn* is asserted here, because the shared check compares only the
    keys an intent declares and a withdrawn key is precisely one it no longer
    does. Without the second half, emptying the region entirely — a legitimate
    apply, and the one provisioning REQ-063 is about — would write, restart, and
    verify nothing at all.

    Both read the effective environment rather than the region that was just
    written, which is the whole point: a region that is on disk and not in force
    is exactly the silently-inert configuration this tool exists to catch.

    Args:
        steps: Where to record it.
        daemon: The robot.
        desired: What should be in force.
        removed: What was taken out of the managed region and should no longer
            be in force.
    """
    if not desired and not removed:
        steps.skipped(_VERIFY, "the declaration names no setting to verify")
        return
    steps.begin(_VERIFY, "asking the robot what configuration is now in force")
    complaints: list[str] = []
    if desired:
        context = CheckContext(daemon=daemon, intent=Intent(configuration=desired))
        result = await run_check(check_by_identifier(CONFIGURATION_EFFECTIVE), context)
        if result.failed:
            complaints.append(result.summary)
    lingering: tuple[str, ...] = ()
    if removed:
        effective = await daemon.effective_configuration()
        lingering = tuple(name for name in removed if name in effective)
        if lingering:
            complaints.append(
                f"{len(lingering)} withdrawn setting(s) are still in force: "
                f"{', '.join(lingering)}. Something outside the managed drop-in "
                f"is setting them",
            )
    if complaints:
        steps.failed(_VERIFY, "; ".join(complaints))
        return
    steps.done(
        _VERIFY,
        f"all {len(desired)} declared setting(s) are in force"
        + (f", and {len(removed)} withdrawn one(s) are gone" if removed else ""),
    )


def _rows_for(difference: Difference) -> tuple[Mapping[str, object], ...]:
    """Shape a comparison into one row per setting.

    Args:
        difference: The comparison.

    Returns:
        The rows, in name order, covering everything either side knows about.
    """
    changes = dict.fromkeys(difference.added, "add")
    changes.update(dict.fromkeys(difference.changed, "change"))
    changes.update(dict.fromkeys(difference.removed, "remove"))
    changes.update(dict.fromkeys(difference.unchanged, "none"))
    changes.update(dict.fromkeys(difference.unmanaged, "unmanaged"))
    return tuple(
        {
            "setting": name,
            "change": changes[name],
            "declared": _shown(name, difference.desired.get(name)),
            "in_force": _shown(name, difference.effective.get(name)),
            "managed": name in difference.managed,
        }
        for name in sorted(changes)
    )


def report_for_difference(
    command: str,
    difference: Difference,
    robot: str,
    *,
    ok: bool,
    summary: str,
    rows: tuple[Mapping[str, object], ...] | None = None,
) -> Report:
    """Shape a configuration result into the thing every rendering is built from.

    Args:
        command: Which command produced it.
        difference: The comparison it was decided from.
        robot: How the robot was addressed.
        ok: Whether what was asked for succeeded.
        summary: The one line a person reads first.
        rows: The rows to show, or `None` for one row per setting.

    Returns:
        The report to emit.
    """
    data: dict[str, object] = {
        "robot": robot,
        "declared": len(difference.desired),
        "to_add": difference.added,
        "to_change": difference.changed,
        "to_remove": difference.removed,
        "not_in_force": difference.not_in_force,
        "unmanaged": difference.unmanaged,
    }
    return Report(
        command=command,
        ok=ok,
        summary=summary,
        data=data,
        columns=(
            ("setting", "change", "declared", "in_force", "managed")
            if rows is None
            else ("step", "status", "detail")
        ),
        rows=_rows_for(difference) if rows is None else rows,
    )


def execute_get(
    daemon: DaemonClient,
    names: Sequence[str],
    reporter: Reporter,
    robot: str,
    close: Closer | None = None,
) -> ExitCode:
    """Report the configuration the robot is actually running with.

    Args:
        daemon: The robot.
        names: The settings to report, or empty for every one the robot has.
        reporter: Where everything is written.
        robot: How the robot was addressed.
        close: How to let the link go.

    Returns:
        The exit status. `FAILURE` when a setting that was asked for by name is
        not in the environment at all, and `OK` otherwise: asking for a named
        setting and being told nothing is a negative answer, where asking for
        everything and getting a short list is the answer.

    Raises:
        CommandError: If the robot could not be reached.
    """

    async def _read() -> tuple[Mapping[str, str], Mapping[str, str]]:
        """Read the effective environment and what the region owns.

        Returns:
            What is in force, and what this tool put there — so the report can
            say which settings are managed and which arrived some other way.
        """
        await daemon.connect()
        effective = await daemon.effective_configuration()
        # Seeded before the region is read, for the reason `read_difference`
        # gives: reading it can fail with the robot's own words in the message.
        guard_secrets(reporter, effective)
        try:
            managed = await daemon.read_managed_settings()
        except MalformedRegionError:
            # `get` reads; it never writes. A region something else wrote is
            # worth knowing about when converging on a declaration, and is not
            # a reason to refuse to show what is in force.
            managed = {}
        guard_secrets(reporter, managed)
        return effective, managed

    effective, managed = asyncio.run(closing(_read(), close))
    shown = sorted(effective) if not names else sorted(set(names))
    absent = tuple(name for name in shown if name not in effective)
    return reporter.emit(
        Report(
            command="config get",
            ok=not absent,
            summary=(
                f"the daemon is running with {len(effective)} setting(s) in its "
                f"environment"
                if not absent
                else f"not set on this robot: {', '.join(absent)}"
            ),
            data={
                "robot": robot,
                "settings": len(effective),
                "absent": absent,
            },
            columns=("setting", "in_force", "managed"),
            rows=tuple(
                {
                    "setting": name,
                    "in_force": _shown(name, effective.get(name)),
                    "managed": name in managed,
                }
                for name in shown
            ),
        ),
    )


def execute_diff(
    daemon: DaemonClient,
    desired: Mapping[str, str],
    reporter: Reporter,
    robot: str,
    close: Closer | None = None,
) -> ExitCode:
    """Compare a declaration against the robot and report the difference.

    Args:
        daemon: The robot.
        desired: What the declaration says should be in force.
        reporter: Where everything is written.
        robot: How the robot was addressed.
        close: How to let the link go.

    Returns:
        The exit status. `FAILURE` when the robot does not match the
        declaration, which is what makes `config diff` usable as a gate in the
        same way `doctor` is; `OK` when it matches.

    Raises:
        CommandError: If the robot could not be reached, or its managed region
            was written by something else.
    """
    difference = asyncio.run(
        closing(read_difference(daemon, desired, reporter), close),
    )
    matches = not difference.changes and not difference.not_in_force
    return reporter.emit(
        report_for_difference(
            "config diff",
            difference,
            robot,
            ok=matches,
            summary=difference.summary(),
        ),
    )


def execute_apply(
    command: str,
    daemon: DaemonClient,
    declared: Mapping[str, str],
    reporter: Reporter,
    robot: str,
    *,
    preview: bool,
    merge: bool = False,
    close: Closer | None = None,
) -> ExitCode:
    """Converge the robot on a declaration, or report what that would do.

    Args:
        command: Which command produced it — `config set` or `config apply`.
        daemon: The robot.
        declared: What was asked for, already validated.
        reporter: Where everything is written.
        robot: How the robot was addressed.
        preview: Whether to report the changes and make none of them.
        merge: Whether `declared` is a set of changes to fold into the region
            rather than the whole of it.
        close: How to let the link go.

    Returns:
        The exit status.

    Raises:
        CommandError: If the robot could not be reached, or its managed region
            was written by something else, or a write failed.
    """
    steps, difference = asyncio.run(
        closing(
            run_apply(daemon, declared, reporter, preview=preview, merge=merge),
            close,
        ),
    )
    summary = (
        f"nothing was changed: this was a preview. {difference.summary()}"
        if preview
        else _applied_summary(steps, difference)
    )
    return reporter.emit(
        report_for_difference(
            command,
            difference,
            robot,
            ok=steps.ok,
            summary=summary,
            rows=steps.rows,
        ),
    )


def _applied_summary(steps: StepLog, difference: Difference) -> str:
    """Say in one line what an apply did.

    Args:
        steps: What happened.
        difference: What it was decided from.

    Returns:
        The line, naming the failing step when one failed.
    """
    failures = [result for result in steps.results if result.failed]
    if failures:
        first = failures[0]
        return f"the apply failed at {first.name}: {first.detail}"
    if not difference.changes:
        return f"nothing to do: {difference.summary()}"
    return (
        f"applied and verified in force: {len(difference.added)} added, "
        f"{len(difference.changed)} changed, {len(difference.removed)} removed"
    )


def merge_settings(
    managed: Mapping[str, str],
    assignments: Mapping[str, str],
) -> dict[str, str]:
    """Fold individual assignments into what the region already carries.

    `set` changes some settings and leaves the rest alone, which is not the
    same thing as the region being appended to: the region is still written
    whole, from the merged desired state, so it stays exactly what this tool
    says it is. `apply` is the verb that removes what is no longer declared,
    and this is why the two are separate verbs rather than one with a flag.

    Args:
        managed: What the region carries now.
        assignments: What `set` was asked to change.

    Returns:
        The desired state.
    """
    return {**managed, **assignments}


def secret_setting_names() -> frozenset[str]:
    """List the settings whose value must not become a command-line argument.

    Returns:
        Their names. `config set` refuses these: an argument is visible in the
        process list and lands in the shell history, which is the rule
        `reachyctl.credentials` is built around and which does not stop being
        true because the secret arrived as a setting.
    """
    return _SECRET_NAMES


def known_setting_names() -> tuple[str, ...]:
    """List the settings the vocabulary declares, for help text.

    Returns:
        The names in the order they are declared.
    """
    return tuple(setting.name for setting in ROBOT_SETTINGS)
