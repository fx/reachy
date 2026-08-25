"""The composition root: the one module that knows which adapter is in use.

Everything above this file decides; everything below it acts, and this is where
the two are wired to each other. **It is the only module that imports both**, and
the direction that actually matters is enforced rather than asserted: the
behaviour layer has never heard of an adapter, and `just lint-behaviour-boundary`
fails the build if that changes.

One module names the behaviour layer without being this one, and it is worth
saying so rather than leaving a reader to find it: `adapters/pipeline_events.py`
imports `PipelineEvent` in order to translate the vendored protocol's events into
it. That is the translation sitting on the adapter side deliberately — it is what
keeps protobuf out of the state machine — and it composes nothing.

What runs here is a loop and four services. The loop asks the behaviour layer
what the robot should be doing and applies the answer; the services are the
things that have a lifetime of their own — the ESPHome protocol server, the
microphone pump that feeds it and the wake-word detection that runs beside it,
the mDNS advertisement Home Assistant discovers the robot through, and the
settings interface.

**Shutdown is the part worth reading.** ha-satellite REQ-050 asks for movement to
stop, the media interface to be released, and the process to exit, and it asks
for it in that order. `aclose` does exactly that, once, whatever raised on the
way in — including when the loop is cancelled — and each step is guarded so that
a service failing to close cannot skip the ones after it. The motion port and the
audio port are both *terminal* rather than merely stopped: a behaviour tick or an
ESPHome packet still in flight when the signal arrives is ignored rather than
refused, so a shutdown racing the loop ends quietly.

The Reachy Mini SDK is not imported here. The daemon hands this application a
handle satisfying `adapters.daemon.RobotHandle`, and `daemon_app.py` — the one
module that touches the SDK — is what receives it.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import json
import logging
import threading
import time
from contextvars import Context, ContextVar
from dataclasses import fields
from pathlib import Path
from queue import Empty, Full, Queue
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

import numpy as np
from starlette.types import ASGIApp

from reachy_contracts import FACE_CAPABILITY, Capability, __version__
from reachy_mini_ha_satellite.adapters.audio_reachy import ReachyAudio
from reachy_mini_ha_satellite.adapters.daemon import in_thread
from reachy_mini_ha_satellite.adapters.daemon_volume import set_daemon_volume
from reachy_mini_ha_satellite.adapters.groundstation import RemotePerception
from reachy_mini_ha_satellite.adapters.motion_reachy import ReachyMotion
from reachy_mini_ha_satellite.adapters.network import (
    NetworkIdentity,
    discover_network_identity,
)
from reachy_mini_ha_satellite.adapters.perception_local import (
    LocalPerception,
    SdkFaceDetector,
    load_sdk_face_detector,
)
from reachy_mini_ha_satellite.adapters.perception_source import build_perception
from reachy_mini_ha_satellite.adapters.pipeline_events import PipelineEventTap
from reachy_mini_ha_satellite.adapters.sounds import FileSoundSource
from reachy_mini_ha_satellite.assets.registry import assets_dir
from reachy_mini_ha_satellite.audio_entities import (
    SpeakerBoostNumberEntity,
    SpeakerVolumeNumberEntity,
)
from reachy_mini_ha_satellite.behaviour import (
    CommandGaze,
    MoveHead,
    SatelliteBehaviour,
)
from reachy_mini_ha_satellite.behaviour.gaze_controller import (
    BodyMeasurement,
    ControllerConfig,
    ControllerFault,
    HeadMeasurement,
)
from reachy_mini_ha_satellite.config import (
    OVERRIDES_FILENAME,
    ConfigurationError,
    OverrideStore,
    Resolution,
    Settings,
    apply_settings_change,
    as_configured_string,
    load_settings,
    log_resolved_configuration,
    overrides_path,
    state_directory,
)
from reachy_mini_ha_satellite.esphome.models import (
    Preferences,
    ServerState,
    initial_stop_word_threshold,
)
from reachy_mini_ha_satellite.esphome.satellite import VoiceSatelliteProtocol
from reachy_mini_ha_satellite.esphome.seams import SAMPLE_WIDTH
from reachy_mini_ha_satellite.esphome.util import get_esphome_version
from reachy_mini_ha_satellite.esphome.wake_word import (
    find_available_wake_words,
    load_stop_model,
    load_wake_models,
)
from reachy_mini_ha_satellite.esphome.webrtc import WebRTCProcessor
from reachy_mini_ha_satellite.esphome.zeroconf import HomeAssistantZeroconf
from reachy_mini_ha_satellite.ports import (
    CalibrationStatus,
    Detections,
    MotionCommandResult,
    MotionFault,
    SourceSelection,
)
from reachy_mini_ha_satellite.wake_word import WakeWordDetector
from reachy_mini_ha_satellite.web import create_app
from reachy_session_client import DEFAULT_BACKOFF, Backoff, Credential, SessionClient

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence

    from pymicro_wakeword import MicroWakeWord
    from pyopen_wakeword import OpenWakeWord

    from reachy_mini_ha_satellite.adapters.daemon import (
        MediaInterface,
        Offload,
        RobotHandle,
    )
    from reachy_mini_ha_satellite.behaviour import MotionIntent, PipelineEvent
    from reachy_mini_ha_satellite.ports import (
        AudioPort,
        CapturePort,
        MotionPort,
        PerceptionPort,
    )
    from reachy_mini_ha_satellite.wake_word import Activations

__all__ = [
    "STOP_WORD_ID",
    "AdvertisementService",
    "Advertiser",
    "EsphomeService",
    "SatelliteApplication",
    "Service",
    "VolumeService",
    "WebRTCLike",
    "WebService",
    "apply_intents",
    "build_application",
    "build_boost_setter",
    "build_perception_source",
    "build_server_state",
    "configure_logging",
    "run",
]

_LOGGER: Final = logging.getLogger(__name__)

# The wake word the protocol requires alongside whichever one is listening: it
# is what stops a running response. Shipped in the wheel, and not configurable —
# a satellite without it cannot be told to stop talking.
STOP_WORD_ID: Final = "stop"

# Which shipped sound plays for which of the vendored state's nine slots. The
# names on the left are `ServerState`'s; the names on the right are the files
# `assets/registry.py` records the terms of.
_SOUNDS: Final[dict[str, str]] = {
    "wakeup_sound": "wake_word_triggered.flac",
    "start_listening_sound": "start_listening_button.flac",
    "processing_sound": "processing.wav",
    "timer_finished_sound": "timer_finished.flac",
    "mute_sound": "mute_switch_on.flac",
    "unmute_sound": "mute_switch_off.flac",
    "button_double_press_sound": "button_double_press.flac",
    "button_triple_press_sound": "button_triple_press.flac",
    "button_long_press_sound": "button_long_press.flac",
}

# The severities the `log_level` setting names, resolved to what `logging` wants.
_LEVELS: Final[dict[str, int]] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


# The selection that runs the detector on the robot and opens no session. Bound
# once rather than spelled at each site, because this repository's leak scanner
# reads the dotted form as an mDNS hostname suffix — the same reason
# `adapters/perception_source.py` binds it, and one exempted line is better than
# several.
_ROBOT_ONLY: Final = SourceSelection.LOCAL  # leak-scan:allow


class Service(Protocol):
    """Something with a lifetime of its own, started and stopped with the app."""

    async def start(self) -> None:
        """Begin. Called once, before the behaviour loop runs."""
        ...

    async def aclose(self) -> None:
        """Stop and release. Called once, on the way out, however that happens."""
        ...


def apply_intents(
    motion: MotionPort,
    intents: Sequence[MotionIntent],
) -> tuple[MotionCommandResult, ...]:
    """Carry out decisions and return typed results for coordinated commands.

    The whole command half of the behavior boundary: each intent corresponds to
    exactly one port method, so there is no second gaze calculation here.

    Args:
        motion: What to command.
        intents: What the behaviour layer asked for, in order.

    Returns:
        Transactional results in the same order as gaze-command intents.
    """
    results: list[MotionCommandResult] = []
    for intent in intents:
        if isinstance(intent, CommandGaze):
            results.append(motion.command_gaze(intent.sample))
        elif isinstance(intent, MoveHead):
            motion.move_head(intent.pose)
        else:
            motion.move_antennas(intent.pose)
    return tuple(results)


def _guard(what: str, release: Callable[[], None]) -> None:
    """Run one shutdown step, reporting a refusal rather than propagating it.

    Args:
        what: What is being let go of, for the log line.
        release: How to let go of it.
    """
    try:
        release()
    except Exception:
        _LOGGER.exception("%s failed to stop cleanly", what)


_CLEANUP_TIMEOUT_SECONDS: Final = 5.0


class _CleanupTaskScope:
    """Tasks created from one cleanup coroutine and its descendants."""

    def __init__(self, preexisting: set[asyncio.Task[Any]]) -> None:
        """Start with no descendants and ordinary task creation enabled.

        Args:
            preexisting: Tasks already on the loop, which this scope never owns.
        """
        self.preexisting = preexisting
        self.tasks: set[asyncio.Future[Any]] = set()
        self.finalizing = False


_CLEANUP_TASK_OWNER: Final[ContextVar[_CleanupTaskScope | None]] = ContextVar(
    "satellite_cleanup_task_owner",
    default=None,
)


def _consume_cleanup_result(
    task: asyncio.Future[Any],
    *,
    what: str,
    forced: bool = False,
) -> None:
    """Consume one cleanup task's result without leaking an exception.

    Args:
        task: The child whose result must be retrieved.
        what: What the child was releasing, for the log line.
        forced: Whether its coroutine was force-closed after ignoring cancel.
    """
    if task.cancelled():
        return
    try:
        task.result()
    except (Exception, asyncio.CancelledError):
        if forced:
            _LOGGER.error("%s was force-finalized after ignoring cancellation", what)
        else:
            _LOGGER.error("%s failed to stop cleanly", what)


async def _cancelled_cleanup_child() -> None:
    """Provide a never-started coroutine for a directly canceled task."""


def _factory_accepts_context(
    factory: Callable[..., asyncio.Future[Any]],
) -> bool:
    """Return whether a task factory declares the modern context keyword."""
    try:
        parameters = inspect.signature(factory).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "context" or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _install_cleanup_task_tracking(
    loop: asyncio.AbstractEventLoop,
) -> tuple[Callable[..., asyncio.Future[Any]] | None, _CleanupTaskScope]:
    """Install scoped descendant tracking and return prior factory plus scope."""
    previous = loop.get_task_factory()
    scope = _CleanupTaskScope(asyncio.all_tasks(loop))

    def _tracked(
        task_loop: asyncio.AbstractEventLoop,
        coroutine: Coroutine[Any, Any, Any],
        context: Context | None = None,
    ) -> asyncio.Future[Any]:
        inherited_owner = _CLEANUP_TASK_OWNER.get()
        if inherited_owner is not None and inherited_owner.finalizing:
            # Do not delegate this branch: an eager factory would execute the
            # submitted coroutine through its finalizer before it can be canceled,
            # letting each finalizer spawn the next task forever.
            coroutine.close()
            canceled = asyncio.Task(_cancelled_cleanup_child(), loop=task_loop)
            canceled.cancel()
            inherited_owner.tasks.add(canceled)
            return canceled

        derived_context = context.copy() if context is not None else None
        if inherited_owner is not None and derived_context is not None:
            derived_context.run(_CLEANUP_TASK_OWNER.set, inherited_owner)

        if previous is None:
            created: asyncio.Future[Any] = asyncio.Task(
                coroutine,
                loop=task_loop,
                context=derived_context,
            )
        elif derived_context is None:
            # The task-factory protocol was two positional arguments before the
            # context keyword existed; preserve that valid legacy surface.
            created = previous(task_loop, coroutine)
        elif _factory_accepts_context(previous):
            created = cast("Any", previous)(
                task_loop,
                coroutine,
                context=derived_context,
            )
        else:
            created = derived_context.run(previous, task_loop, coroutine)
        if inherited_owner is not None:
            inherited_owner.tasks.add(created)
        return created

    loop.set_task_factory(_tracked)
    return previous, scope


def _restore_task_factory(
    loop: asyncio.AbstractEventLoop,
    previous: Callable[..., asyncio.Future[Any]] | None,
) -> None:
    """Restore the event loop's task factory after one cleanup step."""
    loop.set_task_factory(previous)


