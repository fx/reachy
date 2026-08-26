"""Every current operator setting changeable; compatibility values read-only.

These drive the real application over `httpx.ASGITransport`, which speaks HTTP to
the ASGI app in memory. The routes, the form handling and the responses are the
real ones; no socket is opened, so these stay unit tests. The overrides file is a
real file in an in-memory filesystem, which is what lets the write-through be
tested rather than described.

ha-satellite REQ-049 is two claims and both are here: every current
operator-facing setting can be changed from this page, and the one marked secret
is reported as set or unset and never by value — so the page is usable for
rotating a credential and useless for learning one. Bootstrap settings stay
environment-only; retired gaze inputs stay visible but ignored and cannot be
persisted even by a crafted form. The credential used throughout carries a tab, a
newline and a backslash, because a value that reaches a renderer before a
redactor leaks in a form no plain-string search would find.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Final
from urllib.parse import urlencode

import httpx
import pytest
from satellite_support import (
    assert_public_controller_diagnostics_response,
    public_controller_diagnostic_event,
)

from reachy_mini_ha_satellite.config import (
    COMPATIBILITY_SETTINGS,
    ENV_PREFIX,
    GROUNDSTATION_URL_MAX_LENGTH,
    GROUNDSTATION_URL_SETTING,
    SECRET_SETTINGS,
    ConfigurationError,
    OverrideStore,
    Resolution,
    Settings,
    apply_settings_change,
    declared_elsewhere,
    load_settings,
    setting_names,
)
from reachy_mini_ha_satellite.groundstation_url import (
    GroundstationUrlOwner,
    ReplaceableRemoteSource,
)
from reachy_mini_ha_satellite.web import CLEAR_PREFIX, create_app, form_value

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pyfakefs.fake_filesystem import FakeFilesystem
    from starlette.applications import Starlette

    from reachy_mini_ha_satellite.web import OverrideMerge

# Every character that changes shape when something escapes it. Never anybody's
# — see the root AGENTS.md on what may enter a tracked file in a public
# repository.
AWKWARD_CREDENTIAL: Final = "ex\tam\nple\\credential"

# The environment the application under test was started with. The address is
# from the RFC 5737 documentation range.
ENVIRONMENT: Final[dict[str, str]] = {
    f"{ENV_PREFIX}DEVICE_NAME": "reachy-mini-1",
    f"{ENV_PREFIX}GROUNDSTATION_URL": "ws://192.0.2.10:8080/v1/session",
    f"{ENV_PREFIX}GROUNDSTATION_CREDENTIAL": AWKWARD_CREDENTIAL,
    f"{ENV_PREFIX}API_PORT": "9000",
}

_OVERRIDES = Path("/reachy-satellite-web/settings.json")


class RecordingHost:
    """A running application that records what the page did to it."""

    def __init__(self) -> None:
        """Start having been asked for nothing."""
        self.applied: list[Settings] = []
        self.environ: Mapping[str, str] = ENVIRONMENT
        self.submitted: list[Mapping[str, str]] = []
        self.refusal: ConfigurationError | None = None
        self.live: Resolution | None = None
        # What the other surface commits between the request arriving and the
        # write beginning, or `None` for a write nothing raced.
        self.interleaved: Mapping[str, str] | None = None
        self.stops = 0
        self.events: tuple[dict[str, object], ...] = (
            public_controller_diagnostic_event(),
        )
        self.diagnostics_resets = 0

    def status(self) -> dict[str, object]:
        """Report a robot doing nothing in particular.

        Returns:
            The shape the real application reports.
        """
        return {
            "pipeline": "idle",
            "gaze": "unknown",
            "tracking": False,
            "idle": True,
        }

    def apply_live(self, settings: Settings) -> None:
        """Record a live adoption.

        Args:
            settings: What the page resolved.
        """
        self.applied.append(settings)

    def current_resolution(self) -> Resolution | None:
        """Report a configuration changed from somewhere other than this page.

        Returns:
            Whatever a test set, and `None` — the ordinary case — for a page
            whose own record is still the whole story.
        """
        return self.live

    async def apply_settings(self, merge: OverrideMerge) -> Resolution:
        """Persist and adopt the way an application with no address owner does.

        The real application routes this through
        `groundstation_url.GroundstationUrlOwner`, which is covered by
        `test_satellite_groundstation_url.py`. What this page owes is that it
        hands the *computation* to whatever the application says the order is,
        and that the file it computes from is the one the write finds — which
        is what `interleaved` makes checkable.

        Args:
            merge: What to make of the stored overrides.

        Returns:
            The settings in effect afterwards.

        Raises:
            ConfigurationError: Whatever `refusal` was set to, standing in for
                a replacement the real owner refused or compensated. Nothing is
                written in that case, which is what the durable file records.
        """
        store = _store()
        if self.interleaved is not None:
            # Home Assistant's own control, committing between the request
            # arriving and this write beginning. The real owner's lock is what
            # orders the two; here it is simply written first, so a merge
            # computed from a pre-lock snapshot would not have seen it.
            store.save(self.interleaved)
        wanted = merge(store.load())
        self.submitted.append(dict(wanted))
        if self.refusal is not None:
            raise self.refusal
        return apply_settings_change(
            wanted,
            store=store,
            environ=self.environ,
            apply_live=self.apply_live,
        )

    def request_stop(self) -> None:
        """Record a restart request."""
        self.stops += 1

    def controller_diagnostics(self) -> tuple[dict[str, object], ...]:
        """Return fixed scalar evidence."""
        return self.events

    def reset_controller_diagnostics(self) -> None:
        """Clear only the diagnostic fake state."""
        self.events = ()
        self.diagnostics_resets += 1


def _store() -> OverrideStore:
    """Build a store over the fake filesystem.

    Returns:
        The store.
    """
    return OverrideStore(_OVERRIDES)


def _app(
    host: RecordingHost | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Starlette:
    """Build the settings interface over the current overrides file.

    Args:
        host: The running application, or `None` for a page with nothing
            behind it.
        environ: The environment the application was started with.

    Returns:
        The ASGI application.
    """
    source = ENVIRONMENT if environ is None else environ
    if host is not None:
        host.environ = source
    store = _store()
    return create_app(
        resolution=load_settings(source, store.load()),
        store=store,
        application=host,
        environ=source,
    )


def _client(app: Starlette) -> httpx.AsyncClient:
    """Speak HTTP to an application in memory.

    Args:
        app: What to drive.

    Returns:
        The client.
    """
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://satellite.invalid",
    )


def _form(settings: Settings, **changes: str) -> bytes:
    """Build the submission a browser would make from the rendered page.

    Every field is submitted, exactly as a form does, so what is under test is
    the real "which of these differ from the layers below" decision rather than
    a hand-picked subset.

    Args:
        settings: The settings the page was rendered from.
        changes: The fields to type something else into.

    Returns:
        The urlencoded body.
    """
    fields = {name: form_value(settings, name) for name in setting_names()}
    fields.update(changes)
    return urlencode(fields).encode("utf-8")


_FORM_HEADERS: Final = {"content-type": "application/x-www-form-urlencoded"}


@pytest.fixture(autouse=True)
def _fake_state_directory(fs: FakeFilesystem) -> None:
    """Give every test in this module an empty overrides directory.

    Args:
        fs: An in-memory filesystem, so these stay unit tests.
    """
    fs.create_dir(_OVERRIDES.parent)


class TestThePageReadsEverySetting:
    """REQ-049's first half: readable, except where it is not."""

    @pytest.mark.asyncio
    async def test_every_setting_appears_on_the_page(self) -> None:
        """The requirement says every one of them, so the page shows every one."""
        async with _client(_app()) as client:
            page = (await client.get("/")).text

        for name in setting_names():
            assert name in page

    @pytest.mark.asyncio
    async def test_a_value_from_the_environment_is_shown_with_its_source(
        self,
    ) -> None:
        """So the precedence is visible rather than surprising."""
        async with _client(_app()) as client:
            page = (await client.get("/")).text

        assert "9000" in page
        assert "environment" in page

    @pytest.mark.asyncio
    async def test_the_page_warns_about_changing_the_announced_identity(
        self,
    ) -> None:
        """It is the one migration hazard in the whole component."""
        async with _client(_app()) as client:
            page = (await client.get("/")).text

        assert "history detaches" in page

    @pytest.mark.asyncio
    async def test_the_page_says_which_changes_need_a_restart(self) -> None:
        """Rather than pretending every change takes effect immediately."""
        async with _client(_app()) as client:
            page = (await client.get("/")).text

        assert "needs a restart" in page
        assert "applies at once" in page

    @pytest.mark.asyncio
    async def test_the_page_offers_to_stop_rather_than_to_restart(self) -> None:
        """A button promising a restart would promise what nothing performs.

        The daemon leaves a cleanly-stopped application stopped.
        """
        async with _client(_app()) as client:
            page = (await client.get("/")).text

        assert 'action="stop"' in page
        assert ">Restart<" not in page


