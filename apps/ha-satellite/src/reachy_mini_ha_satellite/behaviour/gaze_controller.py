"""Pure predictive gaze estimation and jerk-limited coordinated motion.

One source-qualified observation updates the estimator once. Faster behavior
polls advance only immutable trajectory state. Time, workspace acceptance and
observations are explicit arguments; this module performs no input or output and
has no robot, adapter or clock dependency.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final

__all__ = [
    "AxisLimits",
    "AxisState",
    "ControllerConfig",
    "ControllerMode",
    "ControllerState",
    "ControllerStep",
    "DeadbandState",
    "EstimatorReset",
    "EstimatorState",
    "GazeObservation",
    "GazeSample",
    "ImagePoint",
    "allocate_body",
    "apply_deadband",
    "initial_controller_state",
    "predict_error",
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
    target: ImagePoint | None
    confidence: float | None
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
        if (self.target is None) != (self.confidence is None):
            message = "an observation target and confidence must be supplied together"
            raise ValueError(message)
        if self.target is not None:
            if not -1.0 <= self.target.x <= 1.0 or not -1.0 <= self.target.y <= 1.0:
                message = "an observation target must be inside normalized image bounds"
                raise ValueError(message)
            if self.confidence is None:
                raise AssertionError("paired target confidence was checked above")
            _unit("observation confidence", self.confidence)
        elif self.world_yaw is not None or self.world_elevation is not None:
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
    samples: int = 1
    world_position: ImagePoint | None = None
    world_velocity: ImagePoint = ImagePoint(0.0, 0.0)


class ControllerMode(StrEnum):
    """Externally meaningful pure-controller lifecycle."""

    UNKNOWN = "unknown"
    ACTIVE = "active"
    HOLD = "hold"
    RETURNING = "returning"
    IDLE = "idle"
    WORKSPACE_HOLD = "workspace_hold"


@dataclass(frozen=True, slots=True)
class GazeSample:
    """One atomic world-yaw, elevation and optional body allocation sample."""

    world_yaw: float
    elevation: float
    body_yaw: float
    head_yaw: float
    body_enabled: bool

    def __post_init__(self) -> None:
        """Keep every command scalar finite and coordinated."""
        for name, value in (
            ("world yaw", self.world_yaw),
            ("elevation", self.elevation),
            ("body yaw", self.body_yaw),
            ("head yaw", self.head_yaw),
        ):
            _finite(name, value)
        if not math.isclose(
            self.world_yaw,
            self.body_yaw + self.head_yaw,
            abs_tol=1e-12,
        ):
            message = "world yaw must equal body yaw plus head yaw"
            raise ValueError(message)
        if not self.body_enabled and self.body_yaw != 0.0:
            message = "a body-disabled sample cannot carry body motion"
            raise ValueError(message)


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
            ("body midpoint", self.body_midpoint),
            ("body large point", self.body_large_point),
            ("body head comfort", self.body_head_comfort),
        ):
            _positive(name, value)
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
        if self.minimum_observation_dt > self.maximum_observation_dt:
            message = "minimum observation dt must not exceed its maximum"
            raise ValueError(message)
        if self.actuator_delay > self.prediction_horizon:
            message = "actuator delay must not exceed prediction horizon"
            raise ValueError(message)
        if not 0.0 < self.deadband_stop < self.deadband_start:
            message = "deadband stop must be positive and lower than start"
            raise ValueError(message)
        if not self.body_noise_floor < self.body_midpoint < self.body_large_point:
            message = "body allocation knots must increase"
            raise ValueError(message)
        _unit("body midpoint share", self.body_mid_share)
        _unit("body large share", self.body_large_share)
        if self.body_mid_share > self.body_large_share:
            message = "body allocation share must not decrease"
            raise ValueError(message)
        if self.stall_integration_dt > self.maximum_tick_dt:
            message = "stall integration dt must not exceed maximum tick dt"
            raise ValueError(message)
        if self.workspace_recovery_samples < 1:
            message = "workspace recovery requires at least one sample"
            raise ValueError(message)


_DEFAULT_CONFIG: Final = ControllerConfig()


@dataclass(frozen=True, slots=True)
class ControllerState:
    """All immutable state required by the next synchronous step."""

    mode: ControllerMode
    estimator: EstimatorState | None
    deadband: DeadbandState
    world_yaw: AxisState
    elevation: AxisState
    body_yaw: AxisState
    last_observation_identity: tuple[str, int, int] | None
    target_visible: bool | None
    loss_started_at: float | None
    last_safe_sample: GazeSample
    workspace_valid_streak: int = 0


@dataclass(frozen=True, slots=True)
class ControllerStep:
    """One new immutable state, command sample and decision evidence."""

    state: ControllerState
    sample: GazeSample
    mode: ControllerMode
    observation_consumed: bool
    estimator_reset: EstimatorReset
    prediction_horizon: float
    stale: bool
    deadband_active: bool
    workspace_accepted: bool


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
        body_yaw=AxisState(),
        last_observation_identity=None,
        target_visible=None,
        loss_started_at=None,
        last_safe_sample=neutral,
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
    if observation.target is None:
        message = "an empty result carries no target estimate"
        raise ValueError(message)
    measured = observation.target
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
            samples=estimator.samples + 1,
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
    initial = replace(state, acceleration=0.0) if stalled else state
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
    """Choose one damped neutral-return velocity."""
    return _velocity_to_position(state, 0.0, 2.0, limits)


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


def step_controller(
    state: ControllerState,
    observation: GazeObservation | None,
    *,
    now: float,
    dt: float,
    config: ControllerConfig = _DEFAULT_CONFIG,
    workspace_ok: bool = True,
) -> ControllerStep:
    """Advance one pure controller tick with explicit time and timestep."""
    _finite("controller time", now)
    _non_negative("controller dt", dt)
    estimator = state.estimator
    last_identity = state.last_observation_identity
    target_visible = state.target_visible
    reset = EstimatorReset.NONE
    consumed = False
    if observation is not None and observation.identity != last_identity:
        last_identity = observation.identity
        target_visible = observation.target is not None
        consumed = True
        if observation.target is not None:
            estimator, reset = update_estimator(estimator, observation, config)

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
    elif estimator is None and target_visible is not False:
        deadband = DeadbandState()
        mode = (
            ControllerMode.UNKNOWN
            if state.mode is ControllerMode.UNKNOWN
            else ControllerMode.IDLE
        )
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

    recovering = state.mode is ControllerMode.WORKSPACE_HOLD
    valid_streak = state.workspace_valid_streak
    accepted = workspace_ok
    if not workspace_ok:
        valid_streak = 0
    elif recovering:
        valid_streak += 1
        accepted = valid_streak >= config.workspace_recovery_samples

    if not accepted:
        world, elevation, body = _brake_hidden(state, dt, config)
        held_state = replace(
            state,
            mode=ControllerMode.WORKSPACE_HOLD,
            estimator=estimator,
            deadband=deadband,
            world_yaw=world,
            elevation=elevation,
            body_yaw=body,
            last_observation_identity=last_identity,
            target_visible=target_visible,
            loss_started_at=loss_started,
            workspace_valid_streak=valid_streak,
        )
        return ControllerStep(
            state=held_state,
            sample=state.last_safe_sample,
            mode=ControllerMode.WORKSPACE_HOLD,
            observation_consumed=consumed,
            estimator_reset=reset,
            prediction_horizon=horizon,
            stale=stale,
            deadband_active=deadband.active,
            workspace_accepted=False,
        )

    candidate_state = replace(
        state,
        mode=mode,
        estimator=estimator,
        deadband=deadband,
        world_yaw=world,
        elevation=elevation,
        body_yaw=body,
        last_observation_identity=last_identity,
        target_visible=target_visible,
        loss_started_at=loss_started,
        last_safe_sample=candidate,
        workspace_valid_streak=0,
    )
    if mode is ControllerMode.RETURNING and _idle(candidate_state, config):
        mode = ControllerMode.IDLE
        candidate_state = replace(
            candidate_state,
            mode=mode,
            estimator=None,
            loss_started_at=None,
            deadband=DeadbandState(),
        )
    return ControllerStep(
        state=candidate_state,
        sample=candidate,
        mode=mode,
        observation_consumed=consumed,
        estimator_reset=reset,
        prediction_horizon=horizon,
        stale=stale,
        deadband_active=deadband.active,
        workspace_accepted=True,
    )