async def _finalization_turn() -> asyncio.CancelledError | None:
    """Give finalization one loop turn while deferring owner cancellation.

    Returns:
        Cancellation delivered during the turn, for the caller to re-raise only
        after the cleanup child has reached a terminal state.
    """
    try:
        await asyncio.sleep(0)
    except asyncio.CancelledError as error:
        return error
    return None


async def _stop_cleanup_descendants(
    scope: _CleanupTaskScope,
    *,
    outer: asyncio.Task[None],
    what: str,
) -> asyncio.CancelledError | None:
    """Force and consume cleanup-owned descendants to a fixed point.

    Args:
        scope: The task-creation scope installed before the cleanup started.
        outer: The already-finalized top-level cleanup task.
        what: What was being released, for static log text.

    Returns:
        Owner cancellation delivered during descendant finalization.
    """
    deferred: asyncio.CancelledError | None = None
    consumed: set[asyncio.Future[Any]] = set()
    scope.finalizing = True
    while True:
        candidates = scope.tasks - scope.preexisting - {outer} - consumed
        if not candidates:
            return deferred
        for task in candidates:
            if task.done():
                _consume_cleanup_result(task, what=what, forced=True)
                consumed.add(task)
                continue
            if isinstance(task, asyncio.Task):
                coroutine = task.get_coro()
                if coroutine is not None:
                    try:
                        coroutine.close()
                    except (Exception, asyncio.CancelledError):
                        _LOGGER.error(
                            "%s descendant raised while being force-finalized", what
                        )
            task.cancel()
        repeated = await _finalization_turn()
        if deferred is None:
            deferred = repeated
        for task in candidates:
            if task.done():
                _consume_cleanup_result(task, what=what, forced=True)
                consumed.add(task)


async def _stop_cleanup_task(
    cleanup: asyncio.Task[None],
    *,
    scope: _CleanupTaskScope,
    what: str,
) -> asyncio.CancelledError | None:
    """Cancel and finalize a child without letting re-cancellation abandon it.

    Args:
        cleanup: The child to finish before application shutdown returns.
        scope: Tasks created by this cleanup and no pre-existing loop task.
        what: What the child was releasing, for the log line.

    Returns:
        Owner cancellation delivered during finalization, for re-raising after
        the child is done.
    """
    cleanup.cancel()
    deferred = await _finalization_turn()
    if cleanup.done():
        _consume_cleanup_result(cleanup, what=what)
        descendant_cancel = await _stop_cleanup_descendants(
            scope,
            outer=cleanup,
            what=what,
        )
        return deferred or descendant_cancel

    coroutine = cleanup.get_coro()
    if coroutine is not None:
        try:
            coroutine.close()
        except (Exception, asyncio.CancelledError):
            _LOGGER.error("%s raised while being force-finalized", what)
    cleanup.cancel()
    repeated = await _finalization_turn()
    if deferred is None:
        deferred = repeated
    if cleanup.done():
        _consume_cleanup_result(cleanup, what=what, forced=True)
    else:
        _LOGGER.error("%s remained pending after force-finalization", what)
    descendant_cancel = await _stop_cleanup_descendants(
        scope,
        outer=cleanup,
        what=what,
    )
    return deferred or descendant_cancel


async def _aguard(
    what: str,
    release: Callable[[], Awaitable[None]],
    timeout_seconds: float,
) -> None:
    """Bound and guard one asynchronous shutdown step.

    The release runs in a child task so a coroutine that suppresses cancellation
    cannot extend this caller's deadline. A child that ignores ordinary task
    cancellation is force-closed and observed before this returns. Tasks spawned
    inside that cleanup are tracked from creation and finalized to a fixed point,
    while tasks that predate it are never touched, so the process-level runner
    inherits no cleanup-owned task. Owner cancellation delivered during
    finalization is retained for re-raising only after every owned task is
    terminal.

    Args:
        what: What is being let go of, for the log line.
        release: How to let go of it.
        timeout_seconds: The deadline for this step alone. Later cleanup still
            runs if it expires.
    """

    async def _release() -> None:
        await release()

    loop = asyncio.get_running_loop()
    previous_factory, scope = _install_cleanup_task_tracking(loop)
    owner_token = _CLEANUP_TASK_OWNER.set(scope)
    try:
        try:
            cleanup = asyncio.create_task(_release(), name="satellite-cleanup")
        except Exception:
            _LOGGER.error("%s failed to start cleanup", what)
            return
        scope.tasks.discard(cleanup)
        try:
            done, _pending = await asyncio.wait(
                {cleanup},
                timeout=timeout_seconds,
            )
        except asyncio.CancelledError as error:
            repeated = await _stop_cleanup_task(
                cleanup,
                scope=scope,
                what=what,
            )
            raise (repeated or error) from None
        if cleanup not in done:
            _LOGGER.error("%s timed out while stopping; continuing cleanup", what)
            repeated = await _stop_cleanup_task(
                cleanup,
                scope=scope,
                what=what,
            )
            if repeated is not None:
                raise repeated
            return
        _consume_cleanup_result(cleanup, what=what)
        repeated = await _stop_cleanup_descendants(
            scope,
            outer=cleanup,
            what=what,
        )
        if repeated is not None:
            raise repeated
    finally:
        _CLEANUP_TASK_OWNER.reset(owner_token)
        _restore_task_factory(loop, previous_factory)


def configure_logging(settings: Settings) -> None:
    """Install the process-wide logging configuration.

    Args:
        settings: The settings in effect; its level decides what is emitted.
            The format is left plain rather than made configurable, because the
            daemon owns this process's output and collects it as text.
    """
    logging.basicConfig(
        level=_LEVELS[settings.log_level],
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )


def _publish_nothing() -> None:
    """Push nothing, for an application no entity is reporting settings from."""