class TestLegacyGazeCompatibility:
    """The form reports but cannot write migration-only gaze values."""

    @pytest.mark.asyncio
    async def test_page_labels_every_legacy_value_as_ignored(self) -> None:
        """Operators see why familiar values no longer change movement."""
        async with _client(_app()) as client:
            response = await client.get("/")

        for name in COMPATIBILITY_SETTINGS:
            assert name in response.text
        assert response.text.count("legacy compatibility; ignored") >= len(
            COMPATIBILITY_SETTINGS
        )

    @pytest.mark.asyncio
    async def test_crafted_submission_cannot_persist_compatibility_fields(self) -> None:
        """Read-only rendering is backed by server-side refusal, not browser trust."""
        settings = load_settings(ENVIRONMENT, {}).settings
        fields = {
            "gaze_deadzone": "0.9",
            "gaze_smoothing": "0.9",
            "camera_horizontal_fov_degrees": "100.0",
            "camera_vertical_fov_degrees": "80.0",
        }

        async with _client(_app()) as client:
            response = await client.post(
                "/settings",
                content=_form(settings, **fields),
                headers=_FORM_HEADERS,
            )

        assert response.status_code == 303
        assert not (set(_store().load()) & COMPATIBILITY_SETTINGS)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", sorted(COMPATIBILITY_SETTINGS))
    async def test_ordinary_save_drops_a_stale_legacy_override(self, name: str) -> None:
        """A normal save migrates stale form-owned copies out of the store."""
        _store().save({name: "0.2"})
        settings = load_settings(ENVIRONMENT, _store().load()).settings

        async with _client(_app()) as client:
            response = await client.post(
                "/settings",
                content=_form(settings, api_port="9100"),
                headers=_FORM_HEADERS,
            )

        assert response.status_code == 303
        assert _store().load() == {"api_port": "9100"}


class TestTheSecretIsReportedAndNeverRevealed:
    """REQ-049's second half, on every surface this module serves."""

    @pytest.mark.asyncio
    async def test_the_credential_shows_as_set(self) -> None:
        """Which is what makes the page usable for rotation."""
        async with _client(_app()) as client:
            page = (await client.get("/")).text

        assert "&lt;set&gt;" in page or "<set>" in page

    @pytest.mark.asyncio
    async def test_the_credential_appears_in_no_rendering_of_the_page(self) -> None:
        """Raw, HTML-escaped, JSON-escaped or repr'd."""
        async with _client(_app()) as client:
            page = (await client.get("/")).text
            configuration = (await client.get("/config")).text

        for rendering in (page, configuration):
            for spelling in _spellings(AWKWARD_CREDENTIAL):
                assert spelling not in rendering

    @pytest.mark.asyncio
    async def test_the_credential_is_absent_from_a_refusal_too(self) -> None:
        """An error page renders the submission's refusal, not its contents."""
        settings = load_settings(ENVIRONMENT, {}).settings
        async with _client(_app()) as client:
            response = await client.post(
                "/settings",
                content=_form(settings, api_port="not a port"),
                headers=_FORM_HEADERS,
            )

        assert response.status_code == 400
        for spelling in _spellings(AWKWARD_CREDENTIAL):
            assert spelling not in response.text

    @pytest.mark.asyncio
    async def test_an_unset_credential_shows_as_unset(self) -> None:
        """Nothing configured and something configured are different facts.

        With face tracking off no session is opened, so an unset credential is
        an ordinary configuration rather than one startup refuses.
        """
        environ = dict(ENVIRONMENT)
        del environ[f"{ENV_PREFIX}GROUNDSTATION_CREDENTIAL"]
        environ[f"{ENV_PREFIX}FACE_TRACKING_ENABLED"] = "false"

        async with _client(_app(environ=environ)) as client:
            configuration = (await client.get("/config")).json()

        assert configuration["settings"]["groundstation_credential"] == "<unset>"

    @pytest.mark.asyncio
    async def test_the_secret_field_is_rendered_empty(self) -> None:
        """A browser submits it blank, which is what "leave it alone" means."""
        async with _client(_app()) as client:
            page = (await client.get("/")).text

        assert 'name="groundstation_credential" value=""' in page


