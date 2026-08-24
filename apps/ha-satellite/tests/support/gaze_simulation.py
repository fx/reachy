"""Independent deterministic nonlinear plant for predictive-gaze tests.

The controller does not import this module. Camera projection, distortion,
observation cadence and latency, dropout, command delay, actuator lag, cadence
stalls, workspace rejection and observation faults are injected values or pure
callables. Simulation time advances arithmetically; nothing sleeps or performs
input or output.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from reachy_contracts import FaceDetection, NormalisedPoint
from reachy_mini_ha_satellite.behaviour.gaze_controller import (
    ControllerConfig,
    ControllerMode,
    ControllerState,
    GazeObservation,
    GazeSample,
    ImagePoint,
    initial_controller_state,
    step_controller,
)

__all__ = [
    "DEFAULT_NOISE",
    "GazePlant",
    "PlantConfig",
    "PlantFaults",
    "PlantSample",
    "constant_target",
    "moving_target",
]

TargetPath = Callable[[float], tuple[float, float]]
TimedInterval = Callable[[int, float], float]
Projection = Callable[[float, float], float]
Distortion = Callable[[float], float]
DropObservation = Callable[[int, float], bool]
RejectWorkspace = Callable[[GazeSample, int, float], bool]
CorruptObservation = Callable[[GazeObservation, int], GazeObservation]

DEFAULT_NOISE: Final[tuple[tuple[float, float], ...]] = (
    (-0.018, 0.000),
    (0.012, -0.021),
    (-0.006, 0.025),
    (0.018, -0.013),
    (-0.014, 0.018),
    (0.004, -0.025),
    (0.015, 0.009),
    (-0.010, -0.017),
)


def _observation_interval(_index: int, _at: float) -> float:
    """Default to ten camera results per second."""
    return 0.10


def _observation_latency(_index: int, _at: float) -> float:
    """Default to measured network plus inference latency."""
    return 0.14


def _controller_interval(_index: int, _at: float) -> float:
    """Default to a twenty-hertz behavior loop."""
    return 0.05


def _projection(angle: float, field_of_view: float) -> float:
    """Independent pinhole projection from angle to normalized image error."""
    return math.tan(angle) / math.tan(field_of_view / 2.0)


def _distortion(value: float) -> float:
    """Apply a small odd nonlinear radial distortion."""
    return value * (1.0 + 0.03 * value * value)


def _keep_observation(_index: int, _at: float) -> bool:
    """Default to no frame loss."""
    return False


def _accept_workspace(_sample: GazeSample, _index: int, _at: float) -> bool:
    """Default to no injected workspace rejection."""
    return False


def _keep_observation_value(
    observation: GazeObservation,
    _index: int,
) -> GazeObservation:
    """Default to no injected observation corruption."""
    return observation


@dataclass(frozen=True, slots=True)
class PlantConfig:
    """Explicit deterministic camera, transport, loop and actuator values."""

    plant_dt: float = 0.005
    observation_interval: TimedInterval = _observation_interval
    observation_latency: TimedInterval = _observation_latency
    controller_interval: TimedInterval = _controller_interval
    command_delay: float = 0.08
    head_lag: float = 0.12
    body_lag: float = 0.30
    projection: Projection = _projection
    distortion: Distortion = _distortion
    noise: tuple[tuple[float, float], ...] = ()

    def __post_init__(self) -> None:
        """Reject time values that cannot make simulation progress."""
        for name, value in (
            ("plant dt", self.plant_dt),
            ("head lag", self.head_lag),
            ("body lag", self.body_lag),
        ):
            if not math.isfinite(value) or value <= 0.0:
                message = f"the {name} must be positive and finite"
                raise ValueError(message)
        if not math.isfinite(self.command_delay) or self.command_delay < 0.0:
            message = "the command delay must be non-negative and finite"
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class PlantFaults:
    """Pure fault injections independent from the controller implementation."""

    drop_observation: DropObservation = _keep_observation
    reject_workspace: RejectWorkspace = _accept_workspace
    corrupt_observation: CorruptObservation = _keep_observation_value


@dataclass(frozen=True, slots=True)
class _PendingObservation:
    """One captured result waiting out its injected latency."""

    available_at: float
    observation: GazeObservation


@dataclass(frozen=True, slots=True)
class _PendingCommand:
    """One atomic sample waiting out command transport delay."""

    available_at: float
    sample: GazeSample


@dataclass(frozen=True, slots=True)
class PlantSample:
    """One explicit-time plant state and latest controller evidence."""

    at: float
    target_yaw: float
    target_elevation: float
    plant_head_yaw: float
    plant_body_yaw: float
    plant_elevation: float
    image_error: ImagePoint
    command: GazeSample
    state: ControllerState
    controller_tick: bool
    observation_consumed: bool
    observation_identity: tuple[str, int, int] | None
    prediction_horizon: float

    @property
    def mode(self) -> ControllerMode:
        """Return the controller mode carried by the immutable state."""
        return self.state.mode

    @property
    def deadband_active(self) -> bool:
        """Return whether the state carries an active deadband."""
        return self.state.deadband.active

    @property
    def plant_world_yaw(self) -> float:
        """Reconstruct world gaze from independent head and body actuators."""
        return self.plant_head_yaw + self.plant_body_yaw


def constant_target(
    *,
    yaw: float = 0.0,
    elevation: float = 0.0,
) -> TargetPath:
    """Return a target path fixed at one world angle."""
    return lambda _at: (yaw, elevation)


def moving_target(
    *,
    axis: str,
    starts_at: float,
    stops_at: float,
    speed: float,
) -> TargetPath:
    """Return one bounded constant-velocity horizontal or vertical target."""
    if axis not in {"horizontal", "vertical"}:
        message = f"the moving target axis must be horizontal or vertical, not {axis}"
        raise ValueError(message)
    if stops_at < starts_at:
        message = "a moving target cannot stop before it starts"
        raise ValueError(message)

    def _target(at: float) -> tuple[float, float]:
        elapsed = min(max(0.0, at - starts_at), stops_at - starts_at)
        position = elapsed * speed
        return (position, 0.0) if axis == "horizontal" else (0.0, position)

    return _target


_DEFAULT_PLANT: Final = PlantConfig()
_DEFAULT_FAULTS: Final = PlantFaults()
_DEFAULT_CONTROLLER: Final = ControllerConfig()


class GazePlant:
    """Run the pure controller against an independent delayed nonlinear plant."""

    def __init__(
        self,
        config: PlantConfig = _DEFAULT_PLANT,
        *,
        faults: PlantFaults = _DEFAULT_FAULTS,
        controller: ControllerConfig = _DEFAULT_CONTROLLER,
    ) -> None:
        """Store immutable simulation inputs without advancing time."""
        self._config = config
        self._faults = faults
        self._controller = controller

    def run(self, duration: float, target: TargetPath) -> list[PlantSample]:
        """Advance capture, controller, command and actuator clocks explicitly."""
        if not math.isfinite(duration) or duration < 0.0:
            message = "the simulation duration must be non-negative and finite"
            raise ValueError(message)
        state = initial_controller_state(self._controller)
        command = state.last_safe_sample
        commands: deque[_PendingCommand] = deque()
        observations: list[_PendingObservation] = []
        latest: GazeObservation | None = None
        plant_head_yaw = 0.0
        plant_body_yaw = 0.0
        plant_elevation = 0.0
        observation_index = 0
        controller_index = 0
        next_observation = 0.0
        next_controller = 0.0
        last_controller = 0.0
        last_consumed = False
        last_identity: tuple[str, int, int] | None = None
        last_horizon = 0.0
        trace: list[PlantSample] = []
        at = 0.0

        while at <= duration + 1e-12:
            target_yaw, target_elevation = target(at)
            plant_world_yaw = plant_head_yaw + plant_body_yaw

            if at + 1e-12 >= next_observation:
                interval = self._config.observation_interval(observation_index, at)
                latency = self._config.observation_latency(observation_index, at)
                self._require_interval("observation interval", interval, positive=True)
                self._require_interval("observation latency", latency, positive=False)
                if not self._faults.drop_observation(observation_index, at):
                    image = self._image_error(
                        plant_world_yaw,
                        plant_elevation,
                        target_yaw,
                        target_elevation,
                        observation_index,
                    )
                    observation = GazeObservation(
                        source="simulated-camera",
                        generation=0,
                        sequence=observation_index,
                        captured_at=at,
                        received_at=at + latency,
                        target_key=0,
                        face=FaceDetection(
                            centre=NormalisedPoint(x=image.x, y=image.y),
                            confidence=0.9,
                        ),
                        world_yaw=target_yaw,
                        world_elevation=target_elevation,
                    )
                    observation = self._faults.corrupt_observation(
                        observation,
                        observation_index,
                    )
                    observations.append(
                        _PendingObservation(observation.received_at, observation)
                    )
                observation_index += 1
                next_observation += interval

            pending: list[_PendingObservation] = []
            for item in observations:
                if item.available_at <= at + 1e-12:
                    latest = item.observation
                else:
                    pending.append(item)
            observations = pending

            controller_tick = at + 1e-12 >= next_controller
            if controller_tick:
                dt = (
                    self._config.controller_interval(0, 0.0)
                    if controller_index == 0
                    else at - last_controller
                )
                self._require_interval("controller dt", dt, positive=True)
                rejected = self._faults.reject_workspace(
                    state.last_safe_sample,
                    controller_index,
                    at,
                )
                result = step_controller(
                    state,
                    latest,
                    now=at,
                    dt=dt,
                    config=self._controller,
                    workspace_ok=not rejected,
                )
                state = result.state
                last_consumed = result.observation_consumed
                last_identity = (
                    latest.identity
                    if result.observation_consumed and latest is not None
                    else None
                )
                last_horizon = result.prediction_horizon
                commands.append(
                    _PendingCommand(at + self._config.command_delay, result.sample)
                )
                interval = self._config.controller_interval(controller_index, at)
                self._require_interval("controller interval", interval, positive=True)
                controller_index += 1
                last_controller = at
                next_controller += interval
            else:
                last_consumed = False
                last_identity = None

            while commands and commands[0].available_at <= at + 1e-12:
                command = commands.popleft().sample
            head_fraction = min(1.0, self._config.plant_dt / self._config.head_lag)
            body_fraction = min(1.0, self._config.plant_dt / self._config.body_lag)
            plant_head_yaw += (command.head_yaw - plant_head_yaw) * head_fraction
            plant_body_yaw += (command.body_yaw - plant_body_yaw) * body_fraction
            plant_elevation += (command.elevation - plant_elevation) * head_fraction
            final_image = self._image_error(
                plant_head_yaw + plant_body_yaw,
                plant_elevation,
                target_yaw,
                target_elevation,
                observation_index,
                include_noise=False,
            )
            trace.append(
                PlantSample(
                    at=at,
                    target_yaw=target_yaw,
                    target_elevation=target_elevation,
                    plant_head_yaw=plant_head_yaw,
                    plant_body_yaw=plant_body_yaw,
                    plant_elevation=plant_elevation,
                    image_error=final_image,
                    command=command,
                    state=state,
                    controller_tick=controller_tick,
                    observation_consumed=last_consumed,
                    observation_identity=last_identity,
                    prediction_horizon=last_horizon,
                )
            )
            at = round(at + self._config.plant_dt, 12)
        return trace

    def _image_error(
        self,
        plant_world_yaw: float,
        plant_elevation: float,
        target_yaw: float,
        target_elevation: float,
        observation_index: int,
        *,
        include_noise: bool = True,
    ) -> ImagePoint:
        """Project plant/target geometry through injected independent camera math."""
        x = self._config.distortion(
            self._config.projection(
                plant_world_yaw - target_yaw,
                self._controller.horizontal_fov,
            )
        )
        y = self._config.distortion(
            self._config.projection(
                target_elevation - plant_elevation,
                self._controller.vertical_fov,
            )
        )
        if include_noise and self._config.noise:
            noise_x, noise_y = self._config.noise[
                observation_index % len(self._config.noise)
            ]
            x += noise_x
            y += noise_y
        return ImagePoint(_clamp_unit(x), _clamp_unit(y))

    @staticmethod
    def _require_interval(name: str, value: float, *, positive: bool) -> None:
        """Require an injected interval to preserve deterministic progress."""
        valid = value > 0.0 if positive else value >= 0.0
        if not math.isfinite(value) or not valid:
            qualifier = "positive" if positive else "non-negative"
            message = f"the {name} must be {qualifier} and finite"
            raise ValueError(message)


def _clamp_unit(value: float) -> float:
    """Keep projected image output in the detector contract interval."""
    return min(1.0, max(-1.0, value))
