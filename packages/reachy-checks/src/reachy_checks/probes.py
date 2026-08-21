"""The nine questions, each asked of a port and answered as a finding.

Every probe here is pure with respect to the world: it reaches nothing except
through the ports it is handed, which is what lets each one be exercised in
both the state where it passes and the state where it fails without a robot, a
groundstation or a file. A diagnosis tool exercised only against a healthy
system is exercised only in the case nobody runs it in.

**No probe reports a configuration value.** The effective-configuration check
compares by key and names the keys that differ; it never says what either side
holds. Settings are exactly where a credential ends up, and reachyctl REQ-059
is not satisfied by a rule that holds until somebody puts a token in one. The
announced Home Assistant identity is the one value reported verbatim, because
it is a device name whose whole purpose is to be recognisable and it is what
the operator has to compare.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from reachy_checks.outcomes import Finding

if TYPE_CHECKING:
    from reachy_checks.context import CheckContext

__all__ = [
    "application_installed",
    "application_running",
    "configuration_matches_intent",
    "daemon_reachable",
    "groundstation_capabilities",
    "groundstation_round_trip",
    "groundstation_session",
    "home_assistant_identity",
    "model_files",
]


async def daemon_reachable(context: CheckContext) -> Finding:
    """Ask whether the robot's daemon is there and answering.

    Args:
        context: What the run was given.

    Returns:
        What was found.
    """
    info = await context.require_daemon().ping()
    if not info.responding:
        return Finding.failed(
            f"the robot daemon did not answer: {info.complaint}"
            if info.complaint
            else "the robot daemon did not answer",
        )
    return Finding.passed(
        f"the robot daemon answered, running {info.version or 'an unnamed build'}",
        {"daemon_version": info.version or None},
    )


async def application_installed(context: CheckContext) -> Finding:
    """Ask whether the satellite is installed, and at what version.

    The version is reported on the passing path as well as the failing one.
    The predecessor's most expensive deployment failure was a package that
    installed successfully into an environment the running daemon was not
    using, which looks identical to success unless something says out loud
    which version is actually there.

    Args:
        context: What the run was given.

    Returns:
        What was found.
    """
    installed = await context.require_daemon().installed_application()
    if not installed.installed:
        return Finding.failed(
            f"the application is not installed on the robot: {installed.complaint}"
            if installed.complaint
            else "the application is not installed on the robot",
        )
    return Finding.passed(
        f"the application is installed at version "
        f"{installed.version or 'an unnamed build'}",
        {"application_version": installed.version or None},
    )


async def application_running(context: CheckContext) -> Finding:
    """Ask whether the satellite is running.

    Args:
        context: What the run was given.

    Returns:
        What was found.
    """
    state = await context.require_daemon().application_state()
    if not state.running:
        return Finding.failed(
            f"the application is not running: {state.detail}"
            if state.detail
            else "the application is not running",
            {"application_state": state.detail or None},
        )
    return Finding.passed(
        f"the application is running{f' ({state.detail})' if state.detail else ''}",
        {"application_state": state.detail or None},
    )


async def groundstation_session(context: CheckContext) -> Finding:
    """Ask whether a session opens to the groundstation at all.

    Args:
        context: What the run was given.

    Returns:
        What was found.
    """
    report = await context.require_groundstation().inspect()
    detail: dict[str, object] = {
        "groundstation_endpoint": report.endpoint,
        "establishment_ms": report.establishment_ms,
    }
    if not report.established:
        return Finding.failed(
            f"no session at {report.endpoint}: {report.complaint}",
            detail,
        )
    return Finding.passed(
        f"a session was established at {report.endpoint}",
        detail,
    )


async def groundstation_capabilities(context: CheckContext) -> Finding:
    """Ask what the two sides agreed the session can carry.

    A capability offered and not agreed is an ordinary outcome — two components
    upgrade at different times — so the check is against agreeing to *nothing*,
    which is a session that would never answer a frame.

    Without a session there was no negotiation to judge, and this reports
    itself skipped rather than failed. The session check has already named that
    fault; failing here too would put a second red line under one problem and,
    worse, print a remediation about capability versions to an operator whose
    groundstation is simply not running.

    Args:
        context: What the run was given.

    Returns:
        What was found.
    """
    report = await context.require_groundstation().inspect()
    detail: dict[str, object] = {
        "offered": report.offered,
        "agreed": report.agreed,
    }
    if not report.established:
        return Finding.skipped(
            "no session was established, so there was nothing to negotiate; "
            "see the session check for why",
            detail,
        )
    if not report.agreed:
        return Finding.failed(
            f"the groundstation agreed to none of the capabilities offered "
            f"({', '.join(report.offered) or 'none'}), so nothing would answer "
            f"a frame",
            detail,
        )
    return Finding.passed(
        f"negotiated {', '.join(report.agreed)}",
        detail,
    )


async def groundstation_round_trip(context: CheckContext) -> Finding:
    """Measure how long one frame takes to go out and come back.

    Reported whether or not anything else is wrong, and that is the point. The
    link is the component most likely to be the real problem and the least
    likely to be suspected, so a run where every check passes still ends with a
    number an operator can compare against the last one.

    There is deliberately no threshold. The robot is reached over a WLAN
    measured at 100-170 ms idle with 700 ms spikes, so any line drawn here
    would either be crossed constantly or never — the measurement is the
    result, and what counts as too slow is the benchmark suite's question.

    It fails only when there was something to measure and the measurement did
    not arrive. No session, or a session that agreed to nothing, means there
    was never anything to time — the checks above have already named that — so
    this reports itself skipped, and the one failure it does report keeps a
    remediation that matches it.

    Args:
        context: What the run was given.

    Returns:
        What was found.
    """
    report = await context.require_groundstation().inspect()
    detail: dict[str, object] = {
        "round_trip_ms": report.round_trip_ms,
        "establishment_ms": report.establishment_ms,
    }
    if not report.established:
        return Finding.skipped(
            "no session was established, so there was nothing to measure; "
            "see the session check for why",
            detail,
        )
    if not report.agreed:
        return Finding.skipped(
            "no capability was agreed, so nothing would have answered a frame; "
            "see the capability check",
            detail,
        )
    if report.round_trip_ms is None:
        return Finding.failed(
            f"the session is up but no result came back to time: "
            f"{report.result_complaint}"
            if report.result_complaint
            else "the session is up but no result came back to time",
            detail,
        )
    return Finding.passed(
        f"one frame went out and came back in {report.round_trip_ms:.1f} ms",
        detail,
    )


async def model_files(context: CheckContext) -> Finding:
    """Verify that every pinned model file is present and unaltered.

    The digests are the groundstation's registry and the hashing is its store.
    Re-deriving either here would create a second opinion about which weights
    are the right ones, and the two would be free to drift until a capability
    warmed up against something nobody reviewed.

    A machine with no registry to judge against is skipped rather than failed.
    The registry is an optional dependency, and a machine that does not carry
    it — the control machine a provisioning verification runs from, most
    obviously — has an absent prerequisite rather than a broken installation.
    Failing there would make every such run red, which is how an operator
    learns to stop reading the output. A machine that *does* have the registry
    and a file that is missing or does not match it has a real fault, and that
    still fails.

    Args:
        context: What the run was given.

    Returns:
        What was found.
    """
    report = context.require_models().inspect()
    detail: dict[str, object] = {
        "models_directory": report.directory,
        "models_verified": report.verified,
        "models_problems": report.problems,
    }
    if report.unavailable:
        return Finding.skipped(report.unavailable, detail)
    if report.problems:
        return Finding.failed(
            f"{len(report.problems)} model file problem(s) in {report.directory}: "
            f"{'; '.join(report.problems)}",
            detail,
        )
    if not report.verified:
        return Finding.failed(
            f"no model is registered, so {report.directory} was not checked "
            f"against anything",
            detail,
        )
    return Finding.passed(
        f"{len(report.verified)} model file(s) present and matching their "
        f"pinned digests: {', '.join(report.verified)}",
        detail,
    )


async def configuration_matches_intent(context: CheckContext) -> Finding:
    """Compare what is in force on the robot against what was declared.

    Only the keys the intent declares are compared. A setting present on the
    robot and absent from the declaration is reported as unmanaged rather than
    as a failure: provisioning owns the region it declares, and `doctor` is
    handed whatever subset the operator wanted asserted.

    Args:
        context: What the run was given.

    Returns:
        What was found. Key names only — never a value, on either side.
    """
    declared = context.require_intent().configuration
    if not declared:
        return Finding.skipped("the declared intent names no configuration to compare")
    effective = await context.require_daemon().effective_configuration()
    differing = tuple(
        name for name, value in declared.items() if effective.get(name) != value
    )
    unmanaged = tuple(sorted(set(effective) - set(declared)))
    detail: dict[str, object] = {
        "configuration_declared": len(declared),
        "configuration_differing": differing,
        "configuration_unmanaged": unmanaged,
    }
    if differing:
        return Finding.failed(
            f"{len(differing)} of {len(declared)} declared setting(s) are not "
            f"what is in force on the robot: {', '.join(differing)}",
            detail,
        )
    return Finding.passed(
        f"all {len(declared)} declared setting(s) are in force"
        + (f", and {len(unmanaged)} other(s) are unmanaged" if unmanaged else ""),
        detail,
    )


async def home_assistant_identity(context: CheckContext) -> Finding:
    """Compare the identity the satellite announces against the declared one.

    ha-satellite REQ-040 makes the announced identity configuration precisely
    so that repackaging the application does not silently change it. When it
    does change, Home Assistant registers a new device and the entity history
    attached to the old one is detached — which is invisible until somebody
    opens a dashboard weeks later.

    What is compared is announced against declared. Whether Home Assistant's
    own device registry agrees is a separate question, and answering it would
    mean this tool holding Home Assistant credentials it otherwise has no use
    for; see the change document for that decision.

    Args:
        context: What the run was given.

    Returns:
        What was found.
    """
    declared = context.require_intent().announced_identity
    if declared is None:
        return Finding.skipped(
            "the declared intent names no announced Home Assistant identity",
        )
    announced = await context.require_daemon().announced_identity()
    detail: dict[str, object] = {
        "identity_announced": announced or None,
        "identity_declared": declared,
    }
    if not announced:
        return Finding.failed(
            f"the satellite announces no identity, but {declared!r} is declared",
            detail,
        )
    if announced != declared:
        return Finding.failed(
            f"the satellite announces {announced!r}, but {declared!r} is "
            f"declared; Home Assistant will treat this as a different device "
            f"and the entity history stays attached to the old one",
            detail,
        )
    return Finding.passed(
        f"the satellite announces {announced!r}, as declared",
        detail,
    )
