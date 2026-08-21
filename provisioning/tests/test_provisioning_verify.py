"""The verification role's half of REQ-066, exercised without a robot.

Every case here is a robot in some state, expressed as the evidence the role's
gathering tasks would have collected, and what the shared registry then says
about it. No robot, no container and no network: the seam the checks are written
against is a set of answers, so a run against a broken robot is as easy to
exercise as a run against a working one — which matters, because the working case
is the one nobody needs the verification for.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final

import pytest

from reachy_verify import (
    GATHERED_DAEMON_ABSENT,
    GROUNDSTATION_SKIPPED,
    MODELS_SKIPPED,
    check_run,
    daemon_from,
    parse_properties,
    split_environment,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

# RFC 5737 TEST-NET-1 — see the root AGENTS.md on what may enter a tracked file.
ENDPOINT: Final = "ws://192.0.2.10:8000/v1/session"

UNIT: Final = "reachy-mini-daemon.service"
APPLICATION: Final = "reachy-mini-ha-satellite"
DAEMON: Final = "reachy-mini"

IDENTITY: Final = "Reachy Mini Example"


def properties(
    *,
    load: str = "loaded",
    active: str = "active",
    substate: str = "running",
    environment: str = "",
) -> str:
    """Render what `systemctl show` would have printed.

    Args:
        load: The unit's `LoadState`.
        active: Its `ActiveState`.
        substate: Its `SubState`.
        environment: The one line systemd prints for the whole environment.

    Returns:
        The command's standard output.
    """
    return (
        f"LoadState={load}\n"
        f"ActiveState={active}\n"
        f"SubState={substate}\n"
        f"Environment={environment}\n"
    )


def evidence(
    *,
    unit_properties: str | None = None,
    versions: Mapping[str, str] | None = None,
    status: str | None = None,
    status_complaint: str = "",
) -> dict[str, Any]:
    """Assemble one gathering of evidence, with a healthy robot as the default.

    Args:
        unit_properties: What `systemctl show` printed.
        versions: What the daemon's interpreter answered, by distribution.
        status: The JSON the daemon's application control printed.
        status_complaint: Why it printed nothing, when it printed nothing.

    Returns:
        The record the role hands to the filter.
    """
    return {
        "unit": UNIT,
        "application": APPLICATION,
        "daemon_distribution": DAEMON,
        "properties": (
            properties(
                environment=(
                    f'"REACHY_GROUNDSTATION_URL={ENDPOINT}" '
                    f'"REACHY_HOME_ASSISTANT_IDENTITY={IDENTITY}"'
                ),
            )
            if unit_properties is None
            else unit_properties
        ),
        "versions": json.dumps(
            {DAEMON: "1.9.0", APPLICATION: "0.1.0"} if versions is None else versions,
        ),
        "status": '{"running": true, "detail": "running"}'
        if status is None
        else status,
        "status_complaint": status_complaint,
    }


def intent() -> dict[str, Any]:
    """State what the robot is supposed to be, matching the healthy default.

    Returns:
        The declaration the role passes alongside the evidence.
    """
    return {
        "configuration": {
            "REACHY_GROUNDSTATION_URL": ENDPOINT,
            "REACHY_HOME_ASSISTANT_IDENTITY": IDENTITY,
        },
        "announced_identity": IDENTITY,
    }


def outcome_of(run: Mapping[str, Any], identifier: str) -> str:
    """Pull one check's outcome out of a run.

    Args:
        run: What `check_run` produced.
        identifier: The check to look for.

    Returns:
        Its status.
    """
    results: list[Mapping[str, Any]] = list(run["results"])
    return next(str(row["status"]) for row in results if row["check"] == identifier)


def test_a_working_end_state_passes_every_check_it_can_perform() -> None:
    """And skips the two it deliberately does not, rather than passing them blind."""
    run = check_run(evidence(), intent())

    assert run["ok"]
    assert run["failures"] == []
    assert run["counts"]["failed"] == 0
    assert outcome_of(run, "daemon.reachable") == "passed"
    assert outcome_of(run, "application.installed") == "passed"
    assert outcome_of(run, "application.running") == "passed"
    assert outcome_of(run, "configuration.effective") == "passed"
    assert outcome_of(run, "home-assistant.identity") == "passed"


def test_configuration_that_applied_and_an_application_that_did_not_start_fails() -> (
    None
):
    """REQ-066's own scenario: every configuration step succeeded and the run still fails."""
    run = check_run(
        evidence(status='{"running": false, "detail": "exited with status 1"}'),
        intent(),
    )

    assert not run["ok"]
    assert run["failures"] == ["application.running"]
    assert outcome_of(run, "configuration.effective") == "passed"
    assert "application.running" in run["summary"]


def test_a_daemon_whose_unit_is_stopped_is_reported_as_the_first_broken_link() -> None:
    """The chain is walked in order, so the earliest failure is the one to fix first."""
    run = check_run(
        evidence(unit_properties=properties(active="inactive", substate="dead")),
        intent(),
    )

    assert not run["ok"]
    assert run["failures"][0] == "daemon.reachable"
    assert "inactive" in run["summary"]


def test_a_unit_that_is_not_installed_says_so_rather_than_saying_it_is_stopped() -> (
    None
):
    """Two different faults, and an operator sent to restart a missing unit is stuck."""
    run = check_run(
        evidence(unit_properties=properties(load="not-found", active="inactive")),
        intent(),
    )

    assert "not installed on this robot" in run["summary"]