class TestChangingASetting:
    """REQ-049's scenario: the groundstation address, without a shell."""

    @pytest.mark.asyncio
    async def test_a_change_is_written_and_takes_effect(self) -> None:
        """The whole of the requirement, in one exchange."""
        host = RecordingHost()
        settings = load_settings(ENVIRONMENT, {}).settings
        replacement = "ws://198.51.100.20:8080/v1/session"

        async with _client(_app(host)) as client:
            response = await client.post(
                "/settings",
                content=_form(settings, groundstation_url=replacement),
                headers=_FORM_HEADERS,
            )

        assert response.status_code == 303
        assert _store().load()["groundstation_url"] == replacement
        assert host.applied[-1].groundstation_url == replacement

    @pytest.mark.asyncio
    async def test_an_override_beats_the_environment(self) -> None:
        """Without which the requirement would be false for any exported setting."""
        host = RecordingHost()
        settings = load_settings(ENVIRONMENT, {}).settings

        async with _client(_app(host)) as client:
            await client.post(
                "/settings",
                content=_form(settings, api_port="9100"),
                headers=_FORM_HEADERS,
            )

        assert host.applied[-1].api_port == 9100

    @pytest.mark.asyncio
    async def test_a_value_left_alone_is_not_pinned(self) -> None:
        """Only what an operator actually changed is written."""
        settings = load_settings(ENVIRONMENT, {}).settings

        async with _client(_app(RecordingHost())) as client:
            await client.post(
                "/settings",
                content=_form(settings, api_port="9100"),
                headers=_FORM_HEADERS,
            )

        assert set(_store().load()) == {"api_port"}

    @pytest.mark.asyncio
    async def test_saving_a_value_back_to_the_environment_removes_the_override(
        self,
    ) -> None:
        """So "what is this running on" keeps one answer rather than two."""
        _store().save({"api_port": "9100"})
        settings = load_settings(ENVIRONMENT, _store().load()).settings

        async with _client(_app(RecordingHost())) as client:
            await client.post(
                "/settings",
                content=_form(settings, api_port="9000"),
                headers=_FORM_HEADERS,
            )

        assert _store().load() == {}

    @pytest.mark.asyncio
    async def test_the_redirect_says_what_changed_and_what_needs_a_restart(
        self,
    ) -> None:
        """A redirect rather than a rendered page, so a refresh resubmits nothing."""
        settings = load_settings(ENVIRONMENT, {}).settings

        async with _client(_app(RecordingHost())) as client:
            response = await client.post(
                "/settings",
                content=_form(settings, api_port="9100", log_level="debug"),
                headers=_FORM_HEADERS,
            )

        location = response.headers["location"]
        assert "saved=" in location
        assert "restart=api_port" in location
        assert "restart=log_level" not in location

    @pytest.mark.asyncio
    async def test_the_speaker_boost_is_saved_without_asking_for_a_restart(
        self,
    ) -> None:
        """Both outputs read it per pushed chunk, so it takes effect at once.

        The page and the Home Assistant control write the same setting through
        the same path, so a page that asked for a restart would be telling an
        operator something the control contradicts.
        """
        settings = load_settings(ENVIRONMENT, {}).settings
        host = RecordingHost()

        async with _client(_app(host)) as client:
            response = await client.post(
                "/settings",
                content=_form(settings, speaker_boost_percent="620.0"),
                headers=_FORM_HEADERS,
            )

        location = response.headers["location"]
        assert "saved=speaker_boost_percent" in location
        assert "restart=" not in location
        assert _store().load() == {"speaker_boost_percent": "620.0"}
        assert host.applied[-1].speaker_boost_percent == pytest.approx(620.0)

    @pytest.mark.asyncio
    async def test_the_page_reports_what_the_last_submission_did(self) -> None:
        """Which is what the redirect's query string is for."""
        async with _client(_app(RecordingHost())) as client:
            page = (await client.get("/?saved=api_port&restart=api_port")).text

        assert "Saved" in page
        assert "api_port" in page

    @pytest.mark.asyncio
    async def test_arbitrary_text_cannot_be_put_on_the_page_through_the_query(
        self,
    ) -> None:
        """The names are filtered against the real settings rather than trusted."""
        async with _client(_app(RecordingHost())) as client:
            page = (await client.get("/?saved=%3Cscript%3Ealert%3C/script%3E")).text

        assert "<script>alert" not in page

    @pytest.mark.asyncio
    async def test_a_submission_that_does_not_resolve_changes_nothing(self) -> None:
        """Nothing is written, and the refusal is what comes back."""
        host = RecordingHost()
        settings = load_settings(ENVIRONMENT, {}).settings

        async with _client(_app(host)) as client:
            response = await client.post(
                "/settings",
                content=_form(settings, api_port="70000"),
                headers=_FORM_HEADERS,
            )

        assert response.status_code == 400
        assert _store().load() == {}
        assert host.applied == []

    @pytest.mark.asyncio
    async def test_an_oversized_submission_is_refused(self) -> None:
        """A settings page is not an upload endpoint."""
        async with _client(_app(RecordingHost())) as client:
            response = await client.post(
                "/settings",
                content=b"x" * (128 * 1024),
                headers=_FORM_HEADERS,
            )

        assert response.status_code == 413


