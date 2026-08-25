"""Pure predictive gaze estimation and jerk-limited coordinated motion.

One source-qualified observation updates the estimator once. Faster behavior
polls advance only immutable trajectory state. Time, workspace acceptance and
observations are explicit arguments; this module performs no input or output and
has no robot, adapter or clock dependency.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final

from reachy_contracts import FaceDetection
from reachy_mini_ha_satellite.motion_validation import validate_gaze_sample
from reachy_mini_ha_satellite.ports import (
    GazeSample,
    MotionCommandResult,
    MotionCommandStatus,
)

__all__ = [
    "AxisLimits",
    "AxisState",
    "BodyFeedbackState",
    "BodyMeasurement",
    "ControllerConfig",
    "ControllerFault",
    "ControllerMode",
    "ControllerState",
    "ControllerStep",
    "DeadbandState",
    "EstimatorReset",
    "EstimatorState",
    "GazeObservation",
    "HeadMeasurement",
    "ImagePoint",
    "allocate_body",
    "apply_deadband",
    "initial_controller_state",
    "predict_error",
    "reduce_command_result",
    "step_axis",
    "step_controller",
    "update_estimator",
]

_DEGREES: Final = math.pi / 180.0
_EPSILON: Final = 1e-12


def _finite(name: str, value: float) -> None:
    """Require one finite scalar."""
    if not math.isfinite(value):
        message = f"the {name} must be finite, not {value}"
        raise ValueError(message)


def _positive(name: str, value: float) -> None:
    """Require one finite positive scalar."""
    _finite(name, value)
    if value <= 0.0:
        message = f"the {name} must be positive, not {value}"
        raise ValueError(message)


def _non_negative(name: str, value: float) -> None:
    """Require one finite non-negative scalar."""
    _finite(name, value)
    if value < 0.0:
        message = f"the {name} must not be negative, not {value}"
        raise ValueError(message)


def _unit(name: str, value: float) -> None:
    """Require one finite scalar on the closed unit interval."""
    _finite(name, value)
    if not 0.0 <= value <= 1.0:
        message = f"the {name} must be between zero and one, not {value}"
        raise ValueError(message)


def _positive_integer(name: str, value: object) -> None:
    """Require a real positive integer rather than accepting bool as one."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        message = f"the {name} must be a positive integer, not {value!r}"
        raise ValueError(message)


def _boolean(name: str, value: object) -> None:
    """Require an actual boolean at a runtime boundary."""
    if not isinstance(value, bool):
        message = f"the {name} must be boolean, not {value!r}"
        raise ValueError(message)


def _clamp(value: float, lower: float, upper: float) -> float:
    """Bound a scalar to one closed interval."""
    return min(upper, max(lower, value))


@dataclass(frozen=True, slots=True)
class ImagePoint:
    """Two independent normalized image coordinates or rates."""

    x: float
    y: float

    def __post_init__(self) -> None:
        """Reject arithmetic poison at the boundary."""
        _finite("image x", self.x)
        _finite("image y", self.y)


@dataclass(frozen=True, slots=True)
class GazeObservation:
    """One completed source-qualified face result.

    Identity is ``source + generation + sequence``. ``target_key`` is an
    adapter-independent selected-target association supplied by the caller; a
    change resets velocity without becoming persistent biometric identity.
    """

    source: str
    generation: int
    sequence: int
    captured_at: float
    received_at: float
    target_key: int
    face: FaceDetection | None
    world_yaw: float | None = None
    world_elevation: float | None = None

    def __post_init__(self) -> None:
        """Validate the self-contained observation facts."""
        if not self.source:
            message = "the observation source must not be empty"
            raise ValueError(message)
        if self.generation < 0 or self.sequence < 0 or self.target_key < 0:
            message = (
                "observation generation, sequence and target key must not be negative"
            )
            raise ValueError(message)
        _finite("capture time", self.captured_at)
        _finite("receipt time", self.received_at)
        if self.received_at < self.captured_at:
            message = "an observation cannot be received before capture"
            raise ValueError(message)
        if self.face is None and (
            self.world_yaw is not None or self.world_elevation is not None
        ):
            message = "an empty observation cannot carry a world target"
            raise ValueError(message)
        if (self.world_yaw is None) != (self.world_elevation is None):
            message = "world yaw and elevation anchors must be supplied together"
            raise ValueError(message)
        if self.world_yaw is not None:
            _finite("observation world yaw", self.world_yaw)
            if self.world_elevation is None:
                raise AssertionError("paired world anchors were checked above")
            _finite("observation world elevation", self.world_elevation)

    @property
    def identity(self) -> tuple[str, int, int]:
        """Return source, generation and sequence as one comparison key."""
        return self.source, self.generation, self.sequence


@dataclass(frozen=True, slots=True)
class AxisLimits:
    """Position, velocity, acceleration and jerk bounds for one axis."""

    minimum: float
    maximum: float
    max_velocity: float
    max_acceleration: float
    max_jerk: float

    def __post_init__(self) -> None:
        """Refuse an axis envelope that cannot be integrated safely."""
        for name, value in (
            ("axis minimum", self.minimum),
            ("axis maximum", self.maximum),
            ("axis maximum velocity", self.max_velocity),
            ("axis maximum acceleration", self.max_acceleration),
            ("axis maximum jerk", self.max_jerk),
        ):
            _finite(name, value)
        if self.minimum >= self.maximum:
            message = "an axis minimum must be lower than its maximum"
            raise ValueError(message)
        if min(self.max_velocity, self.max_acceleration, self.max_jerk) <= 0.0:
            message = "axis derivative limits must be positive"
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class AxisState:
    """One online trajectory axis."""

    position: float = 0.0
    velocity: float = 0.0
    acceleration: float = 0.0

    def __post_init__(self) -> None:
        """Reject non-finite hidden trajectory state."""
        _finite("axis position", self.position)
        _finite("axis velocity", self.velocity)
        _finite("axis acceleration", self.acceleration)


@dataclass(frozen=True, slots=True)
class HeadMeasurement:
    """One measured world head direction in controller monotonic time."""

    world_yaw: float
    world_elevation: float
    measured_at: float

    def __post_init__(self) -> None:
        """Reject malformed measured direction before trajectory seeding."""
        _finite("measured world yaw", self.world_yaw)
        _finite("measured world elevation", self.world_elevation)
        _finite("head measurement time", self.measured_at)


