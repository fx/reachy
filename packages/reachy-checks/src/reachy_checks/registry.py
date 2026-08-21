"""Every check there is, declared as data, in the order the chain runs.

This module is the answer to reachyctl REQ-056. `reachyctl doctor` runs these
declarations and the Ansible verification role imports the same ones, so a
check added here is performed by both without having been added twice — and,
more to the point, neither can quietly hold a different notion of healthy. Two
independently written notions drift, and the drift arrives as a robot that
provisioning calls fine and diagnosis calls broken.

**The identifiers and the remediation strings are a published interface.** The
troubleshooting runbook is keyed to the identifiers and shares this text rather
than restating it, so renaming one is a breaking change and paraphrasing one in
another document reintroduces exactly the drift this module exists to prevent.

Three of the commands below — `reachyctl deploy`, `reachyctl app start` and
`reachyctl config apply` — are the command surface the reachyctl spec defines
and change 0009 implements. They are named here because they are the right
remedy and because these strings are meant to be stable, not because they can
be run today.

The order is the order of the links between an operator and a working robot,
and it is for reading only: no check depends on another's result, and the
runner executes every one of them whatever the ones before it did.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from reachy_checks import probes
from reachy_checks.context import CheckContext, Requirement
from reachy_checks.outcomes import Finding, Remediation

__all__ = [
    "APPLICATION_INSTALLED",
    "APPLICATION_RUNNING",
    "CHECKS",
    "CONFIGURATION_EFFECTIVE",
    "DAEMON_REACHABLE",
    "GROUNDSTATION_CAPABILITIES",
    "GROUNDSTATION_ROUND_TRIP",
    "GROUNDSTATION_SESSION",
    "HOME_ASSISTANT_IDENTITY",
    "MODEL_FILES",
    "Check",
    "Probe",
    "check_by_identifier",
    "identifiers",
]

# Every name the alias below mentions is imported at run time rather than under
# `TYPE_CHECKING`, and that is not tidiness. A PEP 695 alias is lazy: its
# right-hand side is evaluated on first access, so `Probe.__value__` — or any
# tool that introspects it — would raise `NameError` on a name that only ever
# existed for the type checker. `Probe` is what a consumer writes a check
# against, and change 0010's verification role imports this package as a
# module, so an alias it cannot evaluate is a trap laid for the one consumer
# this package exists to serve. `reachy_groundstation.ports` settled the same
# question the same way; see the comment above `ImageArray` there.
#
# What a check calls to find out. Asynchronous because half of them talk to
# something over a network, and a runner that had to know which half would be
# deciding per check what the registry is supposed to abstract.
type Probe = Callable[[CheckContext], Awaitable[Finding]]

# --- Identifiers -------------------------------------------------------------
# Stable, greppable, and quoted by the troubleshooting runbook. Dotted segments
# read as "component, then question"; words inside a segment are hyphenated.

DAEMON_REACHABLE: Final = "daemon.reachable"
APPLICATION_INSTALLED: Final = "application.installed"
APPLICATION_RUNNING: Final = "application.running"
GROUNDSTATION_SESSION: Final = "groundstation.session"
GROUNDSTATION_CAPABILITIES: Final = "groundstation.capabilities"
GROUNDSTATION_ROUND_TRIP: Final = "groundstation.round-trip"
MODEL_FILES: Final = "models.files"
CONFIGURATION_EFFECTIVE: Final = "configuration.effective"
HOME_ASSISTANT_IDENTITY: Final = "home-assistant.identity"


@dataclass(frozen=True, slots=True, kw_only=True)
class Check:
    """One thing that has to be true, and everything about asking.

    Attributes:
        identifier: The stable name. Published; see the module documentation.
        description: What this check is for, in one line, for the operator who
            is reading the table rather than the code.
        requires: What has to have been supplied before the probe can say
            anything. The runner skips the check when any of it is absent.
        probe: What to call.
        remediation: What to tell an operator when it fails.
    """

    identifier: str
    description: str
    requires: tuple[Requirement, ...]
    probe: Probe
    remediation: Remediation


#:= docs/specs/reachyctl/index.md#req-056-diagnosis-and-provisioning-agree-on-what-healthy-means
#:% The checks performed by the doctor command and by the provisioning verification
#:% step MUST be defined once and used by both.
#
#:= docs/specs/reachyctl/index.md#req-055-a-failed-check-states-how-to-fix-it
#:% Every diagnostic check that fails MUST report a remediation.
CHECKS: Final[tuple[Check, ...]] = (
    Check(
        identifier=DAEMON_REACHABLE,
        description="The robot's daemon is reachable and answering",
        requires=(Requirement.DAEMON,),
        probe=probes.daemon_reachable,
        remediation=Remediation(
            explanation=(
                "Nothing here starts a daemon it cannot reach. Confirm the "
                "robot is powered on and answering at the address configured, "
                "and that its daemon is running."
            ),
        ),
    ),
    Check(
        identifier=APPLICATION_INSTALLED,
        description="The satellite is installed on the robot, at a known version",
        requires=(Requirement.DAEMON,),
        probe=probes.application_installed,
        remediation=Remediation(
            explanation=(
                "Install the satellite into the robot's application "
                "environment. Deployment installs it and then verifies the "
                "version actually running, rather than trusting the install."
            ),
            command="reachyctl deploy",
        ),
    ),
    Check(
        identifier=APPLICATION_RUNNING,
        description="The satellite is running on the robot",
        requires=(Requirement.DAEMON,),
        probe=probes.application_running,
        remediation=Remediation(
            explanation="Start the application on the robot.",
            command="reachyctl app start",
        ),
    ),
    Check(
        identifier=GROUNDSTATION_SESSION,
        description="A session opens to the groundstation",
        requires=(Requirement.GROUNDSTATION,),
        probe=probes.groundstation_session,
        remediation=Remediation(
            explanation=(
                "The groundstation refused the session or never answered. "
                "Confirm the service is running, that the configured endpoint "
                "reaches it, and that the credential presented is the one it "
                "expects; its readiness endpoint reports whether it finished "
                "warming up."
            ),
        ),
    ),
    Check(
        identifier=GROUNDSTATION_CAPABILITIES,
        description="The session agrees on at least one capability",
        requires=(Requirement.GROUNDSTATION,),
        probe=probes.groundstation_capabilities,
        remediation=Remediation(
            explanation=(
                "The session opened and the two sides have no capability in "
                "common, so nothing would answer a frame. Compare the versions "
                "installed on each side, and check the groundstation's "
                "capability health for one that failed to warm up."
            ),
        ),
    ),
    Check(
        identifier=GROUNDSTATION_ROUND_TRIP,
        description="A frame goes out and comes back, and how long that took",
        requires=(Requirement.GROUNDSTATION,),
        probe=probes.groundstation_round_trip,
        remediation=Remediation(
            explanation=(
                "A session is up and no result came back to time. Read the "
                "groundstation's logs and capability health: a link this quiet "
                "is usually a capability that accepted the frame and produced "
                "nothing, rather than a network fault."
            ),
        ),
    ),
    Check(
        identifier=MODEL_FILES,
        description="Every pinned model file is present and unaltered",
        requires=(Requirement.MODELS,),
        probe=probes.model_files,
        remediation=Remediation(
            explanation=(
                "A model file is missing or is not the bytes the registry "
                "pins. Fetch them again into the directory the groundstation "
                "reads; the fetcher refuses anything whose digest does not "
                "match, so a run that succeeds leaves the reviewed weights in "
                "place."
            ),
            command=(
                "python -m reachy_groundstation.models.fetch "
                '"$REACHY_GROUNDSTATION_MODELS_DIR"'
            ),
        ),
    ),
    Check(
        identifier=CONFIGURATION_EFFECTIVE,
        description="The configuration in force matches what was declared",
        requires=(Requirement.DAEMON, Requirement.INTENT),
        probe=probes.configuration_matches_intent,
        remediation=Remediation(
            explanation=(
                "The robot is not running the configuration that was "
                "declared. Apply the declaration; preview it first to see "
                "what changes."
            ),
            command="reachyctl config apply",
        ),
    ),
    Check(
        identifier=HOME_ASSISTANT_IDENTITY,
        description="The satellite announces the Home Assistant identity declared",
        requires=(Requirement.DAEMON, Requirement.INTENT),
        probe=probes.home_assistant_identity,
        remediation=Remediation(
            explanation=(
                "The satellite announces an identity other than the declared "
                "one, so Home Assistant sees a second device and the entity "
                "history stays attached to the first. Apply the declared "
                "configuration to restore it. Whether Home Assistant's own "
                "device registry already holds a stale entry is a manual "
                "check, in its device list."
            ),
            command="reachyctl config apply",
        ),
    ),
)


def identifiers() -> tuple[str, ...]:
    """List every registered check's identifier, in chain order.

    Returns:
        The identifiers.
    """
    return tuple(check.identifier for check in CHECKS)


def check_by_identifier(identifier: str, checks: tuple[Check, ...] = CHECKS) -> Check:
    """Look a check up by name.

    Args:
        identifier: The check's identifier.
        checks: The registry to search. Defaults to the real one.

    Returns:
        The declaration.

    Raises:
        KeyError: If nothing is registered under that identifier. The message
            lists what is, because the likeliest cause is a runbook quoting a
            name that was renamed.
    """
    for check in checks:
        if check.identifier == identifier:
            return check
    known = ", ".join(check.identifier for check in checks)
    message = f"no check named {identifier!r} is registered; known: {known}"
    raise KeyError(message)
