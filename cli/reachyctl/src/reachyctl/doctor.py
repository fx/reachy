"""`doctor`: walk the chain from this machine to a working robot, link by link.

The command owns three things and delegates everything else. It resolves what
the operator gave it into the resources a check run needs, it runs the shared
registry in `reachy_checks`, and it shapes what came back into the one `Report`
every rendering is built from. What "healthy" means is not decided here — that
is the registry's, deliberately, because the provisioning verification role
asserts the same conditions from the same declarations and reachyctl REQ-056 is
the requirement that they cannot disagree.

**A failed check is a diagnosis, not an error.** `probe` exits `UNREACHABLE`
when the groundstation is not there, because a probe that could not connect has
learned nothing. `doctor` exits `FAILURE` for the same groundstation, because
learning that it is not there is exactly what it was asked to find out. The
statuses that still mean "this run reported nothing" are the ones about the
invocation itself: an address that is not a session URL, an unreadable
credential file, an intent document that is not one.

**Skipped does not fail the run.** An operator with no groundstation configured
is not in an error state. The counts are in the structured output, so a monitor
that wants a complete diagnosis rather than a clean one asserts that nothing
was skipped; a person gets told in the summary that not everything was checked.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from reachy_checks import (
    CheckContext,
    CheckResult,
    CheckRun,
    GroundstationModelFiles,
    Intent,
    Requirement,
    SessionLink,
    counts_of,
    run_checks,
)
from reachy_session_client import open_websocket, redact_url
from reachyctl.credentials import ENV_PREFIX
from reachyctl.errors import ConfigurationError
from reachyctl.output import Report

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from reachy_contracts import Capability
    from reachy_session_client import Credential, TransportFactory
    from reachyctl.exits import ExitCode
    from reachyctl.output import Reporter

__all__ = [
    "INTENT_VARIABLE",
    "MODELS_DIR_VARIABLE",
    "NO_ROBOT_YET",
    "DoctorPlan",
    "execute",
    "load_intent",
    "report_for",
    "run_doctor",
]

# Where the declared intent is, when it is not given on the command line.
INTENT_VARIABLE: Final = f"{ENV_PREFIX}INTENT_FILE"

# Where the model files are. Deliberately the groundstation's own variable
# rather than a second one under this tool's prefix: the files belong to that
# service's artifact, and a `doctor` run on the host serving them should find
# them without being told twice.
MODELS_DIR_VARIABLE: Final = "REACHY_GROUNDSTATION_MODELS_DIR"

# Why the robot-side checks cannot run yet, said out loud. Change 0009 brings
# the robot's remote-access and daemon interfaces; until it lands, nothing in
# this tool can open a connection to a robot, and a skip line that did not say
# so would read as "you have not configured this" to an operator who has.
NO_ROBOT_YET: Final = (
    "reachyctl cannot open a connection to the robot yet: robot access arrives "
    "with the deploy, config and app commands"
)

_INTENT_KEYS: Final = frozenset({"configuration", "announced_identity"})


@dataclass(frozen=True, slots=True, kw_only=True)
class DoctorPlan:
    """What one `doctor` run was asked to look at.

    Attributes:
        url: The groundstation's session endpoint, or `None` when none is
            configured and the groundstation checks are to be skipped.
        capabilities: What to offer during negotiation.
        models_directory: Where the model files are, or `None`.
        intent: What the robot is supposed to be, or `None`.
        timeout: One budget for the whole groundstation exchange — opening the
            session, sending the frame, and waiting for the result that answers
            it. Not a per-step timeout: a bound that restarted at each step
            would describe a part of the run rather than the run, and every
            step is one a wedged service can stop dead.
    """

    url: str | None
    capabilities: tuple[Capability, ...]
    models_directory: Path | None = None
    intent: Intent | None = None
    timeout: float = 10.0


def _read(path: Path) -> str:
    """Read a declared-intent document.

    Nothing but the read, so that what a failure means stays `load_intent`'s to
    decide for whatever reader it was given.

    Args:
        path: Where the document is.

    Returns:
        Its contents.

    Raises:
        OSError: If the file cannot be read.
    """
    return path.read_text(encoding="utf-8")


def load_intent(
    path: Path,
    read: Callable[[Path], str] = _read,
) -> Intent:
    """Read what the robot is supposed to be from a declaration.

    The document is deliberately small — the settings that are supposed to be
    in force, and the identity the satellite is supposed to announce — because
    the authoritative configuration surface is `reachyctl config`, which
    arrives in change 0009, and provisioning holds the declaration this is a
    copy of. Anything richer here would be a second schema to reconcile.

    Args:
        path: Where the document is.
        read: How to read it. Injected so the parsing rules are exercised
            without performing any input.

    Returns:
        The declared intent.

    Raises:
        ConfigurationError: If the file cannot be read, is not JSON, is not an
            object, carries a key this does not understand, or holds a
            configuration that is not a mapping of strings to strings. None of
            the messages quotes a value it read: a setting is exactly where a
            credential ends up.
    """
    try:
        content = read(path)
    except OSError as error:
        reason = error.strerror or type(error).__name__
        message = f"the intent document {path} could not be read: {reason}"
        raise ConfigurationError(message) from error
    try:
        document = json.loads(content)
    except ValueError as error:
        message = (
            f"the intent document {path} is not JSON: {type(error).__name__} "
            f"at position {getattr(error, 'pos', 'unknown')}"
        )
        raise ConfigurationError(message) from error
    if not isinstance(document, dict):
        message = (
            f"the intent document {path} is a "
            f"{type(document).__name__}; it must be an object with the keys "
            f"{sorted(_INTENT_KEYS)}"
        )
        raise ConfigurationError(message)
    unknown = sorted(set(document) - _INTENT_KEYS)
    if unknown:
        message = (
            f"the intent document {path} carries {unknown}, which this "
            f"command does not understand; it reads {sorted(_INTENT_KEYS)}"
        )
        raise ConfigurationError(message)
    return Intent(
        configuration=_configuration(document.get("configuration", {}), path),
        announced_identity=_identity(document.get("announced_identity"), path),
    )


def _configuration(value: object, path: Path) -> Mapping[str, str]:
    """Read the declared settings out of an intent document.

    Args:
        value: What the document held under `configuration`.
        path: Where the document is, for the message.

    Returns:
        The settings by name.

    Raises:
        ConfigurationError: If it is not a mapping of strings to strings. Only
            the offending key is named, never what it holds.
    """
    if not isinstance(value, dict):
        message = (
            f"the intent document {path} declares a configuration that is a "
            f"{type(value).__name__}; it must be an object of setting names to "
            f"values"
        )
        raise ConfigurationError(message)
    settings: dict[str, str] = {}
    for name, setting in value.items():
        if not isinstance(name, str) or not isinstance(setting, str):
            message = (
                f"the intent document {path} declares a setting that is not a "
                f"string; every setting name and value must be one"
            )
            raise ConfigurationError(message)
        settings[name] = setting
    return settings


def _identity(value: object, path: Path) -> str | None:
    """Read the declared announced identity out of an intent document.

    Args:
        value: What the document held under `announced_identity`.
        path: Where the document is, for the message.

    Returns:
        The identity, or `None` when the document declares none.

    Raises:
        ConfigurationError: If it is present and is not a non-empty string.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        message = (
            f"the intent document {path} declares an announced identity that "
            f"is not a non-empty string"
        )
        raise ConfigurationError(message)
    return value