@dataclass(frozen=True, slots=True)
class BodyMeasurement:
    """One measured body-yaw sample in controller monotonic time."""

    yaw: float
    measured_at: float

    def __post_init__(self) -> None:
        """Reject malformed feedback before it reaches safety arithmetic."""
        _finite("measured body yaw", self.yaw)
        _finite("body measurement time", self.measured_at)


@dataclass(frozen=True, slots=True)
class BodyFeedbackState:
    """Independent feedback fault timing and bounded recovery evidence."""

    initialized: bool = False
    last_measurement: BodyMeasurement | None = None
    fault_started_at: float | None = None
    faulted: bool = False
    valid_streak: int = 0


@dataclass(frozen=True, slots=True)
class DeadbandState:
    """Whether the smooth elliptical Schmitt region is active."""

    active: bool = False


class EstimatorReset(StrEnum):
    """Why a new result did not inherit target velocity."""

    NONE = "none"
    FIRST = "first"
    SOURCE = "source"
    GENERATION = "generation"
    TARGET = "target"
    TIME_ORDER = "time_order"
    GAP = "gap"


@dataclass(frozen=True, slots=True)
class EstimatorState:
    """Two-axis position/velocity estimate at the latest capture time."""

    identity: tuple[str, int, int]
    target_key: int
    measured: ImagePoint
    position: ImagePoint
    velocity: ImagePoint
    captured_at: float
    received_at: float
    world_position: ImagePoint | None = None
    world_velocity: ImagePoint = ImagePoint(0.0, 0.0)


class ControllerMode(StrEnum):
    """Externally meaningful target and ownership lifecycle."""

    UNKNOWN = "unknown"
    ACTIVE = "active"
    HOLD = "hold"
    RETURNING = "returning"
    IDLE = "idle"


class ControllerFault(StrEnum):
    """Stable categories for real safety channels, independent from lifecycle."""

    NONE = "none"
    TIMING = "timing"
    POSE = "pose"
    CALIBRATION = "calibration"
    DERIVATIVE = "derivative"
    WORKSPACE = "workspace"
    BODY_FEEDBACK = "body_feedback"
    COMMAND = "command"


