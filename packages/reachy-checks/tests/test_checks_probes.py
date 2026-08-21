"""Every check, in the state where it passes and the state where it fails.

A diagnosis tool exercised only against a healthy system is exercised only in
the case nobody runs it in, so each probe below is asked twice — once of a world
that is in order and once of the world it exists to describe. The third state,
skipped, belongs partly here and partly to the runner: a check whose declared
requirements are absent is skipped by the runner and covered in
`test_checks_runner.py`, and a check that has everything it declared and still
finds nothing to compare against reports that itself, which is covered here.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

from typing import Final

import pytest
from checks_support import (
    ENDPOINT,
    FakeDaemon,
    FakeLink,
    FakeModelFiles,
    healthy_context,
    healthy_daemon,
)

from reachy_checks import (
    ApplicationState,
    CheckContext,
    DaemonInfo,
    InstalledApplication,
    Intent,
    LinkReport,
    MissingResourceError,
    ModelFileReport,
    Outcome,
    probes,
)

IDENTITY: Final = "reachy-mini-example"

BROKEN_LINK: Final = LinkReport(
    endpoint=ENDPOINT,
    established=False,
    offered=("face", "gesture"),
    complaint="ConnectionFailedError: the connection was refused",
)


@pytest.mark.asyncio
async def test_daemon_reachable_passes_when_the_daemon_answers() -> None:
    """A daemon that answers passes and its version is reported."""
    finding = await probes.daemon_reachable(healthy_context())

    assert finding.outcome is Outcome.PASSED
    assert "daemon 1.2.3" in finding.summary
    assert finding.detail["daemon_version"] == "daemon 1.2.3"


@pytest.mark.asyncio
async def test_daemon_reachable_fails_and_carries_the_reason() -> None:
    """A daemon that does not answer fails, saying what it said instead."""
    context = CheckContext(
        daemon=FakeDaemon(
            info=DaemonInfo(
                responding=False, complaint="the host refused the connection"
            ),
        ),
    )

    finding = await probes.daemon_reachable(context)

    assert finding.outcome is Outcome.FAILED
    assert "the host refused the connection" in finding.summary


@pytest.mark.asyncio
async def test_daemon_reachable_fails_without_a_reason_when_none_was_given() -> None:
    """A silent failure still reads as a sentence rather than a dangling colon."""
    context = CheckContext(daemon=FakeDaemon(info=DaemonInfo(responding=False)))

    finding = await probes.daemon_reachable(context)

    assert finding.summary == "the robot daemon did not answer"


@pytest.mark.asyncio
async def test_application_installed_reports_the_version_it_found() -> None:
    """The installed version is reported when the check passes, not only when it fails."""
    finding = await probes.application_installed(healthy_context())

    assert finding.outcome is Outcome.PASSED
    assert finding.detail["application_version"] == "0.1.0"


@pytest.mark.asyncio
async def test_application_installed_fails_when_nothing_is_installed() -> None:
    """Nothing installed is a failure carrying whatever the daemon said."""
    context = CheckContext(
        daemon=FakeDaemon(
            installed=InstalledApplication(
                installed=False,
                complaint="no such package in the application environment",
            ),
        ),
    )

    finding = await probes.application_installed(context)

    assert finding.outcome is Outcome.FAILED
    assert "no such package" in finding.summary


@pytest.mark.asyncio
async def test_application_installed_fails_plainly_without_a_complaint() -> None:
    """A bare negative still reads as a sentence."""
    context = CheckContext(
        daemon=FakeDaemon(installed=InstalledApplication(installed=False)),
    )

    finding = await probes.application_installed(context)

    assert finding.summary == "the application is not installed on the robot"


@pytest.mark.asyncio
async def test_application_installed_names_an_unnamed_build() -> None:
    """An installed package with no version reads as a build, not as a blank."""
    context = CheckContext(
        daemon=FakeDaemon(installed=InstalledApplication(installed=True)),
    )

    finding = await probes.application_installed(context)

    assert finding.outcome is Outcome.PASSED
    assert "an unnamed build" in finding.summary
    assert finding.detail["application_version"] is None


@pytest.mark.asyncio
async def test_daemon_reachable_names_an_unnamed_daemon() -> None:
    """A responding daemon that reports no version still reads as a sentence."""
    context = CheckContext(daemon=FakeDaemon(info=DaemonInfo(responding=True)))

    finding = await probes.daemon_reachable(context)

    assert finding.outcome is Outcome.PASSED
    assert "an unnamed build" in finding.summary


@pytest.mark.asyncio
async def test_application_running_passes_with_the_state_the_daemon_reported() -> None:
    """A running application passes and carries the daemon's own wording."""
    finding = await probes.application_running(healthy_context())

    assert finding.outcome is Outcome.PASSED
    assert "active" in finding.summary