class TestRotatingTheCredential:
    """Usable for replacing a secret, useless for reading one."""

    @pytest.mark.asyncio
    async def test_a_blank_secret_field_leaves_it_alone(self) -> None:
        """Which is what a browser submits for a field nobody typed into."""
        host = RecordingHost()
        settings = load_settings(ENVIRONMENT, {}).settings

        async with _client(_app(host)) as client:
            await client.post(
                "/settings",
                content=_form(settings, api_port="9100"),
                headers=_FORM_HEADERS,
            )

        assert (
            host.applied[-1].groundstation_credential.get_secret_value()
            == AWKWARD_CREDENTIAL
        )

    @pytest.mark.asyncio
    async def test_a_typed_secret_replaces_it(self) -> None:
        """The rotation the requirement's scenario is about."""
        host = RecordingHost()
        settings = load_settings(ENVIRONMENT, {}).settings

        async with _client(_app(host)) as client:
            await client.post(
                "/settings",
                content=_form(settings, groundstation_credential="the-next-one"),
                headers=_FORM_HEADERS,
            )

        assert (
            host.applied[-1].groundstation_credential.get_secret_value()
            == "the-next-one"
        )

    @pytest.mark.asyncio
    async def test_unsetting_it_needs_a_control_that_says_so(self) -> None:
        """Because a blank field already means "leave it alone"."""
        host = RecordingHost()
        environ = {**ENVIRONMENT, f"{ENV_PREFIX}FACE_TRACKING_ENABLED": "false"}
        settings = load_settings(environ, {}).settings
        fields = {f"{CLEAR_PREFIX}groundstation_credential": "1"}

        async with _client(_app(host, environ=environ)) as client:
            await client.post(
                "/settings",
                content=_form(settings, **fields),
                headers=_FORM_HEADERS,
            )

        assert host.applied[-1].groundstation_credential.get_secret_value() == ""

    @pytest.mark.asyncio
    async def test_unsetting_one_a_session_needs_is_refused_with_a_reason(
        self,
    ) -> None:
        """Rather than accepted and found later as a robot connecting to nothing."""
        host = RecordingHost()
        settings = load_settings(ENVIRONMENT, {}).settings
        fields = {f"{CLEAR_PREFIX}groundstation_credential": "1"}

        async with _client(_app(host)) as client:
            response = await client.post(
                "/settings",
                content=_form(settings, **fields),
                headers=_FORM_HEADERS,
            )

        assert response.status_code == 400
        assert "GROUNDSTATION_CREDENTIAL" in response.text
        assert host.applied == []


class TestResettingAndRestarting:
    """The two actions that are not a form submission."""

    @pytest.mark.asyncio
    async def test_reset_discards_every_override(self) -> None:
        """Including the ones that name nothing any more."""
        _store().save({"api_port": "9100", "web_prot": "9200"})

        async with _client(_app(RecordingHost())) as client:
            response = await client.post("/reset")

        assert response.status_code == 303
        assert _store().load() == {}

    @pytest.mark.asyncio
    async def test_stopping_asks_the_application_to_stop(self) -> None:
        """Which is how a restart-required change is taken without a shell."""
        host = RecordingHost()

        async with _client(_app(host)) as client:
            response = await client.post("/stop")

        assert response.status_code == 202
        assert host.stops == 1

    @pytest.mark.asyncio
    async def test_stopping_says_that_nothing_starts_it_again(self) -> None:
        """An operator told otherwise is left looking at a robot that went quiet.

        The daemon leaves a cleanly-stopped application stopped.
        """
        async with _client(_app(RecordingHost())) as client:
            response = await client.post("/stop")

        assert "dashboard" in response.text

    @pytest.mark.asyncio
    async def test_stopping_with_nothing_running_says_so(self) -> None:
        """Rather than reporting a stop that cannot have happened."""
        async with _client(_app()) as client:
            response = await client.post("/stop")

        assert response.status_code == 503


class TestTheMachineReadableSurfaces:
    """What a diagnostic reads instead of scraping the page."""

    @pytest.mark.asyncio
    async def test_the_configuration_endpoint_reports_every_setting(self) -> None:
        """Including the ones left at their defaults."""
        async with _client(_app()) as client:
            configuration = (await client.get("/config")).json()

        assert set(configuration["settings"]) == set(setting_names())

    @pytest.mark.asyncio
    async def test_it_reports_legacy_compatibility_settings_as_ignored(self) -> None:
        """Machine-readable consumers receive the same migration status as the page."""
        async with _client(_app()) as client:
            configuration = (await client.get("/config")).json()

        assert set(configuration["compatibility_ignored"]) == set(
            COMPATIBILITY_SETTINGS
        )

    @pytest.mark.asyncio
    async def test_it_says_which_settings_are_secret(self) -> None:
        """One declaration, read by every surface rather than repeated."""
        async with _client(_app()) as client:
            configuration = (await client.get("/config")).json()

        assert set(configuration["secret"]) == set(SECRET_SETTINGS)

    @pytest.mark.asyncio
    async def test_it_reports_a_stale_override(self) -> None:
        """Dropped, but not quietly."""
        _store().save({"web_prot": "9100"})

        async with _client(_app()) as client:
            configuration = (await client.get("/config")).json()

        assert configuration["ignored_overrides"] == ["web_prot"]

    @pytest.mark.asyncio
    async def test_the_status_endpoint_reports_what_the_robot_is_doing(self) -> None:
        """Which is the behaviour layer's own view, handed through."""
        async with _client(_app(RecordingHost())) as client:
            status = (await client.get("/status")).json()

        assert status["running"]
        assert status["pipeline"] == "idle"

    @pytest.mark.asyncio
    async def test_the_status_endpoint_says_when_nothing_is_running(self) -> None:
        """A page served without an application behind it is a real state."""
        async with _client(_app()) as client:
            status = (await client.get("/status")).json()

        assert not status["running"]

    @pytest.mark.asyncio
    async def test_controller_diagnostics_match_exact_public_response_schema(
        self,
    ) -> None:
        """The unversioned envelope and every event expose only required public keys."""
        host = RecordingHost()

        async with _client(_app(host)) as client:
            response = await client.get("/diagnostics/controller")

        assert response.status_code == 200
        payload = response.json()
        assert payload["running"]
        assert payload["events"] == list(host.events)
        assert_public_controller_diagnostics_response(payload)

    @pytest.mark.asyncio
    async def test_controller_diagnostics_report_when_no_application_is_running(
        self,
    ) -> None:
        """Absent runtime has explicit read and reset responses."""
        async with _client(_app()) as client:
            read = await client.get("/diagnostics/controller")
            reset = await client.post("/diagnostics/controller/reset")

        payload = read.json()
        assert payload == {"running": False, "events": []}
        assert_public_controller_diagnostics_response(payload)
        assert reset.status_code == 503
        assert reset.json() == {"reset": False}

    @pytest.mark.asyncio
    async def test_controller_reset_changes_diagnostics_only(self) -> None:
        """Reset emits no stop, settings adoption or other application mutation."""
        host = RecordingHost()
        before = (tuple(host.applied), host.stops)

        async with _client(_app(host)) as client:
            response = await client.post("/diagnostics/controller/reset")

        assert response.status_code == 200
        assert response.json() == {"reset": True}
        assert host.events == ()
        assert host.diagnostics_resets == 1
        assert (tuple(host.applied), host.stops) == before

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("post", "/diagnostics/controller"),
            ("get", "/diagnostics/controller/reset"),
        ],
    )
    async def test_controller_diagnostics_refuse_wrong_methods(
        self,
        method: str,
        path: str,
    ) -> None:
        """Read and reset routes have one method each."""
        async with _client(_app(RecordingHost())) as client:
            response = await getattr(client, method)(path)

        assert response.status_code == 405

    @pytest.mark.asyncio
    async def test_the_interface_answers_that_it_is_up(self) -> None:
        """The cheapest question a deployment check can ask."""
        async with _client(_app()) as client:
            response = await client.get("/livez")

        assert response.status_code == 200