@dataclass(frozen=True, slots=True)
class ControllerConfig:
    """Validated estimator, deadband and trajectory tuning.

    Defaults are deterministic simulation values, not live calibration. Body
    output is deliberately disabled by default.
    """

    estimator_alpha: float = 0.65
    estimator_beta: float = 0.08
    estimator_gap: float = 0.50
    minimum_observation_dt: float = 0.02
    maximum_observation_dt: float = 0.50
    actuator_delay: float = 0.10
    prediction_horizon: float = 0.35
    image_position_limit: float = 1.5
    image_velocity_limit: float = 2.5
    world_velocity_limit: float = 20.0 * _DEGREES
    deadband_x: float = 0.0185
    deadband_y: float = 0.026
    deadband_start: float = 2.00
    deadband_stop: float = 0.65
    horizontal_fov: float = 87.0 * _DEGREES
    vertical_fov: float = 67.0 * _DEGREES
    feedback_gain: float = 4.0
    feedforward_gain: float = 1.0
    staleness_seconds: float = 2.0
    loss_hold_seconds: float = 0.35
    maximum_tick_dt: float = 0.20
    stall_integration_dt: float = 0.05
    yaw_limits: AxisLimits = AxisLimits(
        minimum=-55.0 * _DEGREES,
        maximum=55.0 * _DEGREES,
        max_velocity=55.0 * _DEGREES,
        max_acceleration=160.0 * _DEGREES,
        max_jerk=700.0 * _DEGREES,
    )
    elevation_limits: AxisLimits = AxisLimits(
        minimum=-30.0 * _DEGREES,
        maximum=35.0 * _DEGREES,
        max_velocity=40.0 * _DEGREES,
        max_acceleration=120.0 * _DEGREES,
        max_jerk=500.0 * _DEGREES,
    )
    body_enabled: bool = False
    require_motion_measurements: bool = False
    head_measurement_max_age: float = 0.25
    body_feedback_max_age: float = 0.25
    body_feedback_divergence: float = 8.0 * _DEGREES
    body_feedback_persistence: float = 0.50
    body_feedback_recovery_samples: int = 3
    body_limits: AxisLimits = AxisLimits(
        minimum=-70.0 * _DEGREES,
        maximum=70.0 * _DEGREES,
        max_velocity=25.0 * _DEGREES,
        max_acceleration=60.0 * _DEGREES,
        max_jerk=180.0 * _DEGREES,
    )
    body_noise_floor: float = 2.0 * _DEGREES
    body_midpoint: float = 15.0 * _DEGREES
    body_mid_share: float = 0.25
    body_large_point: float = 45.0 * _DEGREES
    body_large_share: float = 0.60
    body_head_comfort: float = 25.0 * _DEGREES
    idle_position_epsilon: float = 0.25 * _DEGREES
    idle_velocity_epsilon: float = 0.5 * _DEGREES
    idle_acceleration_epsilon: float = 2.0 * _DEGREES
    workspace_recovery_samples: int = 2

    def __post_init__(self) -> None:
        """Validate scalar and cross-field controller constraints."""
        _unit("estimator alpha", self.estimator_alpha)
        _unit("estimator beta", self.estimator_beta)
        for name, value in (
            ("estimator gap", self.estimator_gap),
            ("minimum observation dt", self.minimum_observation_dt),
            ("maximum observation dt", self.maximum_observation_dt),
            ("prediction horizon", self.prediction_horizon),
            ("image position limit", self.image_position_limit),
            ("image velocity limit", self.image_velocity_limit),
            ("world velocity limit", self.world_velocity_limit),
            ("deadband x", self.deadband_x),
            ("deadband y", self.deadband_y),
            ("horizontal field of view", self.horizontal_fov),
            ("vertical field of view", self.vertical_fov),
            ("feedback gain", self.feedback_gain),
            ("staleness seconds", self.staleness_seconds),
            ("maximum tick dt", self.maximum_tick_dt),
            ("stall integration dt", self.stall_integration_dt),
            ("head measurement maximum age", self.head_measurement_max_age),
            ("body feedback maximum age", self.body_feedback_max_age),
            ("body feedback divergence", self.body_feedback_divergence),
            ("body feedback persistence", self.body_feedback_persistence),
            ("body midpoint", self.body_midpoint),
            ("body large point", self.body_large_point),
            ("body head comfort", self.body_head_comfort),
        ):
            _positive(name, value)
        if self.horizontal_fov >= math.pi or self.vertical_fov >= math.pi:
            message = "camera fields of view must be lower than pi radians"
            raise ValueError(message)
        for name, value in (
            ("actuator delay", self.actuator_delay),
            ("feedforward gain", self.feedforward_gain),
            ("loss hold seconds", self.loss_hold_seconds),
            ("body noise floor", self.body_noise_floor),
            ("idle position epsilon", self.idle_position_epsilon),
            ("idle velocity epsilon", self.idle_velocity_epsilon),
            ("idle acceleration epsilon", self.idle_acceleration_epsilon),
        ):
            _non_negative(name, value)
        if self.estimator_alpha == 0.0 or self.estimator_beta == 0.0:
            message = "estimator gains must be greater than zero"
            raise ValueError(message)
        if (
            not self.minimum_observation_dt
            <= self.maximum_observation_dt
            <= self.estimator_gap
        ):
            message = (
                "observation dt bounds must not exceed the supported estimator gap"
            )
            raise ValueError(message)
        if not self.actuator_delay <= self.prediction_horizon <= self.staleness_seconds:
            message = "actuator delay, prediction horizon and staleness must be ordered"
            raise ValueError(message)
        if self.loss_hold_seconds > self.staleness_seconds:
            message = "loss hold must not exceed observation staleness"
            raise ValueError(message)
        if not 0.0 < self.deadband_stop < self.deadband_start:
            message = "deadband stop must be positive and lower than start"
            raise ValueError(message)
        if (
            max(
                self.deadband_x * self.deadband_start,
                self.deadband_y * self.deadband_start,
            )
            >= self.image_position_limit
        ):
            message = "deadband activation must fit inside the image position envelope"
            raise ValueError(message)
        if not self.body_noise_floor < self.body_midpoint < self.body_large_point:
            message = "body allocation knots must increase"
            raise ValueError(message)
        _unit("body midpoint share", self.body_mid_share)
        _unit("body large share", self.body_large_share)
        if not 0.0 < self.body_mid_share <= self.body_large_share < 1.0:
            message = "body allocation shares must increase strictly below one"
            raise ValueError(message)
        if self.stall_integration_dt > self.maximum_tick_dt:
            message = "stall integration dt must not exceed maximum tick dt"
            raise ValueError(message)
        for name, limits in (
            ("yaw", self.yaw_limits),
            ("elevation", self.elevation_limits),
            ("body", self.body_limits),
        ):
            if not limits.minimum <= 0.0 <= limits.maximum:
                message = f"{name} neutral must lie inside its configured range"
                raise ValueError(message)
        if self.head_measurement_max_age > self.staleness_seconds:
            message = "head measurement age must not exceed observation staleness"
            raise ValueError(message)
        if self.body_feedback_max_age > self.staleness_seconds:
            message = "body feedback age must not exceed observation staleness"
            raise ValueError(message)
        if self.body_feedback_divergence > (
            self.body_limits.maximum - self.body_limits.minimum
        ):
            message = "body feedback divergence must fit inside the body range"
            raise ValueError(message)
        if self.body_large_point > max(
            abs(self.yaw_limits.minimum), self.yaw_limits.maximum
        ):
            message = "body allocation knots must fit inside world-yaw range"
            raise ValueError(message)
        if self.body_head_comfort > max(
            abs(self.yaw_limits.minimum), self.yaw_limits.maximum
        ):
            message = "head comfort must fit inside world-yaw range"
            raise ValueError(message)
        if self.body_large_point * self.body_large_share > max(
            abs(self.body_limits.minimum), self.body_limits.maximum
        ):
            message = "body allocation goal must fit inside body range"
            raise ValueError(message)
        if (
            self.body_large_point * (1.0 - self.body_large_share)
            > self.body_head_comfort
        ):
            message = "large allocation must leave the head inside comfort"
            raise ValueError(message)
        if self.idle_position_epsilon > min(
            min(abs(limits.minimum), limits.maximum)
            for limits in (self.yaw_limits, self.elevation_limits, self.body_limits)
        ):
            message = "idle position epsilon must fit inside every position range"
            raise ValueError(message)
        if self.idle_velocity_epsilon > min(
            self.yaw_limits.max_velocity,
            self.elevation_limits.max_velocity,
            self.body_limits.max_velocity,
        ):
            message = "idle velocity epsilon must fit inside every velocity range"
            raise ValueError(message)
        if self.idle_acceleration_epsilon > min(
            self.yaw_limits.max_acceleration,
            self.elevation_limits.max_acceleration,
            self.body_limits.max_acceleration,
        ):
            message = (
                "idle acceleration epsilon must fit inside every acceleration range"
            )
            raise ValueError(message)
        _positive_integer("workspace recovery samples", self.workspace_recovery_samples)
        _positive_integer(
            "body feedback recovery samples", self.body_feedback_recovery_samples
        )
        _boolean("body enabled", self.body_enabled)
        _boolean("require motion measurements", self.require_motion_measurements)


_DEFAULT_CONFIG: Final = ControllerConfig()


@dataclass(frozen=True, slots=True)
class ControllerState:
    """All immutable state required by the next synchronous step."""

    mode: ControllerMode
    estimator: EstimatorState | None
    deadband: DeadbandState
    world_yaw: AxisState
    elevation: AxisState
    head_initialized: bool
    body_yaw: AxisState
    body_feedback: BodyFeedbackState
    last_observation_identity: tuple[str, int, int] | None
    consumption_watermarks: tuple[tuple[str, int, int], ...]
    target_visible: bool | None
    loss_started_at: float | None
    last_safe_sample: GazeSample
    fault: ControllerFault = ControllerFault.NONE
    recovery_valid_streak: int = 0
    recovery_evidence: tuple[object, ...] | None = None
    last_step_at: float | None = None

    @property
    def safe_hold(self) -> bool:
        """Derive safety hold from the independent fault channel."""
        return self.fault is not ControllerFault.NONE


@dataclass(frozen=True, slots=True)
class ControllerStep:
    """One new immutable state and its transient decision evidence."""

    state: ControllerState
    observation_consumed: bool
    estimator_reset: EstimatorReset
    prediction_horizon: float
    stale: bool

    @property
    def sample(self) -> GazeSample:
        """Return the atomic command retained by the new state."""
        return self.state.last_safe_sample

    @property
    def mode(self) -> ControllerMode:
        """Return the new controller mode."""
        return self.state.mode

    @property
    def deadband_active(self) -> bool:
        """Return whether the new state's deadband is active."""
        return self.state.deadband.active

    @property
    def workspace_accepted(self) -> bool:
        """Return whether this step is outside a workspace fault."""
        return self.state.fault is not ControllerFault.WORKSPACE

    @property
    def safe_hold(self) -> bool:
        """Return the derived safety state without conflating it with mode."""
        return self.state.safe_hold