def _context(plan: DoctorPlan, link: SessionLink | None) -> CheckContext:
    """Assemble what the check run has to work with.

    Args:
        plan: What the run was asked to look at.
        link: The groundstation session, when one is configured.

    Returns:
        The context, carrying a reason for every resource that is absent so a
        skipped check says why rather than leaving a blank.
    """
    unavailable: dict[Requirement, str] = {Requirement.DAEMON: NO_ROBOT_YET}
    if link is None:
        # Two ways to have no link, and they are different mistakes. Saying
        # "configure an address" to somebody who configured one and no
        # credential would send them to look at the thing that is already right.
        unavailable[Requirement.GROUNDSTATION] = (
            "no groundstation is configured: pass --url or set "
            "REACHYCTL_GROUNDSTATION_URL"
            if plan.url is None
            else "a groundstation is configured but no credential was resolved for it"
        )
    if plan.models_directory is None:
        unavailable[Requirement.MODELS] = (
            "no model directory to check: pass --models-dir, or run this where "
            "the groundstation's artifact is"
        )
    if plan.intent is None:
        unavailable[Requirement.INTENT] = (
            "nothing declares what this robot is supposed to be: pass --intent "
            "with a declaration"
        )
    return CheckContext(
        groundstation=link,
        models=(
            None
            if plan.models_directory is None
            else GroundstationModelFiles(plan.models_directory)
        ),
        intent=plan.intent,
        unavailable=unavailable,
    )