def test_an_application_the_daemon_environment_does_not_hold_fails_installed() -> None:
    """The version is read through the interpreter the daemon runs, not a configured one."""
    run = check_run(evidence(versions={DAEMON: "1.9.0", APPLICATION: ""}), intent())

    assert outcome_of(run, "application.installed") == "failed"
    assert "application.installed" in run["failures"]


def test_a_control_that_could_not_be_run_is_not_read_as_a_stopped_application() -> None:
    """A stopped application is an answer; a control that did not run is the absence of one."""
    run = check_run(
        evidence(status="", status_complaint="exited 127: no such module"),
        intent(),
    )

    assert outcome_of(run, "application.running") == "failed"
    detail = next(
        str(row["detail"])
        for row in run["results"]
        if row["check"] == "application.running"
    )
    assert "the check itself failed" in detail
    assert "could not be run" in detail


def test_a_control_answering_with_something_unreadable_quotes_none_of_it() -> None:
    """The daemon's own output is where a setting's value would be."""
    run = check_run(evidence(status="REACHY_GROUNDSTATION_CREDENTIAL=leaked"), intent())

    assert outcome_of(run, "application.running") == "failed"
    assert "leaked" not in json.dumps(run)


def test_a_setting_that_is_declared_and_not_in_force_fails_by_name_only() -> None:
    """The silently-inert configuration this whole stack is written against."""
    run = check_run(
        evidence(
            unit_properties=properties(
                environment=f'"REACHY_HOME_ASSISTANT_IDENTITY={IDENTITY}"',
            ),
        ),
        intent(),
    )

    assert outcome_of(run, "configuration.effective") == "failed"
    assert ENDPOINT not in json.dumps(run)


def test_an_identity_other_than_the_declared_one_fails_and_says_which() -> None:
    """Home Assistant would register a second device and detach the entity history."""
    run = check_run(
        evidence(
            unit_properties=properties(
                environment=(
                    f'"REACHY_GROUNDSTATION_URL={ENDPOINT}" '
                    f'"REACHY_HOME_ASSISTANT_IDENTITY=Something Else"'
                ),
            ),
        ),
        intent(),
    )

    assert outcome_of(run, "home-assistant.identity") == "failed"
    assert "Something Else" in run["summary"] or any(
        "Something Else" in str(row["detail"]) for row in run["results"]
    )


def test_the_groundstation_and_the_models_are_skipped_with_the_reason() -> None:
    """Skipped is not failed: neither is something provisioning installs on the robot."""
    run = check_run(evidence(), intent())

    reasons = {str(row["check"]): str(row["detail"]) for row in run["results"]}
    assert reasons["groundstation.session"] == GROUNDSTATION_SKIPPED
    assert reasons["models.files"] == MODELS_SKIPPED
    assert run["counts"]["skipped"] >= 4


def test_a_run_with_no_evidence_skips_the_robot_rather_than_condemning_it() -> None:
    """A run that learned nothing has not learned that the robot is broken."""
    run = check_run(None, intent())

    assert run["ok"]
    assert outcome_of(run, "daemon.reachable") == "skipped"
    assert GATHERED_DAEMON_ABSENT in json.dumps(run)


def test_a_run_with_no_intent_skips_the_two_checks_that_compare_against_one() -> None:
    """Nothing declares what the robot is supposed to be, which is not a fault in it."""
    run = check_run(evidence(), None)

    assert run["ok"]
    assert outcome_of(run, "configuration.effective") == "skipped"
    assert outcome_of(run, "home-assistant.identity") == "skipped"


def test_an_intent_declaring_no_identity_skips_that_check_alone() -> None:
    """An operator who declares no identity has not declared a wrong one."""
    run = check_run(
        evidence(),
        {"configuration": {"REACHY_GROUNDSTATION_URL": ENDPOINT}},
    )

    assert outcome_of(run, "configuration.effective") == "passed"
    assert outcome_of(run, "home-assistant.identity") == "skipped"


def test_the_properties_of_a_unit_that_is_not_installed_read_as_empty() -> None:
    """`systemctl show` answers rather than failing, so an empty answer means what it says."""
    assert parse_properties("LoadState=\nActiveState=\n") == {
        "LoadState": "",
        "ActiveState": "",
    }
    assert parse_properties("") == {}
    assert parse_properties("no separator here") == {}


def test_the_environment_line_is_split_the_way_a_shell_would() -> None:
    """It is the closest available parse; `systemctl show` offers no structured output."""
    assert split_environment('"A=1" "B=a value" "C=has=equals"') == {
        "A": "1",
        "B": "a value",
        "C": "has=equals",
    }
    assert split_environment("") == {}


@pytest.mark.asyncio
async def test_the_gathered_robot_answers_every_question_the_registry_asks() -> None:
    """The evidence was read once, in tasks whose output is in the run's log."""
    robot = daemon_from(evidence())

    assert (await robot.ping()).responding
    assert (await robot.installed_application()).version == "0.1.0"
    assert (await robot.application_state()).running
    assert (await robot.announced_identity()) == IDENTITY
    assert dict(await robot.effective_configuration())["REACHY_GROUNDSTATION_URL"] == (
        ENDPOINT
    )


@pytest.mark.asyncio
async def test_an_unreadable_version_answer_reads_as_nothing_installed() -> None:
    """Which the installed-application check already reports, with a remediation."""
    gathered = evidence()
    gathered["versions"] = "not json at all"

    robot = daemon_from(gathered)

    assert not (await robot.installed_application()).installed