def initial_controller_state(
    config: ControllerConfig = _DEFAULT_CONFIG,
) -> ControllerState:
    """Build the neutral never-observed controller state."""
    neutral = GazeSample(0.0, 0.0, 0.0, 0.0, config.body_enabled)
    return ControllerState(
        mode=ControllerMode.UNKNOWN,
        estimator=None,
        deadband=DeadbandState(),
        world_yaw=AxisState(),
        elevation=AxisState(),
        head_initialized=False,
        body_yaw=AxisState(),
        body_feedback=BodyFeedbackState(),
        last_observation_identity=None,
        consumption_watermarks=(),
        target_visible=None,
        loss_started_at=None,
        last_safe_sample=neutral,
    )


def reduce_command_result(
    candidate: ControllerState,
    prior_safe: ControllerState,
    result: MotionCommandResult,
    config: ControllerConfig,
) -> ControllerState:
    """Commit one daemon result or atomically restore the prior safe trajectory."""
    evidence = ("command", result.call)
    if result.status is MotionCommandStatus.REJECTED:
        return replace(
            candidate,
            world_yaw=prior_safe.world_yaw,
            elevation=prior_safe.elevation,
            body_yaw=prior_safe.body_yaw,
            last_safe_sample=prior_safe.last_safe_sample,
            fault=ControllerFault.COMMAND,
            recovery_valid_streak=0,
            recovery_evidence=evidence,
        )
    if candidate.fault is not ControllerFault.COMMAND:
        return candidate
    independent = evidence != candidate.recovery_evidence
    streak = candidate.recovery_valid_streak + (1 if independent else 0)
    recovered = streak >= config.workspace_recovery_samples
    return replace(
        candidate,
        fault=ControllerFault.NONE if recovered else ControllerFault.COMMAND,
        recovery_valid_streak=0 if recovered else streak,
        recovery_evidence=None if recovered else evidence,
    )


def _reset_reason(
    estimator: EstimatorState | None,
    observation: GazeObservation,
    config: ControllerConfig,
) -> EstimatorReset:
    """Classify every velocity-discontinuity boundary."""
    if estimator is None:
        return EstimatorReset.FIRST
    if observation.source != estimator.identity[0]:
        return EstimatorReset.SOURCE
    if observation.generation != estimator.identity[1]:
        return EstimatorReset.GENERATION
    if observation.target_key != estimator.target_key:
        return EstimatorReset.TARGET
    capture_dt = observation.captured_at - estimator.captured_at
    if capture_dt <= 0.0:
        return EstimatorReset.TIME_ORDER
    if capture_dt > config.estimator_gap:
        return EstimatorReset.GAP
    return EstimatorReset.NONE


def update_estimator(
    estimator: EstimatorState | None,
    observation: GazeObservation,
    config: ControllerConfig,
) -> tuple[EstimatorState, EstimatorReset]:
    """Consume one new observation using independent alpha-beta axes."""
    if observation.face is None:
        message = "an empty result carries no target estimate"
        raise ValueError(message)
    measured = ImagePoint(
        observation.face.centre.x,
        observation.face.centre.y,
    )
    world_position = (
        ImagePoint(observation.world_yaw, observation.world_elevation)
        if observation.world_yaw is not None and observation.world_elevation is not None
        else None
    )
    reset = _reset_reason(estimator, observation, config)
    if reset is not EstimatorReset.NONE:
        return (
            EstimatorState(
                identity=observation.identity,
                target_key=observation.target_key,
                measured=measured,
                position=measured,
                velocity=ImagePoint(0.0, 0.0),
                captured_at=observation.captured_at,
                received_at=observation.received_at,
                world_position=world_position,
            ),
            reset,
        )
    if estimator is None:
        raise AssertionError("a continuous estimator update requires prior state")
    capture_dt = observation.captured_at - estimator.captured_at
    dt = _clamp(
        capture_dt,
        config.minimum_observation_dt,
        config.maximum_observation_dt,
    )
    prior_x = estimator.position.x + estimator.velocity.x * dt
    prior_y = estimator.position.y + estimator.velocity.y * dt
    residual_x = measured.x - prior_x
    residual_y = measured.y - prior_y
    position = ImagePoint(
        _clamp(
            prior_x + config.estimator_alpha * residual_x,
            -config.image_position_limit,
            config.image_position_limit,
        ),
        _clamp(
            prior_y + config.estimator_alpha * residual_y,
            -config.image_position_limit,
            config.image_position_limit,
        ),
    )
    velocity = ImagePoint(
        _clamp(
            estimator.velocity.x + config.estimator_beta / dt * residual_x,
            -config.image_velocity_limit,
            config.image_velocity_limit,
        ),
        _clamp(
            estimator.velocity.y + config.estimator_beta / dt * residual_y,
            -config.image_velocity_limit,
            config.image_velocity_limit,
        ),
    )
    world_velocity = ImagePoint(0.0, 0.0)
    if world_position is not None and estimator.world_position is not None:
        raw_world_x = (world_position.x - estimator.world_position.x) / capture_dt
        raw_world_y = (world_position.y - estimator.world_position.y) / capture_dt
        world_velocity = ImagePoint(
            (
                0.65 * raw_world_x + 0.35 * estimator.world_velocity.x
                if abs(raw_world_x) <= config.world_velocity_limit
                else 0.0
            ),
            (
                0.65 * raw_world_y + 0.35 * estimator.world_velocity.y
                if abs(raw_world_y) <= config.world_velocity_limit
                else 0.0
            ),
        )
    return (
        EstimatorState(
            identity=observation.identity,
            target_key=observation.target_key,
            measured=measured,
            position=position,
            velocity=velocity,
            captured_at=observation.captured_at,
            received_at=observation.received_at,
            world_position=world_position,
            world_velocity=world_velocity,
        ),
        EstimatorReset.NONE,
    )


def predict_error(
    estimator: EstimatorState,
    *,
    now: float,
    config: ControllerConfig,
) -> tuple[ImagePoint, float]:
    """Predict from capture to actuation with bounded horizon and values."""
    _finite("prediction time", now)
    horizon = _clamp(
        now - estimator.captured_at + config.actuator_delay,
        0.0,
        config.prediction_horizon,
    )
    return (
        ImagePoint(
            _clamp(
                estimator.position.x + estimator.velocity.x * horizon,
                -config.image_position_limit,
                config.image_position_limit,
            ),
            _clamp(
                estimator.position.y + estimator.velocity.y * horizon,
                -config.image_position_limit,
                config.image_position_limit,
            ),
        ),
        horizon,
    )


