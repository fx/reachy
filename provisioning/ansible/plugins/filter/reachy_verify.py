"""Asserting the end state, from the one definition of what a healthy robot is.

Provisioning REQ-066 says a run verifies the robot reaches a working end state
before reporting success, and reachyctl REQ-056 says the checks it performs and
the ones `reachyctl doctor` performs are defined once and used by both. This
module is how the `verify` role satisfies the pair: it imports `reachy_checks`
and runs `CHECKS`. It declares no check of its own, and it does not shell out to
`reachyctl` — a second notion of healthy would drift, and a CLI dependency would
mean provisioning could only run from a machine that had the CLI installed.

**The robot is read by ordinary tasks and answered from here.** The role gathers
three pieces of evidence with `command` tasks that change nothing, and this turns
them into the `RobotDaemon` the registry's probes are written against. The
alternative — an action plugin executing commands from inside the check run —
would put the transport inside a filter, where `--check`, `--diff` and the task
log cannot see it. Everything the robot is asked is a task an operator can read
in the output.

**Two requirements are reported as skipped rather than failed, and the reason is
in the skip line.** The groundstation checks open a session, and a session opened
from the machine running the playbook measures that machine's route to the
groundstation rather than the robot's; `reachyctl doctor --url` is the command
that asks the question this cannot. The model files belong to the groundstation's
own artifact and are not on the robot at all. `reachy_checks` distinguishes
skipped from failed precisely so an absent prerequisite does not read as a broken
installation, and collapsing either into a failure here would make every
provisioning run red for something provisioning does not install.

Nothing here reports a configuration value. The probes already hold that line —
the effective-configuration check names the keys that differ and never what they
hold — and the record this returns carries only what they produced.
"""

from __future__ import annotations

