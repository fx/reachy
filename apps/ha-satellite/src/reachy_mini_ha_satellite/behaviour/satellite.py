"""Pure two-phase predictive gaze and voice-pipeline arbitration.

A tick first selects one source-qualified directive. The composition root may
then ask the motion adapter to calibrate that directive and returns the result to
``finish``. Behavior owns no camera, robot or clock: both phases consume values.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

from reachy_mini_ha_satellite.behaviour.controller_diagnostics import (
    ControllerDiagnostics,
    DiagnosticScalar,
)
from reachy_mini_ha_satellite.behaviour.gaze_controller import (
    BodyMeasurement,
    ControllerConfig,
    ControllerFault,
    ControllerMode,
    ControllerState,
    ControllerStep,
    GazeObservation,
    HeadMeasurement,
    initial_controller_state,
    reduce_command_result,
    step_controller,
)
from reachy_mini_ha_satellite.behaviour.intents import (
    CommandGaze,
    MotionIntent,
    MoveAntennas,
    MoveHead,
)
from reachy_mini_ha_satellite.behaviour.movement import Expression, expression
from reachy_mini_ha_satellite.behaviour.pipeline import (
    PipelineEvent,
    PipelineMachine,
    PipelineState,
)
from reachy_mini_ha_satellite.behaviour.tracking import GazeSelector
from reachy_mini_ha_satellite.ports import (
    NEUTRAL_ANTENNAS,
    NEUTRAL_HEAD,
    GazeDirective,
    GazeOutcome,
    MotionCommandResult,
    MotionCommandStatus,
)

if TYPE_CHECKING:
    from reachy_mini_ha_satellite.ports import (
        AntennaPose,
        CalibratedGaze,
        Detections,
        HeadPose,
    )

__all__ = ["BehaviourStatus", "PreparedGazeTick", "SatelliteBehaviour"]

_POSE_EPSILON: Final = 0.005
_OWNED_MODES: Final = frozenset(
    {
        ControllerMode.ACTIVE,
        ControllerMode.HOLD,
        ControllerMode.RETURNING,
    }
)


def _head_changed(pose: HeadPose, previous: HeadPose | None) -> bool:
    """Return whether a pipeline head pose changed visibly."""
    if previous is None:
        return True
    return (
        abs(pose.yaw - previous.yaw) > _POSE_EPSILON
        or abs(pose.pitch - previous.pitch) > _POSE_EPSILON
        or abs(pose.roll - previous.roll) > _POSE_EPSILON
    )


def _antennas_changed(pose: AntennaPose, previous: AntennaPose | None) -> bool:
    """Return whether an antenna pose changed visibly."""
    if previous is None:
        return True
    return (
        abs(pose.left - previous.left) > _POSE_EPSILON
        or abs(pose.right - previous.right) > _POSE_EPSILON
    )


def _observation_age(directive: GazeDirective, now: float) -> float | None:
    """Report capture age, falling back to receipt for compatible old metadata."""
    observed_at = directive.captured_at
    if observed_at is None:
        observed_at = directive.received_at
    return None if observed_at is None else max(0.0, now - observed_at)


@dataclass(frozen=True, slots=True)
class PreparedGazeTick:
    """One pure selection awaiting optional adapter calibration."""

    directive: GazeDirective
    now: float
    pipeline_expired: bool


@dataclass(frozen=True, slots=True)
class BehaviourStatus:
    """Current pipeline, gaze outcome, ownership and idle state."""

    state: PipelineState
    outcome: GazeOutcome
    idle: bool
    tracking: bool


class SatelliteBehaviour:
    """All motion decisions, without input, output or clock reads."""

    def __init__(
        self,
        *,
        idle_seconds: float = 6.0,
        tracking_enabled: bool = True,
        controller_config: ControllerConfig | None = None,
        diagnostics: ControllerDiagnostics | None = None,
        now: float = 0.0,
    ) -> None:
        """Create pipeline, selector and predictive controller state."""
        if not math.isfinite(idle_seconds) or idle_seconds <= 0.0:
            message = (
                f"the idle interval must be finite and positive, not {idle_seconds}"
            )
            raise ValueError(message)
        self._machine = PipelineMachine(now=now)
        self._selector = GazeSelector()
        self._config = controller_config or ControllerConfig()
        self._controller = initial_controller_state(self._config)
        self._diagnostics = diagnostics or ControllerDiagnostics()
        self._idle_seconds = idle_seconds
        self._tracking_enabled = tracking_enabled
        self._last_head: HeadPose | None = None
        self._last_antennas: AntennaPose | None = None
        self._outcome = GazeOutcome.UNKNOWN
        self._idle_since: float | None = now
        self._pending_prior_state: ControllerState | None = None
        self._pending_command_step: ControllerStep | None = None
        self._pending_observation_age: float | None = None
        self._pending_handoff = False

    @property
    def state(self) -> PipelineState:
        """Return the current voice-pipeline state."""
        return self._machine.state

    @property
    def controller_state(self) -> ControllerState:
        """Return immutable predictive controller state for status and tests."""
        return self._controller

    def controller_diagnostics(self) -> tuple[dict[str, DiagnosticScalar], ...]:
        """Return a bounded identifier-free snapshot of controller evidence."""
        return self._diagnostics.snapshot()

    def reset_controller_diagnostics(self) -> None:
        """Reset diagnostics without touching controller or expression state."""
        self._diagnostics.reset()

    def retune(self, *, idle_seconds: float) -> None:
        """Adopt the only live behavior value without rebuilding state."""
        if not math.isfinite(idle_seconds) or idle_seconds <= 0.0:
            message = (
                f"the idle interval must be finite and positive, not {idle_seconds}"
            )
            raise ValueError(message)
        self._idle_seconds = idle_seconds

    def status(self, now: float) -> BehaviourStatus:
        """Report pipeline and predictive ownership without inferring from motion."""
        return BehaviourStatus(
            state=self._machine.state,
            outcome=self._outcome,
            idle=self._is_idle(now),
            tracking=self._controller.mode is ControllerMode.ACTIVE,
        )

    def handle(self, event: PipelineEvent, now: float) -> tuple[MotionIntent, ...]:
        """Apply one pipeline event, suppressing only its contended head channel."""
        transition = self._machine.handle(event, now)
        if not transition.changed:
            return ()
        return self._express(now, forced=True, allow_head=not self._owns_head())

    def prepare(self, detections: Detections, now: float) -> PreparedGazeTick:
        """Select one qualified directive before any adapter work occurs."""
        expired = self._machine.tick(now)
        directive = (
            self._selector.select(detections)
            if self._tracking_enabled
            else self._selector.stand_down()
        )
        self._outcome = directive.outcome
        if directive.face is not None:
            self._idle_since = None
        elif (
            directive.outcome
            in {
                GazeOutcome.NOBODY,
                GazeOutcome.STALE,
            }
            and self._idle_since is None
        ):
            self._idle_since = now
        return PreparedGazeTick(directive, now, expired is not None)

    def finish(
        self,
        prepared: PreparedGazeTick,
        *,
        calibrated: CalibratedGaze | None,
        head_measurement: HeadMeasurement | None = None,
        body_measurement: BodyMeasurement | None,
        dt: float,
        input_fault: ControllerFault = ControllerFault.NONE,
        input_evidence: tuple[object, ...] | None = None,
    ) -> tuple[MotionIntent, ...]:
        """Advance one controller tick and return ordered gaze/expression intents."""
        observation = self._observation(prepared.directive, calibrated)
        previously_owned = self._owns_head()
        previous_controller = self._controller
        previous_sample = previous_controller.last_safe_sample
        result = step_controller(
            previous_controller,
            observation,
            now=prepared.now,
            dt=dt,
            config=self._config,
            head_measurement=head_measurement,
            body_measurement=body_measurement,
            input_fault=input_fault,
            input_evidence=input_evidence,
        )
        self._controller = result.state
        now_owned = self._owns_head()
        settled_handoff = previously_owned and result.mode is ControllerMode.IDLE

        command_ready = result.state.head_initialized and (
            not self._config.body_enabled or result.state.body_feedback.initialized
        )
        observation_age = _observation_age(prepared.directive, prepared.now)
        intents: list[MotionIntent] = []
        emitted = command_ready and (
            settled_handoff
            or result.state.fault is ControllerFault.COMMAND
            or (
                now_owned and (not previously_owned or result.sample != previous_sample)
            )
        )
        if emitted:
            self._pending_prior_state = previous_controller
            self._pending_command_step = result
            self._pending_observation_age = observation_age
            self._pending_handoff = settled_handoff
            intents.append(CommandGaze(result.sample))
        intents.extend(
            self._express(
                prepared.now,
                forced=prepared.pipeline_expired or settled_handoff,
                allow_head=not now_owned and not self._pending_handoff,
                force_head=settled_handoff,
            )
        )
        if not emitted:
            self._diagnostics.record(
                result,
                config=self._config,
                at=prepared.now,
                observation_age=observation_age,
                emitted=False,
            )
        return tuple(intents)

    def complete_command(
        self,
        result: MotionCommandResult,
    ) -> tuple[MotionIntent, ...]:
        """Reduce one command, record it, then release a committed handoff barrier."""
        previous = self._pending_prior_state
        pending = self._pending_command_step
        observation_age = self._pending_observation_age
        pending_handoff = self._pending_handoff
        self._pending_prior_state = None
        self._pending_command_step = None
        self._pending_observation_age = None
        self._pending_handoff = False
        if previous is None or pending is None:
            return ()
        self._controller = reduce_command_result(
            pending.state,
            previous,
            result,
            self._config,
        )
        at = pending.state.last_step_at
        if at is None:
            raise AssertionError("an emitted controller command must belong to a tick")
        diagnostic_step = replace(
            pending,
            state=replace(pending.state, fault=self._controller.fault),
        )
        accepted = result.status is MotionCommandStatus.ACCEPTED
        self._diagnostics.record(
            diagnostic_step,
            config=self._config,
            at=at,
            observation_age=observation_age,
            emitted=True,
            command_accepted=accepted,
        )
        if (
            not pending_handoff
            or not accepted
            or self._controller.fault is not ControllerFault.NONE
            or self._controller.mode is not ControllerMode.IDLE
        ):
            return ()
        wanted = self._expression(at).head
        self._last_head = wanted
        return (MoveHead(wanted),)

    @staticmethod
    def _observation(
        directive: GazeDirective,
        calibrated: CalibratedGaze | None,
    ) -> GazeObservation | None:
        """Join a selected directive with matching adapter calibration."""
        identity = directive.identity
        if not directive.actionable or identity is None:
            return None
        source, generation, sequence = identity
        if directive.captured_at is None or directive.received_at is None:
            return None
        if directive.face is None:
            return GazeObservation(
                source=source.value,
                generation=generation,
                sequence=sequence,
                captured_at=directive.captured_at,
                received_at=directive.received_at,
                target_key=directive.target_epoch,
                face=None,
            )
        if (
            calibrated is None
            or (
                calibrated.source,
                calibrated.generation,
                calibrated.sequence,
            )
            != identity
            or calibrated.captured_at != directive.captured_at
            or calibrated.received_at != directive.received_at
            or calibrated.target_epoch != directive.target_epoch
        ):
            return None
        return GazeObservation(
            source=source.value,
            generation=generation,
            sequence=sequence,
            captured_at=directive.captured_at,
            received_at=directive.received_at,
            target_key=directive.target_epoch,
            face=directive.face,
            world_yaw=calibrated.world_yaw,
            world_elevation=calibrated.world_elevation,
        )

    def _owns_head(self) -> bool:
        """Return whether predictive gaze has exclusive head ownership."""
        return (
            self._pending_handoff
            or self._controller.mode in _OWNED_MODES
            or self._controller.safe_hold
        )

    def _is_idle(self, now: float) -> bool:
        """Return whether the room has been without a selected face long enough."""
        return (
            self._idle_since is not None
            and now - self._idle_since >= self._idle_seconds
        )

    def _expression(self, now: float) -> Expression:
        """Return independent pipeline/idle head and antenna expression."""
        state = self._machine.state
        if state is not PipelineState.IDLE:
            return expression(state, self._machine.elapsed(now))
        idle_since = self._idle_since
        if idle_since is None or now - idle_since < self._idle_seconds:
            return Expression(antennas=NEUTRAL_ANTENNAS, head=NEUTRAL_HEAD)
        return expression(PipelineState.IDLE, now - idle_since - self._idle_seconds)

    def _express(
        self,
        now: float,
        *,
        forced: bool,
        allow_head: bool,
        force_head: bool = False,
    ) -> tuple[MotionIntent, ...]:
        """Return antenna expression and an uncontented pipeline head command."""
        wanted = self._expression(now)
        intents: list[MotionIntent] = []
        if forced or _antennas_changed(wanted.antennas, self._last_antennas):
            self._last_antennas = wanted.antennas
            intents.append(MoveAntennas(wanted.antennas))
        if allow_head and (
            force_head or forced or _head_changed(wanted.head, self._last_head)
        ):
            self._last_head = wanted.head
            intents.append(MoveHead(wanted.head))
        return tuple(intents)