def _spellings(value: str) -> tuple[str, ...]:
    """Every form a leaked credential could take in a rendering.

    Args:
        value: The credential.

    Returns:
        The raw value, its JSON escaping, its Python `repr` and its HTML
        escaping — the four ways a string reaches a page or an endpoint.
    """
    import html

    return (
        value,
        json.dumps(value)[1:-1],
        repr(value)[1:-1],
        html.escape(value, quote=True),
    )


class TestAStaleOverrideOnThePage:
    """Dropped rather than fatal, and said out loud rather than silently."""

    @pytest.mark.asyncio
    async def test_the_page_names_an_override_that_is_not_a_setting_any_more(
        self,
    ) -> None:
        """An upgrade that renamed a setting must not leave an invisible ghost."""
        _store().save({"web_prot": "9100"})

        async with _client(_app()) as client:
            page = (await client.get("/")).text

        assert "web_prot" in page
        assert "are being ignored" in page


class TestAVariableThisApplicationDoesNotRead:
    """Set on the robot, valid, and inert here — which is worth saying.

    `reachyctl config apply` writes the whole documented vocabulary, and some of
    it is for other components. An operator who set one of those and saw no
    change would otherwise have nowhere to find out why: it is not a typo, so
    startup does not refuse it, and it is not a setting, so it is not in the
    table.
    """

    @pytest.mark.asyncio
    async def test_the_page_says_it_has_no_effect_here(self) -> None:
        """The page an operator is already looking at is where the answer goes."""
        declared = sorted(declared_elsewhere())
        assert declared, "the fixture depends on there being at least one"
        environ = {**ENVIRONMENT, declared[0]: "40"}

        async with _client(_app(environ=environ)) as client:
            page = (await client.get("/")).text

        assert declared[0] in page
        assert "has no effect here" in page

    @pytest.mark.asyncio
    async def test_the_configuration_endpoint_carries_it_too(self) -> None:
        """So a deployment check sees it without scraping the page."""
        declared = sorted(declared_elsewhere())
        environ = {**ENVIRONMENT, declared[0]: "40"}

        async with _client(_app(environ=environ)) as client:
            body = (await client.get("/config")).json()

        assert body["declared_but_unread"] == [declared[0]]

    @pytest.mark.asyncio
    async def test_an_ordinary_environment_says_nothing_about_it(self) -> None:
        """A notice that is always there is a notice nobody reads."""
        async with _client(_app()) as client:
            page = (await client.get("/")).text

        assert "has no effect here" not in page


class TestWhenTheChangeCannotBeWritten:
    """A change that appears to have been accepted and was not is the worst case."""

    @pytest.mark.asyncio
    async def test_a_failed_write_explains_itself_rather_than_erroring(self) -> None:
        """And the running application is not told about a change that was lost."""
        host = RecordingHost()
        settings = load_settings(ENVIRONMENT, {}).settings
        # A directory where the store's temporary file has to go, so the write
        # cannot complete.
        (_OVERRIDES.parent / f"{_OVERRIDES.name}.new").mkdir()

        async with _client(_app(host)) as client:
            response = await client.post(
                "/settings",
                content=_form(settings, api_port="9100"),
                headers=_FORM_HEADERS,
            )

        assert response.status_code == 400
        assert "cannot be written" in response.text
        assert host.applied == []

    @pytest.mark.asyncio
    async def test_resetting_into_an_unusable_environment_is_refused(self) -> None:
        """A robot configured entirely from this page must not be able to brick itself."""
        environ = {f"{ENV_PREFIX}FACE_TRACKING_ENABLED": "false"}
        _store().save({"device_name": "reachy-mini-1"})

        async with _client(_app(RecordingHost(), environ=environ)) as client:
            response = await client.post("/reset")

        assert response.status_code == 400
        assert "DEVICE_NAME" in response.text
        assert _store().load() == {"device_name": "reachy-mini-1"}


class TestTheFormIsUsableWithoutSeeingIt:
    """A table header does not label a control for assistive technology."""

    @pytest.mark.asyncio
    async def test_every_control_is_labelled_by_its_row_header(self) -> None:
        """Otherwise a screen reader announces the kind of field and nothing else."""
        async with _client(_app()) as client:
            page = (await client.get("/")).text

        for name in setting_names():
            assert f'<label for="setting-{name}">' in page
            assert f'id="setting-{name}"' in page


class TestTheSettingTheFormCannotWrite:
    """`state_dir` is where the form's own file lives, so the form does not move it."""

    @pytest.mark.asyncio
    async def test_it_is_shown_but_not_submittable(self) -> None:
        """Readable, which is what the requirement asks of a non-secret setting."""
        async with _client(_app()) as client:
            page = (await client.get("/")).text

        assert 'id="setting-state_dir" disabled' in page
        assert 'name="state_dir"' not in page

    @pytest.mark.asyncio
    async def test_submitting_it_anyway_writes_nothing(self) -> None:
        """A `disabled` attribute is a rendering, not an enforcement."""
        host = RecordingHost()
        settings = load_settings(ENVIRONMENT, {}).settings

        async with _client(_app(host)) as client:
            await client.post(
                "/settings",
                content=_form(settings, state_dir="/reachy-satellite-elsewhere"),
                headers=_FORM_HEADERS,
            )

        assert "state_dir" not in _store().load()

    @pytest.mark.asyncio
    async def test_the_configuration_endpoint_says_which_are_read_only(self) -> None:
        """So a diagnostic does not have to scrape the page for it."""
        async with _client(_app()) as client:
            configuration = (await client.get("/config")).json()

        assert configuration["read_only"] == [
            "state_dir",
            "web_enabled",
            "web_host",
            "web_port",
        ]