@pytest.mark.asyncio
async def test_application_running_fails_when_it_is_stopped() -> None:
    """A stopped application fails, which is the scenario REQ-055 is written about."""
    context = CheckContext(
        daemon=FakeDaemon(
            state=ApplicationState(running=False, detail="inactive (dead)"),
        ),
    )

    finding = await probes.application_running(context)

    assert finding.outcome is Outcome.FAILED
    assert "inactive (dead)" in finding.summary
    assert finding.detail["application_state"] == "inactive (dead)"


@pytest.mark.asyncio
async def test_application_running_fails_plainly_without_a_detail() -> None:
    """A stopped application the daemon says nothing about still reads plainly."""
    context = CheckContext(daemon=FakeDaemon(state=ApplicationState(running=False)))

    finding = await probes.application_running(context)

    assert finding.summary == "the application is not running"
    assert finding.detail["application_state"] is None


@pytest.mark.asyncio
async def test_application_running_passes_plainly_without_a_detail() -> None:
    """A running application the daemon says nothing about does not grow empty brackets."""
    context = CheckContext(daemon=FakeDaemon(state=ApplicationState(running=True)))

    finding = await probes.application_running(context)

    assert finding.summary == "the application is running"


@pytest.mark.asyncio
async def test_groundstation_session_passes_and_reports_the_endpoint() -> None:
    """A session that opened passes and says where."""
    finding = await probes.groundstation_session(healthy_context())

    assert finding.outcome is Outcome.PASSED
    assert finding.detail["groundstation_endpoint"] == ENDPOINT
    assert finding.detail["establishment_ms"] == 42.0


@pytest.mark.asyncio
async def test_groundstation_session_fails_naming_the_broken_link() -> None:
    """A groundstation that is not there fails with the client's own complaint."""
    context = CheckContext(groundstation=FakeLink(BROKEN_LINK))

    finding = await probes.groundstation_session(context)

    assert finding.outcome is Outcome.FAILED
    assert ENDPOINT in finding.summary
    assert "the connection was refused" in finding.summary


@pytest.mark.asyncio
async def test_groundstation_capabilities_reports_what_was_agreed() -> None:
    """A negotiated capability is reported, which is half of what an operator asked."""
    finding = await probes.groundstation_capabilities(healthy_context())

    assert finding.outcome is Outcome.PASSED
    assert finding.detail["agreed"] == ("face",)
    assert finding.detail["offered"] == ("face", "gesture")


@pytest.mark.asyncio
async def test_groundstation_capabilities_fails_when_nothing_was_agreed() -> None:
    """A session that agreed to nothing would never answer a frame."""
    context = CheckContext(
        groundstation=FakeLink(
            LinkReport(
                endpoint=ENDPOINT,
                established=True,
                offered=("face",),
                agreed=(),
                establishment_ms=10.0,
            ),
        ),
    )

    finding = await probes.groundstation_capabilities(context)

    assert finding.outcome is Outcome.FAILED
    assert "agreed to none" in finding.summary
    assert "face" in finding.summary


@pytest.mark.asyncio
async def test_groundstation_capabilities_is_skipped_when_no_session_opened() -> None:
    """One fault gets one red line, and no remediation about capability versions."""
    context = CheckContext(groundstation=FakeLink(BROKEN_LINK))

    finding = await probes.groundstation_capabilities(context)

    assert finding.outcome is Outcome.SKIPPED
    assert "nothing to negotiate" in finding.summary


@pytest.mark.asyncio
async def test_groundstation_capabilities_names_an_empty_offer() -> None:
    """Offering nothing and agreeing to nothing reads as a sentence, not a gap."""
    context = CheckContext(
        groundstation=FakeLink(
            LinkReport(endpoint=ENDPOINT, established=True, offered=(), agreed=()),
        ),
    )

    finding = await probes.groundstation_capabilities(context)

    assert "(none)" in finding.summary