def apply_deadband(
    predicted: ImagePoint,
    *,
    activation: ImagePoint,
    state: DeadbandState,
    config: ControllerConfig,
) -> tuple[ImagePoint, DeadbandState]:
    """Apply smooth elliptical deadband with raw-error Schmitt activation."""
    radius = math.hypot(
        activation.x / config.deadband_x,
        activation.y / config.deadband_y,
    )
    active = state.active
    if active and radius <= config.deadband_stop:
        active = False
    elif not active and radius >= config.deadband_start:
        active = True
    next_state = DeadbandState(active)
    if not active:
        return ImagePoint(0.0, 0.0), next_state
    phase = _clamp(
        (radius - config.deadband_stop)
        / (config.deadband_start - config.deadband_stop),
        0.0,
        1.0,
    )
    smooth = phase * phase * (3.0 - 2.0 * phase)
    radial = max(0.0, 1.0 - config.deadband_stop / max(radius, _EPSILON))
    scale = smooth * radial
    return ImagePoint(predicted.x * scale, predicted.y * scale), next_state


def _stopping_distance(state: AxisState, limits: AxisLimits) -> float:
    """Conservative distance needed to remove current derivatives."""
    return (
        state.velocity * state.velocity / (2.0 * limits.max_acceleration)
        + abs(state.velocity * state.acceleration) / limits.max_jerk
    )


def step_axis(
    state: AxisState,
    velocity_goal: float,
    dt: float,
    limits: AxisLimits,
    *,
    maximum_dt: float = 0.20,
    stall_dt: float = 0.05,
) -> AxisState:
    """Advance one position/velocity/acceleration state through a jerk bound."""
    _finite("axis velocity goal", velocity_goal)
    _non_negative("axis dt", dt)
    _positive("maximum axis dt", maximum_dt)
    _positive("stall axis dt", stall_dt)
    stalled = dt > maximum_dt
    used_dt = min(dt, stall_dt) if stalled else dt
    initial = state
    requested = 0.0 if stalled else velocity_goal
    goal = _clamp(requested, -limits.max_velocity, limits.max_velocity)
    outward = (initial.position >= 0.0 and goal > 0.0) or (
        initial.position < 0.0 and goal < 0.0
    )
    bound = limits.maximum if goal > 0.0 else limits.minimum
    if outward and abs(bound - initial.position) <= _stopping_distance(initial, limits):
        goal = 0.0
    if used_dt == 0.0:
        return initial
    acceleration_goal = _clamp(
        (goal - initial.velocity) / used_dt,
        -limits.max_acceleration,
        limits.max_acceleration,
    )
    acceleration = initial.acceleration + _clamp(
        acceleration_goal - initial.acceleration,
        -limits.max_jerk * used_dt,
        limits.max_jerk * used_dt,
    )
    acceleration = _clamp(
        acceleration,
        -limits.max_acceleration,
        limits.max_acceleration,
    )
    velocity = _clamp(
        initial.velocity + acceleration * used_dt,
        -limits.max_velocity,
        limits.max_velocity,
    )
    proposed = initial.position + 0.5 * (initial.velocity + velocity) * used_dt
    if not limits.minimum <= proposed <= limits.maximum:
        proposed = initial.position
    return AxisState(proposed, velocity, acceleration)


def _smoothstep(value: float) -> float:
    """Return cubic smoothstep on a clamped unit interval."""
    phase = _clamp(value, 0.0, 1.0)
    return phase * phase * (3.0 - 2.0 * phase)


def allocate_body(world_yaw: float, config: ControllerConfig) -> float:
    """Return continuous odd-symmetric monotonic body-yaw allocation."""
    _finite("world yaw allocation input", world_yaw)
    if not config.body_enabled:
        return 0.0
    magnitude = abs(world_yaw)
    if magnitude <= config.body_noise_floor:
        return 0.0
    if magnitude <= config.body_midpoint:
        phase = (magnitude - config.body_noise_floor) / (
            config.body_midpoint - config.body_noise_floor
        )
        share = config.body_mid_share * _smoothstep(phase)
        allocated = share * magnitude
    elif magnitude <= config.body_large_point:
        phase = (magnitude - config.body_midpoint) / (
            config.body_large_point - config.body_midpoint
        )
        share = config.body_mid_share + (
            config.body_large_share - config.body_mid_share
        ) * _smoothstep(phase)
        allocated = share * magnitude
    else:
        head_at_large = config.body_large_point * (1.0 - config.body_large_share)
        comfort_gap = config.body_head_comfort - head_at_large
        head_residual = config.body_head_comfort - (
            comfort_gap * config.body_large_point / magnitude
        )
        allocated = magnitude - head_residual
    return _clamp(
        math.copysign(allocated, world_yaw),
        config.body_limits.minimum,
        config.body_limits.maximum,
    )


def _velocity_to_position(
    state: AxisState,
    target: float,
    gain: float,
    limits: AxisLimits,
) -> float:
    """Map a position target to a damped velocity request that can stop."""
    error = target - state.position
    if abs(error) <= _EPSILON:
        return 0.0
    stopping_speed = math.sqrt(2.0 * limits.max_acceleration * abs(error))
    bound = min(limits.max_velocity, stopping_speed)
    requested = gain * error - 0.775 * state.velocity - 0.1 * state.acceleration
    return _clamp(requested, -bound, bound)


def _return_velocity(state: AxisState, limits: AxisLimits) -> float:
    """Choose one damped neutral-return velocity that settles without cycling."""
    error = -state.position
    if abs(error) <= _EPSILON:
        return 0.0
    stopping_speed = math.sqrt(2.0 * limits.max_acceleration * abs(error))
    bound = min(limits.max_velocity, stopping_speed)
    requested = 2.0 * error - 0.5 * state.velocity
    return _clamp(requested, -bound, bound)


def _sample(
    world_yaw: AxisState,
    elevation: AxisState,
    body_yaw: AxisState,
    config: ControllerConfig,
) -> GazeSample:
    """Build one coordinated command from three bounded axis states."""
    body = body_yaw.position if config.body_enabled else 0.0
    return GazeSample(
        world_yaw=world_yaw.position,
        elevation=elevation.position,
        body_yaw=body,
        head_yaw=world_yaw.position - body,
        body_enabled=config.body_enabled,
        world_yaw_velocity=world_yaw.velocity,
        world_yaw_acceleration=world_yaw.acceleration,
        elevation_velocity=elevation.velocity,
        elevation_acceleration=elevation.acceleration,
        body_yaw_velocity=body_yaw.velocity if config.body_enabled else 0.0,
        body_yaw_acceleration=(body_yaw.acceleration if config.body_enabled else 0.0),
    )