class TestARequestFromSomewhereElse:
    """Not authentication, and the one exposure that needs no peer on the network.

    Any page an operator's browser visits can submit a form to any address that
    browser can reach. Without this check, a web page anywhere could stop the
    robot or replace its groundstation credential the moment somebody with a
    laptop on the same network opened it.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path",
        ["/settings", "/reset", "/stop", "/diagnostics/controller/reset"],
    )
    async def test_a_cross_site_submission_is_refused(self, path: str) -> None:
        """Every route that changes something, not just the form.

        Args:
            path: One of the state-changing routes.
        """
        host = RecordingHost()

        async with _client(_app(host)) as client:
            response = await client.post(
                path,
                content=b"",
                headers={**_FORM_HEADERS, "sec-fetch-site": "cross-site"},
            )

        assert response.status_code == 403
        assert _store().load() == {}
        assert host.stops == 0

    @pytest.mark.asyncio
    async def test_a_submission_from_this_page_is_allowed(self) -> None:
        """Which is what a browser reports for the form the page serves."""
        settings = load_settings(ENVIRONMENT, {}).settings

        async with _client(_app(RecordingHost())) as client:
            response = await client.post(
                "/settings",
                content=_form(settings, api_port="9100"),
                headers={**_FORM_HEADERS, "sec-fetch-site": "same-origin"},
            )

        assert response.status_code == 303

    @pytest.mark.asyncio
    async def test_an_origin_naming_another_host_is_refused(self) -> None:
        """The fallback, for a browser that sends no `Sec-Fetch-Site`."""
        async with _client(_app(RecordingHost())) as client:
            response = await client.post(
                "/stop",
                headers={"origin": "http://198.51.100.9"},
            )

        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_an_origin_naming_this_host_is_allowed(self) -> None:
        """Same fallback, the other way."""
        host = RecordingHost()

        async with _client(_app(host)) as client:
            response = await client.post(
                "/stop",
                headers={"origin": "http://satellite.invalid"},
            )

        assert response.status_code == 202
        assert host.stops == 1

    @pytest.mark.asyncio
    async def test_a_request_carrying_neither_header_is_allowed(self) -> None:
        """It is not from a browser, and a cross-site form post needs one."""
        host = RecordingHost()

        async with _client(_app(host)) as client:
            response = await client.post("/stop")

        assert response.status_code == 202
        assert host.stops == 1

    @pytest.mark.asyncio
    async def test_reading_is_not_gated(self) -> None:
        """The check is about changing something, not about reading.

        A page nobody can read is a page that cannot be used.
        """
        async with _client(_app()) as client:
            response = await client.get(
                "/",
                headers={"sec-fetch-site": "cross-site"},
            )

        assert response.status_code == 200


class TestNothingIsCached:
    """A settings page in a shared browser's cache outlives the session."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path", ["/", "/config", "/status", "/diagnostics/controller", "/livez"]
    )
    async def test_every_response_says_not_to_keep_it(self, path: str) -> None:
        """A robot's address and identity are not things to leave lying about.

        The page carries no secret — those are rendered as set or unset — but a
        browser that kept it would hand the rest to whoever opened that browser
        next.

        Args:
            path: One of the readable routes.
        """
        async with _client(_app(RecordingHost())) as client:
            response = await client.get(path)

        assert response.headers["cache-control"] == "no-store"


class TestSomethingThatIsNotHttp:
    """A response header belongs on a response, and a lifespan event has none.

    The middleware is not the thing to notice a connection it was not built for,
    so it hands anything that is not HTTP straight through.
    """

    @pytest.mark.asyncio
    async def test_a_lifespan_message_passes_straight_through(self) -> None:
        """The header belongs on a response, and a lifespan event has none."""
        started: list[str] = []
        app = _app()

        async def _receive() -> dict[str, str]:
            """Hand the application one lifespan event.

            Returns:
                The event.
            """
            return {"type": "lifespan.startup"}

        async def _send(message: Mapping[str, object]) -> None:
            """Record what came back.

            Args:
                message: One ASGI event.
            """
            started.append(str(message["type"]))

        await app({"type": "lifespan"}, _receive, _send)

        assert started[0] == "lifespan.startup.complete"