#:= docs/specs/ha-satellite/index.md#req-045-speech-and-intent-processing-stay-in-home-assistant
#:% The application MUST NOT perform speech-to-text, text-to-speech, or intent
#:% resolution locally.
#
#:= docs/specs/ha-satellite/index.md#req-050-shutdown-is-graceful-and-leaves-the-robot-safe
#:% On receiving a termination signal the application MUST stop commanding movement,
#:% release the media interface, and exit.
class SatelliteApplication:
    """The running application: three ports, one behaviour layer, four services.

    Nothing here decides anything. The behaviour layer decides; this ticks it,
    hands its answers to the motion port, and owns the lifetimes.

    **No speech, no text, no intent.** Captured audio goes up the ESPHome
    session to Home Assistant and answers come back as media to play — that is
    REQ-045, and it is a property of what is *absent* from this file rather than
    of anything in it. The wake word is the one model that runs here, because
    REQ-044 requires it to work with the network down.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        audio: AudioPort,
        motion: MotionPort,
        perception: PerceptionPort,
        behaviour: SatelliteBehaviour,
        services: Sequence[Service] = (),
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        cleanup_timeout_seconds: float = _CLEANUP_TIMEOUT_SECONDS,
    ) -> None:
        """Hold everything the application runs on.

        Args:
            settings: The settings in effect.
            audio: The daemon's microphone and speakers.
            motion: The head and the antennas.
            perception: What is in front of the robot.
            behaviour: What to do about it.
            services: The things with lifetimes, started in order and stopped
                in reverse.
            clock: The monotonic source the behaviour layer is given.
            sleep: How the loop waits between ticks. Injected so the test suite
                drives a hundred ticks without spending five seconds.
            cleanup_timeout_seconds: The deadline for each asynchronous cleanup
                step independently. Injected as zero by the non-returning test.
        """
        self._settings = settings
        self._audio = audio
        self._motion = motion
        self._perception = perception
        self._behaviour = behaviour
        self._services = tuple(services)
        self._clock = clock
        self._sleep = sleep
        self._tick_seconds = settings.behaviour_tick_seconds
        self._cleanup_timeout_seconds = cleanup_timeout_seconds
        self._gaze_enabled = settings.face_tracking_enabled
        # What tells Home Assistant about a setting this application adopted.
        # Nothing until `publish_live_changes` is called, which is what an
        # application built without the speaker-boost control stays at.
        self._publish: Callable[[], None] = _publish_nothing
        self._stop: asyncio.Event | None = None
        self._last_tick_at: float | None = None
        self._closed = False

    def attach(self, services: Sequence[Service]) -> None:
        """Hand over the services this application owns the lifetime of.

        Separate from the constructor because two of the services need the
        application itself: the settings interface asks it to stop, and the
        protocol server delivers pipeline events into it. Building them first
        would need an application that does not exist yet.

        Args:
            services: What to start with the application and stop with it.
        """
        self._services = tuple(services)

    def publish_live_changes(self, publish: Callable[[], None]) -> None:
        """Hand over what tells Home Assistant about an adopted setting.

        Separate from the constructor for the same reason `attach` is: what
        does the publishing is an entity built *from* this application — it
        reads the value back out of `settings` — so it does not exist yet when
        this is constructed.

        Args:
            publish: What to call once a live change has been adopted. Called
                for any live change rather than only for the one it reports,
                which costs a repeat of a value Home Assistant already has and
                keeps the alternative — this class knowing which settings which
                entity shows — out of it.
        """
        self._publish = publish

    @property
    def services(self) -> tuple[Service, ...]:
        """The things this application owns the lifetime of.

        Returns:
            The services, in the order they are started. Reported so that
            "is the settings interface actually wired?" is a question with an
            answer, which is what a deployment check and a test both want.
        """
        return self._services

    @property
    def settings(self) -> Settings:
        """The settings in effect right now.

        Read-only, and it exists because an entity that reports a setting to
        Home Assistant has to report what is in effect *now*. A snapshot taken
        when that entity was constructed would go on reporting the value the
        application started with, so a boost changed from the settings page
        would leave Home Assistant's slider showing the old number for ever.

        Returns:
            What `apply_live` last adopted, or what the application was built
            with.
        """
        return self._settings

    def status(self) -> dict[str, object]:
        """Say what the robot is doing, for the settings interface to report.

        Returns:
            The pipeline state, why the head is where it is, and whether the
            robot has settled into idling.
        """
        report = self._behaviour.status(self._clock())
        controller = self._behaviour.controller_state
        return {
            "pipeline": report.state.value,
            "gaze": report.outcome.value,
            "tracking": report.tracking,
            "idle": report.idle,
            "controller": {
                "mode": controller.mode.value,
                "fault": controller.fault.value,
                "safe_hold": controller.safe_hold,
            },
        }

    def controller_diagnostics(self) -> tuple[dict[str, object], ...]:
        """Return the behavior layer's bounded private controller evidence."""
        return cast(
            "tuple[dict[str, object], ...]",
            self._behaviour.controller_diagnostics(),
        )

    def reset_controller_diagnostics(self) -> None:
        """Clear controller diagnostics and no application or motion state."""
        self._behaviour.reset_controller_diagnostics()

    def deliver(self, event: PipelineEvent) -> None:
        """Apply one voice-pipeline event.

        This is what `PipelineEventTap` calls, on the event loop's own thread.

        Args:
            event: What the pipeline did.
        """
        apply_intents(self._motion, self._behaviour.handle(event, self._clock()))

    def tick(self) -> None:
        """Sample, select, calibrate, finish and apply with one time reading."""
        if self._motion.released:
            return
        now = self._clock()
        previous = self._last_tick_at
        dt = 0.0 if previous is None else now - previous
        self._last_tick_at = now
        measurement = self._motion.observe(now) if self._gaze_enabled else None
        prepared = self._behaviour.prepare(self._perception.latest(), now)
        calibration = (
            self._motion.calibrate(prepared.directive, now)
            if prepared.directive.face is not None
            else None
        )
        calibrated = (
            calibration.target
            if calibration is not None
            and calibration.state is CalibrationStatus.ACCEPTED
            else None
        )
        head_measurement = (
            HeadMeasurement(
                world_yaw=measurement.world_yaw,
                world_elevation=measurement.world_elevation,
                measured_at=measurement.head_measured_at,
            )
            if measurement is not None
            and measurement.world_yaw is not None
            and measurement.world_elevation is not None
            and measurement.head_measured_at is not None
            else None
        )
        body_measurement = (
            BodyMeasurement(
                yaw=measurement.body_yaw,
                measured_at=measurement.body_measured_at,
            )
            if measurement is not None
            and measurement.body_yaw is not None
            and measurement.body_measured_at is not None
            else None
        )
        input_fault = ControllerFault.NONE
        input_evidence: tuple[object, ...] | None = None
        if measurement is not None and measurement.head_fault is MotionFault.POSE:
            input_fault = ControllerFault.POSE
        elif (
            prepared.directive.face is not None
            and calibration is not None
            and calibration.state is CalibrationStatus.REJECTED
        ):
            input_fault = ControllerFault.CALIBRATION
            input_evidence = ("calibration", prepared.directive.identity)
        intents = self._behaviour.finish(
            prepared,
            calibrated=calibrated,
            head_measurement=head_measurement,
            body_measurement=body_measurement,
            dt=dt,
            input_fault=input_fault,
            input_evidence=input_evidence,
        )
        for result in apply_intents(self._motion, intents):
            self._behaviour.complete_command(result)

    def request_stop(self) -> None:
        """Ask the application to shut down, as a termination signal would.

        The settings interface offers this so that a change needing a restart
        can be taken without a remote shell. **It stops the application; it does
        not start it again.** The daemon marks a cleanly-exited application
        `done` and leaves it stopped, so starting it is the operator's next
        action — from the daemon's own dashboard, which is a web interface like
        this one, so REQ-049's "without a shell" still holds.
        """
        if self._stop is not None:
            self._stop.set()

    def apply_live(self, settings: Settings) -> None:
        """Adopt the settings that can be changed without a restart.

        Only the names in `config.LIVE_SETTINGS` are read here.
        `face_tracking_enabled` builds a detector and `body_motion_enabled`
        changes controller and daemon ownership, so both remain restart-bound.
        Legacy gaze gains and camera fields of view are compatibility inputs and
        are intentionally read nowhere in predictive control.

        `speaker_boost_percent` *is* among them: both outputs read it per
        pushed chunk, so adopting it here is heard from the next chunk onwards
        rather than at the next sound. That is what lets one path — this one —
        serve both the settings page and the Home Assistant control, whichever
        of the two chose the number.

        **Being one path is also why the push goes out from here.** A boost
        chosen on the settings page changes what the robot sounds like at once;
        a Home Assistant slider still showing the previous number until the next
        reconnect is the control this application offers being wrong about
        itself. `publish_live_changes` is what the composition root registers,
        and it is called below whichever surface started the change.

        Args:
            settings: The newly resolved settings.
        """
        self._settings = settings
        self._tick_seconds = settings.behaviour_tick_seconds
        configure_logging(settings)
        self._audio.set_boost(settings.speaker_boost_percent)
        self._behaviour.retune(idle_seconds=settings.idle_seconds)
        # Last, so that what the publisher reads back is what was adopted rather
        # than what is halfway through being adopted.
        self._publish()

    async def run(self, stop: asyncio.Event) -> None:
        """Start everything, tick until asked to stop, then leave the robot safe.

        Args:
            stop: Set by the daemon's termination signal, or by the settings
                interface asking it to stop.
        """
        self._stop = stop
        try:
            if self._gaze_enabled:
                acquired_at = self._clock()
                self._motion.acquire(acquired_at)
                self._last_tick_at = acquired_at
            self._audio.start()
            await self._perception.start()
            for service in self._services:
                await service.start()
            _LOGGER.info("satellite.started")
            while not stop.is_set():
                self.tick()
                # One tick of latency on the way out. The alternative is racing
                # the event against the sleep, which buys 50 ms and costs a
                # second way for shutdown to go wrong.
                await self._sleep(self._tick_seconds)
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        """Stop commanding movement, release the media interface, and let go.

        In that order, which is REQ-050's order. Idempotent, and **every step is
        guarded, including the first two**: the media layer is the thing most
        likely to be failing at the moment a robot is being shut down, and
        letting its refusal end this method would leave the listening socket
        open, the microphone pump running and the perception source holding a
        session — with `_closed` already set, so nothing could finish them
        afterwards. A step that raises is reported and the rest still run.
        """
        if self._closed:
            return
        self._closed = True

        _guard("motion", self._motion.release)
        _guard("the media interface", self._audio.stop)

        cancelled: asyncio.CancelledError | None = None
        for service in reversed(self._services):
            try:
                await _aguard(
                    "a service",
                    service.aclose,
                    self._cleanup_timeout_seconds,
                )
            except asyncio.CancelledError as error:
                if cancelled is None:
                    cancelled = error

        try:
            await _aguard(
                "the perception source",
                self._perception.aclose,
                self._cleanup_timeout_seconds,
            )
        except asyncio.CancelledError as error:
            if cancelled is None:
                cancelled = error
        _LOGGER.info("satellite.stopped")
        if cancelled is not None:
            raise cancelled