def _idle(state: ControllerState, config: ControllerConfig) -> bool:
    """Whether every controlled axis has settled near neutral."""
    return all(
        abs(axis.position) <= config.idle_position_epsilon
        and abs(axis.velocity) <= config.idle_velocity_epsilon
        and abs(axis.acceleration) <= config.idle_acceleration_epsilon
        for axis in (state.world_yaw, state.elevation, state.body_yaw)
    )


def _brake_hidden(
    state: ControllerState,
    dt: float,
    config: ControllerConfig,
) -> tuple[AxisState, AxisState, AxisState]:
    """Brake derivatives while retaining positions behind a held command."""
    world = step_axis(
        state.world_yaw,
        0.0,
        dt,
        config.yaw_limits,
        maximum_dt=config.maximum_tick_dt,
        stall_dt=config.stall_integration_dt,
    )
    elevation = step_axis(
        state.elevation,
        0.0,
        dt,
        config.elevation_limits,
        maximum_dt=config.maximum_tick_dt,
        stall_dt=config.stall_integration_dt,
    )
    body = step_axis(
        state.body_yaw,
        0.0,
        dt,
        config.body_limits,
        maximum_dt=config.maximum_tick_dt,
        stall_dt=config.stall_integration_dt,
    )
    return (
        replace(world, position=state.world_yaw.position),
        replace(elevation, position=state.elevation.position),
        replace(body, position=state.body_yaw.position),
    )


def _transition_valid(
    previous: AxisState,
    candidate: AxisState,
    dt: float,
    limits: AxisLimits,
) -> bool:
    """Check q/v/a envelopes, jerk and conservative discrete coherence."""
    if not limits.minimum <= candidate.position <= limits.maximum:
        return False
    if abs(candidate.velocity) > limits.max_velocity + _EPSILON:
        return False
    if abs(candidate.acceleration) > limits.max_acceleration + _EPSILON:
        return False
    if dt <= 0.0:
        return candidate == previous
    if abs(candidate.acceleration - previous.acceleration) > (
        limits.max_jerk * dt + _EPSILON
    ):
        return False
    if abs(candidate.velocity - previous.velocity) > (
        limits.max_acceleration * dt + _EPSILON
    ):
        return False
    return abs(candidate.position - previous.position) <= (
        limits.max_velocity * dt + _EPSILON
    )


def _accept_workspace(_sample: GazeSample) -> bool:
    """Accept every validated coordinated sample when no workspace is supplied."""
    return True


def _advance_watermark(
    watermarks: tuple[tuple[str, int, int], ...],
    observation: GazeObservation,
) -> tuple[tuple[tuple[str, int, int], ...], bool]:
    """Retain only the newest generation and sequence observed per source."""
    source, generation, sequence = observation.identity
    for index, (seen_source, seen_generation, seen_sequence) in enumerate(watermarks):
        if source != seen_source:
            continue
        if generation < seen_generation or (
            generation == seen_generation and sequence <= seen_sequence
        ):
            return watermarks, False
        updated = (*watermarks[:index], observation.identity, *watermarks[index + 1 :])
        return updated, True
    return (*watermarks, observation.identity), True


def _observe_body_feedback(
    state: ControllerState,
    measurement: BodyMeasurement | None,
    *,
    now: float,
    config: ControllerConfig,
    monitor: bool,
) -> tuple[BodyFeedbackState, AxisState | None, bool]:
    """Update independent body feedback and decide whether commands stay held."""
    if not config.body_enabled:
        return BodyFeedbackState(), None, False

    feedback = state.body_feedback
    previous_measurement = feedback.last_measurement
    newer_measurement = measurement is not None and (
        previous_measurement is None
        or measurement.measured_at > previous_measurement.measured_at
    )
    latest = measurement if newer_measurement else previous_measurement
    age_valid = (
        latest is not None
        and 0.0 <= now - latest.measured_at <= config.body_feedback_max_age
    )
    seed = (
        AxisState(position=latest.yaw)
        if not feedback.initialized and age_valid and latest is not None
        else None
    )
    if not monitor:
        return (
            BodyFeedbackState(
                initialized=feedback.initialized or seed is not None,
                last_measurement=latest,
            ),
            seed,
            False,
        )
    commanded_position = seed.position if seed is not None else state.body_yaw.position
    divergent = (
        age_valid
        and latest is not None
        and abs(commanded_position - latest.yaw) > config.body_feedback_divergence
    )
    invalid = not age_valid or divergent

    if feedback.faulted:
        if invalid:
            return (
                BodyFeedbackState(
                    initialized=feedback.initialized or seed is not None,
                    last_measurement=latest,
                    fault_started_at=feedback.fault_started_at,
                    faulted=True,
                ),
                seed,
                True,
            )
        if not newer_measurement:
            return (
                replace(feedback, last_measurement=latest),
                seed,
                True,
            )
        streak = feedback.valid_streak + 1
        if streak < config.body_feedback_recovery_samples:
            return (
                BodyFeedbackState(
                    initialized=True,
                    last_measurement=latest,
                    faulted=True,
                    valid_streak=streak,
                ),
                seed,
                True,
            )
        return BodyFeedbackState(initialized=True, last_measurement=latest), seed, False

    if invalid:
        started = feedback.fault_started_at
        if started is None:
            started = now
        faulted = now - started + _EPSILON >= config.body_feedback_persistence
        return (
            BodyFeedbackState(
                initialized=feedback.initialized or seed is not None,
                last_measurement=latest,
                fault_started_at=started,
                faulted=faulted,
            ),
            seed,
            faulted,
        )
    return BodyFeedbackState(initialized=True, last_measurement=latest), seed, False


