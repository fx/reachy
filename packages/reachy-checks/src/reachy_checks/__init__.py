"""The one definition of what a healthy Reachy Mini installation is.

reachyctl REQ-056 requires the checks `reachyctl doctor` performs and the ones
the provisioning verification performs to be defined once and used by both.
This package is that definition, and it is a workspace member rather than a
module inside the CLI for one reason: an Ansible play runs on a control machine
that may have no CLI installed, so a registry living inside `reachyctl` would
force provisioning either to install it or to write the checks a second time.
Two independently written notions of healthy drift, and the drift arrives as a
robot that provisioning calls fine and diagnosis calls broken.

What a consumer needs:

- `CHECKS` — every check, declared as data, in the order the chain runs.
- `Check` — identifier, description, what it needs, the probe, the remediation.
- `CheckContext` — what a run was given, and why anything absent is absent.
- `run_checks` — runs all of them independently and collects every result.
- `CheckRun` — the results, with `ok`, the counts, and the line naming the
  first broken link.
- `SessionLink` and `GroundstationModelFiles` — the two adapters that reach the
  world. The robot's own is change 0009's; until it lands, the checks that need
  it report themselves skipped rather than passing on no evidence.

Nothing here renders anything, reads the environment, or decides what a process
exits with. Those belong to the consumer: `reachyctl` settled them in
`reachyctl.output` and `reachyctl.exits`, and an Ansible play settles them
differently.
"""

from __future__ import annotations

from reachy_checks.context import CheckContext, MissingResourceError, Requirement
from reachy_checks.files import REGISTRY_MISSING, GroundstationModelFiles
from reachy_checks.link import PROBE_FRAME, SessionLink
from reachy_checks.outcomes import (
    CheckResult,
    CheckRun,
    Finding,
    Outcome,
    Remediation,
    counts_of,
)
from reachy_checks.ports import (
    ApplicationState,
    DaemonInfo,
    GroundstationLink,
    InstalledApplication,
    Intent,
    LinkReport,
    ModelFileReport,
    ModelFiles,
    RobotDaemon,
)
from reachy_checks.registry import (
    APPLICATION_INSTALLED,
    APPLICATION_RUNNING,
    CHECKS,
    CONFIGURATION_EFFECTIVE,
    DAEMON_REACHABLE,
    GROUNDSTATION_CAPABILITIES,
    GROUNDSTATION_ROUND_TRIP,
    GROUNDSTATION_SESSION,
    HOME_ASSISTANT_IDENTITY,
    MODEL_FILES,
    Check,
    Probe,
    check_by_identifier,
    identifiers,
)
from reachy_checks.runner import run_check, run_checks

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
    "PROBE_FRAME",
    "REGISTRY_MISSING",
    "ApplicationState",
    "Check",
    "CheckContext",
    "CheckResult",
    "CheckRun",
    "DaemonInfo",
    "Finding",
    "GroundstationLink",
    "GroundstationModelFiles",
    "InstalledApplication",
    "Intent",
    "LinkReport",
    "MissingResourceError",
    "ModelFileReport",
    "ModelFiles",
    "Outcome",
    "Probe",
    "Remediation",
    "Requirement",
    "RobotDaemon",
    "SessionLink",
    "check_by_identifier",
    "counts_of",
    "identifiers",
    "run_check",
    "run_checks",
]
