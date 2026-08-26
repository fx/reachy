"""The composition root: what runs, in what order, and what shutdown leaves behind.

**The loop and its lifecycle** are driven entirely against the fakes change 0012
shipped — no robot, no socket, no thread pool and no clock — which is what lets
ha-satellite REQ-050 be asserted rather than described: movement stops, the media
interface is released, and the process leaves. Those are ordinary unit tests.

**The wiring is not, and three groups of tests here say so rather than implying
it.** Each does real input or output, each carries the marker that declares it,
and each is that way because the alternative would be a test of a fake:

* the assembly tests read the wheel's own wake-word models and sounds —
  `@pytest.mark.filesystem`, because REQ-044 is that those files load off the
  robot without a network, and a fake asset directory would pin whatever the
  fake was told to contain;
* `TestStartingUpFromTheEnvironment` binds the real ESPHome port on the loopback
  interface — `@pytest.mark.enable_socket`, because a startup path that never
  bound anything would prove nothing about the one thing startup has to do;
* the thread-boundary tests start real daemon threads for the default starter
  and saturated-queue shutdown. They are not faked or injected because what is
  under test is the thread's own start or exit behaviour.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import threading
from contextvars import Context
from pathlib import Path
from queue import Empty, Full
from typing import TYPE_CHECKING, Any, Final, cast

import httpx
import numpy as np
import pytest

# pylint: disable=no-name-in-module
from aioesphomeapi.api_pb2 import (  # type: ignore[attr-defined]  # generated protobuf module, which mypy cannot see the message classes inside
    ListEntitiesRequest,
    SubscribeHomeAssistantStatesRequest,
    SwitchCommandRequest,
    SwitchStateResponse,
)
from satellite_support import (
    FakeAudio,
    FakeCapture,
    FakeMicroWakeWord,
    FakeMotion,
    FakePerception,
    FakeRobot,
    FakeWakeWordFeatures,
    ManualClock,
    available_wake_word,
    connected,
    face,
    inline,
    motor_worker_threads,
    no_sleep,
    pushed_numbers,
    vendored_server_state,
)

from reachy_mini_ha_satellite import main as satellite_main
from reachy_mini_ha_satellite.adapters.groundstation import RemotePerception
from reachy_mini_ha_satellite.adapters.motion_reachy import (
    ReachyMotion,
    head_pose_matrix,
)
from reachy_mini_ha_satellite.adapters.perception_local import LocalPerception
from reachy_mini_ha_satellite.adapters.perception_source import FallbackPerception
from reachy_mini_ha_satellite.adapters.pipeline_events import PipelineEventTap
from reachy_mini_ha_satellite.audio_entities import SpeakerBoostNumberEntity
from reachy_mini_ha_satellite.behaviour import (
    CommandGaze,
    MoveAntennas,
    MoveHead,
    PipelineEvent,
    PreparedGazeTick,
    SatelliteBehaviour,
)
from reachy_mini_ha_satellite.behaviour.gaze_controller import (
    BodyMeasurement,
    ControllerConfig,
    ControllerFault,
    HeadMeasurement,
)
from reachy_mini_ha_satellite.behaviour.intents import MotionIntent
from reachy_mini_ha_satellite.config import (
    ENV_PREFIX,
    ConfigurationError,
    OverrideStore,
    Settings,
    load_settings,
    overrides_path,
)
from reachy_mini_ha_satellite.esphome.models import Preferences, ServerState
from reachy_mini_ha_satellite.esphome.peripheral_api import LVAEvent
from reachy_mini_ha_satellite.esphome.satellite import VoiceSatelliteProtocol
from reachy_mini_ha_satellite.main import (
    _THREAD_JOIN_SECONDS,
    AdvertisementService,
    EsphomeService,
    SatelliteApplication,
    VolumeService,
    WebRTCLike,
    WebService,
    _daemon_thread,
    _NoPerception,
    apply_intents,
    build_application,
    build_perception_source,
    build_server_state,
    configure_logging,
    load_preferences,
    run,
)
from reachy_mini_ha_satellite.motor_control import (
    ANTENNA_MOTOR_IDS,
    BODY_MOTOR_IDS,
    HEAD_MOTOR_IDS,
    MotorConfirmation,
    MotorConfirmationOutcome,
    MotorEvidence,
    MotorGroup,
    MotorGroupCoordinator,
)
from reachy_mini_ha_satellite.motor_entities import MotorSwitchEntity
from reachy_mini_ha_satellite.ports import (
    AntennaPose,
    CalibratedGaze,
    Detections,
    DetectionSource,
    GazeSample,
    HeadPose,
    MotionCommandResult,
    MotionCommandStatus,
    MotionFault,
    MotionMeasurement,
    SourceSelection,
)
from reachy_mini_ha_satellite.wake_word import WakeWordDetector
from reachy_session_client import Backoff

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Sequence
    from queue import Queue

    from pyfakefs.fake_filesystem import FakeFilesystem

    from reachy_mini_ha_satellite.adapters.daemon import RobotHandle
    from reachy_mini_ha_satellite.adapters.network import NetworkIdentity
    from reachy_mini_ha_satellite.config import Resolution
    from reachy_mini_ha_satellite.ports import PerceptionPort

# The RFC 5737 documentation range. This repository is public.
_GROUNDSTATION: Final = "ws://192.0.2.10:8080/v1/session"

_ENVIRONMENT: Final[dict[str, str]] = {
    f"{ENV_PREFIX}DEVICE_NAME": "reachy-mini-1",
    f"{ENV_PREFIX}GROUNDSTATION_URL": _GROUNDSTATION,
    f"{ENV_PREFIX}GROUNDSTATION_CREDENTIAL": "example-credential",
    f"{ENV_PREFIX}STATE_DIR": "/reachy-satellite-main",
    f"{ENV_PREFIX}ADVERTISE": "false",
    f"{ENV_PREFIX}WEB_ENABLED": "false",
}


# The selection that runs the detector on the robot and opens no session. Bound
# once rather than spelled at each site, because this repository's leak scanner
# reads the dotted form as an mDNS hostname suffix — the same reason
# `adapters/perception_source.py` binds it, and one exempted line is better than
# several.
_ROBOT_ONLY: Final = SourceSelection.LOCAL  # leak-scan:allow

# Where the daemon serves its own API, which is this setting's default. The
# loopback literal rather than anybody's address.
_DAEMON_API: Final = "http://127.0.0.1:8000"

# Fake installation details used only to prove lifecycle exceptions are scrubbed.
_EXCEPTION_IDENTIFIERS: Final = (
    "192.0.2.44",
    "46053",
    "configured-device-name",
    "/example/account-name",
)
_EXCEPTION_DETAIL: Final = ":".join(_EXCEPTION_IDENTIFIERS)

# Where the boost setter's tests keep their overrides file, and the directory
# holding it. Bound once rather than spelled at each of the four sites, so that
# a test standing a file where the directory should be cannot end up naming a
# different path from the store it is meant to break.
_BOOST_STATE_DIR: Final = Path("/reachy-satellite-boost")
_BOOST_OVERRIDES: Final = _BOOST_STATE_DIR / "settings.json"


def _recording(asked: list[str]) -> Callable[[str], bool]:
    """Build a volume setter that records the address it was given.

    Args:
        asked: Where to record it.

    Returns:
        A setter that always reports success.
    """

    def _set(url: str) -> bool:
        """Record the address and report success.

        Args:
            url: Where the daemon is.

        Returns:
            True, always.
        """
        asked.append(url)
        return True

    return _set


def _settings(**overrides: str) -> Settings:
    """Resolve a usable configuration.

    Args:
        overrides: Variables to set on top of the working environment, by
            setting name.

    Returns:
        The settings.
    """
    return load_settings(_ENVIRONMENT, dict(overrides)).settings


def assembled(
    resolution: Resolution,
    handle: RobotHandle,
    *,
    identity: NetworkIdentity | None = None,
) -> SatelliteApplication:
    """Assemble the application from a synchronous test, confirmation and all.

    `build_application` awaits the initial motor confirmation off the loop, so
    it needs one. A loop per assembly is what a synchronous test can give it,
    and the coordinator it builds outlives that loop the same way it outlives
    `run`'s.

    Args:
        resolution: The settings in effect and where they came from.
        handle: What the daemon hands a running application.
        identity: What to announce on the network.

    Returns:
        The assembled application.
    """
    return asyncio.run(build_application(resolution, handle, identity=identity))


def _motor_confirmation(
    names: tuple[str, ...],
    enabled: bool,
) -> MotorConfirmation:
    """Build complete grouped physical evidence for composition tests."""
    return MotorConfirmation(
        True,
        MotorConfirmationOutcome.CONFIRMED,
        tuple(MotorEvidence(name=name, enabled=enabled) for name in names),
    )


class RecordingService:
    """A service that records its own lifecycle, and optionally fails to close."""

    def __init__(self, *, fails: bool = False) -> None:
        """Start neither started nor stopped.

        Args:
            fails: Whether `aclose` raises, which is how the shutdown guard is
                exercised.
        """
        self.started = 0
        self.closed = 0
        self._fails = fails

    async def start(self) -> None:
        """Record a start."""
        self.started += 1

    async def aclose(self) -> None:
        """Record a stop.

        Raises:
            RuntimeError: When this service was built to fail.
        """
        self.closed += 1
        if self._fails:
            message = "this service does not stop cleanly"
            raise RuntimeError(message)


class NeverReturningService:
    """A cleanup step that yields forever until its owner cancels it."""

    def __init__(self) -> None:
        """Start neither entered nor cancelled."""
        self.entered = 0
        self.entered_event = asyncio.Event()
        self.cancelled = 0

    async def start(self) -> None:
        """Do nothing."""

    async def aclose(self) -> None:
        """Wait forever, recording cancellation by a cleanup deadline."""
        self.entered += 1
        self.entered_event.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


class CancellationResistantService:
    """A cleanup step that delays completion after suppressing cancellation."""

    def __init__(self) -> None:
        """Start unfinished and without a cancellation."""
        self.cancelled = 0
        self.resume = asyncio.Event()
        self.finished = asyncio.Event()
        self.finalized = False

    async def start(self) -> None:
        """Do nothing."""

    async def aclose(self) -> None:
        """Suppress cancellation until the test permits late completion."""
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            await self.resume.wait()
            self.finished.set()
        finally:
            self.finalized = True


class ShieldSpawningCleanupService:
    """A cleanup whose shield creates an inner task outside its outer task."""

    def __init__(self) -> None:
        """Start without a recorded outer task or finalization."""
        self.task: asyncio.Task[None] | None = None
        self.spawned: list[asyncio.Task[bool]] = []
        self.entered = asyncio.Event()
        self.finalized = False

    async def start(self) -> None:
        """Do nothing."""

    async def aclose(self) -> None:
        """Keep shielding waits that spawn a descendant while finalizing."""
        task = asyncio.current_task()
        assert task is not None
        self.task = task
        self.entered.set()

        async def _wait_and_spawn() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                # A nested cleanup may allocate its own finalizer task. The
                # production scope must discover this after its first snapshot.
                self.spawned.append(asyncio.create_task(asyncio.Event().wait()))

        try:
            while True:
                try:
                    await asyncio.shield(_wait_and_spawn())
                except asyncio.CancelledError:
                    continue
        finally:
            self.finalized = True


class SuccessfulDescendantCleanupService:
    """A successful cleanup that starts work which remains pending."""

    def __init__(self) -> None:
        """Start without a descendant."""
        self.descendant: asyncio.Task[bool] | None = None

    async def start(self) -> None:
        """Do nothing."""

    async def aclose(self) -> None:
        """Spawn a pending descendant, then return normally."""
        self.descendant = asyncio.create_task(asyncio.Event().wait())


class ExplicitBlankContextCleanupService:
    """A cleanup that deliberately replaces inherited context on its child."""

    def __init__(self) -> None:
        """Start without descendants."""
        self.child: asyncio.Task[None] | None = None
        self.grandchild: asyncio.Task[bool] | None = None

    async def start(self) -> None:
        """Do nothing."""

    async def aclose(self) -> None:
        """Create a child in a blank context; it creates one more task."""

        async def _child() -> None:
            self.grandchild = asyncio.create_task(asyncio.Event().wait())
            await asyncio.Event().wait()

        self.child = asyncio.create_task(_child(), context=Context())
        await asyncio.sleep(0)


class EagerFinalizerCleanupService:
    """A descendant that would spawn successors during eager finalization."""

    def __init__(self) -> None:
        """Start with no descendant and no successors."""
        self.descendant: asyncio.Task[bool] | None = None
        self.successors: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        """Do nothing."""

    async def aclose(self) -> None:
        """Spawn one pending descendant, then return successfully."""

        async def _descendant() -> bool:
            try:
                return await asyncio.Event().wait()
            finally:
                self._spawn_successor()

        self.descendant = asyncio.create_task(_descendant())

    def _spawn_successor(self) -> None:
        """Create a bounded chain that exposes eager finalization churn."""
        if len(self.successors) >= _EAGER_SUCCESSOR_LIMIT:
            return

        async def _successor() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                self._spawn_successor()

        self.successors.append(asyncio.create_task(_successor()))


_EAGER_SUCCESSOR_LIMIT: Final = 32


class IndefinitelyCancellationResistantService:
    """A cleanup step that suppresses every ordinary task cancellation."""

    def __init__(self, on_cancel: Callable[[], None] | None = None) -> None:
        """Start without a child task or finalization.

        Args:
            on_cancel: What to call after the first suppressed cancellation.
        """
        self.task: asyncio.Task[None] | None = None
        self.entered = asyncio.Event()
        self.cancelled = 0
        self.finalized = False
        self._on_cancel = on_cancel

    async def start(self) -> None:
        """Do nothing."""

    async def aclose(self) -> None:
        """Keep waiting after every `CancelledError`, until forcibly finalized."""
        task = asyncio.current_task()
        assert task is not None
        self.task = task
        self.entered.set()
        try:
            while True:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled += 1
                    if self.cancelled == 1 and self._on_cancel is not None:
                        self._on_cancel()
        finally:
            self.finalized = True


async def _finish_test_tasks(
    before: set[asyncio.Task[Any]],
    runner: asyncio.Task[Any],
) -> None:
    """Bound test teardown for tasks a failed regression may leave behind."""
    for _ in range(_EAGER_SUCCESSOR_LIMIT + 4):
        pending = asyncio.all_tasks() - before - {runner}
        if not pending:
            return
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    raise AssertionError("test cleanup task chain did not converge")


def _application(
    *,
    audio: FakeAudio,
    motion: FakeMotion,
    perception: FakePerception,
    behaviour: SatelliteBehaviour | None = None,
    motor_groups: MotorGroupCoordinator | None = None,
    services: Sequence[Any] = (),
    stop_after: int = 2,
) -> tuple[SatelliteApplication, asyncio.Event]:
    """Build an application that stops itself after a few ticks.

    Args:
        audio: The microphone and speakers.
        motion: The head and the antennas.
        perception: What is in front of the robot.
        behaviour: The pure behavior owner, or a fresh default.
        motor_groups: Optional asynchronous motor-operation owner.
        services: The things with lifetimes.
        stop_after: How many ticks to run before the stop event is set. The
            loop's wait is what sets it, so no wall time is spent, and the
            count bounds the loop so a yield cannot become a hang.

    Returns:
        The application and the event that stops it.
    """
    stop = asyncio.Event()
    ticks = 0

    async def _wait(seconds: float) -> None:
        """Stand in for the loop's wait between ticks.

        Args:
            seconds: How long the loop wanted to wait, ignored.
        """
        del seconds
        nonlocal ticks
        ticks += 1
        if ticks >= stop_after:
            stop.set()
        # A zero-delay yield, not a wait: it hands control back to the event
        # loop so a test can act between two ticks, and adds no wall time.
        await asyncio.sleep(0)

    application = SatelliteApplication(
        settings=_settings(),
        audio=audio,
        motion=motion,
        perception=perception,
        behaviour=behaviour or SatelliteBehaviour(now=0.0),
        motor_groups=motor_groups,
        services=services,
        clock=_advancing(),
        sleep=_wait,
    )
    return application, stop


class TestControlledWakeBeforeStartup:
    """The approved SDK wake sequence is the first hardware lifecycle."""

    @pytest.mark.asyncio
    async def test_a_pre_set_stop_skips_wake_and_normal_composition(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A stop already requested must leave sleeping hardware untouched."""
        events: list[str] = []
        robot = FakeRobot()
        monkeypatch.setattr(
            robot,
            "enable_motors",
            lambda: events.append("enable_motors"),
            raising=False,
        )
        monkeypatch.setattr(
            robot,
            "wake_up",
            lambda: events.append("wake_up"),
            raising=False,
        )

        async def _offload(work: Callable[[], object]) -> object:
            return work()

        async def _build(resolution: object, handle: object) -> SatelliteApplication:
            del resolution, handle
            events.append("build_application")
            raise AssertionError("normal services were composed after stop")

        _patch_startup(monkeypatch, build=_build, offload=_offload)
        stop = asyncio.Event()
        stop.set()

        await run(robot, stop)

        assert events == []

    @pytest.mark.asyncio
    async def test_stop_after_motor_enable_skips_wake_and_normal_composition(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The boundary after motor enable is checked before controlled wake."""
        events: list[str] = []
        stop = asyncio.Event()
        robot = FakeRobot()

        def _enable_motors() -> None:
            events.append("enable_motors")
            stop.set()

        monkeypatch.setattr(robot, "enable_motors", _enable_motors, raising=False)
        monkeypatch.setattr(
            robot,
            "wake_up",
            lambda: events.append("wake_up"),
            raising=False,
        )

        async def _offload(work: Callable[[], object]) -> object:
            return work()

        async def _build(resolution: object, handle: object) -> SatelliteApplication:
            del resolution, handle
            events.append("build_application")
            raise AssertionError("normal services were composed after stop")

        _patch_startup(monkeypatch, build=_build, offload=_offload)

        await run(robot, stop)

        assert events == ["enable_motors"]

    @pytest.mark.asyncio
    async def test_stop_during_controlled_wake_skips_normal_composition(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A completed wake observes stop before normal services are composed."""
        events: list[str] = []
        stop = asyncio.Event()
        robot = FakeRobot()
        monkeypatch.setattr(
            robot,
            "enable_motors",
            lambda: events.append("enable_motors"),
            raising=False,
        )

        def _wake_up() -> None:
            events.append("wake_up")
            stop.set()

        monkeypatch.setattr(robot, "wake_up", _wake_up, raising=False)

        async def _offload(work: Callable[[], object]) -> object:
            return work()

        async def _build(resolution: object, handle: object) -> SatelliteApplication:
            del resolution, handle
            events.append("build_application")
            raise AssertionError("normal services were composed after stop")

        _patch_startup(monkeypatch, build=_build, offload=_offload)

        await run(robot, stop)

        assert events == ["enable_motors", "wake_up"]

    @pytest.mark.asyncio
    async def test_motors_and_wake_finish_before_application_composition(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Both blocking SDK calls are offloaded, ordered, and finish first.

        Args:
            monkeypatch: Replaces configuration and composition with inert fakes.
        """
        events: list[str] = []
        robot = FakeRobot()
        monkeypatch.setattr(
            robot,
            "enable_motors",
            lambda: events.append("enable_motors"),
            raising=False,
        )
        monkeypatch.setattr(
            robot,
            "wake_up",
            lambda: events.append("wake_up"),
            raising=False,
        )

        async def _offload(work: Callable[[], object]) -> object:
            """Record that a blocking SDK call left the event loop."""
            events.append("offload")
            return work()

        class _Application:
            async def run(self, stop: asyncio.Event) -> None:
                """Record where normal application execution begins."""
                del stop
                events.append("application.run")

        async def _build(resolution: object, handle: object) -> _Application:
            """Record composition without constructing any real service."""
            del resolution
            assert handle is robot
            events.append("build_application")
            return _Application()

        _patch_startup(monkeypatch, build=_build, offload=_offload)

        await run(robot, asyncio.Event())

        assert events == [
            "offload",
            "enable_motors",
            "offload",
            "wake_up",
            "build_application",
            "application.run",
        ]

    @pytest.mark.asyncio
    async def test_wake_failure_aborts_normal_application_startup(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed wake cannot leave a normally advertised half-started app.

        Args:
            monkeypatch: Replaces configuration, offload and composition.
        """
        events: list[str] = []
        robot = FakeRobot()
        monkeypatch.setattr(
            robot,
            "enable_motors",
            lambda: events.append("enable_motors"),
            raising=False,
        )

        def _fail_wake() -> None:
            events.append("wake_up")
            message = "controlled wake failed"
            raise RuntimeError(message)

        monkeypatch.setattr(robot, "wake_up", _fail_wake, raising=False)

        async def _offload(work: Callable[[], object]) -> object:
            return work()

        async def _must_not_build(
            resolution: object,
            handle: object,
        ) -> SatelliteApplication:
            del resolution, handle
            events.append("build_application")
            raise AssertionError("normal services were composed after wake failed")

        _patch_startup(monkeypatch, build=_must_not_build, offload=_offload)

        with pytest.raises(RuntimeError, match="controlled wake failed"):
            await run(robot, asyncio.Event())

        assert events == ["enable_motors", "wake_up"]


def _patch_startup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    build: Callable[..., Awaitable[object]],
    offload: Callable[[Callable[[], object]], Awaitable[object]],
) -> None:
    """Replace startup's configuration edges, leaving lifecycle order real.

    Args:
        monkeypatch: Installs the inert edges.
        build: What stands in for normal application composition, awaited the
            way the real one is.
        offload: What runs SDK calls without starting a worker thread.
    """

    class _Store:
        def load(self) -> dict[str, str]:
            """Return no persisted overrides without reading a file."""
            return {}

    resolution = load_settings(_ENVIRONMENT)
    monkeypatch.setattr(satellite_main, "OverrideStore", lambda _path: _Store())
    monkeypatch.setattr(satellite_main, "load_settings", lambda **_kwargs: resolution)
    monkeypatch.setattr(satellite_main, "configure_logging", lambda _settings: None)
    monkeypatch.setattr(
        satellite_main, "log_resolved_configuration", lambda _resolution: None
    )
    monkeypatch.setattr(satellite_main, "build_application", build)
    monkeypatch.setattr(satellite_main, "in_thread", offload)


def _advancing() -> Callable[[], float]:
    """A monotonic clock that moves a tenth of a second per reading.

    Returns:
        The clock, which is a function rather than an object because that is
        what the behaviour layer takes.
    """
    reading = 0.0

    def _read() -> float:
        nonlocal reading
        reading += 0.1
        return reading

    return _read


class TestApplyingIntents:
    """Four intents, four port methods, and nothing in between to get wrong."""

    def test_a_coordinated_gaze_sample_reaches_its_port_method(self) -> None:
        """Predictive gaze is already canonical when intent application sees it."""
        motion = FakeMotion()
        sample = GazeSample(0.3, 0.1, 0.0, 0.3, False)

        apply_intents(motion, [CommandGaze(sample)])

        assert motion.coordinated_gaze == [sample]

    def test_a_head_pose_reaches_move_head(self) -> None:
        """The pipeline's secondary channel."""
        motion = FakeMotion()

        apply_intents(motion, [MoveHead(HeadPose(pitch=0.2))])

        assert motion.heads[-1] == HeadPose(pitch=0.2)

    def test_an_antenna_pose_reaches_move_antennas(self) -> None:
        """The channel REQ-046's distinguishable movements travel on."""
        motion = FakeMotion()

        apply_intents(motion, [MoveAntennas(AntennaPose(left=0.5, right=-0.5))])

        assert motion.antennas[-1] == AntennaPose(left=0.5, right=-0.5)

    def test_intents_are_applied_in_order(self) -> None:
        """They reach the port in the order the behaviour layer produced them."""
        motion = FakeMotion()

        apply_intents(
            motion,
            [MoveHead(HeadPose(pitch=0.1)), MoveHead(HeadPose(pitch=0.2))],
        )

        assert [pose.pitch for pose in motion.heads] == [0.1, 0.2]


class TestTheLoop:
    """What ticking does, and what a pipeline event does between ticks."""

    def test_later_intent_failure_cannot_suppress_gaze_transaction_completion(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A successful gaze result is reduced before any later intent can raise."""

        class FailingHeadMotion(FakeMotion):
            """Accept gaze, then fail a competing head command."""

            def move_head(self, pose: HeadPose) -> None:
                del pose
                raise RuntimeError("later head command failed")

        behaviour = SatelliteBehaviour(now=0.0)
        sample = GazeSample(0.0, 0.0, 0.0, 0.0, False)
        completed: list[MotionCommandResult] = []

        def finish(*_args: object, **_kwargs: object) -> tuple[MotionIntent, ...]:
            return CommandGaze(sample), MoveHead(HeadPose(pitch=0.1))

        def complete(result: MotionCommandResult) -> tuple[MotionIntent, ...]:
            completed.append(result)
            return ()

        monkeypatch.setattr(behaviour, "finish", finish)
        monkeypatch.setattr(behaviour, "complete_command", complete)
        application = SatelliteApplication(
            settings=_settings(),
            audio=FakeAudio(),
            motion=FailingHeadMotion(),
            perception=FakePerception(),
            behaviour=behaviour,
            clock=lambda: 0.1,
        )

        with pytest.raises(RuntimeError, match="later head command failed"):
            application.tick()

        assert len(completed) == 1
        assert completed[0].status is MotionCommandStatus.ACCEPTED

    @pytest.mark.asyncio
    async def test_it_starts_everything_before_ticking(self) -> None:
        """A tick against an unstarted perception source would read nothing."""
        audio, motion = FakeAudio(), FakeMotion()
        perception = FakePerception()
        service = RecordingService()
        application, stop = _application(
            audio=audio,
            motion=motion,
            perception=perception,
            services=[service],
        )

        await application.run(stop)

        assert audio.started == 1
        assert perception.started == 1
        assert service.started == 1

    @pytest.mark.asyncio
    async def test_a_visible_face_is_followed(self) -> None:
        """The loop's whole job, end to end against the fakes."""
        audio, motion = FakeAudio(), FakeMotion()
        perception = FakePerception()
        perception.see(face(0.4, 0.2), source=DetectionSource.REMOTE)
        application, stop = _application(
            audio=audio,
            motion=motion,
            perception=perception,
        )

        await application.run(stop)

        assert motion.coordinated_gaze

    @pytest.mark.asyncio
    async def test_stale_results_return_the_head_to_neutral(self) -> None:
        """REQ-048 returns through predictive trajectory before pipeline handoff."""
        audio, motion = FakeAudio(), FakeMotion()
        perception = FakePerception()
        perception.see(face(0.4, 0.2), source=DetectionSource.REMOTE)
        application, stop = _application(
            audio=audio,
            motion=motion,
            perception=perception,
            stop_after=200,
        )

        run = asyncio.create_task(application.run(stop))
        # Two passes of the loop: one to start everything and take the first
        # tick, one to reach the wait. Then the results stop arriving.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        perception.go_stale()
        await run

        assert motion.coordinated_gaze[-1].world_yaw == pytest.approx(0.0, abs=0.01)
        assert motion.heads

    def test_a_pipeline_event_moves_the_robot_at_once(self) -> None:
        """`deliver` is what the tap calls, on the event loop's own thread."""
        motion = FakeMotion()
        application, _stop = _application(
            audio=FakeAudio(),
            motion=motion,
            perception=FakePerception(),
        )

        application.deliver(PipelineEvent.LISTENING)

        assert motion.antennas

    def test_the_three_pipeline_states_produce_three_different_movements(
        self,
    ) -> None:
        """REQ-046, at the surface a person in the room actually watches."""
        commanded = []
        for event in (
            PipelineEvent.LISTENING,
            PipelineEvent.PROCESSING,
            PipelineEvent.RESPONDING,
        ):
            motion = FakeMotion()
            application, _stop = _application(
                audio=FakeAudio(),
                motion=motion,
                perception=FakePerception(),
            )
            application.deliver(event)
            commanded.append((motion.antennas[-1].left, motion.antennas[-1].right))

        assert len(set(commanded)) == len(commanded)

    def test_the_status_is_reported_for_the_settings_interface(self) -> None:
        """Existing keys remain and controller mode, fault and safe hold are explicit."""
        application, _stop = _application(
            audio=FakeAudio(),
            motion=FakeMotion(),
            perception=FakePerception(),
        )

        status = application.status()
        assert status["pipeline"] == "idle"
        assert set(status) == {"pipeline", "gaze", "tracking", "idle", "controller"}
        assert status["controller"] == {
            "mode": "unknown",
            "fault": "none",
            "safe_hold": False,
        }

    def test_resetting_controller_diagnostics_changes_no_runtime_state_or_command(
        self,
    ) -> None:
        """The operator reset is exactly motion-free and state preserving."""
        motion = FakeMotion()
        behaviour = SatelliteBehaviour(now=0.0)
        application, _stop = _application(
            audio=FakeAudio(),
            motion=motion,
            perception=FakePerception(),
            behaviour=behaviour,
        )
        application.tick()
        assert application.controller_diagnostics()
        before_state = behaviour.controller_state
        before_commands = (
            len(motion.coordinated_gaze),
            len(motion.heads),
            len(motion.antennas),
        )

        application.reset_controller_diagnostics()

        assert application.controller_diagnostics() == ()
        assert behaviour.controller_state == before_state
        assert (
            len(motion.coordinated_gaze),
            len(motion.heads),
            len(motion.antennas),
        ) == before_commands

    def test_failed_daemon_command_cannot_promote_candidate_and_needs_new_calls(
        self,
    ) -> None:
        """Command acceptance is transactional and recovery counts distinct calls."""
        clock = ManualClock(0.1)
        motion = FakeMotion()
        motion.command_results = [
            MotionCommandResult(
                MotionCommandStatus.REJECTED,
                MotionFault.COMMAND,
                call=1,
            ),
            MotionCommandResult(MotionCommandStatus.ACCEPTED, call=2),
            MotionCommandResult(MotionCommandStatus.ACCEPTED, call=3),
        ]
        perception = FakePerception()
        perception.see(face(0.5, 0.0), source=DetectionSource.REMOTE)
        behaviour = SatelliteBehaviour(now=0.0)
        application = SatelliteApplication(
            settings=_settings(),
            audio=FakeAudio(),
            motion=motion,
            perception=perception,
            behaviour=behaviour,
            clock=clock,
        )
        before = behaviour.controller_state.last_safe_sample

        application.tick()
        failed_state = behaviour.controller_state

        assert failed_state.fault is ControllerFault.COMMAND
        assert failed_state.last_safe_sample == before
        assert motion.coordinated_gaze == []
        failed_event = application.controller_diagnostics()[-1]
        assert failed_event["command_accepted"] is False
        assert failed_event["fault"] == "command"

        clock.advance(0.05)
        application.tick()
        recovering_state = behaviour.controller_state
        assert recovering_state.fault is ControllerFault.COMMAND
        assert recovering_state.recovery_valid_streak == 1

        clock.advance(0.05)
        application.tick()
        recovered_state = behaviour.controller_state
        assert recovered_state.fault is ControllerFault.NONE
        assert recovered_state.last_safe_sample == before
        assert len(motion.coordinated_gaze) == 2
        assert application.controller_diagnostics()[-1]["command_accepted"] is True


class TestDisabledGazeStartup:
    """Restart-bound disabled tracking never takes daemon gaze ownership."""

    @pytest.mark.asyncio
    async def test_disabled_startup_does_not_touch_motion_feedback_or_auto_yaw(
        self,
    ) -> None:
        """Pipeline-only behavior leaves all gaze acquisition edges untouched."""
        events: list[str] = []
        robot = FakeRobot(events=events)
        motion = ReachyMotion(robot)
        stop = asyncio.Event()

        async def _stop_after_first_tick(_seconds: float) -> None:
            stop.set()

        application = SatelliteApplication(
            settings=_settings(face_tracking_enabled="false"),
            audio=FakeAudio(),
            motion=motion,
            perception=FakePerception(),
            behaviour=SatelliteBehaviour(tracking_enabled=False, now=0.0),
            clock=lambda: 1.0,
            sleep=_stop_after_first_tick,
        )

        await application.run(stop)

        assert not any(event.startswith("motion.auto_yaw") for event in events)
        assert "motion.pose" not in events
        assert "motion.joints" not in events


class TestBodyColdStart:
    """Coordinated motion waits for measured body position before hardware output."""

    def test_missing_first_joint_feedback_emits_no_body_target(self) -> None:
        """A configured body cannot cold-start from an internal zero assumption."""
        robot = FakeRobot(
            measured_joints=(
                RuntimeError("joint feedback unavailable"),
                RuntimeError("joint feedback unavailable"),
            )
        )
        motion = ReachyMotion(robot, body_enabled=True)
        motion.acquire(0.0)
        perception = FakePerception()
        perception.see(face(0.5, 0.0), source=DetectionSource.REMOTE)
        application = SatelliteApplication(
            settings=_settings(body_motion_enabled="true"),
            audio=FakeAudio(),
            motion=motion,
            perception=perception,
            behaviour=SatelliteBehaviour(
                controller_config=ControllerConfig(body_enabled=True),
                now=0.0,
            ),
            clock=lambda: 0.1,
        )

        application.tick()

        assert robot.body_yaws == []
        assert not any(
            head is not None and body is not None for head, _a, body in robot.targets
        )


class TestPredictiveTickTiming:
    """One injected clock read supplies both calibration time and exact controller dt."""

    def test_disabled_gaze_skips_measurement_and_calibration(self) -> None:
        """Restart-bound disable leaves only pure pipeline expression on each tick."""
        motion = FakeMotion()
        application = SatelliteApplication(
            settings=_settings(face_tracking_enabled="false"),
            audio=FakeAudio(),
            motion=motion,
            perception=FakePerception(),
            behaviour=SatelliteBehaviour(tracking_enabled=False, now=0.0),
            clock=lambda: 1.0,
        )

        application.tick()

        assert motion.observed == []
        assert motion.calibrated == []

    def test_each_tick_reads_time_once_and_uses_exact_difference(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No nominal cadence or second clock read may replace measured elapsed time."""
        readings = iter((1.0, 1.125))
        clock_reads: list[float] = []

        def _clock() -> float:
            value = next(readings)
            clock_reads.append(value)
            return value

        behaviour = SatelliteBehaviour(now=0.0)
        original_finish = behaviour.finish
        dts: list[float] = []

        def _finish(
            prepared: PreparedGazeTick,
            *,
            calibrated: CalibratedGaze | None,
            head_measurement: HeadMeasurement | None,
            body_measurement: BodyMeasurement | None,
            dt: float,
            input_fault: ControllerFault = ControllerFault.NONE,
            input_evidence: tuple[object, ...] | None = None,
        ) -> tuple[MotionIntent, ...]:
            dts.append(dt)
            return original_finish(
                prepared,
                calibrated=calibrated,
                head_measurement=head_measurement,
                body_measurement=body_measurement,
                dt=dt,
                input_fault=input_fault,
                input_evidence=input_evidence,
            )

        monkeypatch.setattr(behaviour, "finish", _finish)
        motion = FakeMotion()
        application = SatelliteApplication(
            settings=_settings(),
            audio=FakeAudio(),
            motion=motion,
            perception=FakePerception(),
            behaviour=behaviour,
            clock=_clock,
        )

        application.tick()
        application.tick()

        assert clock_reads == [1.0, 1.125]
        assert motion.observed == clock_reads
        assert dts == [0.0, 0.125]


class TestShutdown:
    """ha-satellite REQ-050, in the order the requirement states it."""

    @pytest.mark.asyncio
    async def test_start_and_cleanup_order_is_deterministic(self) -> None:
        """Acquire first; release motion/media, reverse services, then perception."""
        events: list[str] = []

        class OrderedMotion(FakeMotion):
            """Record motion lifecycle around the ordinary fake."""

            def acquire(self, now: float) -> MotionMeasurement:
                events.append("motion.acquire")
                return super().acquire(now)

            def release(self) -> None:
                events.append("motion.release")
                super().release()

        class OrderedAudio(FakeAudio):
            """Record media lifecycle around the ordinary fake."""

            def start(self) -> None:
                events.append("audio.start")
                super().start()

            def stop(self) -> None:
                events.append("audio.stop")
                super().stop()

        class OrderedPerception(FakePerception):
            """Record perception lifecycle around the ordinary fake."""

            async def start(self) -> None:
                events.append("perception.start")
                await super().start()

            async def aclose(self) -> None:
                events.append("perception.close")
                await super().aclose()

        class OrderedService:
            """Record one service name at both lifecycle edges."""

            def __init__(self, name: str) -> None:
                self.name = name

            async def start(self) -> None:
                events.append(f"{self.name}.start")

            async def aclose(self) -> None:
                events.append(f"{self.name}.close")

        application, stop = _application(
            audio=OrderedAudio(),
            motion=OrderedMotion(),
            perception=OrderedPerception(),
            services=[OrderedService("first"), OrderedService("second")],
            stop_after=1,
        )

        await application.run(stop)

        assert events == [
            "motion.acquire",
            "audio.start",
            "perception.start",
            "first.start",
            "second.start",
            "motion.release",
            "audio.stop",
            "second.close",
            "first.close",
            "perception.close",
        ]

    @pytest.mark.asyncio
    async def test_repeated_close_is_idempotent(self) -> None:
        """A signal and run-finally cleanup release every owner only once."""
        audio = FakeAudio()
        motion = FakeMotion()
        perception = FakePerception()
        service = RecordingService()
        application, _stop = _application(
            audio=audio,
            motion=motion,
            perception=perception,
            services=[service],
        )

        await application.aclose()
        await application.aclose()

        assert audio.stopped == 1
        assert perception.closed == 1
        assert service.closed == 1
        assert motion.released

    @pytest.mark.asyncio
    async def test_it_stops_motion_and_releases_the_media_interface(self) -> None:
        """The two halves of leaving the robot safe."""
        audio, motion = FakeAudio(), FakeMotion()
        application, stop = _application(
            audio=audio,
            motion=motion,
            perception=FakePerception(),
        )

        await application.run(stop)

        assert motion.released
        assert audio.stopped == 1

    @pytest.mark.asyncio
    async def test_it_stops_the_services_and_the_perception_source(self) -> None:
        """Nothing is left holding a socket, a thread or a model."""
        service = RecordingService()
        perception = FakePerception()
        application, stop = _application(
            audio=FakeAudio(),
            motion=FakeMotion(),
            perception=perception,
            services=[service],
        )

        await application.run(stop)

        assert service.closed == 1
        assert perception.closed == 1

    @pytest.mark.asyncio
    async def test_movement_after_shutdown_is_ignored_rather_than_refused(
        self,
    ) -> None:
        """A behaviour tick racing the signal ends quietly."""
        motion = FakeMotion()
        application, stop = _application(
            audio=FakeAudio(),
            motion=motion,
            perception=FakePerception(),
        )
        await application.run(stop)
        before = len(motion.antennas)

        application.deliver(PipelineEvent.LISTENING)

        assert len(motion.antennas) == before

    @pytest.mark.asyncio
    async def test_a_service_that_fails_to_stop_does_not_skip_the_rest(self) -> None:
        """The microphone must not be left open because a socket would not close."""
        audio, motion = FakeAudio(), FakeMotion()
        failing = RecordingService(fails=True)
        healthy = RecordingService()
        perception = FakePerception()
        application, stop = _application(
            audio=audio,
            motion=motion,
            perception=perception,
            services=[healthy, failing],
        )

        await application.run(stop)

        assert healthy.closed == 1
        assert perception.closed == 1
        assert motion.released

    @pytest.mark.asyncio
    async def test_a_non_returning_cleanup_is_bounded_and_later_cleanup_runs(
        self,
    ) -> None:
        """One half-closed service cannot hold every later release hostage."""
        stuck = NeverReturningService()
        later = RecordingService()
        application = SatelliteApplication(
            settings=_settings(),
            audio=FakeAudio(),
            motion=FakeMotion(),
            perception=FakePerception(),
            behaviour=SatelliteBehaviour(now=0.0),
            services=[later, stuck],
            cleanup_timeout_seconds=0.0,
        )

        await application.aclose()

        assert stuck.entered == 1
        assert stuck.cancelled == 1
        assert later.closed == 1

    @pytest.mark.asyncio
    async def test_cancellation_resistant_cleanup_does_not_extend_its_deadline(
        self,
    ) -> None:
        """The owner moves on without waiting for a child that suppresses cancel."""
        stuck = CancellationResistantService()
        later = RecordingService()
        application = SatelliteApplication(
            settings=_settings(),
            audio=FakeAudio(),
            motion=FakeMotion(),
            perception=FakePerception(),
            behaviour=SatelliteBehaviour(now=0.0),
            services=[later, stuck],
            cleanup_timeout_seconds=0.0,
        )

        await application.aclose()

        assert stuck.cancelled == 1
        assert later.closed == 1
        assert not stuck.finished.is_set()
        assert stuck.finalized

    def test_indefinitely_resistant_cleanup_leaves_no_task_for_runner(
        self,
    ) -> None:
        """Application shutdown must leave nothing for `asyncio.run` to await."""

        async def _run_and_close() -> None:
            stuck = IndefinitelyCancellationResistantService()
            later = RecordingService()
            application = SatelliteApplication(
                settings=_settings(),
                audio=FakeAudio(),
                motion=FakeMotion(),
                perception=FakePerception(),
                behaviour=SatelliteBehaviour(now=0.0),
                services=[later, stuck],
                cleanup_timeout_seconds=0.0,
            )

            await application.aclose()

            task = stuck.task
            assert task is not None
            try:
                assert task.done()
                assert stuck.finalized
                assert later.closed == 1
            finally:
                # This makes the RED test itself bounded: before the fix the
                # assertion fails, then the test closes the leaked child so the
                # process-level runner can return rather than hiding the failure.
                if not task.done():
                    coroutine = task.get_coro()
                    assert coroutine is not None
                    coroutine.close()
                    task.cancel()
                    await asyncio.sleep(0)

        asyncio.run(_run_and_close())

    def test_shield_spawned_cleanup_descendants_leave_no_tasks_for_runner(
        self,
    ) -> None:
        """Forced outer finalization must also finish shield-created children."""

        async def _run_and_close() -> None:
            runner = asyncio.current_task()
            assert runner is not None
            unrelated = asyncio.create_task(asyncio.Event().wait())
            await asyncio.sleep(0)
            before = set(asyncio.all_tasks())
            stuck = ShieldSpawningCleanupService()
            later = RecordingService()
            application = SatelliteApplication(
                settings=_settings(),
                audio=FakeAudio(),
                motion=FakeMotion(),
                perception=FakePerception(),
                behaviour=SatelliteBehaviour(now=0.0),
                services=[later, stuck],
                cleanup_timeout_seconds=0.0,
            )

            await application.aclose()

            outer = stuck.task
            assert outer is not None
            leaked = asyncio.all_tasks() - before - {runner}
            try:
                assert outer.done()
                assert stuck.finalized
                assert later.closed == 1
                assert not unrelated.done()
                assert leaked == set()
            finally:
                # Bound the RED run itself without letting asyncio.run teardown
                # conceal the descendant that production shutdown leaked.
                pending = leaked
                while pending:
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    pending = asyncio.all_tasks() - before - {runner}
                unrelated.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await unrelated

        asyncio.run(_run_and_close())

    def test_successful_cleanup_drains_its_pending_descendant(self) -> None:
        """A normal outer return cannot transfer child ownership to the runner."""

        async def _run_and_close() -> None:
            runner = asyncio.current_task()
            assert runner is not None
            before = set(asyncio.all_tasks())
            service = SuccessfulDescendantCleanupService()
            later = RecordingService()
            application = SatelliteApplication(
                settings=_settings(),
                audio=FakeAudio(),
                motion=FakeMotion(),
                perception=FakePerception(),
                behaviour=SatelliteBehaviour(now=0.0),
                services=[later, service],
            )

            try:
                await application.aclose()

                descendant = service.descendant
                assert descendant is not None
                assert descendant.done()
                assert later.closed == 1
                assert asyncio.all_tasks() - before - {runner} == set()
            finally:
                await _finish_test_tasks(before, runner)

        asyncio.run(_run_and_close())

    def test_legacy_two_argument_task_factory_remains_compatible(self) -> None:
        """Absent context must not become an unsupported keyword argument."""

        async def _run_and_close() -> None:
            loop = asyncio.get_running_loop()
            calls = 0

            def _legacy_factory(
                task_loop: asyncio.AbstractEventLoop,
                coroutine: Coroutine[Any, Any, Any],
            ) -> asyncio.Task[Any]:
                nonlocal calls
                calls += 1
                return asyncio.Task(coroutine, loop=task_loop)

            service = RecordingService()
            loop.set_task_factory(_legacy_factory)
            try:
                application = SatelliteApplication(
                    settings=_settings(),
                    audio=FakeAudio(),
                    motion=FakeMotion(),
                    perception=FakePerception(),
                    behaviour=SatelliteBehaviour(now=0.0),
                    services=[service],
                )
                await application.aclose()
            finally:
                loop.set_task_factory(None)

            assert calls >= 1
            assert service.closed == 1

        asyncio.run(_run_and_close())

    def test_explicit_blank_context_still_owns_nested_cleanup_tasks(self) -> None:
        """Replacing inherited context cannot detach a child from cleanup scope."""

        async def _run_and_close() -> None:
            loop = asyncio.get_running_loop()
            runner = asyncio.current_task()
            assert runner is not None
            before = set(asyncio.all_tasks())
            factory_calls: list[Coroutine[Any, Any, Any]] = []

            def _legacy_factory(
                task_loop: asyncio.AbstractEventLoop,
                coroutine: Coroutine[Any, Any, Any],
            ) -> asyncio.Task[Any]:
                factory_calls.append(coroutine)
                return asyncio.Task(coroutine, loop=task_loop)

            service = ExplicitBlankContextCleanupService()
            later = RecordingService()
            application = SatelliteApplication(
                settings=_settings(),
                audio=FakeAudio(),
                motion=FakeMotion(),
                perception=FakePerception(),
                behaviour=SatelliteBehaviour(now=0.0),
                services=[later, service],
            )

            loop.set_task_factory(_legacy_factory)
            try:
                await application.aclose()

                child = service.child
                grandchild = service.grandchild
                assert child is not None
                assert grandchild is not None
                assert sum(call is child.get_coro() for call in factory_calls) == 1
                assert child.done()
                assert grandchild.done()
                assert later.closed == 1
                assert asyncio.all_tasks() - before - {runner} == set()
                assert loop.get_task_factory() is _legacy_factory
            finally:
                loop.set_task_factory(None)
                await _finish_test_tasks(before, runner)

        asyncio.run(_run_and_close())

    def test_eager_finalization_closes_successor_without_churn(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A finalizer-created successor is canceled before eager execution."""

        async def _run_and_close() -> None:
            loop = asyncio.get_running_loop()
            runner = asyncio.current_task()
            assert runner is not None
            before = set(asyncio.all_tasks())
            service = EagerFinalizerCleanupService()
            later = RecordingService()
            loop.set_task_factory(asyncio.eager_task_factory)
            try:
                application = SatelliteApplication(
                    settings=_settings(),
                    audio=FakeAudio(),
                    motion=FakeMotion(),
                    perception=FakePerception(),
                    behaviour=SatelliteBehaviour(now=0.0),
                    services=[later, service],
                )
                await application.aclose()

                descendant = service.descendant
                assert descendant is not None
                assert descendant.done()
                assert len(service.successors) == 1
                assert service.successors[0].done()
                assert later.closed == 1
                assert asyncio.all_tasks() - before - {runner} == set()
            finally:
                loop.set_task_factory(None)
                await _finish_test_tasks(before, runner)

        with caplog.at_level(logging.ERROR, logger="reachy_mini_ha_satellite.main"):
            asyncio.run(_run_and_close())
        forced = [
            record
            for record in caplog.records
            if "force-finalized" in record.getMessage()
        ]
        assert len(forced) <= 2

    def test_repeated_owner_cancellation_cannot_abandon_cleanup_finalization(
        self,
    ) -> None:
        """A second cancel cannot strand a child during the finalization turn."""

        async def _run_and_close() -> None:
            closing: asyncio.Task[None] | None = None

            def _cancel_owner_again() -> None:
                assert closing is not None
                closing.cancel()

            stuck = IndefinitelyCancellationResistantService(_cancel_owner_again)
            later = RecordingService()
            perception = FakePerception()
            application = SatelliteApplication(
                settings=_settings(),
                audio=FakeAudio(),
                motion=FakeMotion(),
                perception=perception,
                behaviour=SatelliteBehaviour(now=0.0),
                services=[later, stuck],
            )
            closing = asyncio.create_task(application.aclose())
            await stuck.entered.wait()

            closing.cancel()

            with pytest.raises(asyncio.CancelledError):
                await closing
            task = stuck.task
            assert task is not None
            try:
                assert stuck.cancelled >= 1
                assert task.done()
                assert stuck.finalized
                assert later.closed == 1
                assert perception.closed == 1
            finally:
                # Bound the RED run itself while still asserting that production
                # shutdown, rather than asyncio.run teardown, finished the child.
                if not task.done():
                    coroutine = task.get_coro()
                    assert coroutine is not None
                    coroutine.close()
                    task.cancel()
                    await asyncio.sleep(0)
                if task.done():
                    with contextlib.suppress(BaseException):
                        task.result()

        asyncio.run(_run_and_close())

    @pytest.mark.asyncio
    async def test_owner_cancellation_attempts_every_remaining_cleanup(self) -> None:
        """Cancellation is re-raised only after later ownership is released."""
        stuck = NeverReturningService()
        later = RecordingService()
        perception = FakePerception()
        application = SatelliteApplication(
            settings=_settings(),
            audio=FakeAudio(),
            motion=FakeMotion(),
            perception=perception,
            behaviour=SatelliteBehaviour(now=0.0),
            services=[later, stuck],
        )
        closing = asyncio.create_task(application.aclose())
        await stuck.entered_event.wait()
        assert stuck.entered == 1

        closing.cancel()

        with pytest.raises(asyncio.CancelledError):
            await closing
        assert stuck.cancelled == 1
        assert later.closed == 1
        assert perception.closed == 1

    @pytest.mark.asyncio
    async def test_a_perception_source_that_fails_to_stop_is_survived(self) -> None:
        """It is the last step, so the failure only has itself to lose."""

        class _Stubborn(FakePerception):
            async def aclose(self) -> None:
                """Refuse to stop.

                Raises:
                    RuntimeError: Always.
                """
                message = "this source does not stop cleanly"
                raise RuntimeError(message)

        motion = FakeMotion()
        application, stop = _application(
            audio=FakeAudio(),
            motion=motion,
            perception=_Stubborn(),
        )

        await application.run(stop)

        assert motion.released

    @pytest.mark.asyncio
    async def test_closing_twice_does_nothing_the_second_time(self) -> None:
        """A termination signal and an ordinary shutdown can both arrive."""
        audio = FakeAudio()
        application, stop = _application(
            audio=audio,
            motion=FakeMotion(),
            perception=FakePerception(),
        )
        await application.run(stop)

        await application.aclose()

        assert audio.stopped == 1

    @pytest.mark.asyncio
    async def test_a_failure_inside_the_loop_still_leaves_the_robot_safe(
        self,
    ) -> None:
        """Shutdown is in a `finally`, so it does not depend on ending tidily."""

        class _Exploding(FakePerception):
            def latest(self) -> Detections:
                """Fail on being read.

                Raises:
                    RuntimeError: Always.
                """
                message = "the source failed mid-tick"
                raise RuntimeError(message)

        audio, motion = FakeAudio(), FakeMotion()
        application, stop = _application(
            audio=audio,
            motion=motion,
            perception=_Exploding(),
        )

        with pytest.raises(RuntimeError, match="mid-tick"):
            await application.run(stop)

        assert motion.released
        assert audio.stopped == 1

    def test_the_settings_interface_can_ask_for_a_restart(self) -> None:
        """Which is how a setting that needs one takes effect without a shell."""
        application, stop = _application(
            audio=FakeAudio(),
            motion=FakeMotion(),
            perception=FakePerception(),
        )

        async def _drive() -> None:
            """Run the loop and stop it from the settings interface's method."""
            task = asyncio.create_task(application.run(stop))
            await asyncio.sleep(0)
            application.request_stop()
            await task

        asyncio.run(_drive())

        assert stop.is_set()


class TestAdoptingSettingsWithoutARestart:
    """The live half of the settings interface."""

    def test_it_retunes_the_behaviour_layer(self) -> None:
        """Rather than rebuilding it, which would forget the conversation."""
        application, _stop = _application(
            audio=FakeAudio(),
            motion=FakeMotion(),
            perception=FakePerception(),
        )
        application.deliver(PipelineEvent.PROCESSING)

        application.apply_live(_settings(face_tracking_enabled="false"))

        assert application.status()["pipeline"] == "processing"

    def test_it_installs_the_new_log_level(self) -> None:
        """One of the settings that can be swapped into a running process."""
        application, _stop = _application(
            audio=FakeAudio(),
            motion=FakeMotion(),
            perception=FakePerception(),
        )

        application.apply_live(_settings(log_level="debug"))

        assert logging.getLogger().level == logging.DEBUG

    def test_it_hands_the_new_boost_to_both_outputs(self) -> None:
        """This is the one path from "a boost was chosen" to the speaker.

        Both the settings page and the Home Assistant control write through
        here, so a boost that stopped arriving would be silently inert on both.
        """
        audio = FakeAudio()
        application, _stop = _application(
            audio=audio,
            motion=FakeMotion(),
            perception=FakePerception(),
        )

        application.apply_live(_settings(speaker_boost_percent="640"))

        assert audio.boosts == [pytest.approx(640.0)]
        assert audio.music.boost == pytest.approx(640.0)
        assert audio.speech.boost == pytest.approx(640.0)

    def test_the_settings_it_adopted_are_what_it_reports(self) -> None:
        """The boost entity reads this, so a stale answer is a stale slider."""
        application, _stop = _application(
            audio=FakeAudio(),
            motion=FakeMotion(),
            perception=FakePerception(),
        )

        application.apply_live(_settings(speaker_boost_percent="220"))

        assert application.settings.speaker_boost_percent == pytest.approx(220.0)

    def test_the_boost_control_pushes_what_was_adopted(self) -> None:
        """The settings page changes the boost; Home Assistant has to be told.

        Driven through the real objects the composition root wires together —
        the application, the vendored state, the control and its broadcast —
        with only the connected client standing in, because the real one writes
        to a socket.
        """
        state = vendored_server_state()
        application, _stop = _application(
            audio=FakeAudio(),
            motion=FakeMotion(),
            perception=FakePerception(),
        )

        def _unused(percent: float) -> None:
            """Take a chosen boost and drop it.

            This test is about what a boost *adopted* pushes, and the setter is
            the other direction — `build_boost_setter` has its own tests.

            Args:
                percent: What Home Assistant would have chosen.
            """
            del percent

        boost = SpeakerBoostNumberEntity(
            state=state,
            key=len(state.entities),
            get_percent=lambda: application.settings.speaker_boost_percent,
            set_percent=_unused,
        )
        state.entities.append(boost)
        application.publish_live_changes(boost.publish)
        client = connected(state)[0]

        application.apply_live(_settings(speaker_boost_percent="640"))

        assert pushed_numbers(client, boost.key) == pytest.approx([640.0])

    def test_it_pushes_after_the_new_settings_are_in_effect(self) -> None:
        """A publisher called mid-adoption would report the value it replaced."""
        application, _stop = _application(
            audio=FakeAudio(),
            motion=FakeMotion(),
            perception=FakePerception(),
        )
        seen: list[float] = []
        application.publish_live_changes(
            lambda: seen.append(application.settings.speaker_boost_percent),
        )

        application.apply_live(_settings(speaker_boost_percent="640"))

        assert seen == [pytest.approx(640.0)]

    def test_an_application_with_nothing_registered_adopts_settings_anyway(
        self,
    ) -> None:
        """Every test above builds one, and `run` builds one before the wiring."""
        audio = FakeAudio()
        application, _stop = _application(
            audio=audio,
            motion=FakeMotion(),
            perception=FakePerception(),
        )

        application.apply_live(_settings(speaker_boost_percent="180"))

        assert audio.boosts == [pytest.approx(180.0)]


class TestTheEsphomeService:
    """The protocol server, and the microphone pump that feeds it."""

    @pytest.mark.asyncio
    async def test_it_binds_and_binds_once(self, fs: FakeFilesystem) -> None:
        """The listening socket and the pump are one lifetime.

        Args:
            fs: An in-memory filesystem, for the wheel's asset paths.
        """
        del fs
        bound: list[tuple[str, int]] = []
        service, _state = _esphome_service(bound)

        await service.start()
        await service.aclose()

        assert bound == [("127.0.0.1", 6053)]

    @pytest.mark.asyncio
    async def test_it_binds_the_tap_to_the_running_loop(self) -> None:
        """The vendored code emits from other threads, so the loop has to be known."""
        received: list[PipelineEvent] = []
        tap = PipelineEventTap(received.append)
        service, _state = _esphome_service([], tap=tap)

        await service.start()
        await asyncio.to_thread(service.pump)
        await service.aclose()

        assert received == []

    @pytest.mark.asyncio
    async def test_the_pump_feeds_captured_audio_into_the_protocol(self) -> None:
        """The feed the discarded upstream entry point used to provide."""
        chunks = [[b"\x00" * 320, b"\x01" * 320], [b"\x02" * 320, b"\x03" * 320]]
        capture = FakeCapture(chunks)
        service, state = _esphome_service([], capture=capture)
        satellite = _RecordingSatellite()
        # The vendored state declares this as its own protocol class; the pump
        # calls exactly one method on it, and a real one would need a socket.
        state.satellite = cast("VoiceSatelliteProtocol", satellite)
        # `start` is what sets the pump running; the injected thread starter
        # does not run it, so the pump is driven here instead of on a thread.
        await service.start()

        service.pump()

        assert satellite.chunks == [
            (b"\x00" * 320, b"\x01" * 320),
            (b"\x02" * 320, b"\x03" * 320),
        ]

    @pytest.mark.asyncio
    async def test_the_pump_drops_audio_while_nothing_is_connected(self) -> None:
        """A chunk arriving before Home Assistant does is dropped, not queued.

        A backlog replayed at connection time is a conversation that already
        happened.
        """
        capture = FakeCapture([[b"\x00" * 320]])
        service, state = _esphome_service([], capture=capture)
        state.satellite = None
        await service.start()

        service.pump()

        assert capture.read_chunk() is None

    @pytest.mark.asyncio
    async def test_the_pump_stops_when_the_capture_source_closes(self) -> None:
        """Which is what a released media interface looks like."""
        service, _state = _esphome_service([], capture=FakeCapture([]))
        await service.start()

        service.pump()

        assert True  # returning at all is the assertion: the loop is bounded

    @pytest.mark.asyncio
    async def test_detection_returns_at_once_when_no_detector_was_given(self) -> None:
        """A service built without one never starts the thread; `detect` is public."""
        service, _state = _esphome_service([], capture=FakeCapture([]))
        await service.start()

        service.detect()

        assert True  # as above: it returns rather than blocking on the queue

    @pytest.mark.asyncio
    async def test_a_healthy_listener_is_not_rebound(self) -> None:
        """A health check must leave the listener that is still serving alone."""
        bound: list[_FakeServer] = []
        service = _listener_service(bound)
        await service.start()

        await service.check_listener()

        assert len(bound) == 1
        assert bound[0].closed == 0

    @pytest.mark.asyncio
    async def test_a_stopped_listener_is_rebound(self) -> None:
        """A listener that died unexpectedly is replaced without a socket test."""
        bound: list[_FakeServer] = []
        service = _listener_service(bound)
        await service.start()
        bound[0].serving = False

        await service.check_listener()

        assert len(bound) == 2
        assert bound[0].closed == 1
        assert bound[0].waited == 1
        assert bound[1].serving

    @pytest.mark.asyncio
    async def test_successful_listener_lifecycle_logs_no_configured_identity(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Initial bind and supervised rebind report only static event text."""
        host = "192.0.2.44"
        port = 46053
        state = vendored_server_state()
        state.name = "configured-device-name"
        bound: list[_FakeServer] = []
        service = _listener_service(
            bound,
            state=state,
            host=host,
            port=port,
        )

        with caplog.at_level(logging.INFO, logger="reachy_mini_ha_satellite.main"):
            await service.start()
            bound[0].serving = False
            await service.check_listener()

        messages = [record.getMessage() for record in caplog.records]
        assert messages == [
            "esphome.listening",
            "esphome.listener stopped unexpectedly; rebinding",
            "esphome.listening",
        ]
        emitted = "\n".join(messages)
        assert host not in emitted
        assert str(port) not in emitted
        assert state.name not in emitted

    @pytest.mark.asyncio
    async def test_listener_bind_failures_use_a_capped_backoff(self) -> None:
        """Repeated bind refusal grows the delay only up to its declared cap."""
        bound: list[_FakeServer] = []
        delays: list[float] = []
        attempts = 0
        service: EsphomeService

        async def _listen(factory: object, host: str, port: int) -> _FakeServer:
            """Succeed initially, then refuse every replacement bind."""
            del factory, host, port
            nonlocal attempts
            attempts += 1
            if attempts > 1:
                message = "address is unavailable"
                raise OSError(message)
            server = _FakeServer()
            bound.append(server)
            return server

        async def _sleep(seconds: float) -> None:
            """Record virtual delay and stop after the cap has repeated."""
            delays.append(seconds)
            if len(delays) == 4:
                service._running = False

        service = _listener_service(
            bound,
            listen=_listen,
            sleep=_sleep,
            backoff=Backoff(
                initial_seconds=1.0,
                multiplier=2.0,
                maximum_seconds=4.0,
            ),
        )
        await service.start()
        bound[0].serving = False

        await service._supervise_listener()

        assert delays == [1.0, 2.0, 4.0, 4.0]

    @pytest.mark.asyncio
    async def test_listener_retry_log_omits_bind_exception_identifiers(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A failed replacement bind reports retry timing, not exception text."""
        bound: list[_FakeServer] = []
        attempts = 0
        service: EsphomeService

        async def _listen(factory: object, host: str, port: int) -> _FakeServer:
            del factory, host, port
            nonlocal attempts
            attempts += 1
            if attempts > 1:
                raise OSError(_EXCEPTION_DETAIL)
            server = _FakeServer()
            bound.append(server)
            return server

        async def _sleep(seconds: float) -> None:
            del seconds
            service._running = False

        service = _listener_service(bound, listen=_listen, sleep=_sleep)
        await service.start()
        bound[0].serving = False

        with caplog.at_level(logging.ERROR, logger="reachy_mini_ha_satellite.main"):
            await service._supervise_listener()

        messages = [
            record.getMessage()
            for record in caplog.records
            if record.levelno >= logging.ERROR
        ]
        assert messages == [
            "esphome.listener rebind failed; retrying in 0.5 seconds",
        ]
        for identifier in _EXCEPTION_IDENTIFIERS:
            assert identifier not in caplog.text

    @pytest.mark.asyncio
    async def test_intentional_close_prevents_listener_rebind(self) -> None:
        """Shutdown cannot race its own supervisor into opening a new listener."""
        bound: list[_FakeServer] = []
        service = _listener_service(bound)
        await service.start()
        bound[0].serving = False

        await service.aclose()
        await service.check_listener()

        assert len(bound) == 1

    @pytest.mark.asyncio
    async def test_close_during_initial_bind_does_not_revive_background_work(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A bind completing after shutdown cannot restart the closed service."""
        bind_started = asyncio.Event()
        finish_bind = asyncio.Event()
        server = _FakeServer()
        started_work: list[Callable[[], None]] = []

        async def _listen(factory: object, host: str, port: int) -> _FakeServer:
            del factory, host, port
            bind_started.set()
            await finish_bind.wait()
            return server

        def _start_thread(work: Callable[[], None]) -> None:
            started_work.append(work)

        service = EsphomeService(
            vendored_server_state(),
            FakeCapture([]),
            PipelineEventTap(lambda _: None),
            host="127.0.0.1",
            port=6053,
            detector=cast("WakeWordDetector", object()),
            listen=_listen,
            pump_sleep=no_sleep,
        )

        async def _supervise_listener() -> None:
            return

        monkeypatch.setattr(satellite_main, "_daemon_thread", _start_thread)
        monkeypatch.setattr(service, "_supervise_listener", _supervise_listener)
        start = asyncio.create_task(service.start())
        await bind_started.wait()
        close = asyncio.create_task(service.aclose())

        await close
        finish_bind.set()
        await start
        running_after_start = service._running
        supervisor_after_start = service._supervisor
        threads_after_start = list(service._threads)
        await service.aclose()

        assert server.closed == 1
        assert server.waited == 1
        assert not running_after_start
        assert supervisor_after_start is None
        assert threads_after_start == []
        assert started_work == []
        assert start.done()
        assert close.done()
        assert service._supervisor is None
        assert service._threads == []

    @pytest.mark.asyncio
    async def test_close_releases_all_accepted_protocols_and_disconnects_once(
        self,
    ) -> None:
        """Transport callbacks own one final shared disconnect during shutdown."""
        state = vendored_server_state()
        events = _RecordingPeripheralEvents()
        state.peripheral_api = events
        accepted = [VoiceSatelliteProtocol(state), VoiceSatelliteProtocol(state)]
        transports = [_DisconnectLifecycleTransport(protocol) for protocol in accepted]
        for protocol, transport in zip(accepted, transports, strict=True):
            protocol.connection_made(transport)
            protocol._authenticated = True
        state.satellite = accepted[-1]
        state.connected = True
        service, _state = _esphome_service([], state=state)
        await service.start()

        await service.aclose()
        await asyncio.sleep(0)

        assert [transport.closed for transport in transports] == [1, 1]
        assert state.connections == []
        assert cast("object | None", state.satellite) is None
        assert not state.connected
        assert (
            cast(Any, state.music_player.stop).call_count,
            cast(Any, state.tts_player.stop).call_count,
            events.events,
        ) == (1, 1, [LVAEvent.DISCONNECTED])


class TestTheMicrophonePumpSurvivesTransientFailures:
    """One bad chunk must not become a permanently deaf satellite."""

    @pytest.mark.asyncio
    async def test_a_capture_failure_does_not_discard_the_next_chunk(self) -> None:
        """A transient daemon read error is isolated to that read."""
        later = b"\x02" * 320
        capture = _FlakyCapture([RuntimeError("capture failed"), [later]])
        service, _state = _esphome_service([], capture=capture)
        satellite = _RecordingSatellite()
        _state.satellite = cast("VoiceSatelliteProtocol", satellite)
        await service.start()

        service.pump()

        assert satellite.chunks == [(later, None)]

    @pytest.mark.asyncio
    async def test_a_conditioning_failure_does_not_discard_the_next_chunk(
        self,
    ) -> None:
        """Native conditioning may fail once without ending the pump."""
        first, later = b"\x02" * 320, b"\x04" * 320
        conditioner = _FailsOnceWebRTC()
        service, state = _esphome_service(
            [],
            capture=FakeCapture([[first], [later]]),
            build_webrtc=lambda _gain, _noise: conditioner,
        )
        state.preferences.mic_auto_gain = 1
        satellite = _RecordingSatellite()
        state.satellite = cast("VoiceSatelliteProtocol", satellite)
        await service.start()

        service.pump()

        assert conditioner.inputs == [first, later]
        assert satellite.chunks == [(later, None)]

    @pytest.mark.asyncio
    async def test_forwarding_failure_still_detects_that_chunk_and_continues(
        self,
    ) -> None:
        """Local wake detection is independent of Home Assistant forwarding."""
        first, later = b"\x02" * 320, b"\x04" * 320
        model = FakeMicroWakeWord("okay_nabu")
        state = vendored_server_state(
            wake_words={model.id: model},
            active_wake_words={model.id},
            available_wake_words={model.id: available_wake_word(model.id)},
            stop_word=FakeMicroWakeWord("stop"),
        )
        service, state = _esphome_service(
            [],
            capture=FakeCapture([[first], [later]]),
            state=state,
            detector=WakeWordDetector(
                state,
                micro_features=FakeWakeWordFeatures,
                open_features=FakeWakeWordFeatures,
            ),
        )
        satellite = _FailsOneForward()
        state.satellite = cast("VoiceSatelliteProtocol", satellite)
        await service.start()

        service.pump()
        service.detect()

        assert satellite.attempted == [first, later]
        assert [inputs.tobytes() for inputs in model.inputs] == [first, later]

    @pytest.mark.asyncio
    async def test_chunk_failure_logs_omit_exception_identifiers(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Capture, conditioning and forwarding identify only their static stage."""
        chunk = b"\x02" * 320

        capture_service, _state = _esphome_service(
            [],
            capture=_FlakyCapture([RuntimeError(_EXCEPTION_DETAIL)]),
        )
        await capture_service.start()

        conditioner = _FailsOnceWebRTC(_EXCEPTION_DETAIL)
        condition_service, condition_state = _esphome_service(
            [],
            capture=FakeCapture([[chunk]]),
            build_webrtc=lambda _gain, _noise: conditioner,
        )
        condition_state.preferences.mic_auto_gain = 1
        await condition_service.start()

        forward_service, forward_state = _esphome_service(
            [],
            capture=FakeCapture([[chunk]]),
        )
        forward_state.satellite = cast(
            "VoiceSatelliteProtocol",
            _FailsOneForward(_EXCEPTION_DETAIL),
        )
        await forward_service.start()

        with caplog.at_level(logging.ERROR, logger="reachy_mini_ha_satellite.main"):
            capture_service.pump()
            condition_service.pump()
            forward_service.pump()

        messages = [
            record.getMessage()
            for record in caplog.records
            if record.levelno >= logging.ERROR
        ]
        assert messages == [
            "microphone capture failed for a chunk; continuing (1 failures)",
            "microphone conditioning failed for a chunk; continuing (1 failures)",
            "Home Assistant audio forwarding failed for a chunk; continuing (1 failures)",
        ]
        for identifier in _EXCEPTION_IDENTIFIERS:
            assert identifier not in caplog.text


class _FlakyCapture(FakeCapture):
    """A capture seam that can raise before producing later audio."""

    def __init__(self, outcomes: Sequence[object]) -> None:
        """Script exceptions and chunks in their exact read order."""
        super().__init__()
        self._outcomes = list(outcomes)

    def read_chunk(self) -> Sequence[bytes] | None:
        """Raise or return the next scripted outcome."""
        if not self._outcomes:
            return None
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return cast("Sequence[bytes]", outcome)


class _FailsOnceWebRTC:
    """A conditioner whose first chunk fails and whose second succeeds."""

    def __init__(self, message: str = "conditioning failed") -> None:
        """Start with no chunks seen.

        Args:
            message: Exception text for the first chunk.
        """
        self.inputs: list[bytes] = []
        self._message = message

    def update_settings(self, agc_level: int, ns_level: int) -> None:
        """Accept unchanged settings without rebuilding anything."""
        del agc_level, ns_level

    def process(self, raw_bytes: bytes) -> bytes:
        """Fail once, then pass subsequent chunks through."""
        self.inputs.append(raw_bytes)
        if len(self.inputs) == 1:
            raise RuntimeError(self._message)
        return raw_bytes


class _FailsOneForward:
    """A Home Assistant protocol that rejects only its first chunk."""

    def __init__(self, message: str = "forwarding failed") -> None:
        """Start having attempted no forwards.

        Args:
            message: Exception text for the first forwarding attempt.
        """
        self.attempted: list[bytes] = []
        self.chunks: list[tuple[bytes, bytes | None]] = []
        self._message = message

    def handle_audio(
        self,
        audio_chunk: bytes,
        audio_chunk_2: bytes | None = None,
    ) -> None:
        """Raise on the first call and record later audio normally."""
        self.attempted.append(audio_chunk)
        if len(self.attempted) == 1:
            raise RuntimeError(self._message)
        self.chunks.append((audio_chunk, audio_chunk_2))


class TestTheWakeWordFeed:
    """The half of the feed that *starts* a conversation.

    This is the regression suite for the defect that reached a robot: the
    models loaded, Home Assistant listed them, the microphone worked, audio
    reached Home Assistant once a pipeline was running — and nothing anywhere
    called `wakeup`, so speaking to the robot did nothing at all. The whole
    suite passed. Every test here fails if the detection half is removed again.
    """

    @pytest.mark.asyncio
    async def test_a_model_that_fires_wakes_the_robot(self) -> None:
        """The one assertion the previous suite was missing entirely."""
        model = FakeMicroWakeWord("okay_nabu", fires=[True])
        service, _state, satellite = await self._feed(model)

        service.pump()
        service.detect()
        # The detection thread hands the protocol to the event loop rather than
        # touching it; one turn of the loop is what delivers it.
        await asyncio.sleep(0)

        assert satellite.woken == [model]

    @pytest.mark.asyncio
    async def test_it_still_streams_every_chunk_to_home_assistant(self) -> None:
        """Detection must not cost the streaming half a single chunk."""
        model = FakeMicroWakeWord("okay_nabu", fires=[True])
        service, _state, satellite = await self._feed(model, chunks=3)

        service.pump()

        assert len(satellite.chunks) == 3

    @pytest.mark.asyncio
    async def test_it_detects_while_home_assistant_is_not_connected(self) -> None:
        """REQ-044's own scenario: `connection_lost` sets the satellite to `None`.

        A robot whose network has failed has no connection, so skipping
        detection for one would make the wake word depend on exactly the thing
        the requirement says it must not.
        """
        model = FakeMicroWakeWord("okay_nabu", fires=[True])
        service, state, satellite = await self._feed(model, chunks=2)
        state.satellite = None

        service.pump()
        service.detect()
        await asyncio.sleep(0)

        assert len(model.inputs) == 2
        assert satellite.chunks == []
        # The models ran and fired; there was simply nobody to tell, and the
        # connection that had gone is not woken behind Home Assistant's back.
        assert satellite.woken == []

    @pytest.mark.asyncio
    async def test_it_wakes_whichever_connection_is_current(self) -> None:
        """Home Assistant reconnecting in that moment is an ordinary event.

        A restart or a network blip between the model firing and the loop
        getting round to the activation would otherwise wake the connection
        that had already gone — on a closed transport, while the new one heard
        nothing and the refractory window swallowed the next attempt.
        """
        model = FakeMicroWakeWord("okay_nabu", fires=[True])
        service, state, gone = await self._feed(model)

        service.pump()
        service.detect()
        fresh = _RecordingSatellite()
        state.satellite = cast("VoiceSatelliteProtocol", fresh)
        await asyncio.sleep(0)

        assert gone.woken == []
        assert fresh.woken == [model]

    @pytest.mark.asyncio
    async def test_a_wake_word_queued_as_shutdown_begins_is_dropped(self) -> None:
        """REQ-050: the last word spoken to a robot being shut down is not one.

        The activation is already on the event loop's own queue when the
        termination signal arrives, so nothing can stop it being *delivered* —
        what stops it starting a conversation is that the service knows it is
        closing.
        """
        model = FakeMicroWakeWord("okay_nabu", fires=[True])
        service, _state, satellite = await self._feed(model)

        service.pump()
        service.detect()
        await service.aclose()
        await asyncio.sleep(0)

        assert satellite.woken == []

    @pytest.mark.asyncio
    async def test_it_ends_detection_even_when_the_queue_is_full(self) -> None:
        """A detector that has fallen behind must still be able to stop.

        The sentinel that ends `detect` goes on the same bounded queue the
        chunks do, so a full queue would refuse it and leave the thread
        blocked on `get` through shutdown and beyond. This runs `detect` on a
        real thread rather than inline, because the regression it guards is a
        thread that never returns — and a test that asserted inline would hang
        rather than fail.
        """
        model = FakeMicroWakeWord("okay_nabu")
        service, _state, _satellite = await self._feed(model, chunks=5, backlog=2)
        service.pump()

        finished = threading.Event()

        def _drain() -> None:
            """Run the detection loop, and record that it came back."""
            service.detect()
            finished.set()

        thread = threading.Thread(target=_drain, daemon=True)
        thread.start()
        thread.join(timeout=_THREAD_JOIN_SECONDS)

        assert finished.is_set()

    @pytest.mark.asyncio
    async def test_a_muted_robot_is_not_woken(self) -> None:
        """Asserted here as well as in the detector: it is what mute means."""
        model = FakeMicroWakeWord("okay_nabu", fires=[True])
        service, state, satellite = await self._feed(model)
        state.muted = True

        service.pump()
        service.detect()
        await asyncio.sleep(0)

        assert satellite.woken == []

    @pytest.mark.asyncio
    async def test_the_stop_word_stops_a_response(self) -> None:
        """The other thing the detection half is for."""
        model = FakeMicroWakeWord("okay_nabu")
        stop = FakeMicroWakeWord("stop", fires=[True])
        service, state, satellite = await self._feed(model, stop_word=stop)
        state.active_wake_words = {model.id, "stop"}

        service.pump()
        service.detect()
        await asyncio.sleep(0)

        assert satellite.stops == 1

    @pytest.mark.asyncio
    async def test_a_model_that_raises_does_not_end_or_flood_detection(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A broken model stays isolated without logging every audio chunk.

        Args:
            caplog: Captures the rate-limited detector failures.
        """
        broken = FakeMicroWakeWord("okay_nabu", fails=True)
        service, _state, satellite = await self._feed(
            broken,
            chunks=101,
            backlog=102,
        )

        service.pump()
        with caplog.at_level(logging.ERROR, logger="reachy_mini_ha_satellite.main"):
            service.detect()

        assert len(satellite.chunks) == 101
        assert len(broken.inputs) == 101
        assert caplog.text.count("wake-word detection failed for a chunk") == 2

    @pytest.mark.asyncio
    async def test_a_backlog_costs_detection_and_not_streaming(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A detector that cannot keep up must not slow the conversation down.

        Args:
            caplog: Where the pump says it is dropping chunks, which is the
                only outward sign that it happened.
        """
        model = FakeMicroWakeWord("okay_nabu")
        service, _state, satellite = await self._feed(model, chunks=5, backlog=2)

        with caplog.at_level(logging.WARNING, logger="reachy_mini_ha_satellite.main"):
            service.pump()

        assert len(satellite.chunks) == 5
        assert "behind the microphone" in caplog.text

    @pytest.mark.asyncio
    async def test_detection_ends_when_the_microphone_does(self) -> None:
        """Otherwise `detect` blocks on the queue for ever and shutdown hangs."""
        model = FakeMicroWakeWord("okay_nabu")
        service, _state, _satellite = await self._feed(model, chunks=1)

        service.pump()
        service.detect()

        assert True  # returning at all is the assertion: `detect` is bounded

    @pytest.mark.asyncio
    async def test_closing_ends_detection_that_never_started(self) -> None:
        """A service stopped before its first chunk still has a thread to end."""
        model = FakeMicroWakeWord("okay_nabu")
        service, _state, _satellite = await self._feed(model)

        await service.aclose()
        service.detect()

        assert True  # as above: the sentinel `aclose` leaves is what ends it

    @pytest.mark.asyncio
    async def test_an_activation_arriving_after_the_loop_has_gone_is_dropped(
        self,
    ) -> None:
        """Shutdown races the microphone, and this is the losing side of it.

        A wake word that fires in the moment between the event loop closing and
        the detection thread noticing has nothing left to start, and must not
        take the thread down with an exception nobody is there to see.
        """
        closed = _ClosedLoop()
        ran: list[str] = []

        # Reached through a private name deliberately: the guard is the unit
        # here, and the path to it runs from a thread this test cannot schedule
        # against a loop that is in the act of closing.
        EsphomeService._on_loop(
            cast("asyncio.AbstractEventLoop", closed),
            lambda: ran.append("woken"),
        )

        assert ran == []

    @pytest.mark.asyncio
    async def test_it_gives_up_rather_than_hanging_the_event_loop(self) -> None:
        """`aclose` runs on the event loop, so the sentinel cannot loop for ever.

        A queue that refuses every insertion and yields nothing to drain cannot
        happen with the real one; the bound exists so that if it ever did, the
        detection thread would be left to the process exit instead of wedging
        shutdown.
        """
        model = FakeMicroWakeWord("okay_nabu")
        service, _state, _satellite = await self._feed(model)
        # A private name again, and for the same kind of reason: the bound is
        # what is under test, and no real queue behaves this way.
        service._pending = cast("Queue[bytes | None]", _ImmovableQueue())

        await service.aclose()

        assert True  # returning at all is the assertion: the drain is bounded

    async def _feed(
        self,
        model: FakeMicroWakeWord,
        *,
        chunks: int = 1,
        stop_word: FakeMicroWakeWord | None = None,
        backlog: int = 50,
    ) -> tuple[EsphomeService, ServerState, _RecordingSatellite]:
        """Build a started service with a real detector over a fake model.

        Real detector, fake model: what is under test is that the pump runs
        detection at all and acts on what it says, and a canned detector would
        assert only half of that.

        Args:
            model: The wake word to activate.
            chunks: How many chunks the microphone produces.
            stop_word: The stop word, or a silent one.
            backlog: How many chunks may wait for detection.

        Returns:
            The started service, its state, and the connected satellite.
        """
        from satellite_support import vendored_server_state

        state = vendored_server_state(
            wake_words={model.id: model},
            active_wake_words={model.id},
            available_wake_words={model.id: available_wake_word(model.id)},
            stop_word=stop_word if stop_word is not None else FakeMicroWakeWord("stop"),
        )
        detector = WakeWordDetector(
            state,
            micro_features=FakeWakeWordFeatures,
            open_features=FakeWakeWordFeatures,
        )
        service, _state = _esphome_service(
            [],
            capture=FakeCapture([[b"\x00" * 320, b"\x01" * 320]] * chunks),
            state=state,
            detector=detector,
            backlog=backlog,
        )
        satellite = _RecordingSatellite()
        state.satellite = cast("VoiceSatelliteProtocol", satellite)
        await service.start()
        return service, state, satellite


class _ClosedLoop:
    """An event loop that has been closed, without one ever having existed.

    A real closed loop would have to be created first, and creating one opens a
    socket pair — which the harness refuses, and rightly.
    """

    def call_soon_threadsafe(
        self,
        callback: Callable[..., None],
        *args: object,
    ) -> None:
        """Refuse, the way a closed loop does.

        Args:
            callback: What was to be run.
            args: What it was to be run with.

        Raises:
            RuntimeError: Always.
        """
        del callback, args
        message = "Event loop is closed"
        raise RuntimeError(message)


class _ImmovableQueue:
    """A queue that will neither take anything nor give anything up."""

    maxsize = 1

    def put_nowait(self, item: object) -> None:
        """Refuse the item.

        Args:
            item: What was to be queued.

        Raises:
            Full: Always.
        """
        del item
        raise Full

    def get_nowait(self) -> object:
        """Refuse to hand anything back.

        Returns:
            Nothing; this always raises.

        Raises:
            Empty: Always.
        """
        raise Empty


class TestTheMicrophoneSettings:
    """The three entities Home Assistant owns that were inert until now."""

    @pytest.mark.asyncio
    async def test_the_microphone_volume_attenuates_what_is_streamed(self) -> None:
        """It was persisted, reported back, and applied to nothing."""
        loud = np.full(160, 1000, dtype="<i2").tobytes()
        service, state = _esphome_service([], capture=FakeCapture([[loud]]))
        state.mic_volume = 50
        satellite = _RecordingSatellite()
        state.satellite = cast("VoiceSatelliteProtocol", satellite)
        await service.start()

        service.pump()

        streamed = np.frombuffer(satellite.chunks[0][0], dtype="<i2")
        assert list(streamed) == [500] * 160

    def test_the_whole_slider_does_something(self) -> None:
        """Every step of the slider maps to its own factor, the bottom included.

        The bottom is what this is really about. A floor at a tenth would
        flatten `mic_volume` 1 through 10 onto one value, so an operator
        dragging Home Assistant's slider from ten to two would hear no change
        and have no way to find out why. Silence is not what the floor was
        guarding against either: `persist_mic_volume` clamps to 1, so the
        quietest the slider can ask for is a hundredth.
        """
        assert satellite_main._mic_volume_scalar(100) == pytest.approx(1.0)
        assert satellite_main._mic_volume_scalar(50) == pytest.approx(0.5)
        assert satellite_main._mic_volume_scalar(10) == pytest.approx(0.1)
        assert satellite_main._mic_volume_scalar(2) == pytest.approx(0.02)
        assert satellite_main._mic_volume_scalar(1) == pytest.approx(0.01)

    def test_it_only_ever_attenuates(self) -> None:
        """A microphone volume is not a way to amplify past what was captured."""
        assert satellite_main._mic_volume_scalar(150) == pytest.approx(1.0)

    def test_it_never_inverts_the_waveform(self) -> None:
        """Which a negative factor would do. Only a hand-edited file gets here."""
        assert satellite_main._mic_volume_scalar(-20) == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_audio_quiet_enough_to_round_to_zero_is_still_streamed(
        self,
    ) -> None:
        """At the bottom of the slider a quiet room quantises to silence.

        That is arithmetic rather than a failure, and nothing downstream may
        read an all-zero chunk as one: the emptiness the pump *does* act on is
        the conditioner having swallowed a block, which is zero bytes, not
        zero-valued samples.
        """
        quiet = np.full(160, 40, dtype="<i2").tobytes()
        service, state = _esphome_service([], capture=FakeCapture([[quiet]]))
        state.mic_volume = 1
        satellite = _RecordingSatellite()
        state.satellite = cast("VoiceSatelliteProtocol", satellite)
        await service.start()

        service.pump()

        streamed = np.frombuffer(satellite.chunks[0][0], dtype="<i2")
        assert list(streamed) == [0] * 160

    @pytest.mark.asyncio
    async def test_the_volume_never_touches_the_echo_reference(self) -> None:
        """Channel 1 is not quieter microphone audio, and scaling it is a defect.

        It is the speaker reference a server-side echo canceller subtracts, and
        the subtraction works on the gain relationship between the two
        channels. Attenuating the reference while the speaker played at full
        level leaves the canceller under-cancelling or adapting to the wrong
        filter — the robot hears its own voice and talks over itself. It fails
        silently, and only on a device that has a second channel, so nothing
        but this test would say it had happened.
        """
        speech = np.full(160, 1000, dtype="<i2").tobytes()
        reference = np.full(160, 800, dtype="<i2").tobytes()
        service, state = _esphome_service(
            [],
            capture=FakeCapture([[speech, reference]]),
        )
        state.mic_volume = 50
        satellite = _RecordingSatellite()
        state.satellite = cast("VoiceSatelliteProtocol", satellite)
        await service.start()

        service.pump()

        streamed, sent_reference = satellite.chunks[0]
        assert list(np.frombuffer(streamed, dtype="<i2")) == [500] * 160
        assert sent_reference == reference

    @pytest.mark.asyncio
    async def test_the_reference_is_passed_through_rather_than_rebuilt(self) -> None:
        """The default path allocates for neither channel."""
        speech = np.full(160, 1000, dtype="<i2").tobytes()
        reference = np.full(160, 800, dtype="<i2").tobytes()
        service, state = _esphome_service(
            [],
            capture=FakeCapture([[speech, reference]]),
        )
        satellite = _RecordingSatellite()
        state.satellite = cast("VoiceSatelliteProtocol", satellite)
        await service.start()

        service.pump()

        assert satellite.chunks[0][0] is speech
        assert satellite.chunks[0][1] is reference

    @pytest.mark.asyncio
    async def test_the_detector_is_only_ever_handed_channel_zero(self) -> None:
        """The reference channel reaching a wake-word model is the same defect.

        A model fed the speaker's own output would hear the robot rather than
        the room. The queue between the pump and the detector carries `bytes`
        rather than a sequence of channels, so this is structural — the test
        pins that it stays so.
        """
        from satellite_support import vendored_server_state

        speech = np.full(160, 1000, dtype="<i2").tobytes()
        reference = np.full(160, 800, dtype="<i2").tobytes()
        model = FakeMicroWakeWord("okay_nabu")
        state = vendored_server_state(
            wake_words={model.id: model},
            active_wake_words={model.id},
            available_wake_words={model.id: available_wake_word(model.id)},
            stop_word=FakeMicroWakeWord("stop"),
        )
        service, _state = _esphome_service(
            [],
            capture=FakeCapture([[speech, reference]]),
            state=state,
            detector=WakeWordDetector(
                state,
                micro_features=FakeWakeWordFeatures,
                open_features=FakeWakeWordFeatures,
            ),
        )
        state.satellite = cast("VoiceSatelliteProtocol", _RecordingSatellite())
        await service.start()

        service.pump()
        service.detect()

        assert [inputs.tobytes() for inputs in model.inputs] == [speech]

    @pytest.mark.asyncio
    async def test_the_default_volume_leaves_the_audio_exactly_alone(self) -> None:
        """Which is every chunk on a robot nobody has touched that slider on."""
        chunk = np.full(160, 1000, dtype="<i2").tobytes()
        service, state = _esphome_service([], capture=FakeCapture([[chunk]]))
        satellite = _RecordingSatellite()
        state.satellite = cast("VoiceSatelliteProtocol", satellite)
        await service.start()

        service.pump()

        assert satellite.chunks[0][0] is chunk

    @pytest.mark.asyncio
    async def test_gain_and_noise_suppression_reach_the_conditioner(self) -> None:
        """Two more entities that took a value and changed nothing audible."""
        built: list[tuple[int, int]] = []
        service, state = _esphome_service(
            [],
            capture=FakeCapture([[b"\x02" * 320]] * 2),
            build_webrtc=lambda gain, noise: _FakeWebRTC(built, gain, noise),
        )
        state.preferences.mic_auto_gain = 2
        state.preferences.mic_noise_suppression = 3
        satellite = _RecordingSatellite()
        state.satellite = cast("VoiceSatelliteProtocol", satellite)
        await service.start()

        service.pump()

        assert built == [(2, 3)]
        assert satellite.chunks[0][0] == b"\x03" * 320

    @pytest.mark.asyncio
    async def test_a_block_the_conditioner_swallowed_is_not_streamed(self) -> None:
        """It buffers into ten-millisecond frames and can answer with nothing."""
        service, state = _esphome_service(
            [],
            capture=FakeCapture([[b"\x02" * 320]]),
            build_webrtc=lambda gain, noise: _FakeWebRTC([], gain, noise, swallow=True),
        )
        state.preferences.mic_auto_gain = 1
        satellite = _RecordingSatellite()
        state.satellite = cast("VoiceSatelliteProtocol", satellite)
        await service.start()

        service.pump()

        assert satellite.chunks == []


class _FakeWebRTC:
    """A microphone conditioner that records how it was built and driven."""

    def __init__(
        self,
        built: list[tuple[int, int]],
        agc_level: int,
        ns_level: int,
        *,
        swallow: bool = False,
    ) -> None:
        """Record the levels it was built with.

        Args:
            built: Where to record that it was built.
            agc_level: The gain level.
            ns_level: The noise-suppression level.
            swallow: Whether to answer with nothing, as the real one does while
                it is still filling a frame.
        """
        built.append((agc_level, ns_level))
        self.updates: list[tuple[int, int]] = []
        self._swallow = swallow

    def update_settings(self, agc_level: int, ns_level: int) -> None:
        """Record a change of levels.

        Args:
            agc_level: The gain level.
            ns_level: The noise-suppression level.
        """
        self.updates.append((agc_level, ns_level))

    def process(self, raw_bytes: bytes) -> bytes:
        """Condition one chunk, visibly.

        Args:
            raw_bytes: The audio.

        Returns:
            Something recognisably different from the input, or nothing at all.
        """
        if self._swallow:
            return b""
        return bytes(byte + 1 for byte in raw_bytes)


class TestTheDaemonsOwnVolume:
    """R7: the coarse control below this application is driven too.

    It was found at 62 of 100 on the robot, with nothing here aware it existed
    — a third of the level thrown away before the software boost is asked to
    make any of it up.
    """

    @pytest.mark.asyncio
    async def test_starting_asks_the_daemon_for_its_loudest(self) -> None:
        """Once, at start-up, so the boost begins from the loudest signal."""
        asked: list[str] = []
        service = VolumeService(
            _DAEMON_API,
            set_volume=_recording(asked),
            offload=inline,
        )

        await service.start()

        assert asked == [_DAEMON_API]

    @pytest.mark.asyncio
    async def test_a_daemon_that_refuses_does_not_stop_the_application(
        self,
    ) -> None:
        """A quieter robot is better than one that would not start."""
        service = VolumeService(
            _DAEMON_API,
            set_volume=lambda _url: False,
            offload=inline,
        )

        await service.start()

        assert True  # returning at all is the assertion: nothing propagated

    @pytest.mark.asyncio
    async def test_closing_leaves_the_volume_where_it_is(self) -> None:
        """The daemon's volume is the robot's, not this application's.

        An operator who turned it up after start-up should not have it put back
        because a voice satellite stopped.
        """
        asked: list[str] = []
        service = VolumeService(
            _DAEMON_API,
            set_volume=_recording(asked),
            offload=inline,
        )

        await service.start()
        await service.aclose()

        assert asked == [_DAEMON_API]


class TestTheAdvertisement:
    """The mDNS record Home Assistant discovers the satellite through."""

    @pytest.mark.asyncio
    async def test_it_registers_what_the_robot_announces(self) -> None:
        """Name, port and hardware address, which is the identity."""
        built: list[dict[str, object]] = []
        service = AdvertisementService(
            name="reachy-mini-1",
            port=6053,
            identity=_identity(),
            build=lambda **kwargs: _FakeZeroconf(built, **kwargs),
        )

        await service.start()

        assert built[0]["name"] == "reachy-mini-1"
        assert built[0]["mac_address"] == "02:00:5e:10:00:00"

    @pytest.mark.asyncio
    async def test_it_withdraws_the_record_on_the_way_out(self) -> None:
        """Otherwise Home Assistant holds a device that is not there."""
        built: list[dict[str, object]] = []
        advertiser: list[_FakeZeroconf] = []

        def _build(**kwargs: object) -> _FakeZeroconf:
            """Build the fake advertiser and keep it.

            Args:
                kwargs: What the service passed.

            Returns:
                The advertiser.
            """
            made = _FakeZeroconf(built, **kwargs)
            advertiser.append(made)
            return made

        service = AdvertisementService(
            name="reachy-mini-1",
            port=6053,
            identity=_identity(),
            build=_build,
        )
        await service.start()

        await service.aclose()

        assert advertiser[0].closed == 1

    @pytest.mark.asyncio
    async def test_closing_one_that_never_started_is_not_an_error(self) -> None:
        """Shutdown runs however startup went."""
        service = AdvertisementService(
            name="reachy-mini-1",
            port=6053,
            identity=_identity(),
        )

        await service.aclose()

        assert True  # not raising is the assertion


class TestTheSettingsService:
    """The interface, run beside the protocol on its own port."""

    @pytest.mark.asyncio
    async def test_it_serves_until_it_is_asked_to_stop(self) -> None:
        """The lifecycle, with the server injected so nothing binds a socket."""
        stopped = asyncio.Event()

        async def _serve() -> None:
            """Serve until asked to stop."""
            await stopped.wait()

        service = WebService(
            _nothing_asgi,
            host="127.0.0.1",
            port=8088,
            serve=_serve,
            shutdown=stopped.set,
        )
        await service.start()

        await service.aclose()

        assert stopped.is_set()

    @pytest.mark.asyncio
    async def test_closing_one_that_never_started_is_not_an_error(self) -> None:
        """Shutdown runs however startup went."""
        service = WebService(_nothing_asgi, host="127.0.0.1", port=8088)

        await service.aclose()

        assert True  # not raising is the assertion


class TestBuildingThePerceptionSource:
    """ha-satellite REQ-047: three selections, and one way of having none."""

    def test_tracking_switched_off_builds_nothing(self) -> None:
        """No session is opened and no model is loaded."""
        assert (
            build_perception_source(
                _settings(face_tracking_enabled="false"),
                FakeRobot().media,
            )
            is None
        )

    def test_the_remote_selection_builds_a_session_source(self) -> None:
        """The default, and the one the robot's core budget is measured against."""
        source = build_perception_source(_settings(), FakeRobot().media)

        assert isinstance(source, RemotePerception)

    def test_the_local_selection_builds_a_detector_and_no_session(self) -> None:
        """An installation with no groundstation deployed."""
        source = build_perception_source(
            _settings(
                detection_source=_ROBOT_ONLY.value,
                local_model_path="/models/face.onnx",
            ),
            FakeRobot().media,
        )

        assert isinstance(source, LocalPerception)

    def test_the_fallback_selection_builds_both(self) -> None:
        """And the behaviour layer cannot tell which of them answered."""
        source = build_perception_source(
            _settings(
                detection_source=SourceSelection.REMOTE_WITH_LOCAL_FALLBACK.value,
                local_model_path="/models/face.onnx",
            ),
            FakeRobot().media,
        )

        assert isinstance(source, FallbackPerception)


class TestPreferences:
    """What Home Assistant sets through entities, not what an operator configures."""

    def test_a_missing_file_yields_the_defaults(self, fs: FakeFilesystem) -> None:
        """Losing a volume setting is not worth refusing to start over.

        Args:
            fs: An in-memory filesystem.
        """
        del fs

        assert load_preferences(Path("/nowhere/preferences.json")) == Preferences()

    def test_what_was_saved_is_read_back(self, fs: FakeFilesystem) -> None:
        """Volume and the active wake words survive a restart.

        Args:
            fs: An in-memory filesystem.
        """
        path = Path("/reachy-satellite-prefs/preferences.json")
        fs.create_file(
            path,
            contents='{"volume": 0.4, "active_wake_words": ["okay_nabu"]}',
        )

        preferences = load_preferences(path)

        assert preferences.volume == pytest.approx(0.4)
        assert preferences.active_wake_words == ["okay_nabu"]

    def test_an_unknown_key_is_ignored(self, fs: FakeFilesystem) -> None:
        """A file written by a later version must not stop this one starting.

        Args:
            fs: An in-memory filesystem.
        """
        path = Path("/reachy-satellite-prefs/preferences.json")
        fs.create_file(path, contents='{"volume": 0.4, "invented": 1}')

        assert not hasattr(load_preferences(path), "invented")

    def test_a_file_that_is_not_an_object_yields_the_defaults(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """Reported in the log, and not a reason to refuse to start.

        Args:
            fs: An in-memory filesystem.
        """
        path = Path("/reachy-satellite-prefs/preferences.json")
        fs.create_file(path, contents="[1, 2]")

        assert load_preferences(path) == Preferences()

    def test_a_file_that_is_not_json_yields_the_defaults(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """Same reason.

        Args:
            fs: An in-memory filesystem.
        """
        path = Path("/reachy-satellite-prefs/preferences.json")
        fs.create_file(path, contents="{not json")

        assert load_preferences(path) == Preferences()


class TestLogging:
    """One setting, installed process-wide."""

    def test_the_configured_level_is_installed(self) -> None:
        """So `log_level=debug` is a thing that has an effect."""
        configure_logging(_settings(log_level="warning"))

        assert logging.getLogger().level == logging.WARNING


class TestWritingABoostChosenFromHomeAssistant:
    """The setter the speaker-boost control is handed, over a real store."""

    def test_it_persists_the_value_and_adopts_it_at_once(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """Persisted, so a restart keeps it; adopted, so the answer changes now.

        Args:
            fs: An in-memory filesystem, so the overrides file is a real file
                and nothing reaches a disk.
        """
        del fs
        store = OverrideStore(_BOOST_OVERRIDES)
        adopted: list[Settings] = []

        satellite_main.build_boost_setter(
            store=store,
            apply_live=adopted.append,
            environ=_ENVIRONMENT,
        )(640.0)

        assert store.load() == {"speaker_boost_percent": "640.0"}
        assert [settings.speaker_boost_percent for settings in adopted] == [
            pytest.approx(640.0),
        ]

    def test_it_leaves_every_other_override_alone(self, fs: FakeFilesystem) -> None:
        """A slider is not a form: it must not drop what somebody else wrote.

        Args:
            fs: An in-memory filesystem.
        """
        del fs
        store = OverrideStore(_BOOST_OVERRIDES)
        store.save({"log_level": "debug"})
        adopted: list[Settings] = []

        satellite_main.build_boost_setter(
            store=store,
            apply_live=adopted.append,
            environ=_ENVIRONMENT,
        )(300.0)

        assert store.load() == {
            "log_level": "debug",
            "speaker_boost_percent": "300.0",
        }

    def test_setting_the_value_already_in_the_file_writes_nothing(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """A scene re-sending what it sent last time must not cost an erase cycle.

        The inode is the evidence, because nothing about the bytes could tell
        "written again identically" from "not written". So the test moves the
        value once first, and only then repeats it: the first pair of readings
        establishes that a write does move the inode here, which is what makes
        the second pair's equality mean the write was declined.

        Args:
            fs: An in-memory filesystem.
        """
        del fs
        store = OverrideStore(_BOOST_OVERRIDES)
        adopted: list[Settings] = []
        setter = satellite_main.build_boost_setter(
            store=store,
            apply_live=adopted.append,
            environ=_ENVIRONMENT,
        )
        setter(300.0)
        before_a_real_change = _BOOST_OVERRIDES.stat().st_ino

        setter(640.0)
        after_a_real_change = _BOOST_OVERRIDES.stat().st_ino

        # The store renames a new file into place rather than writing in place,
        # so a write that changes the value leaves a different inode behind.
        # That is what makes the repeat below mean anything: without it, a
        # `save` that wrote in place would satisfy the equality while writing
        # every single time.
        assert after_a_real_change != before_a_real_change

        setter(640.0)

        assert _BOOST_OVERRIDES.stat().st_ino == after_a_real_change
        assert store.load() == {"speaker_boost_percent": "640.0"}
        assert [settings.speaker_boost_percent for settings in adopted] == [
            pytest.approx(300.0),
            pytest.approx(640.0),
        ]

    def test_a_change_after_a_repeat_is_still_written(self, fs: FakeFilesystem) -> None:
        """The guard drops a repeat, never the next real move of the slider.

        Args:
            fs: An in-memory filesystem.
        """
        del fs
        store = OverrideStore(_BOOST_OVERRIDES)
        adopted: list[Settings] = []
        setter = satellite_main.build_boost_setter(
            store=store,
            apply_live=adopted.append,
            environ=_ENVIRONMENT,
        )

        setter(640.0)
        setter(640.0)
        setter(300.0)

        assert store.load() == {"speaker_boost_percent": "300.0"}
        assert [settings.speaker_boost_percent for settings in adopted] == [
            pytest.approx(640.0),
            pytest.approx(300.0),
        ]

    def test_a_value_equal_to_the_environments_is_still_pinned(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """The guard is about the file, not about the layer underneath it.

        A slider has no "revert to the environment" gesture to undo a pin with,
        so a first set is written even where the environment already says that —
        which is what `build_boost_setter`'s own docstring promises, and what the
        skip-an-identical-write guard must not quietly reverse.

        Args:
            fs: An in-memory filesystem.
        """
        del fs
        store = OverrideStore(_BOOST_OVERRIDES)
        adopted: list[Settings] = []

        satellite_main.build_boost_setter(
            store=store,
            apply_live=adopted.append,
            environ={
                **_ENVIRONMENT,
                f"{ENV_PREFIX}SPEAKER_BOOST_PERCENT": "300.0",
            },
        )(300.0)

        assert store.load() == {"speaker_boost_percent": "300.0"}
        assert [settings.speaker_boost_percent for settings in adopted] == [
            pytest.approx(300.0),
        ]

    def test_a_store_that_cannot_be_written_is_reported_and_not_raised(
        self,
        fs: FakeFilesystem,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """It runs inside the protocol's loop, so raising would drop a client.

        Args:
            fs: An in-memory filesystem.
            caplog: Where the refusal is looked for.
        """
        # A file where the store wants a directory, so the write cannot succeed
        # and the failure is the store's own rather than a patched one.
        fs.create_file(_BOOST_STATE_DIR)
        store = OverrideStore(_BOOST_OVERRIDES)
        adopted: list[Settings] = []

        with caplog.at_level(logging.ERROR):
            satellite_main.build_boost_setter(
                store=store,
                apply_live=adopted.append,
                environ=_ENVIRONMENT,
            )(300.0)

        assert adopted == []
        assert "the speaker boost could not be saved" in caplog.text

    def test_a_store_that_cannot_be_read_is_reported_and_not_raised(
        self,
        fs: FakeFilesystem,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The read is inside the guard too, so a hand-broken file is reported.

        `OverrideStore.load` raises for a file that exists and is not a JSON
        object of strings, and the setter reads before it decides whether the
        write is a repeat. A read left outside the `try` would send that error
        out of `handle_message` and into the protocol's loop — the very thing
        the reported-not-raised rule above exists to prevent.

        Args:
            fs: An in-memory filesystem.
            caplog: Where the refusal is looked for.
        """
        fs.create_file(_BOOST_OVERRIDES, contents="{not json")
        store = OverrideStore(_BOOST_OVERRIDES)
        adopted: list[Settings] = []

        with caplog.at_level(logging.ERROR):
            satellite_main.build_boost_setter(
                store=store,
                apply_live=adopted.append,
                environ=_ENVIRONMENT,
            )(300.0)

        assert adopted == []
        assert "the speaker boost could not be saved" in caplog.text


@pytest.mark.asyncio
async def test_long_motor_confirmation_does_not_trigger_controller_timing_fault() -> (
    None
):
    """A paused SDK worker leaves behavior cadence and controller health intact."""

    class PausedRobot(FakeRobot):
        """Pause one confirmed disable without blocking the event loop."""

        def __init__(self) -> None:
            super().__init__()
            self.started = threading.Event()
            self.release_confirmation = threading.Event()

        def disable_motors_confirmed(self, ids: list[str]) -> MotorConfirmation:
            self.started.set()
            self.release_confirmation.wait()
            return super().disable_motors_confirmed(ids)

    robot = PausedRobot()
    groups = MotorGroupCoordinator(robot, clock=ManualClock())
    await groups.initialize()
    robot.motor_disables_confirmed.append(_motor_confirmation(HEAD_MOTOR_IDS, False))
    motion = FakeMotion()
    application, stop = _application(
        audio=FakeAudio(),
        motion=motion,
        perception=FakePerception(),
        motor_groups=groups,
        stop_after=60,
    )
    published: list[bool] = []
    assert groups.reserve_transition(
        MotorGroup.HEAD,
        False,
        lambda: published.append(True),
    )

    # Park the worker before the behavior loop starts, so what the ticks below
    # are measured against is a confirmation that is already in flight. Waiting
    # for the pause on a helper thread rather than counting loop turns keeps the
    # loop free for the operation's own executor callbacks, which is what lets
    # the worker reach the pause at all.
    await asyncio.get_running_loop().run_in_executor(None, robot.started.wait)
    running = asyncio.create_task(application.run(stop))
    for _ in range(20):
        await asyncio.sleep(0)

    controller = cast("dict[str, object]", application.status()["controller"])
    assert controller["fault"] == "none"
    assert controller["safe_hold"] is False
    assert len(motion.observed) >= 2
    assert not running.done()

    robot.release_confirmation.set()
    await running

    controller = cast("dict[str, object]", application.status()["controller"])
    assert controller["fault"] == "none"
    assert controller["safe_hold"] is False
    assert published == []


@pytest.mark.asyncio
async def test_application_shutdown_drains_reserved_target_before_motion_release() -> (
    None
):
    """Terminal is responsive but cleanup cannot release beneath an active target."""
    events: list[str] = []
    started = threading.Event()
    release_target = threading.Event()
    robot = FakeRobot()
    groups = MotorGroupCoordinator(robot, clock=ManualClock())
    await groups.initialize()

    class RecordingMotion(FakeMotion):
        def release(self) -> None:
            events.append("motion.release")
            super().release()

    motion = RecordingMotion()
    application, _stop = _application(
        audio=FakeAudio(),
        motion=motion,
        perception=FakePerception(),
        motor_groups=groups,
    )

    def _target() -> None:
        events.append("target.start")
        started.set()
        release_target.wait()
        events.append("target.finish")

    producer = threading.Thread(
        target=lambda: groups.command((MotorGroup.HEAD,), _target),
        name="paused-application-target",
    )
    producer.start()
    started.wait()

    closing = asyncio.create_task(application.aclose())
    await asyncio.sleep(0)
    assert events == ["target.start"]
    assert not motion.released
    assert not groups.command((MotorGroup.HEAD,), lambda: None)

    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done()
    release_target.set()
    producer.join()
    with pytest.raises(asyncio.CancelledError):
        await closing

    assert events == ["target.start", "target.finish", "motion.release"]


@pytest.mark.filesystem
class TestTheWiringAgainstTheWheelsOwnAssets:
    """The assembly, reading the wake-word models and sounds the wheel ships.

    These read real files, and that is the point: ha-satellite REQ-044 says the
    wake word runs on the robot without depending on the groundstation or on
    Home Assistant, and a fake asset directory would pin whatever the fake was
    told to contain rather than what the wheel carries.
    """

    @staticmethod
    async def _composed_head_switch(
        robot: FakeRobot,
    ) -> tuple[SatelliteApplication, ServerState, MotorSwitchEntity]:
        """Assemble production wiring and return its registered head control."""
        application = await build_application(
            load_settings(_ENVIRONMENT),
            robot,
            identity=_identity(),
        )
        esphome = next(
            service
            for service in application.services
            if isinstance(service, EsphomeService)
        )
        control = next(
            entity
            for entity in esphome._state.entities
            if isinstance(entity, MotorSwitchEntity)
        )
        return application, esphome._state, control

    def test_the_server_state_announces_the_configured_identity(self) -> None:
        """REQ-040, at the place the announcement is actually assembled."""
        state = build_server_state(
            _settings(),
            identity=_identity(),
            audio=FakeAudio(),
            state_dir=Path("/reachy-satellite-main"),
        )

        assert state.name == "reachy-mini-1"
        assert state.mac_address == "02:00:5e:10:00:00"

    def test_the_shipped_wake_word_loads_from_the_wheel(self) -> None:
        """No network, no groundstation, no Home Assistant — REQ-044."""
        state = build_server_state(
            _settings(),
            identity=_identity(),
            audio=FakeAudio(),
            state_dir=Path("/reachy-satellite-main"),
        )

        assert state.active_wake_words
        assert state.stop_word is not None

    def test_the_persisted_microphone_settings_come_back(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Otherwise every restart quietly puts the four of them back to default.

        The preferences are supplied rather than written to a file: what is
        under test is that they reach the state, and `load_preferences` has its
        own tests. The wake-word models still load off the real asset
        directory, which is why this belongs in this class.

        Args:
            monkeypatch: Used to hand the assembly a set of preferences.
        """
        saved = Preferences(
            mic_volume=40,
            mic_auto_gain=2,
            mic_noise_suppression=3,
            stop_word_sensitivity=0.25,
        )
        monkeypatch.setattr(satellite_main, "load_preferences", lambda _path: saved)

        state = build_server_state(
            _settings(),
            identity=_identity(),
            audio=FakeAudio(),
            state_dir=Path("/reachy-satellite-main"),
        )

        assert state.mic_volume == 40
        assert state.mic_auto_gain == 2
        assert state.mic_noise_suppression == 3
        assert state.stop_word_threshold == pytest.approx(0.25)

    def test_both_audio_seams_are_filled_by_the_ports(self) -> None:
        """Which is what change 0011 left open and 0012 supplied."""
        audio = FakeAudio()

        state = build_server_state(
            _settings(),
            identity=_identity(),
            audio=audio,
            state_dir=Path("/reachy-satellite-main"),
        )

        assert state.audio_capture is audio.capture
        assert state.music_player is audio.music
        assert state.tts_player is audio.speech

    def test_every_sound_slot_points_at_a_shipped_file(self) -> None:
        """A missing chime is a silent robot, which is hard to notice."""
        state = build_server_state(
            _settings(),
            identity=_identity(),
            audio=FakeAudio(),
            state_dir=Path("/reachy-satellite-main"),
        )

        for slot in (
            state.wakeup_sound,
            state.start_listening_sound,
            state.processing_sound,
            state.timer_finished_sound,
            state.mute_sound,
            state.unmute_sound,
            state.button_double_press_sound,
            state.button_triple_press_sound,
            state.button_long_press_sound,
        ):
            assert Path(slot).is_file()

    def test_the_daemons_volume_service_is_wired_in(self) -> None:
        """R7: a service nothing starts is a requirement nothing satisfies."""
        application = assembled(
            load_settings(_ENVIRONMENT),
            cast("RobotHandle", FakeRobot()),
            identity=_identity(),
        )

        assert any(
            isinstance(service, VolumeService) for service in application.services
        )

    def test_a_boost_adopted_after_assembly_reaches_home_assistant(self) -> None:
        """The composition root is where the control and the application meet.

        A publisher that was never registered would leave the slider showing the
        previous number after a change made on the settings page, with nothing
        else failing anywhere — so this pins the wiring rather than the entity,
        which `test_satellite_audio_entities.py` covers on its own.
        """
        application = assembled(
            load_settings(_ENVIRONMENT),
            cast("RobotHandle", FakeRobot()),
            identity=_identity(),
        )
        esphome = next(
            service
            for service in application.services
            if isinstance(service, EsphomeService)
        )
        # Reaching past the private name deliberately, exactly as the settings
        # page's own test below does: `EsphomeService` exists to own a
        # lifecycle, and widening its public surface so a test could read the
        # state back would be shaping production code around this check. The
        # alternative is not pinning the wiring at all.
        state = esphome._state
        boost = next(
            entity
            for entity in state.entities
            if isinstance(entity, SpeakerBoostNumberEntity)
        )
        client = connected(state)[0]

        application.apply_live(_settings(speaker_boost_percent="640"))

        assert pushed_numbers(client, boost.key) == pytest.approx([640.0])


class TestMotorComposition:
    """Production motor wiring exercised entirely through deterministic fakes."""

    _composed_head_switch = staticmethod(
        TestTheWiringAgainstTheWheelsOwnAssets._composed_head_switch
    )

    @pytest.mark.asyncio
    async def test_a_cancelled_initial_confirmation_registers_and_leaks_nothing(
        self,
    ) -> None:
        """Assembly owns the coordinator until the application is handed it.

        A confirmation cancelled part-way therefore has exactly one thing that
        can close its worker, and it is here. Nothing downstream exists yet to
        do it, so a startup that let the exception past would leave a thread
        holding the robot for the life of the process.
        """
        workers_before = motor_worker_threads()
        started = threading.Event()
        release = threading.Event()

        class _ParkedRobot(FakeRobot):
            """Park the first correlated read until this test releases it."""

            def read_motor_torque(self, ids: list[str]) -> MotorConfirmation:
                started.set()
                release.wait()
                return super().read_motor_torque(ids)

        robot = _ParkedRobot()
        assembling = asyncio.create_task(
            build_application(
                load_settings(_ENVIRONMENT),
                robot,
                identity=_identity(),
            )
        )
        await asyncio.get_running_loop().run_in_executor(None, started.wait)

        assembling.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await assembling

        assert motor_worker_threads() <= workers_before

    def test_confirmed_motor_switches_are_composed_after_stable_audio_entities(
        self,
    ) -> None:
        """All three exact groups are present only after their initial read-back."""
        application = assembled(
            load_settings(_ENVIRONMENT),
            FakeRobot(),
            identity=_identity(),
        )
        esphome = next(
            service
            for service in application.services
            if isinstance(service, EsphomeService)
        )
        motors = [
            entity
            for entity in esphome._state.entities
            if isinstance(entity, MotorSwitchEntity)
        ]

        assert [entity.key for entity in motors] == [2, 3, 4]
        motion = cast("ReachyMotion", application._motion)
        assert application.motor_groups is motion._coordinator
        assert "motors" in application.status()

    def test_enabled_startup_reseeds_measured_targets_before_policy_and_registration(
        self,
    ) -> None:
        """All enabled groups start from hardware with no hidden target jump."""
        measured_head = head_pose_matrix(HeadPose(yaw=0.4, pitch=0.2))
        measured_joints = ([0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], [0.3, -0.4])
        robot = FakeRobot(
            measured_head_poses=[measured_head, measured_head],
            measured_joints=[measured_joints, measured_joints, measured_joints],
        )
        application = assembled(
            load_settings(_ENVIRONMENT, {"body_motion_enabled": "true"}),
            robot,
            identity=_identity(),
        )
        groups = application.motor_groups
        assert groups is not None
        motion = cast("ReachyMotion", application._motion)
        behaviour = application._behaviour

        head_read = robot.events.index("motors.read")
        first_pose = robot.events.index("motion.pose", head_read)
        body_quiesce = robot.events.index("motion.auto_yaw.false", first_pose)
        body_read = robot.events.index("motors.read", body_quiesce)
        body_pose = robot.events.index("motion.pose", body_read)
        body_joints = robot.events.index("motion.joints", body_pose)
        body_restore = robot.events.index("motion.auto_yaw.true", body_joints)
        antenna_read = robot.events.index("motors.read", body_restore)
        antenna_joints = robot.events.index("motion.joints", antenna_read)

        assert (
            head_read
            < first_pose
            < body_quiesce
            < body_read
            < body_pose
            < body_joints
            < body_restore
            < antenna_read
            < antenna_joints
        )
        assert all(groups.gate_open(group) for group in MotorGroup)
        assert behaviour._last_head is not None
        assert behaviour._last_head.yaw == pytest.approx(0.3)
        assert behaviour._last_head.pitch == pytest.approx(0.2)
        assert behaviour._last_head.roll == 0.0
        assert behaviour._last_antennas == AntennaPose(right=0.3, left=-0.4)
        controller = behaviour.controller_state
        assert controller.world_yaw.position == pytest.approx(0.4)
        assert controller.elevation.position == pytest.approx(0.2)
        assert controller.body_yaw.position == pytest.approx(0.1)
        assert motion._cache == {}
        assert motion._deferred == {}
        assert robot.targets == []

    def test_confirmed_disabled_startup_registers_false_without_movement_reseed(
        self,
    ) -> None:
        """A physically off group is truthful and closed without sampling its target."""
        robot = FakeRobot(
            motor_reads=[
                _motor_confirmation(HEAD_MOTOR_IDS, False),
                _motor_confirmation(BODY_MOTOR_IDS, True),
                _motor_confirmation(ANTENNA_MOTOR_IDS, True),
            ]
        )
        application = assembled(
            load_settings(_ENVIRONMENT),
            robot,
            identity=_identity(),
        )
        esphome = next(
            service
            for service in application.services
            if isinstance(service, EsphomeService)
        )
        head = next(
            entity
            for entity in esphome._state.entities
            if isinstance(entity, MotorSwitchEntity)
            and entity._group is MotorGroup.HEAD
        )
        groups = application.motor_groups
        assert groups is not None

        assert head.state_message() == SwitchStateResponse(key=head.key, state=False)
        assert not groups.gate_open(MotorGroup.HEAD)
        assert groups.last_confirmed(MotorGroup.HEAD) is False
        assert robot.events[:2] == ["motors.read", "motion.auto_yaw.false"]
        assert robot.targets == []

    @pytest.mark.asyncio
    async def test_composed_reconnect_refreshes_once_without_list_reads_or_broadcast(
        self,
    ) -> None:
        """Reconnect returns retained state while one worker read completes later."""
        robot = FakeRobot(
            motor_reads=[
                _motor_confirmation(HEAD_MOTOR_IDS, True),
                _motor_confirmation(BODY_MOTOR_IDS, True),
                _motor_confirmation(ANTENNA_MOTOR_IDS, True),
                _motor_confirmation(HEAD_MOTOR_IDS, False),
                _motor_confirmation(HEAD_MOTOR_IDS, True),
            ]
        )
        application, state, control = await self._composed_head_switch(robot)
        groups = application.motor_groups
        assert groups is not None
        clients = connected(state, 2)
        before = len(robot.motor_requests)

        listed = list(control.handle_message(ListEntitiesRequest()))
        first = list(control.handle_message(SubscribeHomeAssistantStatesRequest()))

        assert len(listed) == 1
        assert first == [SwitchStateResponse(key=control.key, state=True)]
        assert len(robot.motor_requests) == before
        assert all(client.sent == [] for client in clients)
        await groups.wait_idle()
        assert robot.motor_requests[before:] == [("read", HEAD_MOTOR_IDS)]
        assert groups.last_confirmed(MotorGroup.HEAD) is False
        assert not groups.gate_open(MotorGroup.HEAD)
        assert all(
            client.sent == [SwitchStateResponse(key=control.key, state=False)]
            for client in clients
        )

        second = list(control.handle_message(SubscribeHomeAssistantStatesRequest()))

        assert second == [SwitchStateResponse(key=control.key, state=False)]
        assert robot.motor_requests[before:] == [("read", HEAD_MOTOR_IDS)]
        await groups.wait_idle()
        assert robot.motor_requests[before:] == [
            ("read", HEAD_MOTOR_IDS),
            ("read", HEAD_MOTOR_IDS),
        ]
        assert groups.last_confirmed(MotorGroup.HEAD) is True
        assert not groups.gate_open(MotorGroup.HEAD)
        assert all(
            client.sent
            == [
                SwitchStateResponse(key=control.key, state=False),
                SwitchStateResponse(key=control.key, state=True),
            ]
            for client in clients
        )
        await groups.aclose()

    @pytest.mark.asyncio
    async def test_composed_failed_command_refreshes_once_and_yields_fresh_readback(
        self,
    ) -> None:
        """A failed command returns retained state while one recovery read runs."""
        robot = FakeRobot(
            motor_reads=[
                _motor_confirmation(HEAD_MOTOR_IDS, True),
                _motor_confirmation(BODY_MOTOR_IDS, True),
                _motor_confirmation(ANTENNA_MOTOR_IDS, True),
                _motor_confirmation(HEAD_MOTOR_IDS, False),
            ],
            motor_disables_confirmed=[MotorConfirmation.failed()],
        )
        application, state, control = await self._composed_head_switch(robot)
        groups = application.motor_groups
        assert groups is not None
        clients = connected(state, 2)
        before = len(robot.motor_requests)

        responses = list(
            control.handle_message(
                SwitchCommandRequest(key=control.key, state=False),
            )
        )

        assert responses == [SwitchStateResponse(key=control.key, state=True)]
        assert len(robot.motor_requests) == before
        assert not groups.gate_open(MotorGroup.HEAD)
        await groups.wait_idle()
        assert robot.motor_requests[before:] == [
            ("disable", HEAD_MOTOR_IDS),
            ("read", HEAD_MOTOR_IDS),
        ]
        assert groups.last_confirmed(MotorGroup.HEAD) is False
        assert all(
            client.sent == [SwitchStateResponse(key=control.key, state=False)]
            for client in clients
        )
        await groups.aclose()

    @pytest.mark.asyncio
    async def test_composed_incomplete_refresh_retains_until_one_later_attempt(
        self,
    ) -> None:
        """Incomplete recovery remains closed and another request gets one attempt."""
        robot = FakeRobot(
            motor_reads=[
                _motor_confirmation(HEAD_MOTOR_IDS, True),
                _motor_confirmation(BODY_MOTOR_IDS, True),
                _motor_confirmation(ANTENNA_MOTOR_IDS, True),
                MotorConfirmation.failed(),
                _motor_confirmation(HEAD_MOTOR_IDS, False),
            ],
            motor_disables_confirmed=[
                MotorConfirmation.failed(),
                MotorConfirmation.failed(),
            ],
        )
        application, state, control = await self._composed_head_switch(robot)
        groups = application.motor_groups
        assert groups is not None
        clients = connected(state, 2)
        before = len(robot.motor_requests)

        retained = list(
            control.handle_message(
                SwitchCommandRequest(key=control.key, state=False),
            )
        )

        assert retained == [SwitchStateResponse(key=control.key, state=True)]
        await groups.wait_idle()
        assert robot.motor_requests[before:] == [
            ("disable", HEAD_MOTOR_IDS),
            ("read", HEAD_MOTOR_IDS),
        ]
        assert groups.last_confirmed(MotorGroup.HEAD) is True
        assert not groups.gate_open(MotorGroup.HEAD)
        assert all(client.sent == [] for client in clients)

        updated = list(
            control.handle_message(
                SwitchCommandRequest(key=control.key, state=False),
            )
        )

        assert updated == [SwitchStateResponse(key=control.key, state=True)]
        await groups.wait_idle()
        assert robot.motor_requests[before:] == [
            ("disable", HEAD_MOTOR_IDS),
            ("read", HEAD_MOTOR_IDS),
            ("disable", HEAD_MOTOR_IDS),
            ("read", HEAD_MOTOR_IDS),
        ]
        assert groups.last_confirmed(MotorGroup.HEAD) is False
        assert not groups.gate_open(MotorGroup.HEAD)
        assert all(
            client.sent == [SwitchStateResponse(key=control.key, state=False)]
            for client in clients
        )
        await groups.aclose()

    @pytest.mark.asyncio
    async def test_composed_body_refresh_true_keeps_policy_quiesced_and_gate_closed(
        self,
    ) -> None:
        """Independent truth cannot restore automatic yaw or reopen body producers."""
        robot = FakeRobot(
            motor_reads=[
                _motor_confirmation(HEAD_MOTOR_IDS, True),
                _motor_confirmation(BODY_MOTOR_IDS, True),
                _motor_confirmation(ANTENNA_MOTOR_IDS, True),
                _motor_confirmation(BODY_MOTOR_IDS, True),
                _motor_confirmation(BODY_MOTOR_IDS, True),
            ],
            motor_disables_confirmed=[MotorConfirmation.failed()],
        )
        application = await build_application(
            load_settings(_ENVIRONMENT),
            robot,
            identity=_identity(),
        )
        esphome = next(
            service
            for service in application.services
            if isinstance(service, EsphomeService)
        )
        controls = [
            entity
            for entity in esphome._state.entities
            if isinstance(entity, MotorSwitchEntity)
        ]
        body = controls[1]
        groups = application.motor_groups
        assert groups is not None
        reconnect_requests = len(robot.motor_requests)
        reconnect_policy = len(robot.automatic_body_yaw)

        reconnect = list(body.handle_message(SubscribeHomeAssistantStatesRequest()))

        assert reconnect == [SwitchStateResponse(key=body.key, state=True)]
        await groups.wait_idle()
        assert robot.motor_requests[reconnect_requests:] == [("read", BODY_MOTOR_IDS)]
        assert robot.automatic_body_yaw[reconnect_policy:] == [False, True]
        assert groups.gate_open(MotorGroup.BODY)

        before_requests = len(robot.motor_requests)
        before_policy = len(robot.automatic_body_yaw)
        responses = list(
            body.handle_message(SwitchCommandRequest(key=body.key, state=False))
        )

        assert responses == [SwitchStateResponse(key=body.key, state=True)]
        assert not groups.gate_open(MotorGroup.BODY)
        await groups.wait_idle()
        assert robot.motor_requests[before_requests:] == [
            ("disable", BODY_MOTOR_IDS),
            ("read", BODY_MOTOR_IDS),
        ]
        assert robot.automatic_body_yaw[before_policy:] == [False]
        assert groups.last_confirmed(MotorGroup.BODY) is True
        assert not groups.gate_open(MotorGroup.BODY)
        await groups.aclose()

    @pytest.mark.asyncio
    async def test_composed_terminal_reconnect_performs_no_hardware_read(self) -> None:
        """A reconnect racing shutdown reports retained state without new work."""
        robot = FakeRobot()
        application, _state, control = await self._composed_head_switch(robot)
        groups = application.motor_groups
        assert groups is not None
        groups.terminal()
        before = len(robot.motor_requests)

        responses = list(control.handle_message(SubscribeHomeAssistantStatesRequest()))

        assert responses == [SwitchStateResponse(key=control.key, state=True)]
        assert len(robot.motor_requests) == before
        assert not groups.gate_open(MotorGroup.HEAD)

    def test_unconfirmed_motor_groups_register_no_operable_switch(self) -> None:
        """Compatibility with the released SDK gates entities closed, not optimistic."""
        robot = FakeRobot(
            motor_reads=[MotorConfirmation.unavailable() for _group in range(3)]
        )
        application = assembled(
            load_settings(_ENVIRONMENT),
            robot,
            identity=_identity(),
        )
        esphome = next(
            service
            for service in application.services
            if isinstance(service, EsphomeService)
        )

        assert not any(
            isinstance(entity, MotorSwitchEntity) for entity in esphome._state.entities
        )
        assert all(
            not cast("dict[str, object]", group)["gate_open"]
            for group in cast(
                "dict[str, object]",
                cast("dict[str, object]", application.status()["motors"])["groups"],
            ).values()
        )


@pytest.mark.filesystem
class TestRemainingWheelAssembly:
    """Composition checks that intentionally load committed wheel assets."""

    def test_the_whole_application_assembles_over_a_fake_robot(self) -> None:
        """Ports to adapters, the behaviour layer, and the services it owns."""
        resolution = load_settings(_ENVIRONMENT, {})

        application = assembled(
            resolution,
            FakeRobot(),
            identity=_identity(),
        )

        assert application.status()["pipeline"] == "idle"
        motion = cast("ReachyMotion", application._motion)
        assert motion._config is application._behaviour._config
        assert application._behaviour._config.require_motion_measurements

    def test_released_short_staleness_still_assembles_with_bounded_dependents(
        self,
    ) -> None:
        """A previously valid sub-0.35s setting is migrated, not rejected at startup."""
        resolution = load_settings(
            _ENVIRONMENT,
            {"staleness_seconds": "0.2"},
        )

        application = assembled(
            resolution,
            FakeRobot(),
            identity=_identity(),
        )

        config = application._behaviour._config
        assert config.staleness_seconds == pytest.approx(0.2)
        assert config.prediction_horizon <= config.staleness_seconds
        assert config.loss_hold_seconds <= config.staleness_seconds
        assert config.head_measurement_max_age <= config.staleness_seconds
        assert config.body_feedback_max_age <= config.staleness_seconds

    def test_a_robot_with_tracking_off_still_assembles(self) -> None:
        """And its behaviour layer is handed a source that answers "nothing yet"."""
        resolution = load_settings(
            _ENVIRONMENT,
            {"face_tracking_enabled": "false"},
        )

        application = assembled(
            resolution,
            FakeRobot(),
            identity=_identity(),
        )

        assert application.status()["gaze"] == "unknown"

    def test_the_settings_interface_is_wired_when_it_is_enabled(self) -> None:
        """It is a service like the others, so it starts and stops with the app."""
        resolution = load_settings(
            {**_ENVIRONMENT, f"{ENV_PREFIX}WEB_ENABLED": "true"},
            {"advertise": "false"},
        )

        application = assembled(
            resolution,
            FakeRobot(),
            identity=_identity(),
        )

        assert any(isinstance(service, WebService) for service in application.services)

    @pytest.mark.asyncio
    async def test_the_page_writes_the_file_that_startup_reads(self) -> None:
        """The claim `build_application` makes about its own store, checked.

        `run` opens the overrides with `config.overrides_path`; the settings
        interface is handed an `OverrideStore` the composition root builds from
        `state_dir` separately. If those two ever named different files the page
        would write somewhere startup never looks — an operator would save a
        setting, be told it was saved, and find it gone at the next start, with
        nothing failing anywhere. Nothing else pins them together, so this does.
        """
        environ = {**_ENVIRONMENT, f"{ENV_PREFIX}WEB_ENABLED": "true"}
        resolution = load_settings(environ, {})

        application = await build_application(
            resolution,
            FakeRobot(),
            identity=_identity(),
        )

        web = next(
            service
            for service in application.services
            if isinstance(service, WebService)
        )
        # Reaching past the private name deliberately: `WebService` exists to
        # own a lifecycle, and widening its public surface so a test can read
        # the application back would be shaping production code around this
        # check. The alternative is not pinning the two paths at all.
        transport = httpx.ASGITransport(app=cast("Any", web._app))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://satellite.invalid",
        ) as client:
            page = await client.get("/")

        assert str(overrides_path(environ)) in page.text

    def test_the_advertisement_is_wired_when_it_is_enabled(self) -> None:
        """Home Assistant discovers the robot through it."""
        resolution = load_settings(
            _ENVIRONMENT,
            {"advertise": "true", "web_enabled": "false"},
        )

        application = assembled(
            resolution,
            FakeRobot(),
            identity=_identity(),
        )

        assert any(
            isinstance(service, AdvertisementService)
            for service in application.services
        )


class _RecordingSatellite:
    """Stands in for a connected `VoiceSatelliteProtocol`."""

    def __init__(self) -> None:
        """Start having been handed nothing."""
        self.chunks: list[tuple[bytes, bytes | None]] = []
        self.woken: list[object] = []
        self.stops = 0

    def handle_audio(
        self,
        audio_chunk: bytes,
        audio_chunk_2: bytes | None = None,
    ) -> None:
        """Record one chunk, with the vendored signature.

        Args:
            audio_chunk: What the wake word listens to.
            audio_chunk_2: The speaker reference, where the device has one.
        """
        self.chunks.append((audio_chunk, audio_chunk_2))

    def wakeup(self, wake_word: object) -> None:
        """Record that a wake word started a pipeline.

        Args:
            wake_word: The model that fired.
        """
        self.woken.append(wake_word)

    def stop(self) -> None:
        """Record that a response was told to stop."""
        self.stops += 1


class _FakeZeroconf:
    """An mDNS advertiser that registers nothing."""

    def __init__(self, built: list[dict[str, object]], **kwargs: object) -> None:
        """Record what it was asked to advertise.

        Args:
            built: Where to record it.
            kwargs: What the service passed.
        """
        built.append(kwargs)
        self.closed = 0
        self._aiozc = _FakeAiozc(self)

    async def register_server(self) -> None:
        """Pretend to register."""


class _FakeAiozc:
    """The zeroconf instance the vendored advertiser holds."""

    def __init__(self, owner: _FakeZeroconf) -> None:
        """Record which advertiser this belongs to.

        Args:
            owner: The advertiser.
        """
        self._owner = owner

    async def async_close(self) -> None:
        """Record a close."""
        self._owner.closed += 1


async def _nothing_asgi(
    scope: object,
    receive: object,
    send: object,
) -> None:
    """An ASGI application that is never called.

    Args:
        scope: Unused.
        receive: Unused.
        send: Unused.
    """
    del scope, receive, send


def _identity() -> NetworkIdentity:
    """What the robot announces on the network, from documentation ranges.

    Returns:
        The identity.
    """
    from reachy_mini_ha_satellite.adapters.network import NetworkIdentity

    return NetworkIdentity(
        interface="eth0",
        ip_address="192.0.2.20",
        mac_address="02:00:5e:10:00:00",
    )


def _esphome_service(
    bound: list[tuple[str, int]],
    *,
    capture: FakeCapture | None = None,
    tap: PipelineEventTap | None = None,
    state: ServerState | None = None,
    detector: WakeWordDetector | None = None,
    build_webrtc: Callable[[int, int], WebRTCLike] | None = None,
    backlog: int = 50,
) -> tuple[EsphomeService, ServerState]:
    """Build the protocol service over a state nothing binds a socket for.

    Args:
        bound: Where to record what the service asked to listen on.
        capture: The microphone, or a silent one.
        tap: What pipeline events go into.
        state: The vendored state, or an inert one.
        detector: What runs the wake-word models, or nothing at all.
        build_webrtc: How to make the microphone conditioner.
        backlog: How many chunks may wait for detection.

    Returns:
        The service and the vendored state it was built over.
    """
    from satellite_support import vendored_server_state

    if state is None:
        state = vendored_server_state()

    async def _listen(factory: object, host: str, port: int) -> _FakeServer:
        """Stand in for opening a listening socket.

        Args:
            factory: What would build a connection, unused.
            host: The address.
            port: The port.

        Returns:
            Something with the shape a server has.
        """
        del factory
        bound.append((host, port))
        return _FakeServer()

    service = EsphomeService(
        state,
        capture if capture is not None else FakeCapture([]),
        tap if tap is not None else PipelineEventTap(lambda _: None),
        host="127.0.0.1",
        port=6053,
        detector=detector,
        listen=_listen,
        start_thread=lambda _work: None,
        build_webrtc=build_webrtc if build_webrtc is not None else _RefusingWebRTC,
        backlog=backlog,
        pump_sleep=no_sleep,
    )
    return service, state


class _RefusingWebRTC:
    """A conditioner that fails the test if anything asks for one.

    The default in every test that has not asked for microphone conditioning,
    because the alternative — the real one — loads a native library, and a test
    that built one by accident would be quietly exercising it.
    """

    def __init__(self, agc_level: int, ns_level: int) -> None:
        """Refuse to be built.

        Args:
            agc_level: The gain level asked for.
            ns_level: The noise-suppression level asked for.

        Raises:
            AssertionError: Always.
        """
        message = (
            f"the pump built a microphone conditioner nobody asked for "
            f"(gain {agc_level}, noise {ns_level})"
        )
        raise AssertionError(message)

    def update_settings(self, agc_level: int, ns_level: int) -> None:
        """Never reached.

        Args:
            agc_level: The gain level asked for.
            ns_level: The noise-suppression level asked for.
        """

    def process(self, raw_bytes: bytes) -> bytes:
        """Never reached.

        Args:
            raw_bytes: The audio.

        Returns:
            The same audio.
        """
        return raw_bytes


class _FakeServer:
    """An asyncio server whose serving state a test controls directly."""

    def __init__(self) -> None:
        """Start healthy and open."""
        self.serving = True
        self.closed = 0
        self.waited = 0

    def is_serving(self) -> bool:
        """Report the scripted listener health."""
        return self.serving

    def close(self) -> None:
        """Record closure and stop serving."""
        self.closed += 1
        self.serving = False

    async def wait_closed(self) -> None:
        """Record that closure was awaited, without wall time."""
        self.waited += 1


class _DisconnectLifecycleTransport:
    """An accepted transport that schedules the real lost-connection callback."""

    def __init__(self, protocol: VoiceSatelliteProtocol) -> None:
        """Remember the protocol and the event loop that owns its lifecycle.

        Args:
            protocol: The accepted protocol this transport serves.
        """
        self.closed = 0
        self.writes: list[object] = []
        self._protocol = protocol
        self._loop = asyncio.get_running_loop()

    def writelines(self, packets: object) -> None:
        """Record protocol writes without opening a socket.

        Args:
            packets: Encoded packets written by the protocol.
        """
        self.writes.append(packets)

    def close(self) -> None:
        """Schedule connection loss as an asyncio transport does."""
        self.closed += 1
        self._loop.call_soon(self._protocol.connection_lost, None)


class _RecordingPeripheralEvents:
    """Record the shared connection-lifecycle events emitted during shutdown."""

    def __init__(self) -> None:
        """Start without an emitted event."""
        self.events: list[LVAEvent] = []

    def emit_event_sync(
        self,
        event: LVAEvent,
        data: dict[str, object] | None = None,
    ) -> None:
        """Record one event and ignore its payload.

        Args:
            event: The lifecycle transition.
            data: An optional transition payload.
        """
        del data
        self.events.append(event)


def _listener_service(
    bound: list[_FakeServer],
    *,
    state: ServerState | None = None,
    host: str = "127.0.0.1",
    port: int = 6053,
    listen: Callable[..., Awaitable[_FakeServer]] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    backoff: Backoff | None = None,
) -> EsphomeService:
    """Build a listener service with every health/retry edge deterministic.

    Args:
        bound: Every successfully bound fake listener.
        state: Shared protocol state, or a fresh fake.
        host: Configured bind host.
        port: Configured bind port.
        listen: A custom bind attempt, or one that always succeeds.
        sleep: The virtual retry wait.
        backoff: The retry policy under test.

    Returns:
        The service, not yet started.
    """

    async def _listen(factory: object, host: str, port: int) -> _FakeServer:
        del factory, host, port
        server = _FakeServer()
        bound.append(server)
        return server

    return EsphomeService(
        state if state is not None else vendored_server_state(),
        FakeCapture([]),
        PipelineEventTap(lambda _: None),
        host=host,
        port=port,
        listen=listen if listen is not None else _listen,
        start_thread=lambda _work: None,
        sleep=sleep if sleep is not None else asyncio.sleep,
        backoff=backoff if backoff is not None else Backoff(),
        pump_sleep=no_sleep,
    )


class TestTheDefaultThreadStarter:
    """What the microphone pump actually runs on, when nothing is injected."""

    def test_it_runs_the_work_and_does_not_hold_the_process_open(self) -> None:
        """A pump that kept the interpreter alive would make shutdown a hang."""
        ran = threading.Event()

        thread = _daemon_thread(ran.set)
        thread.join(timeout=_THREAD_JOIN_SECONDS)

        assert ran.is_set()
        assert thread.daemon


class TestNoPerception:
    """What the behaviour layer is handed when face tracking is switched off."""

    @pytest.mark.asyncio
    async def test_it_reports_that_nothing_has_ever_been_seen(self) -> None:
        """Which is the one answer that makes the tracker command nothing."""
        source: PerceptionPort = _NoPerception()
        await source.start()

        latest = source.latest()

        assert latest.source is None
        assert not latest.fresh
        await source.aclose()


@pytest.mark.filesystem
class TestStartingUpFromTheEnvironment:
    """`run` is the whole of startup: read, log, wire, loop, leave.

    Marked because assembling the application reads the wake-word models the
    wheel ships, which is what makes ha-satellite REQ-044 true rather than
    asserted.
    """

    @pytest.mark.enable_socket
    @pytest.mark.asyncio
    async def test_it_reads_the_environment_and_runs_until_it_is_stopped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The real startup path, including binding the port Home Assistant uses.

        An integration test rather than a unit test, and it says so: it opens
        the ESPHome listening socket in-process, on the loopback interface and
        on a port the operating system picked, because a startup path that
        never bound anything would prove nothing about the one thing startup
        has to do.

        The boot log is read off the captured stream rather than through
        `caplog`, and that is not a preference: startup installs the
        process-wide logging configuration, which replaces every handler on the
        root logger — including the one `caplog` had put there.

        Args:
            monkeypatch: Used to supply the environment the daemon would.
            capsys: Captures what startup actually printed.
        """
        for name, value in _ENVIRONMENT.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setenv(f"{ENV_PREFIX}FACE_TRACKING_ENABLED", "false")
        monkeypatch.setenv(f"{ENV_PREFIX}API_HOST", "127.0.0.1")
        monkeypatch.setenv(f"{ENV_PREFIX}API_PORT", str(_free_port()))
        stop = asyncio.Event()
        start_esphome = EsphomeService.start

        async def _start_esphome_then_stop(service: EsphomeService) -> None:
            """Stop only after the real listener has successfully bound."""
            await start_esphome(service)
            stop.set()

        monkeypatch.setattr(EsphomeService, "start", _start_esphome_then_stop)

        await run(FakeRobot(), stop)

        emitted = capsys.readouterr().err
        assert "configuration.resolved device_name=reachy-mini-1" in emitted
        assert "groundstation_credential=<set>" in emitted
        assert "esphome.listening" in emitted
        assert "satellite.stopped" in emitted


class TestAnIncompleteInstallation:
    """Two wake-word failures, and neither of them is a traceback."""

    def test_no_wake_words_at_all_refuses_to_start(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """The vendored loader raises; this says where it looked.

        Args:
            fs: An in-memory filesystem, standing in for a wheel whose
                wake-word directory did not survive installation.
        """
        del fs

        with pytest.raises(ConfigurationError, match="installation is incomplete"):
            build_server_state(
                _settings(),
                identity=_identity(),
                audio=FakeAudio(),
                state_dir=Path("/reachy-satellite-main"),
            )

    @pytest.mark.filesystem
    def test_a_missing_stop_word_refuses_to_start(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A satellite that cannot be told to stop talking is worse than none.

        The stop word is loaded by a vendored function, and the case being
        exercised is a wheel that shipped every wake word but that one — so the
        function is replaced rather than the tree rearranged.

        Args:
            monkeypatch: Used to make the stop word unfindable.
        """
        monkeypatch.setattr(satellite_main, "load_stop_model", lambda *_: None)

        with pytest.raises(ConfigurationError, match="stop word"):
            build_server_state(
                _settings(),
                identity=_identity(),
                audio=FakeAudio(),
                state_dir=Path("/reachy-satellite-main"),
            )


def _free_port() -> int:
    """Ask the operating system for a port nothing is listening on.

    Args:
        None.

    Returns:
        The port number.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class TestShutdownFinishesWhateverRefuses:
    """Every step is guarded, and the first two most of all.

    The media layer is the thing most likely to be failing at the moment a robot
    is being shut down, so a refusal there is exactly when the steps after it
    matter — and `aclose` marks itself done before it starts, so nothing could
    finish them afterwards.
    """

    @pytest.mark.asyncio
    async def test_a_motion_port_that_refuses_to_release_stops_everything_else(
        self,
    ) -> None:
        """It is the first step, so it has the most to abandon."""

        class _Stuck(FakeMotion):
            def release(self) -> None:
                """Refuse to stop commanding movement.

                Raises:
                    RuntimeError: Always.
                """
                message = "the motion layer will not let go"
                raise RuntimeError(message)

        audio = FakeAudio()
        service = RecordingService()
        perception = FakePerception()
        application, stop = _application(
            audio=audio,
            motion=_Stuck(),
            perception=perception,
            services=[service],
        )

        await application.run(stop)

        assert audio.stopped == 1
        assert service.closed == 1
        assert perception.closed == 1

    @pytest.mark.asyncio
    async def test_a_media_layer_that_refuses_to_release_stops_everything_else(
        self,
    ) -> None:
        """Otherwise the listening socket and the microphone pump outlive it."""

        class _Stuck(FakeAudio):
            def stop(self) -> None:
                """Refuse to release the media interface.

                Raises:
                    RuntimeError: Always.
                """
                message = "the daemon will not release the media interface"
                raise RuntimeError(message)

        motion = FakeMotion()
        service = RecordingService()
        perception = FakePerception()
        application, stop = _application(
            audio=_Stuck(),
            motion=motion,
            perception=perception,
            services=[service],
        )

        await application.run(stop)

        assert motion.released
        assert service.closed == 1
        assert perception.closed == 1


class TestBackgroundServiceTaskReporting:
    """A task nobody awaits until shutdown is a failure nobody sees."""

    @pytest.mark.asyncio
    async def test_a_failed_named_task_reports_only_its_safe_reason(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The task name and error type are useful; exception text is private.

        Args:
            caplog: Captures what the failure was reported as.
        """
        loop = asyncio.get_running_loop()
        unhandled: list[object] = []
        previous_handler = loop.get_exception_handler()

        async def _fail() -> None:
            """Fail with fake installation details in the exception text.

            Raises:
                OSError: Always.
            """
            raise OSError(_EXCEPTION_DETAIL)

        async def _run() -> None:
            task = asyncio.create_task(_fail(), name="satellite-settings")
            task.add_done_callback(satellite_main._report_if_it_failed)
            # Two passes of the loop: one for the task to run and fail, one for
            # its done-callback. Both yield rather than sleeping, so nothing
            # waits for wall time.
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        try:
            loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
            with caplog.at_level(
                logging.ERROR,
                logger="reachy_mini_ha_satellite.main",
            ):
                await _run()
                await asyncio.sleep(0)
                reported = caplog.text
        finally:
            loop.set_exception_handler(previous_handler)

        assert "satellite-settings stopped unexpectedly" in reported
        assert "OSError" in reported
        for identifier in _EXCEPTION_IDENTIFIERS:
            assert identifier not in reported
        assert unhandled == []

    @pytest.mark.asyncio
    async def test_a_successful_named_task_reports_nothing(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A normal background-service exit is not an error."""

        async def _succeed() -> None:
            """Return normally."""

        with caplog.at_level(logging.ERROR, logger="reachy_mini_ha_satellite.main"):
            task = asyncio.create_task(_succeed(), name="satellite-settings")
            task.add_done_callback(satellite_main._report_if_it_failed)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        assert caplog.records == []

    @pytest.mark.asyncio
    async def test_a_cancelled_named_task_reports_nothing(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Shutdown cancellation is not a failure to tell anybody about."""
        stopped = asyncio.Event()

        async def _serve() -> None:
            """Serve until cancelled."""
            await stopped.wait()

        with caplog.at_level(logging.ERROR, logger="reachy_mini_ha_satellite.main"):
            task = asyncio.create_task(_serve(), name="satellite-settings")
            task.add_done_callback(satellite_main._report_if_it_failed)
            await asyncio.sleep(0)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)

        assert caplog.records == []