def step_controller(
    state: ControllerState,
    observation: GazeObservation | None,
    *,
    now: float,
    dt: float,
    config: ControllerConfig = _DEFAULT_CONFIG,
    workspace_accepts: Callable[[GazeSample], bool] = _accept_workspace,
    head_measurement: HeadMeasurement | None = None,
    body_measurement: BodyMeasurement | None = None,
    input_fault: ControllerFault = ControllerFault.NONE,
    input_evidence: tuple[object, ...] | None = None,
) -> ControllerStep:
    """Advance one pure controller tick with explicit validated evidence."""
    time_valid = math.isfinite(now) and math.isfinite(dt) and dt >= 0.0
    if time_valid and state.last_step_at is not None:
        elapsed = now - state.last_step_at
        time_valid = elapsed >= 0.0 and dt <= elapsed + _EPSILON
    if not time_valid:
        world, elevation, body = _brake_hidden(
            state,
            0.0 if not math.isfinite(dt) or dt < 0.0 else dt,
            config,
        )
        faulted = replace(
            state,
            world_yaw=world,
            elevation=elevation,
            body_yaw=body,
            fault=ControllerFault.TIMING,
            recovery_valid_streak=0,
            recovery_evidence=None,
        )
        return ControllerStep(faulted, False, EstimatorReset.NONE, 0.0, False)
    if input_fault is not ControllerFault.NONE:
        world, elevation, body = _brake_hidden(state, dt, config)
        faulted = replace(
            state,
            world_yaw=world,
            elevation=elevation,
            body_yaw=body,
            fault=input_fault,
            recovery_valid_streak=0,
            recovery_evidence=input_evidence,
            last_step_at=now,
        )
        return ControllerStep(faulted, False, EstimatorReset.NONE, 0.0, False)
    head_is_current = (
        head_measurement is not None
        and 0.0 <= now - head_measurement.measured_at <= config.head_measurement_max_age
    )
    if not state.head_initialized and head_is_current and head_measurement is not None:
        world_yaw = AxisState(position=head_measurement.world_yaw)
        elevation = AxisState(position=head_measurement.world_elevation)
        body_yaw = state.body_yaw.position if config.body_enabled else 0.0
        state = replace(
            state,
            world_yaw=world_yaw,
            elevation=elevation,
            head_initialized=True,
            last_safe_sample=GazeSample(
                world_yaw=world_yaw.position,
                elevation=elevation.position,
                body_yaw=body_yaw,
                head_yaw=world_yaw.position - body_yaw,
                body_enabled=config.body_enabled,
            ),
        )
    motion_expected = observation is not None or state.mode in {
        ControllerMode.ACTIVE,
        ControllerMode.HOLD,
        ControllerMode.RETURNING,
    }
    if config.require_motion_measurements and motion_expected and not head_is_current:
        world, elevation, body = _brake_hidden(state, dt, config)
        faulted = replace(
            state,
            world_yaw=world,
            elevation=elevation,
            body_yaw=body,
            fault=ControllerFault.POSE,
            recovery_valid_streak=0,
            recovery_evidence=None,
            last_step_at=now,
        )
        return ControllerStep(faulted, False, EstimatorReset.NONE, 0.0, False)
    body_feedback, body_seed, body_feedback_hold = _observe_body_feedback(
        state,
        body_measurement,
        now=now,
        config=config,
        monitor=(
            state.mode not in {ControllerMode.UNKNOWN, ControllerMode.IDLE}
            or state.fault is ControllerFault.BODY_FEEDBACK
        ),
    )
    if body_seed is not None:
        state = replace(
            state,
            body_yaw=body_seed,
            body_feedback=body_feedback,
            last_safe_sample=GazeSample(
                world_yaw=state.world_yaw.position,
                elevation=state.elevation.position,
                body_yaw=body_seed.position,
                head_yaw=state.world_yaw.position - body_seed.position,
                body_enabled=True,
            ),
        )
    estimator = state.estimator
    last_identity = state.last_observation_identity
    watermarks = state.consumption_watermarks
    target_visible = state.target_visible
    reset = EstimatorReset.NONE
    consumed = False
    if observation is not None:
        watermarks, consumed = _advance_watermark(watermarks, observation)
    if observation is not None and consumed:
        last_identity = observation.identity
        target_visible = observation.face is not None
        if observation.face is not None:
            estimator, reset = update_estimator(estimator, observation, config)
        else:
            estimator = None

    stale = (
        estimator is not None
        and now - estimator.received_at >= config.staleness_seconds
    )
    deadband = state.deadband
    horizon = 0.0
    yaw_goal = 0.0
    elevation_goal = 0.0
    loss_started = state.loss_started_at
    mode = state.mode
    body_world_target = state.world_yaw.position

    if estimator is not None and not stale and target_visible is True:
        predicted, horizon = predict_error(estimator, now=now, config=config)
        filtered, deadband = apply_deadband(
            predicted,
            activation=estimator.measured,
            state=deadband,
            config=config,
        )
        horizontal_scale = math.tan(config.horizontal_fov / 2.0)
        vertical_scale = math.tan(config.vertical_fov / 2.0)
        if estimator.world_position is not None and deadband.active:
            body_world_target = (
                estimator.world_position.x + estimator.world_velocity.x * horizon
            )
            elevation_target = (
                estimator.world_position.y + estimator.world_velocity.y * horizon
            )
            yaw_goal = (
                _velocity_to_position(
                    state.world_yaw,
                    body_world_target,
                    config.feedback_gain,
                    config.yaw_limits,
                )
                + config.feedforward_gain * estimator.world_velocity.x
            )
            elevation_goal = (
                _velocity_to_position(
                    state.elevation,
                    elevation_target,
                    config.feedback_gain,
                    config.elevation_limits,
                )
                + config.feedforward_gain * estimator.world_velocity.y
            )
        elif deadband.active:
            yaw_goal = -config.feedback_gain * math.atan(filtered.x * horizontal_scale)
            yaw_goal -= (
                config.feedforward_gain
                * (
                    horizontal_scale
                    / (1.0 + (estimator.measured.x * horizontal_scale) ** 2)
                )
                * estimator.velocity.x
            )
            elevation_goal = config.feedback_gain * math.atan(
                filtered.y * vertical_scale
            )
            elevation_goal += (
                config.feedforward_gain
                * (
                    vertical_scale
                    / (1.0 + (estimator.measured.y * vertical_scale) ** 2)
                )
                * estimator.velocity.y
            )
        loss_started = None
        mode = ControllerMode.ACTIVE
    elif estimator is None and (
        target_visible is not False
        or state.mode in {ControllerMode.UNKNOWN, ControllerMode.IDLE}
    ):
        deadband = DeadbandState()
        mode = (
            ControllerMode.UNKNOWN
            if state.mode is ControllerMode.UNKNOWN
            else ControllerMode.IDLE
        )
        loss_started = None
    else:
        deadband = DeadbandState()
        if loss_started is None:
            if target_visible is False and observation is not None:
                loss_started = observation.received_at
            elif estimator is not None:
                loss_started = estimator.received_at + config.staleness_seconds
            else:
                loss_started = now
        if now - loss_started < config.loss_hold_seconds:
            mode = ControllerMode.HOLD
        else:
            mode = ControllerMode.RETURNING
            yaw_goal = _return_velocity(state.world_yaw, config.yaw_limits)
            elevation_goal = _return_velocity(state.elevation, config.elevation_limits)

    body_cold = (
        config.body_enabled
        and not body_feedback.initialized
        and mode
        in {
            ControllerMode.ACTIVE,
            ControllerMode.HOLD,
            ControllerMode.RETURNING,
        }
    )
    if body_feedback_hold or body_cold:
        world, elevation, body = _brake_hidden(state, dt, config)
        if body_cold and not body_feedback.faulted:
            body_feedback = replace(body_feedback, faulted=True)
        latest_body = body_feedback.last_measurement
        held_state = replace(
            state,
            mode=mode,
            estimator=estimator,
            deadband=deadband,
            world_yaw=world,
            elevation=elevation,
            body_yaw=body,
            body_feedback=body_feedback,
            last_observation_identity=last_identity,
            consumption_watermarks=watermarks,
            target_visible=target_visible,
            loss_started_at=loss_started,
            fault=ControllerFault.BODY_FEEDBACK,
            recovery_valid_streak=body_feedback.valid_streak,
            recovery_evidence=(
                None if latest_body is None else latest_body.measured_at,
            ),
            last_step_at=now,
        )
        return ControllerStep(
            state=held_state,
            observation_consumed=consumed,
            estimator_reset=reset,
            prediction_horizon=horizon,
            stale=stale,
        )

    world = step_axis(
        state.world_yaw,
        yaw_goal,
        dt,
        config.yaw_limits,
        maximum_dt=config.maximum_tick_dt,
        stall_dt=config.stall_integration_dt,
    )
    elevation = step_axis(
        state.elevation,
        elevation_goal,
        dt,
        config.elevation_limits,
        maximum_dt=config.maximum_tick_dt,
        stall_dt=config.stall_integration_dt,
    )
    body_target = allocate_body(body_world_target, config)
    body_goal = _velocity_to_position(
        state.body_yaw,
        body_target,
        3.0,
        config.body_limits,
    )
    if mode is ControllerMode.RETURNING:
        body_goal = _return_velocity(state.body_yaw, config.body_limits)
    body = step_axis(
        state.body_yaw,
        body_goal,
        dt,
        config.body_limits,
        maximum_dt=config.maximum_tick_dt,
        stall_dt=config.stall_integration_dt,
    )
    candidate = _sample(world, elevation, body, config)

    sample_fault = validate_gaze_sample(candidate, config)
    validation_fault = ControllerFault(sample_fault.value)
    if validation_fault == ControllerFault.NONE and not all(
        (
            _transition_valid(state.world_yaw, world, dt, config.yaw_limits),
            _transition_valid(state.elevation, elevation, dt, config.elevation_limits),
            _transition_valid(state.body_yaw, body, dt, config.body_limits),
        )
    ):
        validation_fault = ControllerFault.DERIVATIVE
    if validation_fault == ControllerFault.NONE and not workspace_accepts(candidate):
        validation_fault = ControllerFault.WORKSPACE

    recovering_fault: ControllerFault = state.fault
    evidence: tuple[object, ...] | None
    if recovering_fault is ControllerFault.BODY_FEEDBACK:
        recovering_fault = ControllerFault.NONE
    if recovering_fault is ControllerFault.POSE:
        evidence = (
            "pose",
            None if head_measurement is None else head_measurement.measured_at,
        )
    elif recovering_fault is ControllerFault.CALIBRATION:
        evidence = (
            "calibration",
            None if observation is None else observation.identity,
        )
    elif recovering_fault is ControllerFault.COMMAND:
        evidence = input_evidence
    elif recovering_fault is ControllerFault.TIMING:
        evidence = ("timing", now)
    else:
        evidence = (
            "candidate",
            now,
            candidate.world_yaw,
            candidate.elevation,
            candidate.body_yaw,
            candidate.world_yaw_velocity,
            candidate.elevation_velocity,
            candidate.body_yaw_velocity,
        )

    fault: ControllerFault
    if validation_fault != ControllerFault.NONE:
        fault = validation_fault
        valid_streak = 0
        recovery_evidence = None
    elif recovering_fault != ControllerFault.NONE:
        independent = evidence is not None and evidence != state.recovery_evidence
        valid_streak = state.recovery_valid_streak + (1 if independent else 0)
        recovery_evidence = evidence if independent else state.recovery_evidence
        fault = (
            ControllerFault.NONE
            if valid_streak >= config.workspace_recovery_samples
            else recovering_fault
        )
    else:
        fault = ControllerFault.NONE
        valid_streak = 0
        recovery_evidence = None

    if fault != ControllerFault.NONE:
        world, elevation, body = _brake_hidden(state, dt, config)
        held_state = replace(
            state,
            mode=mode,
            estimator=estimator,
            deadband=deadband,
            world_yaw=world,
            elevation=elevation,
            body_yaw=body,
            body_feedback=body_feedback,
            last_observation_identity=last_identity,
            consumption_watermarks=watermarks,
            target_visible=target_visible,
            loss_started_at=loss_started,
            fault=fault,
            recovery_valid_streak=valid_streak,
            recovery_evidence=recovery_evidence,
            last_step_at=now,
        )
        return ControllerStep(
            state=held_state,
            observation_consumed=consumed,
            estimator_reset=reset,
            prediction_horizon=horizon,
            stale=stale,
        )

    candidate_state = replace(
        state,
        mode=mode,
        estimator=estimator,
        deadband=deadband,
        world_yaw=world,
        elevation=elevation,
        body_yaw=body,
        body_feedback=body_feedback,
        last_observation_identity=last_identity,
        consumption_watermarks=watermarks,
        target_visible=target_visible,
        loss_started_at=loss_started,
        last_safe_sample=candidate,
        fault=ControllerFault.NONE,
        recovery_valid_streak=0,
        recovery_evidence=None,
        last_step_at=now,
    )
    if mode is ControllerMode.RETURNING and _idle(candidate_state, config):
        mode = ControllerMode.IDLE
        candidate_state = replace(
            candidate_state,
            mode=mode,
            estimator=None,
            target_visible=None,
            loss_started_at=None,
            deadband=DeadbandState(),
        )
    return ControllerStep(
        state=candidate_state,
        observation_consumed=consumed,
        estimator_reset=reset,
        prediction_horizon=horizon,
        stale=stale,
    )