import asyncio
import json
import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from reachy_checks import (
    APPLICATION_INSTALLED,
    APPLICATION_RUNNING,
    CHECKS,
    ApplicationState,
    CheckContext,
    DaemonInfo,
    InstalledApplication,
    Intent,
    Requirement,
    counts_of,
    run_checks,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

__all__ = [
    "GATHERED_DAEMON_ABSENT",
    "GROUNDSTATION_SKIPPED",
    "MODELS_SKIPPED",
    "NO_APPLICATION_DECLARED",
    "DaemonControlUnavailableError",
    "FilterModule",
    "GatheredDaemon",
    "check_run",
    "daemon_from",
    "parse_properties",
    "split_environment",
]

# systemd's own spelling for "this unit is running".
_ACTIVE: Final = "active"

# The setting the satellite's announced Home Assistant identity is carried in.
# Named here rather than inlined because it is the one setting whose value the
# checks report verbatim, and a reader should be able to see which one that is.
IDENTITY_SETTING: Final = "REACHY_HOME_ASSISTANT_IDENTITY"

GROUNDSTATION_SKIPPED: Final = (
    "provisioning does not open a session to the groundstation: one opened from "
    "the machine running the playbook would measure that machine's route rather "
    "than the robot's. Run `reachyctl doctor --url ...` to exercise the link"
)

MODELS_SKIPPED: Final = (
    "the model files belong to the groundstation's artifact and are not on the "
    "robot; nothing here has a registry to judge them against"
)

GATHERED_DAEMON_ABSENT: Final = (
    "no evidence was gathered from the robot, so nothing can be said about it"
)

# The two application checks have no subject when nothing declares an
# application. They are withheld rather than run, because running them would
# report a robot as broken for not carrying something nobody asked it to carry —
# and the registry has no requirement to express "there is an application" with,
# so a skip cannot come from the runner. What comes back instead names them and
# says why, which keeps the report honest about the difference between a check
# that passed and one that was never put.
NO_APPLICATION_DECLARED: Final = (
    "no application is declared, so there is nothing to ask about. Set "
    "reachy_app_distribution to name what this robot is supposed to be running"
)

_APPLICATION_CHECKS: Final = frozenset({APPLICATION_INSTALLED, APPLICATION_RUNNING})


class DaemonControlUnavailableError(RuntimeError):
    """The daemon's application control could not be run, or answered unreadably.

    Raised rather than answered with "not running", because the two are different
    facts and only one of them is about the application. `reachy_checks.run_check`
    turns anything a probe raises into a failed result naming what was raised, so
    the run still reports every other link — and the line an operator reads says
    the control could not be asked rather than claiming an answer it never got.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class GatheredDaemon:
    """One gathering of evidence, answering the questions the checks ask.

    The registry's `RobotDaemon` protocol is asynchronous because half of its
    real implementations talk over a network. This one talked over the network
    already — the role's tasks did — so every method here answers from what was
    gathered. That is also why nothing is memoised and nothing needs to be: the
    facts were read once, in tasks whose output is in the run's log.

    Attributes:
        info: What the daemon's unit reported about itself.
        application: Whether the application is installed, and at what version.
        state: Whether the daemon says it is running the application, or `None`
            when the control could not be asked.
        control_complaint: Why the control could not be asked, when it could not.
        effective: The environment the unit ended up with, whichever drop-in or
            unit file put it there.
    """

    info: DaemonInfo
    application: InstalledApplication
    state: ApplicationState | None = None
    control_complaint: str = ""
    effective: Mapping[str, str] = field(default_factory=dict)

    async def ping(self) -> DaemonInfo:
        """Say whether the daemon is there and what it is.

        Returns:
            What the unit reported.
        """
        return self.info

    async def installed_application(self) -> InstalledApplication:
        """Say what version of the application the daemon's environment holds.

        Returns:
            What the daemon's own interpreter answered.
        """
        return self.application

    async def application_state(self) -> ApplicationState:
        """Say whether the daemon is running the application.

        Returns:
            What the daemon's control reported.

        Raises:
            DaemonControlUnavailableError: If the control could not be run or
                answered with something unreadable. See the class documentation
                for why this is not "not running".
        """
        if self.state is None:
            raise DaemonControlUnavailableError(self.control_complaint)
        return self.state

    async def effective_configuration(self) -> Mapping[str, str]:
        """Say what environment the daemon is actually running with.

        Returns:
            The settings by name. Values are returned so a check can compare
            them and are never reported — see the module documentation.
        """
        return self.effective

    async def announced_identity(self) -> str:
        """Say what identity the satellite announces to Home Assistant.

        Returns:
            The announced identity, or an empty string when nothing sets one.
        """
        return self.effective.get(IDENTITY_SETTING, "")


def parse_properties(text: str) -> dict[str, str]:
    """Read the `KEY=VALUE` lines `systemctl show` prints.

    Args:
        text: What the command wrote to standard output.

    Returns:
        The properties by name. A unit that is not installed answers with empty
        values rather than failing, so an empty answer means what it says.
    """
    found: dict[str, str] = {}
    for line in text.splitlines():
        name, separator, value = line.partition("=")
        if separator:
            found[name] = value
    return found


def split_environment(value: str) -> dict[str, str]:
    """Read the one line `systemctl show --property=Environment` prints.

    systemd renders the whole environment on one line, quoting an assignment that
    needs it. Splitting it the way a shell would is the closest available parse
    and not an exact one — systemd's escaping is its own — and it is what there
    is, because `systemctl show` offers no structured output. The managed region
    itself is read from the file, where the format is this repository's.

    Args:
        value: The property's value.

    Returns:
        The settings by name.
    """
    settings: dict[str, str] = {}
    for assignment in shlex.split(value.strip()):
        name, separator, held = assignment.partition("=")
        if separator:
            settings[name] = held
    return settings


def daemon_from(evidence: Mapping[str, Any]) -> GatheredDaemon:
    """Turn what the role gathered into the robot the checks are written against.

    Args:
        evidence: What the gathering tasks produced. `unit` and `application`
            name what was asked about; `properties` is what `systemctl show`
            printed; `versions` is the JSON the daemon's interpreter printed;
            `status` is the JSON the daemon's application control printed, and
            `status_complaint` is why it did not, when it did not.

    Returns:
        The robot, as one immutable answer to every question the registry asks.
    """
    unit = str(evidence.get("unit", ""))
    application = str(evidence.get("application", ""))
    daemon_distribution = str(evidence.get("daemon_distribution", ""))
    properties = parse_properties(str(evidence.get("properties", "")))
    versions = _versions(str(evidence.get("versions", "")))
    effective = split_environment(properties.get("Environment", ""))
    return GatheredDaemon(
        info=_daemon_info(unit, properties, versions.get(daemon_distribution, "")),
        application=_installed(application, versions.get(application, "")),
        state=_application_state(str(evidence.get("status", ""))),
        control_complaint=_control_complaint(evidence, application),
        effective=effective,
    )


def check_run(
    evidence: Mapping[str, Any] | None,
    intent: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run every registered check against what was gathered, and report it.

    Args:
        evidence: What the gathering tasks produced, or `None` when nothing was
            gathered — in which case every robot-side check is skipped saying so
            rather than failed, because a run that learned nothing has not
            learned that the robot is broken.
        intent: What the robot is supposed to be: `configuration` by name, and
            `announced_identity` when one is declared.

    Returns:
        A record carrying `ok`, the one-line `summary` naming the first broken
        link, the `counts` by outcome, the `failures` as identifiers, the checks
        `not_asked` and why, and one row per check that was. Names and outcomes
        only — the probes report no value and this adds none.
    """
    context = _context(evidence, intent)
    checks = CHECKS
    not_asked: list[dict[str, str]] = []
    if evidence is not None and not str(evidence.get("application", "")).strip():
        checks = tuple(
            check for check in CHECKS if check.identifier not in _APPLICATION_CHECKS
        )
        not_asked = [
            {"check": identifier, "reason": NO_APPLICATION_DECLARED}
            for identifier in sorted(_APPLICATION_CHECKS)
        ]
    run = asyncio.run(run_checks(context, checks))
    return {
        "ok": run.ok,
        "summary": run.summary(),
        "counts": counts_of(run.results),
        "failures": [result.identifier for result in run.failures],
        "not_asked": not_asked,
        "results": [
            {
                "check": result.identifier,
                "description": result.description,
                "status": result.outcome.value,
                "detail": result.summary,
                "remediation": (
                    None
                    if result.remediation is None
                    else result.remediation.explanation
                ),
                "command": (
                    None
                    if result.remediation is None or not result.remediation.command
                    else result.remediation.command
                ),
            }
            for result in run.results
        ],
    }


def _context(
    evidence: Mapping[str, Any] | None,
    intent: Mapping[str, Any] | None,
) -> CheckContext:
    """Assemble what the check run has to work with.

    Args:
        evidence: What was gathered from the robot, or `None`.
        intent: What the robot is supposed to be, or `None`.

    Returns:
        The context, carrying a reason for every resource that is absent so a
        skipped check says why rather than leaving a blank.
    """
    unavailable: dict[Requirement, str] = {
        Requirement.GROUNDSTATION: GROUNDSTATION_SKIPPED,
        Requirement.MODELS: MODELS_SKIPPED,
    }
    if evidence is None:
        unavailable[Requirement.DAEMON] = GATHERED_DAEMON_ABSENT
    declared = dict(intent.get("configuration", {})) if intent else {}
    identity = intent.get("announced_identity") if intent else None
    return CheckContext(
        daemon=None if evidence is None else daemon_from(evidence),
        intent=(
            None
            if intent is None
            else Intent(
                configuration=declared,
                announced_identity=identity or None,
            )
        ),
        unavailable=unavailable,
    )


def _daemon_info(
    unit: str,
    properties: Mapping[str, str],
    version: str,
) -> DaemonInfo:
    """Judge what the unit reported about itself.

    Args:
        unit: The unit that was asked about.
        properties: What `systemctl show` reported.
        version: The daemon distribution's version, when its environment holds
            one.

    Returns:
        Whether it is answering, and why it is not when it is not. A unit that is
        not loaded and one that is loaded and stopped are different faults, and
        the complaint says which.
    """
    load = properties.get("LoadState", "")
    active = properties.get("ActiveState", "")
    if load and load != "loaded":
        return DaemonInfo(
            responding=False,
            complaint=(
                f"the unit {unit} is {load}; the daemon is not installed on this robot"
            ),
        )
    if active != _ACTIVE:
        substate = properties.get("SubState", "")
        return DaemonInfo(
            responding=False,
            complaint=(
                f"the unit {unit} is {active or 'not reporting a state'}"
                f"{f' ({substate})' if substate else ''}"
            ),
        )
    return DaemonInfo(responding=True, version=version)


def _installed(application: str, version: str) -> InstalledApplication:
    """Judge what the daemon's interpreter said it holds.

    Args:
        application: The distribution that was asked about.
        version: What it answered, empty when nothing is installed.

    Returns:
        Whether it is installed, and at what version.
    """
    if not version:
        return InstalledApplication(
            installed=False,
            complaint=(
                f"{application or 'the application'} is not installed in the "
                f"environment the daemon runs"
            ),
        )
    return InstalledApplication(installed=True, version=version)


def _application_state(status: str) -> ApplicationState | None:
    """Read what the daemon's application control reported.

    Args:
        status: The JSON it printed, or an empty string when it printed nothing.

    Returns:
        The state, or `None` when there is no readable answer — which
        `GatheredDaemon.application_state` turns into a raise rather than into a
        claim that the application is stopped.
    """
    if not status.strip():
        return None
    try:
        decoded = json.loads(status)
    except ValueError:
        return None
    if not isinstance(decoded, dict):
        return None
    detail = decoded.get("detail")
    return ApplicationState(
        running=decoded.get("running") is True,
        detail=detail if isinstance(detail, str) else "",
    )


def _control_complaint(evidence: Mapping[str, Any], application: str) -> str:
    """Say why the daemon's application control could not be asked.

    Args:
        evidence: What the gathering tasks produced.
        application: The distribution that was asked about.

    Returns:
        The complaint the role supplied, or a line saying the control answered
        with something unreadable. Nothing the robot printed is quoted: the
        daemon's own output is where a setting's value would be.
    """
    supplied = str(evidence.get("status_complaint", "")).strip()
    if supplied:
        return (
            f"the daemon's application control could not be run for "
            f"{application or 'the application'}: {supplied}"
        )
    return (
        f"the daemon's application control answered about "
        f"{application or 'the application'} with something that is not a JSON "
        f"object. What it wrote is withheld, because the daemon's output is "
        f"where a setting's value would be"
    )


def _versions(payload: str) -> dict[str, str]:
    """Read the JSON the daemon's interpreter printed.

    Args:
        payload: What it wrote to standard output.

    Returns:
        The version by distribution name, empty where nothing was installed or
        where the answer could not be read. An unreadable answer therefore
        surfaces as "not installed", which the installed-application check
        already reports as a failure with a remediation — and the version query
        is a task in the run's log, so what actually happened is readable there.
    """
    if not payload.strip():
        return {}
    try:
        decoded = json.loads(payload)
    except ValueError:
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {str(name): str(held or "") for name, held in decoded.items()}


class FilterModule:
    """Expose this module's functions to Jinja, which is how the roles reach them."""

    def filters(self) -> dict[str, Callable[..., Any]]:
        """List the filters this plugin provides.

        Returns:
            The filters by the name a template writes.
        """
        return {
            "reachy_check_run": check_run,
            # Used by the removal playbook, which asserts that no declared
            # setting is left in force. It reads the same property the checks
            # read, through the same parse, so "in force" means one thing.
            "reachy_environment": split_environment,
        }