class TestASpellingIsNotAChange:
    """The form renders each field from the parsed settings.

    Not from the raw string somebody typed into the environment, which is why
    the two have to be compared after being rendered the same way.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("variable", "written"),
        [
            (f"{ENV_PREFIX}ADVERTISE", "TRUE"),
            (f"{ENV_PREFIX}API_PORT", "09000"),
            (f"{ENV_PREFIX}FRAME_INTERVAL_SECONDS", "0.10"),
            (f"{ENV_PREFIX}DETECTION_SOURCE", "remote"),
        ],
    )
    async def test_a_valid_but_uncanonical_value_is_not_pinned(
        self,
        variable: str,
        written: str,
    ) -> None:
        """Saving one setting must not pin every value spelled unusually.

        An override sits above the environment, so a pinned one is a setting the
        environment can no longer change — and the operator changed one thing.

        Args:
            variable: A variable written in a valid but non-canonical way.
            written: How it was written.
        """
        environ = {**ENVIRONMENT, variable: written}
        settings = load_settings(environ, {}).settings

        async with _client(_app(RecordingHost(), environ=environ)) as client:
            await client.post(
                "/settings",
                content=_form(settings, log_level="debug"),
                headers=_FORM_HEADERS,
            )

        assert set(_store().load()) == {"log_level"}


# Elements that may not appear inside a paragraph: HTML's content model forbids
# them, so a parser meeting one closes the `<p>` implicitly and builds a DOM
# that is not the one the template wrote.
_NOT_INSIDE_A_PARAGRAPH: Final[frozenset[str]] = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "div",
        "dl",
        "fieldset",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "ul",
    },
)

# Elements that carry no closing tag, so their absence from the stack is not a
# fault.
_VOID_ELEMENTS: Final[frozenset[str]] = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    },
)


class _Nesting(HTMLParser):
    """Reports every place the rendered page's structure is not what it says.

    Three faults, and each of them means the DOM a browser builds differs from
    the markup this repository wrote: a block-level element inside a paragraph,
    a form inside a form, and a tag left open or closed twice. A page whose
    rendered structure cannot be predicted from its source is one nobody can
    reason about from the code — and this page is the deliverable for
    ha-satellite REQ-049.
    """

    def __init__(self) -> None:
        """Start with nothing open and nothing wrong."""
        super().__init__()
        self.open: list[str] = []
        self.faults: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        """Note an element opening, and whether it may open here.

        Args:
            tag: The element's name, already lower-cased by the parser.
            attrs: Its attributes, which nothing here inspects.
        """
        del attrs
        if tag in _VOID_ELEMENTS:
            return
        if tag in _NOT_INSIDE_A_PARAGRAPH and "p" in self.open:
            self.faults.append(f"<{tag}> inside <p>")
        if tag == "form" and "form" in self.open:
            self.faults.append("<form> inside <form>")
        self.open.append(tag)

    def handle_endtag(self, tag: str) -> None:
        """Note an element closing, and whether it closes what is open.

        Args:
            tag: The element's name.
        """
        if tag in _VOID_ELEMENTS:
            return
        if tag not in self.open:
            self.faults.append(f"</{tag}> closes nothing that is open")
            return
        while self.open and self.open[-1] != tag:
            self.faults.append(f"<{self.open.pop()}> left unclosed inside <{tag}>")
        self.open.pop()

    def report(self) -> list[str]:
        """Every fault found, including anything still open at the end.

        Returns:
            One line per fault.
        """
        return [
            *self.faults,
            *(f"<{tag}> is never closed" for tag in reversed(self.open)),
        ]


def _nesting_faults(page: str) -> list[str]:
    """Parse a rendered page and report how its structure is malformed.

    Args:
        page: The HTML.

    Returns:
        One line per fault, empty when the page is well formed.
    """
    parser = _Nesting()
    parser.feed(page)
    parser.close()
    return parser.report()


class TestThePageIsTheStructureItSays:
    """Parsed rather than string-matched, so this is a gate rather than a habit.

    A `<p>` cannot contain a `<form>`: a browser closes the paragraph first and
    builds a DOM the template does not describe. Checking the *parse* catches
    that whole class rather than the one tag somebody happened to look at, and
    it keeps catching it as this page grows.
    """

    @pytest.mark.asyncio
    async def test_the_page_as_served_is_well_formed(self) -> None:
        """The ordinary rendering, over the real routes."""
        async with _client(_app(RecordingHost())) as client:
            page = (await client.get("/")).text

        assert _nesting_faults(page) == []

    @pytest.mark.asyncio
    async def test_the_page_carrying_a_refusal_is_well_formed(self) -> None:
        """The error note is a different branch of the template."""
        settings = load_settings(ENVIRONMENT, {}).settings

        async with _client(_app(RecordingHost())) as client:
            page = (
                await client.post(
                    "/settings",
                    content=_form(settings, api_port="70000"),
                    headers=_FORM_HEADERS,
                )
            ).text

        assert _nesting_faults(page) == []

    @pytest.mark.asyncio
    async def test_the_page_reporting_a_save_is_well_formed(self) -> None:
        """So are the saved and needs-a-restart notes, and a stale override."""
        _store().save({"web_prot": "9000"})

        async with _client(_app(RecordingHost())) as client:
            page = (await client.get("/?saved=api_port&restart=api_port")).text

        assert _nesting_faults(page) == []

    def test_the_checker_reports_a_block_element_inside_a_paragraph(self) -> None:
        """A gate nobody has watched fail is a gate that does not exist."""
        faults = _nesting_faults("<p><form><button>Go</button></form></p>")

        assert "<form> inside <p>" in faults

    def test_the_checker_reports_a_tag_left_open(self) -> None:
        """The second fault it exists to catch."""
        assert _nesting_faults("<div><span>text</div>") == [
            "<span> left unclosed inside <div>",
        ]

    def test_the_checker_reports_a_tag_that_closes_nothing(self) -> None:
        """And the third."""
        assert _nesting_faults("<div>text</div></section>") == [
            "</section> closes nothing that is open",
        ]

    def test_the_checker_accepts_a_void_element_without_a_closing_tag(self) -> None:
        """Otherwise every `<input>` on the page would read as unclosed."""
        assert _nesting_faults('<p>text<br><input type="text"></p>') == []


class TestTheGroundstationAddressOnThePage:
    """REQ-095's other configuration surface: one bound, one read-back."""

    @pytest.mark.asyncio
    async def test_the_field_carries_the_shared_maximum(self) -> None:
        """The browser stops an over-long address before the model has to."""
        async with _client(_app(RecordingHost())) as client:
            page = (await client.get("/")).text

        field = page.split(f'name="{GROUNDSTATION_URL_SETTING}"')[1].split(">")[0]
        assert f'maxlength="{GROUNDSTATION_URL_MAX_LENGTH}"' in field

    @pytest.mark.asyncio
    async def test_the_page_says_the_address_applies_at_once(self) -> None:
        """It no longer needs a restart, and the page must not say it does."""
        async with _client(_app(RecordingHost())) as client:
            page = (await client.get("/")).text

        row = page.split(f'name="{GROUNDSTATION_URL_SETTING}"')[1].split("</tr>")[0]
        assert "applies at once" in row
        assert "needs a restart" not in row

    @pytest.mark.asyncio
    async def test_a_submission_goes_through_the_application_s_own_order(
        self,
    ) -> None:
        """The page hands the whole submission over rather than writing first.

        Which order that is belongs to the application — see
        `test_satellite_groundstation_url.py` — and what this asserts is that
        the page asks rather than deciding.
        """
        host = RecordingHost()
        settings = load_settings(ENVIRONMENT, {}).settings
        replacement = "ws://192.0.2.30:8080/v1/session"

        async with _client(_app(host)) as client:
            response = await client.post(
                "/settings",
                content=_form(settings, groundstation_url=replacement),
                headers=_FORM_HEADERS,
            )

        assert response.status_code == 303
        assert host.submitted == [{GROUNDSTATION_URL_SETTING: replacement}]
        assert _store().load() == {GROUNDSTATION_URL_SETTING: replacement}

    @pytest.mark.asyncio
    async def test_a_change_made_from_home_assistant_shows_on_the_page(
        self,
    ) -> None:
        """Rendering only what this page last wrote would report a stale address.

        Home Assistant's own control changes the address with nobody here, so
        the page renders the application's live resolution when it keeps one.
        """
        host = RecordingHost()
        elsewhere = "ws://192.0.2.40:8080/v1/session"
        host.live = load_settings(
            ENVIRONMENT,
            {GROUNDSTATION_URL_SETTING: elsewhere},
        )

        async with _client(_app(host)) as client:
            page = (await client.get("/")).text
            reported = (await client.get("/config")).json()

        assert elsewhere in page
        assert reported["settings"][GROUNDSTATION_URL_SETTING] == elsewhere

    @pytest.mark.asyncio
    async def test_a_refused_submission_leaves_the_page_reporting_the_old_value(
        self,
    ) -> None:
        """The read-back is the effective address, never the requested one."""
        host = RecordingHost()
        host.refusal = ConfigurationError("the replacement could not be started")
        settings = load_settings(ENVIRONMENT, {}).settings

        async with _client(_app(host)) as client:
            response = await client.post(
                "/settings",
                content=_form(
                    settings,
                    groundstation_url="ws://192.0.2.30:8080/v1/session",
                ),
                headers=_FORM_HEADERS,
            )

        assert response.status_code == 400
        assert "could not be started" in response.text
        assert ENVIRONMENT[f"{ENV_PREFIX}GROUNDSTATION_URL"] in response.text
        assert _store().load() == {}

    @pytest.mark.asyncio
    async def test_an_overlong_submission_is_refused_with_the_runtime_remedy(
        self,
    ) -> None:
        """The page reaches the runtime check, not the startup migration's.

        The two refuse the same length and offer different remedies. The
        migration's tells an operator to remove an entry from the overrides file
        and start the application again — and here nothing was written, there is
        no entry, and the robot is running. So this one drives the real owner
        rather than the recording stand-in: the wording an operator acts on is
        decided by which of the two checks the page's submission reaches first.
        """
        host = _OwnedHost()
        settings = load_settings(ENVIRONMENT, {}).settings

        async with _client(_app(host)) as client:
            response = await client.post(
                "/settings",
                content=_form(settings, groundstation_url="ws://" + "a" * 300),
                headers=_FORM_HEADERS,
            )

        assert response.status_code == 400
        assert str(GROUNDSTATION_URL_MAX_LENGTH) in response.text
        assert "Nothing was changed." in response.text
        assert "overrides file" not in response.text
        assert "start the application again" not in response.text
        assert _store().load() == {}
        assert host.owner.effective_url == ENVIRONMENT[f"{ENV_PREFIX}GROUNDSTATION_URL"]

    @pytest.mark.asyncio
    async def test_a_write_that_lands_first_is_merged_against_not_over(self) -> None:
        """The page's map is computed from the file the write finds, not the request.

        Home Assistant's control writes the same file. A map computed when the
        POST arrived would be a map from before that write, and committing it
        would silently drop whatever it had pinned — here a credential the page
        never retyped and therefore carries over from the stored overrides.
        """
        host = RecordingHost()
        host.interleaved = {"groundstation_credential": "rotated-elsewhere"}
        settings = load_settings(ENVIRONMENT, {}).settings

        async with _client(_app(host)) as client:
            response = await client.post(
                "/settings",
                content=_form(settings, idle_seconds="9.0"),
                headers=_FORM_HEADERS,
            )

        assert response.status_code == 303
        assert host.submitted == [
            {"groundstation_credential": "rotated-elsewhere", "idle_seconds": "9.0"},
        ]
        assert _store().load()["groundstation_credential"] == "rotated-elsewhere"