@pytest.mark.asyncio
async def test_round_trip_is_measured_when_everything_is_healthy() -> None:
    """The measurement is reported on the passing path, which is the point of it."""
    finding = await probes.groundstation_round_trip(healthy_context())

    assert finding.outcome is Outcome.PASSED
    assert finding.detail["round_trip_ms"] == 17.5
    assert "17.5 ms" in finding.summary


@pytest.mark.asyncio
async def test_round_trip_is_skipped_when_the_session_never_opened() -> None:
    """There was never anything to time, and the session check has already said why."""
    context = CheckContext(groundstation=FakeLink(BROKEN_LINK))

    finding = await probes.groundstation_round_trip(context)

    assert finding.outcome is Outcome.SKIPPED
    assert "nothing to measure" in finding.summary


@pytest.mark.asyncio
async def test_round_trip_is_skipped_when_nothing_was_agreed() -> None:
    """A session that would never answer a frame is the capability check's finding."""
    context = CheckContext(
        groundstation=FakeLink(
            LinkReport(
                endpoint=ENDPOINT,
                established=True,
                offered=("face",),
                agreed=(),
                establishment_ms=12.0,
                result_complaint="no capability was agreed",
            ),
        ),
    )

    finding = await probes.groundstation_round_trip(context)

    assert finding.outcome is Outcome.SKIPPED
    assert "no capability was agreed" in finding.summary


@pytest.mark.asyncio
async def test_round_trip_fails_when_no_result_came_back() -> None:
    """A session that is up and answers nothing is the failure this check catches."""
    context = CheckContext(
        groundstation=FakeLink(
            LinkReport(
                endpoint=ENDPOINT,
                established=True,
                agreed=("face",),
                establishment_ms=30.0,
                result_complaint="no result came back within 10.0s",
            ),
        ),
    )

    finding = await probes.groundstation_round_trip(context)

    assert finding.outcome is Outcome.FAILED
    assert "no result came back within 10.0s" in finding.summary


@pytest.mark.asyncio
async def test_round_trip_fails_plainly_when_nothing_explains_the_silence() -> None:
    """The failure reads as a sentence even with no complaint to append."""
    context = CheckContext(
        groundstation=FakeLink(
            LinkReport(endpoint=ENDPOINT, established=True, agreed=("face",)),
        ),
    )

    finding = await probes.groundstation_round_trip(context)

    assert finding.summary == "the session is up but no result came back to time"


@pytest.mark.asyncio
async def test_model_files_passes_when_every_digest_matches() -> None:
    """Verified files pass and are named, so an operator can see which were checked."""
    finding = await probes.model_files(healthy_context())

    assert finding.outcome is Outcome.PASSED
    assert finding.detail["models_verified"] == ("face_detection_yunet",)


@pytest.mark.asyncio
async def test_model_files_fails_and_carries_every_problem() -> None:
    """One bad file does not hide the state of the others."""
    context = CheckContext(
        models=FakeModelFiles(
            ModelFileReport(
                directory="/opt/reachy/models",
                verified=("one",),
                problems=("two: absent", "three: hashes to something else"),
            ),
        ),
    )

    finding = await probes.model_files(context)

    assert finding.outcome is Outcome.FAILED
    assert "two: absent" in finding.summary
    assert "three: hashes to something else" in finding.summary


@pytest.mark.asyncio
async def test_model_files_fails_when_nothing_was_verified_against() -> None:
    """A directory checked against an empty registry has not been checked."""
    context = CheckContext(
        models=FakeModelFiles(ModelFileReport(directory="/opt/reachy/models")),
    )

    finding = await probes.model_files(context)

    assert finding.outcome is Outcome.FAILED
    assert "no model is registered" in finding.summary


@pytest.mark.asyncio
async def test_configuration_passes_when_every_declared_setting_is_in_force() -> None:
    """Matching configuration passes and counts what it compared."""
    finding = await probes.configuration_matches_intent(healthy_context())

    assert finding.outcome is Outcome.PASSED
    assert finding.detail["configuration_declared"] == 2
    assert finding.detail["configuration_differing"] == ()


