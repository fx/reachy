"""Pure two-phase predictive gaze and voice-pipeline arbitration.

A tick first selects one source-qualified directive. The composition root may
then ask the motion adapter to calibrate that directive and returns the result to
``finish``. Behavior owns no camera, robot or clock: both phases consume values.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from reachy_mini_ha_satellite.behaviour.gaze_controller import (
    BodyMeasurement,
    ControllerConfig,
    ControllerMode,
    ControllerState,
    GazeObservation,
    initial_controller_state,
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
from reachy_mini_ha_satellite.behaviour.tracking import (
    GazeDirective,
    GazeOutcome,
    GazeSelector,
)
from reachy_mini_ha_satellite.ports import NEUTRAL_ANTENNAS, NEUTRAL_HEAD

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
        ControllerMode.WORKSPACE_HOLD,
        ControllerMode.BODY_FEEDBACK_HOLD,
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
        self._idle_seconds = idle_seconds
        self._tracking_enabled = tracking_enabled
        self._last_head: HeadPose | None = None
        self._last_antennas: AntennaPose | None = None
        self._outcome = GazeOutcome.UNKNOWN
        self._idle_since: float | None = now

    @property
    def state(self) -> PipelineState:
        """Return the current voice-pipeline state."""
        return self._machine.state

    @property
    def controller_state(self) -> ControllerState:
        """Return immutable predictive controller state for status and tests."""
        return self._controller

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
        body_measurement: BodyMeasurement | None,
        dt: float,
    ) -> tuple[MotionIntent, ...]:
        """Advance one controller tick and return ordered gaze/expression intents."""
        observation = self._observation(prepared.directive, calibrated)
        previously_owned = self._owns_head()
        result = step_controller(
            self._controller,
            observation,
            now=prepared.now,
            dt=dt,
            config=self._config,
            body_measurement=body_measurement,
        )
        self._controller = result.state
        now_owned = self._owns_head()
        settled_handoff = previously_owned and result.mode is ControllerMode.IDLE

        intents: list[MotionIntent] = []
        if now_owned or settled_handoff:
            intents.append(CommandGaze(result.sample))
        intents.extend(
            self._express(
                prepared.now,
                forced=prepared.pipeline_expired or settled_handoff,
                allow_head=not now_owned,
                force_head=settled_handoff,
            )
        )
        return tuple(intents)

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
        return self._controller.mode in _OWNED_MODES

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