class TestAPageWithNothingBehindIt:
    """`create_app(application=None)` is supported, and is not a way round the owner."""

    @pytest.mark.asyncio
    async def test_an_address_change_is_refused_rather_than_persisted(self) -> None:
        """Persisting one nothing adopted is what the next start would read.

        There is no running source here to open a session at the new address,
        and `apply_settings_change` persists first — which for this one setting
        is exactly the ordering `groundstation_url` exists to remove. Writing it
        anyway would be a second write path around the owner.
        """
        settings = load_settings(ENVIRONMENT, {}).settings

        async with _client(_app()) as client:
            response = await client.post(
                "/settings",
                content=_form(
                    settings,
                    groundstation_url="ws://192.0.2.30:8080/v1/session",
                ),
                headers=_FORM_HEADERS,
            )

        assert response.status_code == 400
        assert "cannot be changed from here" in response.text
        assert _store().load() == {}

    @pytest.mark.asyncio
    async def test_every_other_setting_still_saves(self) -> None:
        """The refusal is about the one setting, not about the mode."""
        settings = load_settings(ENVIRONMENT, {}).settings

        async with _client(_app()) as client:
            response = await client.post(
                "/settings",
                content=_form(settings, idle_seconds="9.0"),
                headers=_FORM_HEADERS,
            )

        assert response.status_code == 303
        assert _store().load() == {"idle_seconds": "9.0"}

    @pytest.mark.asyncio
    async def test_resubmitting_the_address_unchanged_is_not_a_change(self) -> None:
        """An ordinary save carries every field, this one included, and still saves."""
        settings = load_settings(ENVIRONMENT, {}).settings

        async with _client(_app()) as client:
            response = await client.post(
                "/settings",
                content=_form(
                    settings,
                    groundstation_url=ENVIRONMENT[f"{ENV_PREFIX}GROUNDSTATION_URL"],
                    idle_seconds="9.0",
                ),
                headers=_FORM_HEADERS,
            )

        assert response.status_code == 303
        assert _store().load() == {"idle_seconds": "9.0"}


class _OwnedHost(RecordingHost):
    """A host whose submissions go through a real `GroundstationUrlOwner`.

    The stand-in above records what the page handed over, which is what most of
    these tests are about. This one is for the cases where *which* refusal the
    operator reads is the thing under test, and that is decided inside the owner
    rather than by the page.
    """

    def __init__(self) -> None:
        """Assemble an owner over the same store the page writes through."""
        super().__init__()
        store = _store()
        self.owner = GroundstationUrlOwner(
            store=store,
            resolution=load_settings(ENVIRONMENT, store.load()),
            source=ReplaceableRemoteSource(),
            factory=_no_remote_source,
            environ=ENVIRONMENT,
            apply_live=self.apply_live,
        )

    async def apply_settings(self, merge: OverrideMerge) -> Resolution:
        """Hand the computation to the owner, as the application does.

        Args:
            merge: What to make of the stored overrides.

        Returns:
            The settings in effect afterwards.
        """
        return await self.owner.submit_merged(merge)


async def _no_remote_source(settings: Settings) -> None:
    """Build no session, which is what a page test has no need of.

    Args:
        settings: The candidate configuration, unread.

    Returns:
        `None`, the local-only composition — so nothing here opens a socket.
    """
    del settings
    return