async def run_doctor(
    plan: DoctorPlan,
    credential: Credential | None,
    reporter: Reporter,
    open_transport: TransportFactory = open_websocket,
) -> CheckRun:
    """Run every check and collect what they all found.

    Args:
        plan: What the run was asked to look at.
        credential: What to present to the groundstation, or `None` when there
            is no groundstation to present it to.
        reporter: Where per-check progress goes while the run is happening.
        open_transport: How to open the connection. Injected only so the
            integration test can watch which connections were opened; the
            transport itself is always the real one.

    Returns:
        Every result, in the order the chain runs.
    """
    link: SessionLink | None = None
    if plan.url is not None and credential is not None:
        # The staleness window is left at the client's own default and is not
        # an option here. It governs `latest()` and `stale`, and this command
        # reads neither — it takes the first result off `results()` within the
        # run's budget. A `--staleness` that changed nothing observable would
        # be exactly the silently-inert setting this command exists to catch.
        link = SessionLink(
            url=plan.url,
            credential=credential,
            capabilities=plan.capabilities,
            timeout=plan.timeout,
            open_transport=open_transport,
        )

    def announce(result: CheckResult) -> None:
        """Say what a check found, as soon as it has found it.

        A run waits on a network, so a verbose run reports each link as it is
        walked rather than printing the whole table at the end. It goes through
        the reporter, which scrubs it like every other string.

        Args:
            result: What the check found.
        """
        reporter.detail(
            f"{result.identifier}: {result.outcome.value} — {result.summary}",
        )

    try:
        return await run_checks(_context(plan, link), observer=announce)
    finally:
        if link is not None:
            await link.aclose()


#:= docs/specs/reachyctl/index.md#req-058-output-is-machine-readable-on-request
#:% Every command that reports results MUST offer a structured output format
#:% suitable for consumption by another program.
def report_for(run: CheckRun, plan: DoctorPlan) -> Report:
    """Shape what the run found into the thing every rendering is built from.

    One report, and the command never learns which format was asked for, which
    is what keeps the structured output and the human one carrying the same
    fields. The remediation is split across two columns on purpose: a script
    reading the structured output gets the command on its own rather than
    having to cut it out of a sentence.

    Args:
        run: What the checks found.
        plan: What the run was asked to look at.

    Returns:
        The report to emit.
    """
    tally = counts_of(run.results)
    broken = run.first_failure
    data: dict[str, object] = {
        # Rendered rather than repeated: the address has been validated and
        # carries nothing to hide, so this changes no output an operator sees
        # and makes the report safe by construction rather than by the
        # validator having run first.
        "groundstation": None if plan.url is None else redact_url(plan.url),
        "checks": len(run.results),
        "passed": tally["passed"],
        "failed": tally["failed"],
        "skipped": tally["skipped"],
        "first_failure": None if broken is None else broken.identifier,
        "round_trip_ms": _round_trip(run),
        # Always present, empty in the ordinary case. A progress callback that
        # threw is a defect in this tool rather than a diagnosis of the robot,
        # and it is reported rather than swallowed so that output which stopped
        # partway is not left as a puzzle.
        "observer_failures": run.observer_failures,
    }
    return Report(
        command="doctor",
        ok=run.ok,
        summary=run.summary(),
        data=data,
        columns=("check", "status", "detail", "remediation", "command"),
        rows=tuple(
            {
                "check": result.identifier,
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
        ),
    )


def _round_trip(run: CheckRun) -> float | None:
    """Pull the measured round trip out of whichever check measured it.

    Promoted out of the row it was found in and into the run's scalar fields,
    because it is the number an operator compares against the last run and the
    one a monitor graphs, and neither should have to know which check produced
    it.

    Args:
        run: What the checks found.

    Returns:
        The round trip in milliseconds, or `None` when nothing measured one.
    """
    for result in run.results:
        measured = result.detail.get("round_trip_ms")
        if isinstance(measured, float):
            return measured
    return None


#:= docs/specs/reachyctl/index.md#req-054-diagnosis-covers-the-whole-chain-and-names-the-failing-link
#:% The doctor command MUST report the status of every link between the operator and
#:% a working robot individually, and MUST identify which link is broken when one
#:% is.
def execute(
    plan: DoctorPlan,
    credential: Credential | None,
    reporter: Reporter,
    open_transport: TransportFactory = open_websocket,
) -> ExitCode:
    """Diagnose the chain and report it, link by link.

    Args:
        plan: What the run was asked to look at.
        credential: What to present to the groundstation, when one is
            configured.
        reporter: Where everything is written.
        open_transport: How to open the connection.

    Returns:
        The exit status: `OK` when nothing failed, `FAILURE` when something
        did. A check that failed is a diagnosis and not an error, so the
        statuses that mean "nothing was learned" are raised before this by the
        command surface.
    """
    run = asyncio.run(run_doctor(plan, credential, reporter, open_transport))
    return reporter.emit(report_for(run, plan))