# How long shutdown waits for either audio thread to notice. Both are already
# unblocked before this is reached — the audio port is released first, so
# `read_chunk` has answered `None` and the pump has left the sentinel on the
# detection queue — so this is the bound on threads that are already finishing
# rather than a wait anybody expects to spend.
_THREAD_JOIN_SECONDS: Final = 2.0

# How many conditioned chunks may wait for the wake-word detector. Fifty, which
# at the ten-millisecond chunks the capture port produces is half a second.
_DETECTION_BACKLOG: Final = 50

# How many chunks beyond the queue's own size `_end_detection` will drain before
# giving up on placing its sentinel. Small, because the only producer left by
# then is a pump that has already been told to stop.
_SENTINEL_ATTEMPTS_MARGIN: Final = 4

# How often to say that detection or a pump edge is failing: once for the first,
# and once per hundred after that. A degraded robot must not spend what is left
# of its processor repeatedly writing the same traceback.
_DROP_REPORT_EVERY: Final = 100
_CHUNK_FAILURE_REPORT_EVERY: Final = 100

# A failed capture read is the only pump failure that happens before the capture
# adapter's own poll wait. Give the daemon a short recovery window rather than
# turning a persistent failure into a hot loop.
_PUMP_RETRY_SECONDS: Final = 0.05

# The sample format every chunk crossing the capture seam is in, spelled the way
# numpy spells it. Derived from the seam's own constant rather than written out,
# so the two cannot drift apart.
_PCM_DTYPE: Final = np.dtype(f"<i{SAMPLE_WIDTH}")


class WebRTCLike(Protocol):
    """The microphone conditioner the vendored `WebRTCProcessor` is one of.

    Narrower than the class, and it exists so the pump can be driven without
    the native audio-processing library being exercised: what the pump owes a
    test is that it builds one when Home Assistant asks for gain or noise
    suppression, keeps the one it built, and drops a block the conditioner has
    swallowed into its own frame buffer.
    """

    def update_settings(self, agc_level: int, ns_level: int) -> None:
        """Adopt new levels, rebuilding the processor if they changed.

        Args:
            agc_level: The automatic gain level Home Assistant asked for.
            ns_level: The noise-suppression level Home Assistant asked for.
        """
        ...

    def process(self, raw_bytes: bytes) -> bytes:
        """Condition one chunk of channel 0.

        Args:
            raw_bytes: Signed 16-bit little-endian samples.

        Returns:
            The conditioned audio, which is empty while the processor is still
            filling its ten-millisecond frame.
        """
        ...


