"""Starting and configuring a stock robot, with nothing configured at all.

Stock-robot installation REQ-101, REQ-102 and REQ-103, driven end to end without
a robot, a Home Assistant instance or a groundstation. The three are one story
told from three sides and it is worth stating the whole of it once, because each
half is easy to satisfy in a way that breaks the other:

**REQ-101** says the application starts and serves its settings interface with no
announced identity. It used to refuse, and on a robot reached only through the
surfaces its shipped image exposes that refusal hid the one surface capable of
supplying the value.

**REQ-102** says nothing is announced while the identity is unresolved, which is
what makes REQ-101 safe. Home Assistant keys a device on the announced identity,
so the hazard the old refusal guarded is announcing under a *wrong* one — and
announcing under none is the absence of that hazard rather than a weaker form of
it. The embargo is structural: `build_application` constructs no `ServerState`,
no entity, no listener and no mDNS record, so the tests below assert an
*absence of machinery* rather than a suppressed call.

**REQ-103** is the same shape for the groundstation. An unresolved address or
credential opens no session, which every surface reports as `unconfigured`
rather than as failed, and the first one an operator supplies is adopted through
the replacement transition REQ-095 already owns rather than through a second
path written for the first time.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Final

import httpx
import pytest
from satellite_support import (
    FakeAudio,
    FakeMedia,
    FakeMotion,
    FakePerception,
    FakeRobot,
)

from reachy_mini_ha_satellite.adapters.network import NetworkError, NetworkIdentity
from reachy_mini_ha_satellite.adapters.perception_source import FallbackPerception
from reachy_mini_ha_satellite.behaviour import SatelliteBehaviour
from reachy_mini_ha_satellite.config import (
    ENV_PREFIX,
    IDENTITY_SETTING,
    OVERRIDES_FILENAME,
    ConfigurationError,
    OverrideStore,
    Resolution,
    Settings,
    configuration_report,
    groundstation_is_resolved,
    identity_is_resolved,
    load_settings,
)
from reachy_mini_ha_satellite.groundstation_url import (
    GroundstationUrlOwner,
    ReplaceableRemoteSource,
)
from reachy_mini_ha_satellite.main import (
    AdvertisementService,
    EsphomeService,
    SatelliteApplication,
    VolumeService,
    WebService,
    build_application,
    build_perception_source,
    build_remote_source,
)
from reachy_mini_ha_satellite.motor_control import TorqueConfirmationSupport
from reachy_mini_ha_satellite.ports import SourceSelection
from reachy_mini_ha_satellite.web import (
    CLEARED_IDENTITY_HEADING,
    UNCONFIGURED_HEADING,
    create_app,
    render_settings_page,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from starlette.applications import Starlette

    from reachy_mini_ha_satellite.adapters.perception_source import ConnectableSource

# The RFC 5737 documentation range, and a placeholder credential. This
# repository is public — see the root `AGENTS.md`.
_GROUNDSTATION: Final = "ws://192.0.2.10:8080/v1/session"
_CREDENTIAL: Final = "example-credential"

# Where these tests keep state. Nothing under it is read by an assembly with an
# unresolved identity, which is itself part of what REQ-102 promises: the
# wake-word models are loaded by `build_server_state`, and that is not called.
_STATE_DIR: Final = Path("/reachy-satellite-bootstrap")

# A stock robot on its very first start: the daemon's environment carries
# nothing this application reads. `advertise` is left at its default `true`, so
# a mDNS record appearing would be this application's own decision rather than a
# setting having switched it off.
STOCK: Final[Mapping[str, str]] = {
    f"{ENV_PREFIX}STATE_DIR": str(_STATE_DIR),
    f"{ENV_PREFIX}WEB_ENABLED": "true",
}

# The same robot once somebody has named it.
NAMED: Final[Mapping[str, str]] = {**STOCK, f"{ENV_PREFIX}DEVICE_NAME": "reachy-mini-1"}

_FORM_HEADERS: Final = {"content-type": "application/x-www-form-urlencoded"}


def _identity() -> NetworkIdentity:
    """What the robot would announce on the network, from documentation ranges.

    Returns:
        The identity, supplied rather than discovered so that nothing reads a
        network interface.
    """
    return NetworkIdentity(
        interface="eth0",
        ip_address="192.0.2.20",
        mac_address="02:00:5e:10:00:00",
    )


def _services(application: SatelliteApplication) -> set[type[object]]:
    """Name the kinds of service an assembly produced.

    Args:
        application: What `build_application` returned.

    Returns:
        The service classes, which is the level REQ-102 is about: an announcing
        surface is a kind of object rather than a flag on one.
    """
    return {type(service) for service in application.services}


def _page(application: SatelliteApplication, resolution: Resolution) -> Starlette:
    """Serve the real settings interface over the real application.

    Args:
        application: The assembled application.
        resolution: The settings it started with.

    Returns:
        The ASGI application, resolved against the stock environment so that
        nothing reads the process environment.
    """
    return create_app(
        resolution=resolution,
        store=OverrideStore(_STATE_DIR / OVERRIDES_FILENAME),
        application=application,
        environ=STOCK,
    )


def _client(app: Starlette) -> httpx.AsyncClient:
    """Speak HTTP to an ASGI application in memory, opening no socket.

    Args:
        app: What to drive.

    Returns:
        The client.
    """
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://robot.invalid",
    )


class TestAnUnresolvedIdentityStartsTheApplication:
    """REQ-101: it starts, it serves its settings interface, and it says so."""

    @pytest.mark.asyncio
    async def test_a_stock_robot_assembles_with_nothing_configured(self) -> None:
        """The refusal this replaces happened before any of this was reached."""
        resolution = load_settings(STOCK, {})

        application = await build_application(
            resolution,
            FakeRobot(),
            identity=_identity(),
        )

        assert not identity_is_resolved(resolution.settings)
        assert application.status()["identity"] == "unresolved"

    @pytest.mark.asyncio
    async def test_the_settings_interface_is_served(self) -> None:
        """Which is the whole point: it is the surface the identity arrives on."""
        application = await build_application(
            load_settings(STOCK, {}),
            FakeRobot(),
            identity=_identity(),
        )

        assert WebService in _services(application)

    @pytest.mark.asyncio
    async def test_the_health_surface_answers_and_names_the_state(self) -> None:
        """An operator has to be able to tell unconfigured from broken."""
        resolution = load_settings(STOCK, {})
        application = await build_application(
            resolution,
            FakeRobot(),
            identity=_identity(),
        )

        async with _client(_page(application, resolution)) as client:
            body = (await client.get("/status")).json()

        assert body["running"] is True
        assert body["identity"] == "unresolved"
        assert body["announcing"] is False

    @pytest.mark.asyncio
    async def test_the_page_says_the_identity_is_unresolved(self) -> None:
        """Explicitly, rather than by rendering an empty name and no comment."""
        resolution = load_settings(STOCK, {})
        application = await build_application(
            resolution,
            FakeRobot(),
            identity=_identity(),
        )

        async with _client(_page(application, resolution)) as client:
            page = (await client.get("/")).text

        assert UNCONFIGURED_HEADING in page
        assert "Nothing is announced to Home Assistant until" in page

    @pytest.mark.asyncio
    async def test_an_identity_supplied_from_the_page_is_adopted(
        self,
        fs: object,
    ) -> None:
        """REQ-101's second scenario: no shell, and no reinstallation.

        Args:
            fs: The in-memory filesystem the overrides file is written into.
        """
        del fs
        resolution = load_settings(STOCK, {})
        application = await build_application(
            resolution,
            FakeRobot(),
            identity=_identity(),
        )

        async with _client(_page(application, resolution)) as client:
            response = await client.post(
                "/settings",
                content="device_name=reachy-mini-1",
                headers=_FORM_HEADERS,
            )

        assert response.status_code == 303
        stored = OverrideStore(_STATE_DIR / OVERRIDES_FILENAME).load()
        assert stored["device_name"] == "reachy-mini-1"
        assert identity_is_resolved(load_settings(STOCK, stored).settings)

    @pytest.mark.asyncio
    async def test_an_identity_the_contract_rejects_is_refused(
        self,
        fs: object,
    ) -> None:
        """REQ-101's third scenario. The constraint is stated and nothing is kept.

        Args:
            fs: The in-memory filesystem, so a write would be observable.
        """
        del fs
        resolution = load_settings(STOCK, {})
        application = await build_application(
            resolution,
            FakeRobot(),
            identity=_identity(),
        )

        async with _client(_page(application, resolution)) as client:
            response = await client.post(
                "/settings",
                content=f"device_name={'r' * 65}",
                headers=_FORM_HEADERS,
            )
            after = (await client.get("/")).text

        assert response.status_code == 400
        assert "DEVICE_NAME" in response.text
        assert OverrideStore(_STATE_DIR / OVERRIDES_FILENAME).load() == {}
        # Still usable for another attempt, which is the half of that scenario a
        # refusal that took the page down with it would fail.
        assert UNCONFIGURED_HEADING in after


class TestNothingIsAnnouncedWhileTheIdentityIsUnresolved:
    """REQ-102: the embargo, asserted as machinery that was never built."""

    @pytest.mark.asyncio
    async def test_no_announcing_service_exists(self) -> None:
        """Neither the ESPHome listener nor the mDNS record Home Assistant finds."""
        application = await build_application(
            load_settings(STOCK, {}),
            FakeRobot(),
            identity=_identity(),
        )

        assert _services(application) == {VolumeService, WebService}

    @pytest.mark.asyncio
    async def test_no_server_state_is_built_at_all(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The strongest form of the claim: the state is never constructed.

        Every announced entity, the pipeline tap and both announcing services
        are built over one `ServerState`. Making its construction fatal turns
        "nothing announced" from something a reader has to check service by
        service into one assertion that keeps holding as services are added.

        Args:
            monkeypatch: Used to make building the announcing state a failure.
        """
        import reachy_mini_ha_satellite.main as satellite_main

        def _refuse(*_args: object, **_kwargs: object) -> None:
            message = "the announcing state must not be built"
            raise AssertionError(message)

        monkeypatch.setattr(satellite_main, "build_server_state", _refuse)

        application = await build_application(
            load_settings(STOCK, {}),
            FakeRobot(),
            identity=_identity(),
        )

        assert application.status()["announcing"] is False

    @pytest.mark.asyncio
    async def test_repeated_starts_announce_nothing_every_time(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REQ-102's third scenario: several runs, and still no first announcement.

        Args:
            monkeypatch: Used to make building the announcing state a failure.
        """
        import reachy_mini_ha_satellite.main as satellite_main

        def _refuse(*_args: object, **_kwargs: object) -> None:
            message = "the announcing state must not be built"
            raise AssertionError(message)

        monkeypatch.setattr(satellite_main, "build_server_state", _refuse)

        for _ in range(3):
            application = await build_application(
                load_settings(STOCK, {}),
                FakeRobot(),
                identity=_identity(),
            )
            assert _services(application) == {VolumeService, WebService}

    @pytest.mark.asyncio
    async def test_a_robot_with_no_network_yet_still_serves_its_settings_page(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The order these are built in decides whether REQ-101 holds at all.

        `discover_network_identity` refuses a machine with no default route, no
        IPv4 address or no hardware address, and every word of its reason is
        about announcing: a satellite advertising at no address would be a
        device Home Assistant found and could not reach. Run before the branch
        that decides whether anything announces, it would refuse to assemble an
        application that announces nothing — on a robot that has been given no
        identity, has not been put on a network yet, and whose settings
        interface is the surface an operator would fix both from.

        Args:
            monkeypatch: Used to make network discovery fail as it does on a
                machine with no default route.
        """
        import reachy_mini_ha_satellite.main as satellite_main

        def _no_network(**_kwargs: object) -> NetworkIdentity:
            message = "no default network interface was found"
            raise NetworkError(message)

        monkeypatch.setattr(satellite_main, "discover_network_identity", _no_network)

        application = await build_application(load_settings(STOCK, {}), FakeRobot())

        assert _services(application) == {VolumeService, WebService}
        assert application.status()["announcing"] is False

    @pytest.mark.asyncio
    async def test_the_dead_end_that_remains_is_named_in_the_boot_log(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A page that is not served cannot be where the operator sets the value.

        `web_enabled` is environment-only, so a robot with it switched off and
        no identity has no configuration surface at all — the one arrangement
        REQ-101 cannot rescue, because the setting that disabled the rescue is
        itself outside the layer the rescue writes. Pointing at the settings
        interface there would be a sentence this repository calls a defect.

        Args:
            caplog: Where the boot log is captured.
        """
        environ = {**STOCK, f"{ENV_PREFIX}WEB_ENABLED": "false"}

        with caplog.at_level(logging.WARNING):
            application = await build_application(
                load_settings(environ, {}),
                FakeRobot(),
                identity=_identity(),
            )

        assert _services(application) == {VolumeService}
        assert "the daemon's environment" in caplog.text
        assert "Set it on the settings interface" not in caplog.text

    @pytest.mark.filesystem
    @pytest.mark.asyncio
    async def test_a_resolved_identity_announces_exactly_it(self) -> None:
        """REQ-102's second scenario, and the proof the embargo is not a wall.

        This one reads the wake-word models the wheel ships, because
        `build_server_state` is exactly the step the unresolved case skips and a
        fake asset directory would pin whatever the fake was told to contain.
        """
        application = await build_application(
            load_settings(NAMED, {}),
            FakeRobot(),
            identity=_identity(),
        )

        esphome = [
            service
            for service in application.services
            if isinstance(service, EsphomeService)
        ]
        advertisements = [
            service
            for service in application.services
            if isinstance(service, AdvertisementService)
        ]

        assert len(esphome) == 1
        assert len(advertisements) == 1
        assert esphome[0]._state.name == "reachy-mini-1"
        assert application.status()["announcing"] is True
        assert application.status()["identity"] == "resolved"

    @pytest.mark.asyncio
    async def test_the_microphone_is_not_taken_when_nothing_consumes_it(
        self,
    ) -> None:
        """Capture feeds the announcing surface, and there is not one.

        Starting the daemon's recording pipeline would take the robot's
        microphone and accumulate audio nothing ever reads, for as long as an
        unconfigured robot is left running — which REQ-102 says may be
        indefinitely.
        """
        audio = FakeAudio()
        stop = asyncio.Event()
        elapsed = iter(range(1000))

        async def _one_tick(seconds: float) -> None:
            """Stand in for the loop's wait, ending the loop after one tick.

            Args:
                seconds: How long the loop wanted to wait, ignored.
            """
            del seconds
            stop.set()
            await asyncio.sleep(0)

        application = SatelliteApplication(
            settings=load_settings(STOCK, {}).settings,
            audio=audio,
            motion=FakeMotion(),
            perception=FakePerception(),
            behaviour=SatelliteBehaviour(now=0.0),
            announced_identity=None,
            clock=lambda: float(next(elapsed)),
            sleep=_one_tick,
        )

        await application.run(stop)

        assert audio.started == 0

    def test_the_page_says_so_when_an_identity_is_set_and_nothing_announces(
        self,
    ) -> None:
        """The middle state, rendered — an identity, and still no announcement.

        Rendered directly rather than driven through the application, because
        the state under test is the one where the page's two inputs disagree:
        the resolution names an identity and the process built no announcing
        surface. Only the application can report the second, so the page has to
        be handed it rather than inferring it.
        """
        resolution = load_settings(NAMED, {})

        page = render_settings_page(
            resolution,
            configuration_report(resolution),
            status={},
            overrides_path=str(_STATE_DIR / OVERRIDES_FILENAME),
            announcing=False,
            announced_identity=None,
        )

        assert "Nothing is announced to Home Assistant yet." in page
        assert "started without one" in page
        assert UNCONFIGURED_HEADING not in page

    def test_a_changed_identity_is_not_reported_as_the_announced_one(self) -> None:
        """The other direction of the same gap, and the one that reads as done.

        The identity is restart-bound, so a running satellite saved with a new
        `device_name` goes on announcing the old one and Home Assistant stays
        keyed on it. A page that read the sentence "Announced to Home Assistant
        as ..." off the configuration would describe a rename that has not
        happened — on the page whose standing hazard is that very key, and
        whose reader would then not restart.
        """
        renamed = load_settings(NAMED, {IDENTITY_SETTING: "reachy-mini-2"})

        page = render_settings_page(
            renamed,
            configuration_report(renamed),
            status={},
            overrides_path=str(_STATE_DIR / OVERRIDES_FILENAME),
            announcing=True,
            announced_identity="reachy-mini-1",
        )

        assert "Announced to Home Assistant as <code>reachy-mini-1</code>." in page
        assert "The configured identity is now <code>reachy-mini-2</code>" in page
        assert "still keyed on the one this application started with" in page

    def test_clearing_a_live_identity_does_not_claim_an_embargo_it_has_not_got(
        self,
    ) -> None:
        """The fourth state, and the one where getting it wrong lies outward.

        An unresolved identity is a state now, so clearing `device_name` on a
        robot that is announcing resolves, is persisted, and is badged "needs a
        restart" like any other restart-bound change — while the process goes on
        announcing under the identity it was built with. A page that showed the
        embargo there would tell an operator no device was registered while Home
        Assistant was still connected to one.

        It is not refused, and that is deliberate: the identity can come from an
        override alone, so refusing would make *Reset* impossible on such a
        robot, and stopping it to get round that starts it again with the same
        override — a dead end of exactly the kind this change exists to remove.
        """
        cleared = load_settings(NAMED, {IDENTITY_SETTING: ""})

        page = render_settings_page(
            cleared,
            configuration_report(cleared),
            status={},
            overrides_path=str(_STATE_DIR / OVERRIDES_FILENAME),
            announcing=True,
            announced_identity="reachy-mini-1",
        )

        assert not identity_is_resolved(cleared.settings)
        assert "Nothing is announced to Home Assistant until" not in page
        assert UNCONFIGURED_HEADING not in page
        assert CLEARED_IDENTITY_HEADING in page
        assert "still announcing under the one it started with" in page

    @pytest.mark.asyncio
    async def test_an_identity_resolved_after_this_process_started_is_not_announced(
        self,
    ) -> None:
        """The gap between "configured" and "announcing", reported rather than hidden.

        `device_name` is restart-bound, so a value supplied a moment ago is
        resolved configuration and still nothing announced. Reporting the
        settings would tell an operator their robot was on Home Assistant while
        the embargo was in force.
        """
        application = await build_application(
            load_settings(STOCK, {}),
            FakeRobot(),
            identity=_identity(),
        )

        application.apply_live(load_settings(NAMED, {}).settings)

        assert application.status()["identity"] == "resolved"
        assert application.status()["announcing"] is False


class TestTheStateAStockRobotIsActuallyIn:
    """Both halves of a stock robot's first boot, which is one robot.

    REQ-099/100 and REQ-101/102/103 were implemented as separate changes, and
    each could only test its own half against a robot that was otherwise
    ordinary: a configured robot on a stock daemon, or an unconfigured robot on
    a daemon that confirms torque. **Neither is a robot anybody owns.** A Reachy
    Mini out of its box has no announced identity *and* a released daemon with
    no correlated-torque surface, so the two decisions land in the same process
    on the same boot.

    They are independent, and this is where that is pinned. The composition root
    makes them separately — one probes the handle, the other reads the settings
    — and reading either from the other would be wrong on exactly this robot:
    a stock daemon says nothing about whether somebody has named the robot, and
    an unnamed robot says nothing about what its daemon can confirm.
    """

    @pytest.mark.asyncio
    async def test_it_reports_being_unconfigured_and_ungated_at_once(self) -> None:
        """The first boot of a robot out of its box, in one status document."""
        resolution = load_settings(STOCK, {})
        robot = FakeRobot(
            torque_confirmation_support=TorqueConfirmationSupport.ABSENT,
        )

        application = await build_application(
            resolution,
            robot,
            identity=_identity(),
        )
        status = application.status()

        assert status["identity"] == "unresolved"
        assert status["announcing"] is False
        assert status["announced_as"] is None
        assert status["remote"] == "unconfigured"
        assert status["motion_gating"] == {
            "mode": "ungated",
            "reason": "daemon_confirmation_absent",
        }
        # No coordinator, so no `motors` key — which is the reason
        # `motion_gating` is top-level rather than nested under it.
        assert "motors" not in status
        assert application.motor_groups is None
        assert _services(application) == {VolumeService, WebService}

    @pytest.mark.asyncio
    async def test_naming_it_does_not_gate_its_motion(self) -> None:
        """The two decisions are independent, and one of the two ways to show it.

        An identity is what an operator supplies from the settings page; it says
        nothing about what the robot's daemon can confirm. A robot named on a
        stock daemon announces, and still commands motion ungated.
        """
        robot = FakeRobot(
            torque_confirmation_support=TorqueConfirmationSupport.ABSENT,
        )

        application = await build_application(
            load_settings(NAMED, {}),
            robot,
            identity=_identity(),
        )
        status = application.status()

        assert status["announcing"] is True
        assert status["motion_gating"]["mode"] == "ungated"  # type: ignore[index]  # `status()` is a `dict[str, object]`; this key's shape is asserted whole above
        assert application.motor_groups is None

    @pytest.mark.asyncio
    async def test_a_confirming_daemon_does_not_announce_an_unnamed_robot(
        self,
    ) -> None:
        """And the other way, which is the direction that would be unsafe.

        A daemon that can confirm torque says nothing about whether anybody has
        named the robot. Deriving the embargo from the probe would announce an
        unconfigured robot to Home Assistant the moment its daemon was capable
        enough, which is REQ-102 failing on the robot the fork is installed on.
        """
        robot = FakeRobot(
            torque_confirmation_support=TorqueConfirmationSupport.AVAILABLE,
        )

        application = await build_application(
            load_settings(STOCK, {}),
            robot,
            identity=_identity(),
        )
        status = application.status()

        assert status["announcing"] is False
        assert status["motion_gating"]["mode"] == "confirmed"  # type: ignore[index]  # as above: the shape is asserted whole in the first test of this class
        assert application.motor_groups is not None
        assert _services(application) == {VolumeService, WebService}
        await application.aclose()


class TestRemotePerceptionIsOptionalAtFirstStart:
    """REQ-103: unconfigured is not failed, and the first one adopts normally."""

    @pytest.mark.parametrize(
        "environ",
        [
            {},
            {f"{ENV_PREFIX}GROUNDSTATION_URL": _GROUNDSTATION},
            {f"{ENV_PREFIX}GROUNDSTATION_CREDENTIAL": _CREDENTIAL},
        ],
        ids=["neither", "address only", "credential only"],
    )
    def test_no_session_client_is_built(self, environ: Mapping[str, str]) -> None:
        """One half of a groundstation opens nothing, so nothing is constructed.

        Args:
            environ: The half-configured environments, and the empty one.
        """
        settings = load_settings({**STOCK, **environ}, {}).settings

        assert not groundstation_is_resolved(settings)
        assert build_remote_source(settings, FakeMedia()) is None

    @pytest.mark.asyncio
    async def test_the_health_surface_says_unconfigured_rather_than_failed(
        self,
    ) -> None:
        """The distinction this repository refuses to collapse anywhere else."""
        resolution = load_settings(STOCK, {})
        application = await build_application(
            resolution,
            FakeRobot(),
            identity=_identity(),
        )

        async with _client(_page(application, resolution)) as client:
            body = (await client.get("/status")).json()

        assert body["remote"] == "unconfigured"

    def test_an_unconfigured_remote_selection_runs_on_local_detection(self) -> None:
        """REQ-103's first scenario, for the selection a stock robot defaults to.

        `remote` says "the groundstation answers" and there is no groundstation,
        so until one is supplied the robot runs on its own detector — which is
        what the requirement asks for in as many words. The composition is the
        fallback rather than `local`, and that is not a detail: it holds the
        same `ReplaceableRemoteSource` every other composition does, so the
        address owner can swap a source in behind it and the operator's `remote`
        choice reasserts itself the moment a session exists. Rewriting the
        composition to `local` would break exactly that.
        """
        environ = {**STOCK, f"{ENV_PREFIX}LOCAL_MODEL_PATH": "/models/face.onnx"}
        settings = load_settings(environ, {}).settings
        remote = ReplaceableRemoteSource(build_remote_source(settings, FakeMedia()))

        composed = build_perception_source(settings, FakeMedia(), remote=remote)

        assert settings.detection_source is SourceSelection.REMOTE
        assert isinstance(composed, FallbackPerception)
        assert remote.delegate is None
        assert not remote.connected

    def test_a_configured_remote_selection_is_left_alone(self) -> None:
        """The fallback is for the unconfigured case and not a new default.

        An operator who selected `remote` on a robot that has a groundstation
        chose not to spend the robot's cores on a local model, and that choice
        is untouched: the substitution is keyed on the groundstation being
        unresolved, not on the model path being set.
        """
        environ = {
            **STOCK,
            f"{ENV_PREFIX}LOCAL_MODEL_PATH": "/models/face.onnx",
            f"{ENV_PREFIX}GROUNDSTATION_URL": _GROUNDSTATION,
            f"{ENV_PREFIX}GROUNDSTATION_CREDENTIAL": _CREDENTIAL,
        }
        settings = load_settings(environ, {}).settings
        remote = ReplaceableRemoteSource(build_remote_source(settings, FakeMedia()))

        composed = build_perception_source(settings, FakeMedia(), remote=remote)

        assert composed is remote

    def test_with_no_model_there_is_nothing_local_to_run_on(self) -> None:
        """The limit REQ-103 meets on a stock robot, admitted rather than faked.

        The face-detection weights are not shipped in this wheel — they are
        somebody else's model under somebody else's terms, which is the licensing
        limit `local_model_path` records. A fallback composed with no path would
        be a detector that fails to load on every pass and logs about it, which
        is worse than seeing nothing and saying so: `/status` reports the remote
        detector as `unconfigured`, and the settings page says what to set.
        """
        settings = load_settings(STOCK, {}).settings
        remote = ReplaceableRemoteSource(build_remote_source(settings, FakeMedia()))

        composed = build_perception_source(settings, FakeMedia(), remote=remote)

        assert settings.local_model_path == ""
        assert composed is remote
        assert not composed.latest().faces

    @pytest.mark.asyncio
    async def test_supplying_both_adopts_through_the_replacement_owner(
        self,
        fs: object,
    ) -> None:
        """REQ-103's second scenario, over the transition REQ-095 already owns.

        Args:
            fs: The in-memory filesystem the durable commit lands in.
        """
        del fs
        owner = _owner(STOCK)
        # Read into locals rather than asserted twice on the property: mypy
        # narrows a property access the way it narrows an attribute, so the
        # second assertion would make everything after it unreachable.
        before = owner.remote_available

        resolved = await owner.submit(
            {
                "groundstation_url": _GROUNDSTATION,
                "groundstation_credential": _CREDENTIAL,
            },
        )

        assert not before
        assert owner.remote_available
        assert resolved.settings.groundstation_url == _GROUNDSTATION
        assert owner.effective_url == _GROUNDSTATION

    @pytest.mark.asyncio
    async def test_supplying_only_the_missing_credential_adopts_too(
        self,
        fs: object,
    ) -> None:
        """The address was already in the daemon's environment; the credential was not.

        It changes no address, and it reaches the transition anyway: what
        selects that path is `_opens_a_different_session`, which reads the
        credential too. It has to end with a source, because the reason there
        was none is exactly the thing the submission supplied.

        Args:
            fs: The in-memory filesystem the durable commit lands in.
        """
        del fs
        environ = {**STOCK, f"{ENV_PREFIX}GROUNDSTATION_URL": _GROUNDSTATION}
        owner = _owner(environ)
        before = owner.remote_available

        await owner.submit({"groundstation_credential": _CREDENTIAL})

        assert not before
        assert owner.remote_available

    @pytest.mark.asyncio
    async def test_clearing_the_address_retires_the_source(self, fs: object) -> None:
        """Un-configuring is a state now, so it is not the refusal it used to be.

        Args:
            fs: The in-memory filesystem the durable commit lands in.
        """
        del fs
        environ = {
            **STOCK,
            f"{ENV_PREFIX}GROUNDSTATION_URL": _GROUNDSTATION,
            f"{ENV_PREFIX}GROUNDSTATION_CREDENTIAL": _CREDENTIAL,
        }
        owner = _owner(environ)
        before = owner.remote_available

        await owner.submit({"groundstation_url": ""})

        assert before
        assert not owner.remote_available
        assert owner.effective_url == ""

    @pytest.mark.asyncio
    async def test_clearing_the_credential_retires_the_source_too(
        self,
        fs: object,
    ) -> None:
        """Both halves count, so removing either one is an un-configuration.

        The released transition selected itself on the address alone, and that
        is not the question a session turns on. Clearing the credential leaves
        the address exactly as it was, so the address test sends it down the
        branch that persists and adopts — which would store an unresolved
        groundstation while the running client went on answering under the
        secret the operator had just revoked, with every surface reporting the
        source as available. That is *unconfigured* collapsing into
        *connected*, which is the distinction REQ-103 exists to hold.

        Args:
            fs: The in-memory filesystem the durable commit lands in.
        """
        del fs
        environ = {
            **STOCK,
            f"{ENV_PREFIX}GROUNDSTATION_URL": _GROUNDSTATION,
            f"{ENV_PREFIX}GROUNDSTATION_CREDENTIAL": _CREDENTIAL,
        }
        owner = _owner(environ)
        before = owner.remote_available

        resolved = await owner.submit({"groundstation_credential": ""})

        assert before
        assert not owner.remote_available
        assert not groundstation_is_resolved(resolved.settings)
        # The address is untouched: what was removed is the credential, and a
        # transition that also cleared the address would be doing more than the
        # submission asked for.
        assert owner.effective_url == _GROUNDSTATION

    @pytest.mark.asyncio
    async def test_rotating_a_credential_reopens_the_session(
        self,
        fs: object,
    ) -> None:
        """The third credential submission, and the same defect as the other two.

        A rotation that took effect only at the next start would leave the robot
        authenticating with the secret the operator had just revoked — which is
        clearing it, one step along. So it goes through the transition too: the
        preceding source is retired and a new one built with the new value, and
        `groundstation_credential` is in `LIVE_SETTINGS` so the settings page
        does not tell an operator to restart for something already adopted.

        Args:
            fs: The in-memory filesystem the durable commit lands in.
        """
        del fs
        environ = {
            **STOCK,
            f"{ENV_PREFIX}GROUNDSTATION_URL": _GROUNDSTATION,
            f"{ENV_PREFIX}GROUNDSTATION_CREDENTIAL": _CREDENTIAL,
        }
        owner = _owner(environ)
        retired = owner._source.delegate

        resolved = await owner.submit(
            {"groundstation_credential": "another-credential"},
        )

        assert owner.remote_available
        assert owner._source.delegate is not retired
        assert resolved.settings.groundstation_credential.get_secret_value() == (
            "another-credential"
        )
        # The address is untouched, so the transition rebuilt the session rather
        # than pointing the robot somewhere else.
        assert owner.effective_url == _GROUNDSTATION

    @pytest.mark.asyncio
    async def test_retiring_into_nothing_for_any_other_reason_is_still_refused(
        self,
        fs: object,
    ) -> None:
        """0020's guarantee, kept: only an unconfigured submission may retire.

        Changing the address together with a restart-bound setting that decides
        whether a session exists at all would close the running source, install
        nothing, commit and report success. That is still refused, because the
        submitted configuration still names a groundstation.

        Args:
            fs: The in-memory filesystem, so a commit would be observable.
        """
        del fs
        environ = {
            **STOCK,
            f"{ENV_PREFIX}GROUNDSTATION_URL": _GROUNDSTATION,
            f"{ENV_PREFIX}GROUNDSTATION_CREDENTIAL": _CREDENTIAL,
        }
        owner = _owner(environ)

        with pytest.raises(ConfigurationError) as raised:
            await owner.submit(
                {
                    "groundstation_url": "ws://192.0.2.11:8080/v1/session",
                    "face_tracking_enabled": "false",
                },
            )

        assert "opens no groundstation session" in str(raised.value)
        assert owner.effective_url == _GROUNDSTATION
        assert owner.remote_available


def _owner(environ: Mapping[str, str]) -> GroundstationUrlOwner:
    """Assemble the real address owner over the real source factory.

    The production factory rather than a stand-in, because what these tests are
    about is that the first groundstation an operator supplies travels the same
    path as a replacement. A stand-in would prove the transition ran and say
    nothing about what it built.

    Args:
        environ: The environment candidates resolve against.

    Returns:
        The owner, over a chain that has not been started — so nothing it builds
        opens a session.
    """
    store = OverrideStore(_STATE_DIR / OVERRIDES_FILENAME)
    resolution = load_settings(environ, {})
    media = FakeMedia()

    async def _factory(candidate: Settings) -> ConnectableSource | None:
        """Build a source for a candidate exactly as the composition root does.

        Args:
            candidate: What the submission resolves to.

        Returns:
            The source, or `None` for a configuration that opens no session.
        """
        return build_remote_source(candidate, media)

    return GroundstationUrlOwner(
        store=store,
        resolution=resolution,
        source=ReplaceableRemoteSource(
            build_remote_source(resolution.settings, media),
        ),
        factory=_factory,
        environ=environ,
    )