@pytest.mark.asyncio
async def test_configuration_fails_naming_only_the_keys_that_differ() -> None:
    """A drifted setting fails, and neither value appears anywhere in the finding."""
    context = CheckContext(
        daemon=FakeDaemon(configuration={"token": "the-value-on-the-robot"}),
        intent=Intent(configuration={"token": "the-value-that-was-declared"}),
    )

    finding = await probes.configuration_matches_intent(context)

    assert finding.outcome is Outcome.FAILED
    assert finding.detail["configuration_differing"] == ("token",)
    rendered = f"{finding.summary} {finding.detail}"
    assert "the-value-on-the-robot" not in rendered
    assert "the-value-that-was-declared" not in rendered


@pytest.mark.asyncio
async def test_configuration_reports_unmanaged_settings_without_failing() -> None:
    """A setting the declaration says nothing about is not a fault."""
    context = CheckContext(
        daemon=FakeDaemon(configuration={"a": "1", "b": "2"}),
        intent=Intent(configuration={"a": "1"}),
    )

    finding = await probes.configuration_matches_intent(context)

    assert finding.outcome is Outcome.PASSED
    assert finding.detail["configuration_unmanaged"] == ("b",)
    assert "unmanaged" in finding.summary


@pytest.mark.asyncio
async def test_configuration_is_skipped_when_nothing_is_declared() -> None:
    """An intent naming no settings is not a robot with the wrong ones."""
    context = CheckContext(daemon=healthy_daemon(), intent=Intent())

    finding = await probes.configuration_matches_intent(context)

    assert finding.outcome is Outcome.SKIPPED


@pytest.mark.asyncio
async def test_identity_passes_when_the_announcement_matches() -> None:
    """The declared identity being announced is what keeps entity history attached."""
    finding = await probes.home_assistant_identity(healthy_context())

    assert finding.outcome is Outcome.PASSED
    assert finding.detail["identity_announced"] == IDENTITY


@pytest.mark.asyncio
async def test_identity_fails_when_the_announcement_changed() -> None:
    """A silently changed identity is the detached-history case, and it is named."""
    context = CheckContext(
        daemon=FakeDaemon(identity="reachy-mini-renamed"),
        intent=Intent(announced_identity=IDENTITY),
    )

    finding = await probes.home_assistant_identity(context)

    assert finding.outcome is Outcome.FAILED
    assert "reachy-mini-renamed" in finding.summary
    assert IDENTITY in finding.summary
    assert "history" in finding.summary


@pytest.mark.asyncio
async def test_identity_fails_when_nothing_is_announced() -> None:
    """Announcing nothing is different from announcing the wrong thing, and both fail."""
    context = CheckContext(
        daemon=FakeDaemon(identity=""),
        intent=Intent(announced_identity=IDENTITY),
    )

    finding = await probes.home_assistant_identity(context)

    assert finding.outcome is Outcome.FAILED
    assert "announces no identity" in finding.summary
    assert finding.detail["identity_announced"] is None


@pytest.mark.asyncio
async def test_identity_is_skipped_when_none_is_declared() -> None:
    """Without a declared identity there is nothing to compare, and that is not a fault."""
    context = CheckContext(daemon=healthy_daemon(), intent=Intent())

    finding = await probes.home_assistant_identity(context)

    assert finding.outcome is Outcome.SKIPPED


@pytest.mark.asyncio
async def test_a_probe_reaching_for_something_undeclared_says_so() -> None:
    """The guard that turns an `X | None` into an X names the fix, in this package."""
    with pytest.raises(MissingResourceError, match="requires"):
        await probes.daemon_reachable(CheckContext())


@pytest.mark.asyncio
async def test_each_guard_names_the_resource_it_was_asked_for() -> None:
    """Every requirement's guard fires, so none of the four is a branch nobody took."""
    for probe, resource in (
        (probes.groundstation_session, "groundstation"),
        (probes.model_files, "models"),
        (probes.configuration_matches_intent, "intent"),
    ):
        with pytest.raises(MissingResourceError, match=resource):
            await probe(CheckContext())


@pytest.mark.asyncio
async def test_the_three_groundstation_checks_share_one_session() -> None:
    """Opening three sessions would triple the cost and measure three moments."""
    context = healthy_context()
    link = context.groundstation
    assert isinstance(link, FakeLink)

    await probes.groundstation_session(context)
    await probes.groundstation_capabilities(context)
    await probes.groundstation_round_trip(context)

    # The fake counts every call; the real link answers all three from one
    # session, and this is the assertion that would catch it opening more.
    assert link.inspections == 3