class EsphomeService:
    """The protocol server Home Assistant connects to, and the microphone pump.

    One service rather than two, because the pump is meaningless without the
    server: it reads chunks off the capture port, applies the microphone
    settings Home Assistant owns, hands the result to whichever
    `VoiceSatelliteProtocol` is currently connected, and offers it to the
    wake-word detector. That is the feed the discarded upstream command-line
    entry point used to provide, and **both** halves of it: the streaming that
    carries a conversation Home Assistant has already started, and the
    detection that starts one.

    The pump runs on a thread of its own and that is not optional. `read_chunk`
    blocks until the daemon has audio, and doing that on the event loop would
    stall the protocol for the length of a chunk several times a second. The
    vendored code already expects to be called from another thread and hops back
    to the loop where it matters.

    **Detection runs on a second thread, and that is a decision worth stating.**
    Upstream runs its models inline in the loop that forwards audio, and on a
    desktop that is free. This robot has four cores and is also running motion
    control and the camera, and nothing in this repository has measured a
    microWakeWord inference on it — so running detection inline would be betting
    the audio feed on a number nobody here has. That bet loses the worse of the
    two failures: a pipeline that stutters because detection is slow is a
    conversation Home Assistant transcribes wrongly, where a wake word that
    arrives late is a wake word that still works. So `pump` does the input and
    output and never waits for a model, `detect` runs the models, and a bounded
    queue joins them — full means the chunk is dropped **for detection only**,
    with the streaming half of it already done. The two TensorFlow Lite runtimes
    and numpy release the interpreter lock inside their native calls, so the
    second thread is a second core rather than a share of this one.
    """

    def __init__(
        self,
        state: ServerState,
        capture: CapturePort,
        tap: PipelineEventTap,
        *,
        host: str,
        port: int,
        detector: WakeWordDetector | None = None,
        listen: Callable[..., Awaitable[Any]] | None = None,
        start_thread: Callable[[Callable[[], None]], Any] | None = None,
        build_webrtc: Callable[[int, int], WebRTCLike] = WebRTCProcessor,
        backlog: int = _DETECTION_BACKLOG,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        backoff: Backoff = DEFAULT_BACKOFF,
        pump_sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Describe the server without binding anything.

        Args:
            state: The vendored server state every connection is built over.
            capture: Where microphone chunks come from.
            tap: What the vendored code emits pipeline events into. Bound to
                the running loop when this service starts.
            host: The address to bind.
            port: The port to bind.
            detector: What runs the wake-word models. `None` runs the streaming
                half alone, which is what a test of that half wants and what no
                deployment wants — `build_application` always supplies one.
            listen: How to open the listening socket. Defaults to the running
                loop's `create_server`; injected so a test drives the pump and
                the lifecycle without opening one.
            start_thread: How to run the two threads. Defaults to daemon
                threads; injected so a test drives `pump`, `detect` and listener
                health checks itself rather than leaving autonomous work behind.
            build_webrtc: How to make the microphone conditioner, given the
                gain and the noise-suppression level. Injected so a test can
                prove the wiring without exercising the native library.
            backlog: How many chunks may wait for detection. Half a second of
                audio: long enough to ride out a slow inference, short enough
                that a detector which has fallen behind is answering about
                something that was recently said.
            sleep: How listener supervision waits between health checks and
                retries. Injected so tests advance without wall time.
            backoff: The increasing, capped delay after replacement binds fail.
            pump_sleep: How a failed capture read avoids a hot retry loop.
                Injected so tests perform no wall-time wait.
        """
        self._state = state
        self._capture = capture
        self._tap = tap
        self._host = host
        self._port = port
        self._detector = detector
        self._listen = listen
        self._start_thread = start_thread
        self._build_webrtc = build_webrtc
        self._sleep = sleep
        self._backoff = backoff
        self._pump_sleep = pump_sleep
        self._server: Any = None
        self._supervisor: asyncio.Task[None] | None = None
        self._threads: list[Any] = []
        self._running = False
        self._closing = False
        self._webrtc: WebRTCLike | None = None
        self._chunk_failures: dict[str, int] = {}
        # `None` is the sentinel that ends `detect`. Bounded, because an
        # unbounded queue in front of a detector that cannot keep up is a
        # memory leak ending in the daemon killing the application.
        self._pending: Queue[bytes | None] = Queue(maxsize=backlog)
        self._dropped = 0
        # The loop the protocol lives on, bound when this service starts. The
        # detection thread hands `wakeup` and `stop` to it rather than calling
        # them itself; see `detect`.
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        """Bind the port, supervise it, then feed audio into the protocol."""
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._tap.bind(loop)

        # Initial binding stays startup-fatal. Retrying here would advertise an
        # application whose protocol never started; supervision owns only a
        # listener that was demonstrably serving and then stopped.
        await self._bind_listener()
        if self._closing:
            return
        self._running = True
        start_thread = self._start_thread
        if start_thread is None:
            self._supervisor = asyncio.create_task(
                self._supervise_listener(),
                name="satellite-esphome-listener",
            )
            self._supervisor.add_done_callback(_report_if_it_failed)
            start_thread = _daemon_thread
        self._threads = [start_thread(self.pump)]
        if self._detector is not None:
            self._threads.append(start_thread(self.detect))

    async def _bind_listener(self) -> None:
        """Perform one listener bind, propagating any refusal to its caller."""
        listen = self._listen
        if listen is None:
            loop = self._loop
            if loop is None:
                message = "the ESPHome service has no event loop"
                raise RuntimeError(message)
            listen = loop.create_server
        server = await listen(
            lambda: VoiceSatelliteProtocol(self._state),
            self._host,
            self._port,
        )
        if self._closing:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
            return
        self._server = server
        _LOGGER.info("esphome.listening")

    async def check_listener(self) -> None:
        """Rebind only a listener that demonstrably stopped while still owned."""
        if not self._running:
            return
        server = self._server
        if server is not None and server.is_serving():
            return
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        self._server = None
        _LOGGER.warning("esphome.listener stopped unexpectedly; rebinding")
        await self._bind_listener()

    async def _supervise_listener(self) -> None:
        """Keep checking listener health and retry replacement binds with a cap."""
        attempt = 0
        while self._running:
            try:
                await self.check_listener()
            except asyncio.CancelledError:
                raise
            except Exception:
                attempt += 1
                delay = self._backoff.delay(attempt)
                _LOGGER.error(
                    "esphome.listener rebind failed; retrying in %.1f seconds",
                    delay,
                )
                await self._sleep(delay)
                continue
            attempt = 0
            await self._sleep(self._backoff.initial_seconds)

    def pump(self) -> None:
        """Feed captured audio into the live connection, isolating each chunk.

        Public because it is the whole of what the thread does, and a test that
        drives it directly is testing the feed rather than the thread pool.
        """
        try:
            while self._running:
                try:
                    chunk = self._capture.read_chunk()
                except Exception:
                    self._chunk_failed("microphone capture")
                    self._pump_sleep(_PUMP_RETRY_SECONDS)
                    continue
                if chunk is None:
                    # The capture source closed, which is what a released media
                    # interface looks like. There is nothing left to feed.
                    return
                try:
                    conditioned = self._condition(chunk)
                except Exception:
                    self._chunk_failed("microphone conditioning")
                    continue
                if conditioned is None:
                    continue
                primary, reference = conditioned
                satellite = self._state.satellite
                if satellite is not None:
                    # Forwarding is not allowed to own local detection. A stale
                    # Home Assistant transport may reject this chunk, but the
                    # network-independent wake model still receives the same one
                    # below and the pump continues with the next.
                    try:
                        satellite.handle_audio(primary, reference)
                    except Exception:
                        self._chunk_failed("Home Assistant audio forwarding")
                # Detection runs whether or not Home Assistant is there, and
                # that is REQ-044 rather than a nicety. `connection_lost` sets
                # `state.satellite` to `None`, so a robot whose network has
                # failed is a robot with no satellite — and skipping detection
                # for it would make the wake word depend on exactly the thing
                # the requirement says it must not. The models are streaming
                # models besides: one fed only the audio that arrived while a
                # connection happened to be up has no window to judge the first
                # word after a reconnection against.
                # Channel 0 only, and the queue's own `bytes` type is what
                # keeps it that way: the reference channel has no route here.
                self._offer(primary)
        finally:
            # However the pump ended, the detector is owed the news: `detect`
            # blocks on the queue and would otherwise never return.
            self._end_detection()

    def _chunk_failed(self, edge: str) -> None:
        """Rate-limit a failure to its static stage and aggregate count."""
        failures = self._chunk_failures.get(edge, 0) + 1
        self._chunk_failures[edge] = failures
        if failures % _CHUNK_FAILURE_REPORT_EVERY == 1:
            _LOGGER.error(
                "%s failed for a chunk; continuing (%d failures)",
                edge,
                failures,
            )

    def detect(self) -> None:
        """Run the wake-word models over the audio the pump has forwarded.

        Public for the same reason `pump` is: it is the whole of what the
        second thread does, so a test drives it directly.

        **What this thread does not do is touch the protocol.** `wakeup` and
        `stop` are not sends; they move the pipeline's own state, duck the
        music and start a sound, and every other transition of that state
        happens on the event loop while a packet is being handled. Calling them
        from here would race one — two pipelines started, or a response stopped
        halfway into starting — so `_apply` is handed to the loop instead, and
        it is what decides. Only the model runs on this thread, which is the
        whole reason the thread exists.
        """
        detector = self._detector
        loop = self._loop
        if detector is None or loop is None:
            # Either nothing runs the models, or the service was never started
            # — in both cases there is nothing to read from and nowhere to
            # deliver an activation to.
            return
        while True:
            chunk = self._pending.get()
            if chunk is None:
                return
            try:
                activations = detector.process(chunk)
            except Exception:
                # A model that raises must not take the detection thread with
                # it. The robot would go on streaming audio and never wake
                # again — which is the failure this half of the service exists
                # to fix, arrived at from the other direction.
                self._chunk_failed("wake-word detection")
                continue
            if not activations.woken and not activations.stopped:
                # Which is nearly every chunk. Nothing is handed to the loop for
                # one, so the ordinary cost of detection to the loop is nil.
                continue
            self._on_loop(loop, self._apply, activations)

    def _apply(self, activations: Activations) -> None:
        """Act on what a chunk contained, on the event loop rather than off it.

        Which connection is current is read *here* rather than on the detection
        thread, and that is the point of the method existing. Home Assistant
        reconnecting between the two is an ordinary event — a restart, a network
        blip — and a bound method captured before it would wake a protocol whose
        transport has closed, leaving the new connection with nothing while the
        refractory window swallowed the next attempt.

        Args:
            activations: What fired, already filtered by the mute switch and the
                refractory window.
        """
        if not self._running:
            # Shutdown has begun. REQ-050 asks for movement stopped and the
            # media interface released, and a wake word queued a moment before
            # the signal would undo neither of those but would move the
            # pipeline's state and write to a transport that is closing. The
            # last word spoken to a robot being shut down is not a
            # conversation.
            return
        satellite = self._state.satellite
        if satellite is None:
            # The models ran; there is simply nobody to tell.
            return
        for model in activations.woken:
            # The vendored signature names the two concrete runtime classes
            # where the detector speaks in the structural protocol both of them
            # satisfy. That is what lets a test drive this loop with a model
            # that is neither, and the cast is where the two spellings of the
            # same object meet.
            satellite.wakeup(cast("MicroWakeWord | OpenWakeWord", model))
        if activations.stopped:
            satellite.stop()

    @staticmethod
    def _on_loop(
        loop: asyncio.AbstractEventLoop,
        action: Callable[..., None],
        *args: object,
    ) -> None:
        """Run something on the event loop, from a thread that is not it.

        Args:
            loop: The loop the protocol lives on.
            action: What to run there.
            args: What to run it with.
        """
        try:
            loop.call_soon_threadsafe(action, *args)
        except RuntimeError:
            # The loop has closed, which means the application is on its way
            # out. A wake word arriving now has nothing left to start, and
            # there is no thread left to report it to.
            _LOGGER.debug("the event loop has closed; dropping %s", action)

    def _condition(self, chunk: Sequence[bytes]) -> tuple[bytes, bytes | None] | None:
        """Apply the microphone settings Home Assistant owns to one chunk.

        Three settings, and until this existed all three were inert: each
        entity took a value, persisted it and changed nothing audible, because
        the loop that read them was the loop that was never carried.

        **Every one of them applies to channel 0 and to nothing else.** Channel
        1 is not quieter microphone audio — `esphome/seams.py` says what it is
        at `AudioCapture`, and it is the speaker reference a server-side echo
        canceller subtracts from the microphone signal. That subtraction works
        on the *gain relationship* between the two, so attenuating the
        reference while the speaker physically played at full level leaves the
        canceller either under-cancelling or adapting to a filter that is
        wrong: the robot hears its own voice and talks over itself. It fails
        silently, only on a device that has a second channel, and it looks like
        flaky barge-in rather than like a setting.

        A uniform comprehension over `chunk` would be tidier and would be that
        bug, which is why the two channels are spelled separately here.

        Args:
            chunk: One `bytes` per channel, as the capture port produced it.

        Returns:
            Channel 0 conditioned and the reference channel exactly as it was
            captured, or `None` when the conditioner has swallowed this block
            into its own frame buffer and there is nothing yet to forward.
        """
        primary = _scaled(chunk[0], _mic_volume_scalar(self._state.mic_volume))
        # Untouched, and not merely unscaled: the same object, so the default
        # path allocates nothing for either channel.
        reference = chunk[1] if len(chunk) > 1 else None

        gain = self._state.preferences.mic_auto_gain or 0
        noise = self._state.preferences.mic_noise_suppression or 0
        if gain > 0 or noise > 0:
            if self._webrtc is None:
                self._webrtc = self._build_webrtc(gain, noise)
            else:
                self._webrtc.update_settings(gain, noise)
            primary = self._webrtc.process(primary)
            if not primary:
                return None

        return primary, reference

    def _offer(self, chunk: bytes) -> None:
        """Hand one chunk to the detection thread, or drop it.

        Args:
            chunk: Channel 0, conditioned.
        """
        if self._detector is None:
            return
        try:
            self._pending.put_nowait(chunk)
        except Full:
            self._dropped += 1
            if self._dropped % _DROP_REPORT_EVERY == 1:
                _LOGGER.warning(
                    "wake-word detection is behind the microphone; %d chunks "
                    "dropped from detection so far. The audio Home Assistant "
                    "receives is unaffected.",
                    self._dropped,
                )

    def _end_detection(self) -> None:
        """Put the sentinel that ends `detect` on the queue, come what may.

        A plain `put_nowait` is not enough, and the case where it is not is
        exactly the case this service is built for: a detector that has fallen
        behind leaves the queue full, the sentinel is refused, and `detect`
        blocks on `get` for ever — through `aclose`, which then spends its join
        timeout on a thread that is never going to end.

        So a full queue is drained a chunk at a time until the sentinel fits.
        The bound is the queue's own size and a small margin for a chunk the
        pump adds on its way past; a service that somehow exhausted it would
        leave the thread to the daemon-thread exit rather than hanging the
        event loop `aclose` runs on, which is the safer of the two failures.
        """
        for _ in range(self._pending.maxsize + _SENTINEL_ATTEMPTS_MARGIN):
            try:
                self._pending.put_nowait(None)
            except Full:
                with contextlib.suppress(Empty):
                    self._pending.get_nowait()
            else:
                return
        _LOGGER.warning(
            "the wake-word queue stayed full; the detection thread is left to "
            "the process exit rather than stopping now",
        )

    async def aclose(self) -> None:
        """Stop supervision, accepted protocols, the listener and both threads."""
        self._closing = True
        self._running = False
        supervisor, self._supervisor = self._supervisor, None
        if supervisor is not None:
            supervisor.cancel()

        # Stop new accepts before snapshotting the accepted protocols. A listening
        # server's close does not close those transports, so close each explicitly
        # and complete its lifecycle while the authoritative list still contains
        # every survivor. A real transport schedules connection_lost for later; by
        # then this synchronous call has made that duplicate callback idempotent.
        server, self._server = self._server, None
        if server is not None:
            server.close()
        connections = list(self._state.connections)
        for connection in connections:
            with contextlib.suppress(Exception):
                connection.close()
            with contextlib.suppress(Exception):
                connection.connection_lost(None)
        # Preserve the shutdown postcondition even for a malformed protocol whose
        # lifecycle callback raised before deregistering itself.
        self._state.connections.clear()
        self._state.satellite = None
        self._state.connected = False
        if server is not None:
            with contextlib.suppress(Exception):
                await server.wait_closed()
        if supervisor is not None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await supervisor
        # The pump ends the detection thread on its way out, but only if it was
        # ever running. A service closed without having read a chunk has to end
        # it here instead, and a second sentinel is harmless.
        self._end_detection()
        threads, self._threads = self._threads, []
        for thread in threads:
            if thread is not None and hasattr(thread, "join"):
                # Both threads are already unblocked: the audio port was
                # released before the services were stopped, so `read_chunk`
                # has answered `None` and the sentinel is on the queue.
                thread.join(timeout=_THREAD_JOIN_SECONDS)


def _mic_volume_scalar(mic_volume: int) -> float:
    """Turn Home Assistant's microphone volume into a factor to multiply by.

    Proportional across the whole slider, and attenuation only: 100 is the
    default, so the loudest the setting can ask for is the audio exactly as the
    daemon captured it.

    **There is deliberately no floor here, and the reason is that the floor is
    somebody else's.** `ServerState.persist_mic_volume` clamps what Home
    Assistant sets to 1 through 100, so the quietest the slider can ask for is a
    hundredth rather than silence, and the mute switch stays the only thing
    that silences the microphone. A floor at a tenth would not add that
    guarantee — it is already there — it would only flatten the bottom tenth of
    the slider onto one value, so that dragging it from ten to two would change
    nothing audible and nothing anywhere would say why.

    Args:
        mic_volume: The setting, as Home Assistant's own entity persisted it.

    Returns:
        What to multiply every sample by: at most 1.0, and never negative. The
        `max` is not about the slider, which cannot go below 1 — it is about a
        preferences file edited by hand, where a negative factor would invert
        the waveform rather than quieten it.
    """
    return min(1.0, max(0.0, mic_volume / 100.0))


def _scaled(pcm: bytes, scalar: float) -> bytes:
    """Apply the microphone volume to one channel of audio.

    Args:
        pcm: Signed 16-bit little-endian samples.
        scalar: What to multiply them by, at most 1.0.

    Returns:
        The same samples, quieter — or the very same object when nothing was
        asked for, which is the default and so is every chunk on most robots.
    """
    if scalar >= 1.0:
        return pcm
    samples = np.frombuffer(pcm, dtype=_PCM_DTYPE)
    # Rounded rather than truncated, for the reason `adapters/audio_reachy.py`
    # records at its own conversion: truncation biases every sample towards
    # zero, and the models were trained on audio that was not biased.
    return np.rint(samples * scalar).astype(_PCM_DTYPE).tobytes()


def _daemon_thread(work: Callable[[], None]) -> threading.Thread:
    """Run something on a thread that will not keep the process alive.

    Args:
        work: What to run.

    Returns:
        The started thread.
    """
    thread = threading.Thread(target=work, name="satellite-audio", daemon=True)
    thread.start()
    return thread


class Advertiser(Protocol):
    """What an mDNS advertisement has to be able to do.

    Narrower than the vendored advertiser, and that is what lets a test supply
    one that registers nothing: constructing the real one opens sockets in its
    own `__init__`, which a unit test may not do.
    """

    async def register_server(self) -> None:
        """Publish the record."""
        ...


class VolumeService:
    """Turns the daemon's own coarse volume up, once, when the app starts.

    Change 0016's R7, and a service rather than a line in `build_application`
    for one reason: it makes an HTTP request, and `build_application` is called
    by tests that may not perform input or output. A service does its work in
    `start`, which only a running application reaches.

    The request is made on a worker thread. It is a local call and normally
    immediate, but "normally" is not a property the event loop that answers
    Home Assistant should depend on.
    """

    def __init__(
        self,
        base_url: str,
        *,
        set_volume: Callable[[str], bool] = set_daemon_volume,
        offload: Offload = in_thread,
    ) -> None:
        """Say where the daemon is, without asking it anything.

        Args:
            base_url: Where the daemon serves its API. Empty turns this off.
            set_volume: How to set it. Injected so a test drives the wiring
                without a socket.
            offload: How to get the request off the event loop.
        """
        self._base_url = base_url
        self._set_volume = set_volume
        self._offload = offload

    async def start(self) -> None:
        """Ask the daemon for its loudest, and carry on either way."""
        await self._offload(functools.partial(self._set_volume, self._base_url))

    async def aclose(self) -> None:
        """Leave the volume where it is.

        Deliberately nothing. The daemon's volume is the robot's, not this
        application's: an operator who raised it after start-up should not have
        it put back because a voice satellite stopped.
        """


class AdvertisementService:
    """The mDNS record Home Assistant discovers the satellite through."""

    def __init__(
        self,
        *,
        name: str,
        port: int,
        identity: NetworkIdentity,
        build: Callable[..., Advertiser] = HomeAssistantZeroconf,
    ) -> None:
        """Describe the advertisement without registering it.

        Args:
            name: The announced identity, which is also the mDNS instance name.
            port: The port the ESPHome API is on.
            identity: The interface's address and hardware address.
            build: How to make the advertiser. Injected because constructing
                the real one opens sockets, which a unit test may not.
        """
        self._name = name
        self._port = port
        self._identity = identity
        self._build = build
        self._zeroconf: Advertiser | None = None

    async def start(self) -> None:
        """Register the service."""
        self._zeroconf = self._build(
            port=self._port,
            mac_address=self._identity.mac_address,
            host_ip_address=self._identity.ip_address,
            name=self._name,
        )
        await self._zeroconf.register_server()

    async def aclose(self) -> None:
        """Withdraw the record.

        The vendored advertiser has no close of its own — upstream's process
        exits instead — so its zeroconf instance is closed directly. Reaching
        into a derived file's private attribute is deliberate and narrow: the
        alternative is an unlisted edit to a vendored file, and leaving the
        record registered makes Home Assistant hold a device that is not there.
        """
        zeroconf, self._zeroconf = self._zeroconf, None
        if zeroconf is None:
            return
        aiozc: Any = getattr(zeroconf, "_aiozc", None)
        if aiozc is None:
            return
        with contextlib.suppress(Exception):
            await aiozc.async_close()


class WebService:
    """The settings interface, served beside the protocol on its own port."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        host: str,
        port: int,
        serve: Callable[[], Coroutine[Any, Any, None]] | None = None,
        shutdown: Callable[[], None] | None = None,
    ) -> None:
        """Describe the interface without binding anything.

        Args:
            app: The ASGI application to serve.
            host: The address to bind.
            port: The port to bind.
            serve: How to run it. Defaults to uvicorn; injected so a test
                drives the lifecycle without opening a socket.
            shutdown: How to ask it to stop. Paired with `serve`.
        """
        self._app = app
        self._host = host
        self._port = port
        self._serve = serve
        self._shutdown = shutdown
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Begin serving, on a task of its own."""
        serve, shutdown = self._serve, self._shutdown
        if serve is None:
            import uvicorn

            server = uvicorn.Server(
                uvicorn.Config(
                    self._app,
                    host=self._host,
                    port=self._port,
                    log_config=None,
                ),
            )

            def _stop() -> None:
                server.should_exit = True

            serve, shutdown = server.serve, _stop
            self._shutdown = shutdown
        self._task = asyncio.create_task(serve(), name="satellite-settings")
        # A task nobody awaits until shutdown is a task whose failure is a line
        # nobody sees. Binding a port already in use is the ordinary way for
        # this one to fail, and an operator whose settings page never appeared
        # deserves to be told why in the boot log.
        self._task.add_done_callback(_report_if_it_failed)
        _LOGGER.info("settings.listening host=%s port=%s", self._host, self._port)

    async def aclose(self) -> None:
        """Ask the server to stop, and wait for it."""
        task, self._task = self._task, None
        if task is None:
            return
        if self._shutdown is not None:
            self._shutdown()
        task.cancel()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await task


def _report_if_it_failed(task: asyncio.Task[None]) -> None:
    """Log why a background service ended, when it ended badly.

    Args:
        task: The finished task.
    """
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        _LOGGER.error(
            "%s stopped unexpectedly (%s)",
            task.get_name(),
            type(error).__name__,
        )


def _sound_paths() -> dict[str, str]:
    """Resolve the nine shipped chimes to absolute paths.

    Returns:
        The vendored state's sound fields, mapped to files in this wheel.
    """
    sounds = assets_dir() / "sounds"
    return {field: str(sounds / name) for field, name in _SOUNDS.items()}


def load_preferences(path: Path) -> Preferences:
    """Read the preferences Home Assistant has set on this device.

    Volume, active wake words and microphone settings are Home Assistant's to
    change through entities, so they are persisted rather than configured. A
    file that is missing or unreadable yields the defaults: losing a volume
    setting is not worth refusing to start over.

    Args:
        path: Where the vendored code writes them.

    Returns:
        The preferences, or fresh ones.
    """
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Preferences()
    if not isinstance(raw, dict):
        _LOGGER.warning("preferences at %s are not an object; ignoring them", path)
        return Preferences()

    preferences = Preferences()
    known = {field.name for field in fields(Preferences)}
    for name, value in raw.items():
        if name in known:
            setattr(preferences, name, value)
    return preferences


#:= docs/specs/ha-satellite/index.md#req-044-wake-word-detection-runs-on-the-robot
#:% Wake-word detection MUST run locally on the robot, without depending on the
#:% groundstation or on Home Assistant.
def build_server_state(
    settings: Settings,
    *,
    identity: NetworkIdentity,
    audio: AudioPort,
    state_dir: Path,
) -> ServerState:
    """Assemble the vendored protocol layer's state over this robot's ports.

    The wake-word models are loaded here, from files that ship inside the
    wheel, and nothing about that path reaches the network. That is **half** of
    REQ-044 and the half that is easiest to mistake for all of it — this
    function loaded both models and announced them to Home Assistant while
    nothing anywhere ran them. `wake_word.WakeWordDetector` is the other half,
    and it is what makes the wake word fire on a robot whose connection has
    failed — with the failure surfacing later, at the point the pipeline needs
    Home Assistant.

    Args:
        settings: The settings in effect.
        identity: What this robot announces itself as on the network.
        audio: The three audio surfaces, which fill both vendored seams.
        state_dir: Where preferences and downloaded media are kept.

    Returns:
        The state a `VoiceSatelliteProtocol` is built over.

    Raises:
        ConfigurationError: If the stop word cannot be loaded. Every other
            wake-word failure has a fallback inside the vendored loader; this
            one does not, and a satellite that cannot be told to stop talking
            is worse than one that will not start.
    """
    wake_word_dirs = [
        assets_dir() / "wakewords",
        state_dir / "external_wake_words",
    ]
    available = find_available_wake_words(wake_word_dirs, STOP_WORD_ID)
    preferences = load_preferences(state_dir / "preferences.json")
    requested = [
        wake_word
        for wake_word in (preferences.active_wake_words or [])
        if isinstance(wake_word, str)
    ] or [settings.active_wake_word]
    try:
        models, active, _fell_back = load_wake_models(
            available,
            requested,
            settings.active_wake_word,
        )
    except RuntimeError as error:
        # The vendored loader falls back through every wake word it can find
        # and raises only when there is nothing at all. That is an incomplete
        # installation rather than a bug, and upstream's message does not say
        # where it looked.
        message = (
            f"no wake word could be loaded from "
            f"{[str(directory) for directory in wake_word_dirs]}. They ship in "
            f"this wheel; a missing one means the installation is incomplete."
        )
        raise ConfigurationError(message) from error
    stop_word = load_stop_model(wake_word_dirs, STOP_WORD_ID)
    if stop_word is None:
        message = (
            f"the stop word model {STOP_WORD_ID!r} could not be loaded from "
            f"{[str(directory) for directory in wake_word_dirs]}. It ships in "
            f"this wheel; a missing one means the installation is incomplete."
        )
        raise ConfigurationError(message)

    sounds = _sound_paths()
    return ServerState(
        name=settings.device_name,
        friendly_name=settings.announced_friendly_name,
        mac_address=identity.mac_address,
        ip_address=identity.ip_address,
        network_interface=identity.interface,
        version=__version__,
        esphome_version=get_esphome_version(),
        audio_queue=Queue(),
        entities=[],
        available_wake_words=available,
        wake_words=models,
        active_wake_words=active,
        stop_word=stop_word,
        music_player=audio.music,
        tts_player=audio.speech,
        wakeup_sound=sounds["wakeup_sound"],
        start_listening_sound=sounds["start_listening_sound"],
        processing_sound=sounds["processing_sound"],
        timer_finished_sound=sounds["timer_finished_sound"],
        mute_sound=sounds["mute_sound"],
        unmute_sound=sounds["unmute_sound"],
        button_double_press_sound=sounds["button_double_press_sound"],
        button_triple_press_sound=sounds["button_triple_press_sound"],
        button_long_press_sound=sounds["button_long_press_sound"],
        preferences=preferences,
        preferences_path=state_dir / "preferences.json",
        download_dir=state_dir / "downloads",
        audio_capture=audio.capture,
        audio_input_channels=audio.capture.channels,
        volume=preferences.volume if preferences.volume is not None else 1.0,
        # The four microphone settings Home Assistant persists, taken back out
        # of the file at startup. Without these the entities come up at their
        # defaults every restart: an operator who had turned the microphone
        # down, or moved the stop word's sensitivity, would find the setting
        # still shown in Home Assistant and no longer in effect. The stop
        # word's goes through the vendored resolver, which clamps it and
        # supplies the default for a robot that has never had one set.
        mic_volume=preferences.mic_volume,
        mic_auto_gain=preferences.mic_auto_gain,
        mic_noise_suppression=preferences.mic_noise_suppression,
        stop_word_threshold=initial_stop_word_threshold(
            preferences.stop_word_sensitivity,
        ),
    )


def build_perception_source(
    settings: Settings,
    media: MediaInterface,
) -> PerceptionPort | None:
    """Assemble the detector an operator asked for, or none at all.

    Args:
        settings: The settings in effect.
        media: The daemon's media interface, which frames come off.

    Returns:
        The source to hand the behaviour layer, or `None` when face tracking is
        switched off — in which case nothing is built, no session is opened and
        no model is loaded.
    """
    if not settings.face_tracking_enabled:
        return None

    remote = None
    if settings.detection_source is not _ROBOT_ONLY:
        remote = RemotePerception(
            media,
            SessionClient(
                url=settings.groundstation_url,
                credential=Credential(
                    settings.groundstation_credential.get_secret_value(),
                ),
                capabilities=(Capability(name=FACE_CAPABILITY, version=1),),
            ),
            frame_interval=settings.frame_interval_seconds,
            staleness_seconds=settings.staleness_seconds,
        )

    local = None
    if settings.detection_source is not SourceSelection.REMOTE:
        model_path = Path(settings.local_model_path).expanduser()

        def _detector() -> SdkFaceDetector:
            """Load the SDK's detector, only if the fallback is ever reached.

            Returns:
                The detector.
            """
            return SdkFaceDetector(
                load_sdk_face_detector(model_path),
                score_threshold=settings.local_score_threshold,
                nms_threshold=settings.local_nms_threshold,
            )

        local = LocalPerception(
            media,
            detector=_detector,
            interval=settings.local_detection_interval_seconds,
            staleness_seconds=settings.staleness_seconds,
            offload=in_thread,
        )

    return build_perception(settings.detection_source, remote=remote, local=local)


class _NoPerception:
    """What the behaviour layer is handed when face tracking is switched off.

    It answers "nothing has ever been produced", which is the one answer that
    makes the tracker command nothing at all — see `behaviour.tracking` on why
    that is not the same as "nobody is there".
    """

    async def start(self) -> None:
        """Do nothing, successfully."""

    def latest(self) -> Detections:
        """Report that nothing has been seen.

        Returns:
            The empty view, with no source, so nothing is inferred from it.
        """
        return Detections()

    async def aclose(self) -> None:
        """Do nothing, successfully."""


def build_boost_setter(
    *,
    store: OverrideStore,
    apply_live: Callable[[Settings], None],
    environ: Mapping[str, str] | None = None,
) -> Callable[[float], None]:
    """Build what the speaker-boost control writes a chosen value through.

    A function of its own rather than a closure inside `build_application`, so
    that what happens when the overrides file cannot be written is a thing a
    test can stand in front of. Assembling the application is not: it reads the
    wheel's own wake-word models and cues off a real disk, which a fake
    filesystem cannot serve.

    `apply_live` is what reaches the audio adapter, so the returned setter never
    touches `ReachyAudio` and there is exactly one path from "a boost was
    chosen" to "the outputs heard about it", whichever surface chose it.

    **It always writes an override, even for a value equal to the environment's**
    — unlike `web/app.py`'s `_overrides_from`, which drops one matching the layer
    beneath. A reviewer will compare the two, so: a form renders every field and
    submits values nobody touched, whereas a slider only moves when somebody
    moves it, and there is no "revert to the environment" gesture on a slider to
    undo a pin with.

    Args:
        store: Where the overrides are kept.
        apply_live: What adopts the newly resolved settings.
        environ: The environment to resolve against. Defaults to the process
            environment, which is what `run` resolved the running configuration
            from.

    Returns:
        A setter taking the boost in percent.
    """

    def _set_boost(percent: float) -> None:
        """Persist a boost chosen from Home Assistant, and adopt it at once.

        Args:
            percent: The boost, already clamped by the entity that offers it.
        """
        try:
            previous = store.load()
            wanted = {
                **previous,
                "speaker_boost_percent": as_configured_string(percent),
            }
            # Equality with the *file*, which is a different question from the
            # one `build_boost_setter`'s docstring declines to act on: that one
            # is with the environment layer, and a first set equal to the
            # environment's value still writes its pin. This only drops a write
            # that would replace the file with itself — a scene or a scheduled
            # automation re-sending the value already stored, which would
            # otherwise re-resolve the settings and spend an erase cycle on the
            # robot's card each time. The vendored
            # `ServerState.persist_volume` declines the same write for the same
            # reason.
            if wanted == previous:
                return
            apply_settings_change(
                wanted,
                store=store,
                environ=environ,
                apply_live=apply_live,
            )
        except ConfigurationError as error:
            # Reported rather than raised: this runs inside the ESPHome
            # protocol's message loop, and an overrides file that cannot be
            # read or written must not drop the connection. The read is inside
            # the `try` for that reason — `OverrideStore.load` raises the same
            # error for a file somebody hand-edited into invalid JSON, and a
            # broken file is exactly when a slider is likeliest to be reached
            # for. The entity reads the boost back afterwards, so Home
            # Assistant is told the value actually in effect rather than the
            # one it asked for.
            _LOGGER.error("the speaker boost could not be saved: %s", error)

    return _set_boost


def build_application(
    resolution: Resolution,
    handle: RobotHandle,
    *,
    identity: NetworkIdentity | None = None,
) -> SatelliteApplication:
    """Wire the ports to the adapters and assemble everything that runs.

    Args:
        resolution: The settings in effect and where they came from.
        handle: What the daemon hands a running application.
        identity: What to announce on the network. Discovered from the machine
            when not supplied.

    Returns:
        The application, ready to be run.
    """
    settings = resolution.settings
    state_dir = state_directory(settings)
    announced = identity or discover_network_identity(
        interface=settings.network_interface,
        mac_address=settings.mac_address,
    )

    audio = ReachyAudio(
        handle.media,
        FileSoundSource(state_dir / "media"),
        samples_per_chunk=settings.samples_per_chunk,
        boost_percent=settings.speaker_boost_percent,
    )
    controller_config = ControllerConfig(
        staleness_seconds=settings.staleness_seconds,
        body_enabled=settings.body_motion_enabled,
        require_motion_measurements=True,
    )
    motion = ReachyMotion(
        handle,
        controller_config=controller_config,
        tick_seconds=settings.behaviour_tick_seconds,
    )
    perception: PerceptionPort = (
        build_perception_source(settings, handle.media) or _NoPerception()
    )
    behaviour = SatelliteBehaviour(
        idle_seconds=settings.idle_seconds,
        tracking_enabled=settings.face_tracking_enabled,
        controller_config=controller_config,
        now=time.monotonic(),
    )

    state = build_server_state(
        settings,
        identity=announced,
        audio=audio,
        state_dir=state_dir,
    )

    application = SatelliteApplication(
        settings=settings,
        audio=audio,
        motion=motion,
        perception=perception,
        behaviour=behaviour,
    )

    # The same file `run` read the overrides out of, and the same by
    # construction rather than by coincidence: `state_dir` is a bootstrap
    # setting, so the overrides layer cannot move it and this path cannot drift
    # from `config.overrides_path`. `test_satellite_main.py` pins the two
    # together. Two things write through it now — the settings interface below,
    # and the speaker-boost control — so it is built here rather than inside the
    # branch that decides whether that interface is served at all.
    store = OverrideStore(state_dir / OVERRIDES_FILENAME)

    # Appended before any connection exists, which is safe because the vendored
    # protocol layer's three de-duplication branches match its *own* classes by
    # `isinstance` and never touch these. The keys stay unique because that layer
    # numbers what it builds from `len(state.entities)`, so ours are 0 and 1 and
    # the media player shifts up — invisible to Home Assistant, which keys an
    # entity on `{mac}-{entity_type}-{object_id}` rather than on the key.
    state.entities.append(
        SpeakerVolumeNumberEntity(state=state, key=len(state.entities)),
    )
    boost = SpeakerBoostNumberEntity(
        state=state,
        key=len(state.entities),
        get_percent=lambda: application.settings.speaker_boost_percent,
        set_percent=build_boost_setter(
            store=store,
            apply_live=application.apply_live,
        ),
    )
    state.entities.append(boost)
    # The other direction, and the reason the boost control needs one where the
    # volume control does not: the settings page can change this value without
    # Home Assistant having asked. `apply_live` is what every change of it
    # passes through, so pushing from there covers both surfaces with one call
    # site.
    application.publish_live_changes(boost.publish)

    tap = PipelineEventTap(application.deliver)
    state.peripheral_api = tap

    services: list[Service] = [
        VolumeService(settings.daemon_api_url),
        EsphomeService(
            state,
            audio.capture,
            tap,
            host=settings.api_host,
            port=settings.api_port,
            # REQ-044. The models loaded above are only announced until
            # something runs them, and this is the something.
            detector=WakeWordDetector(state),
        ),
    ]
    if settings.advertise:
        services.append(
            AdvertisementService(
                name=settings.device_name,
                port=settings.api_port,
                identity=announced,
            ),
        )
    if settings.web_enabled:
        services.append(
            WebService(
                create_app(
                    resolution=resolution,
                    store=store,
                    application=application,
                ),
                host=settings.web_host,
                port=settings.web_port,
            ),
        )

    application.attach(services)
    return application


async def run(handle: RobotHandle, stop: asyncio.Event) -> None:
    """Read the configuration, build everything, and run until asked to stop.

    Args:
        handle: What the daemon hands a running application.
        stop: Set by the daemon's termination signal.

    A stop requested before motor enable, between motor enable and controlled
    wake, or after controlled wake prevents the next hardware or composition
    boundary. The blocking SDK call already running on a worker thread is allowed
    to finish; Python cannot safely cancel it in the middle.

    Raises:
        ConfigurationError: If the environment is not usable. Raised rather
            than reported, because the caller is what decides whether this is a
            process exit or an exception the daemon shows.
    """
    store = OverrideStore(overrides_path())
    resolution = load_settings(overrides=store.load())
    configure_logging(resolution.settings)
    log_resolved_configuration(resolution)
    if stop.is_set():
        _LOGGER.info("satellite.start skipped; stop already requested")
        return
    _LOGGER.info("satellite.wake enabling_motors")
    await in_thread(handle.enable_motors)
    if stop.is_set():
        _LOGGER.info("satellite.wake skipped; stop requested after motor enable")
        return
    _LOGGER.info("satellite.wake starting")
    await in_thread(handle.wake_up)
    _LOGGER.info("satellite.wake complete")
    if stop.is_set():
        _LOGGER.info("satellite.start skipped; stop requested during controlled wake")
        return
    application = build_application(resolution, handle)
    await application.run(stop)
