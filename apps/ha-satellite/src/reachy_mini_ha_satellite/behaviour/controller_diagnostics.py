"""Pure bounded scalar evidence for the predictive gaze controller."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, fields
from enum import StrEnum
from typing import Final

from reachy_mini_ha_satellite.behaviour.gaze_controller import (
    AxisLimits,
    AxisState,
    ControllerConfig,
    ControllerFault,
    ControllerMode,
    ControllerStep,
)

__all__ = ["ControllerDiagnostics", "ControllerEvent", "DiagnosticScalar"]

_DEFAULT_CAPACITY: Final = 128

type DiagnosticScalar = float | bool | str | None


def _bounded_capacity(value: object) -> int:
    """Return one positive integer capacity, rejecting bool explicitly."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        message = "diagnostics capacity must be a positive integer"
        raise ValueError(message)
    return value


@dataclass(frozen=True, slots=True)
class _AxisEvidence:
    """Derived scalar evidence for one configured trajectory axis."""

    jerk: float
    position_limited: bool
    velocity_limited: bool
    acceleration_limited: bool
    jerk_limited: bool


def _axis_evidence(
    state: AxisState,
    limits: AxisLimits,
    *,
    previous_acceleration: float | None,
    dt: float,
) -> _AxisEvidence:
    """Derive active-limit flags and observed jerk from adjacent events."""
    jerk = (
        0.0
        if previous_acceleration is None or dt <= 0.0
        else (state.acceleration - previous_acceleration) / dt
    )
    return _AxisEvidence(
        jerk=jerk,
        position_limited=(
            math.isclose(state.position, limits.minimum, abs_tol=1e-12)
            or math.isclose(state.position, limits.maximum, abs_tol=1e-12)
        ),
        velocity_limited=math.isclose(
            abs(state.velocity), limits.max_velocity, abs_tol=1e-12
        ),
        acceleration_limited=math.isclose(
            abs(state.acceleration), limits.max_acceleration, abs_tol=1e-12
        ),
        jerk_limited=math.isclose(abs(jerk), limits.max_jerk, abs_tol=1e-9),
    )


@dataclass(frozen=True, slots=True)
class ControllerEvent:
    """One fixed-schema event containing scalar, enum, boolean or null values."""

    at: float
    mode: ControllerMode
    fault: ControllerFault
    safe_hold: bool
    observation_age: float | None
    observation_consumed: bool
    estimator_reset: StrEnum
    prediction_horizon: float
    deadband_active: bool
    world_yaw: float
    elevation: float
    body_yaw: float
    head_yaw: float = 0.0
    allocation_body_share: float = 0.0
    world_yaw_velocity: float = 0.0
    world_yaw_acceleration: float = 0.0
    world_yaw_jerk: float = 0.0
    elevation_velocity: float = 0.0
    elevation_acceleration: float = 0.0
    elevation_jerk: float = 0.0
    body_yaw_velocity: float = 0.0
    body_yaw_acceleration: float = 0.0
    body_yaw_jerk: float = 0.0
    world_yaw_minimum: float = 0.0
    world_yaw_maximum: float = 0.0
    world_yaw_max_velocity: float = 0.0
    world_yaw_max_acceleration: float = 0.0
    world_yaw_max_jerk: float = 0.0
    elevation_minimum: float = 0.0
    elevation_maximum: float = 0.0
    elevation_max_velocity: float = 0.0
    elevation_max_acceleration: float = 0.0
    elevation_max_jerk: float = 0.0
    body_yaw_minimum: float = 0.0
    body_yaw_maximum: float = 0.0
    body_yaw_max_velocity: float = 0.0
    body_yaw_max_acceleration: float = 0.0
    body_yaw_max_jerk: float = 0.0
    world_yaw_position_limited: bool = False
    world_yaw_velocity_limited: bool = False
    world_yaw_acceleration_limited: bool = False
    world_yaw_jerk_limited: bool = False
    elevation_position_limited: bool = False
    elevation_velocity_limited: bool = False
    elevation_acceleration_limited: bool = False
    elevation_jerk_limited: bool = False
    body_yaw_position_limited: bool = False
    body_yaw_velocity_limited: bool = False
    body_yaw_acceleration_limited: bool = False
    body_yaw_jerk_limited: bool = False
    emitted: bool = False
    command_accepted: bool | None = None

    def __post_init__(self) -> None:
        """Reject non-finite diagnostics rather than serializing invalid JSON."""
        values = tuple(
            value
            for field in fields(self)
            if isinstance((value := getattr(self, field.name)), float)
        )
        if not all(math.isfinite(value) for value in values):
            message = "controller diagnostics must contain only finite scalars"
            raise ValueError(message)

    def snapshot(self) -> dict[str, DiagnosticScalar]:
        """Return the fixed JSON-safe public shape without installation identity."""
        return {
            "at": self.at,
            "mode": self.mode.value,
            "fault": self.fault.value,
            "safe_hold": self.safe_hold,
            "observation_age": self.observation_age,
            "observation_consumed": self.observation_consumed,
            "estimator_reset": self.estimator_reset.value,
            "prediction_horizon": self.prediction_horizon,
            "deadband_active": self.deadband_active,
            "world_yaw": self.world_yaw,
            "elevation": self.elevation,
            "body_yaw": self.body_yaw,
            "head_yaw": self.head_yaw,
            "allocation_body_share": self.allocation_body_share,
            "world_yaw_velocity": self.world_yaw_velocity,
            "world_yaw_acceleration": self.world_yaw_acceleration,
            "world_yaw_jerk": self.world_yaw_jerk,
            "elevation_velocity": self.elevation_velocity,
            "elevation_acceleration": self.elevation_acceleration,
            "elevation_jerk": self.elevation_jerk,
            "body_yaw_velocity": self.body_yaw_velocity,
            "body_yaw_acceleration": self.body_yaw_acceleration,
            "body_yaw_jerk": self.body_yaw_jerk,
            "world_yaw_minimum": self.world_yaw_minimum,
            "world_yaw_maximum": self.world_yaw_maximum,
            "world_yaw_max_velocity": self.world_yaw_max_velocity,
            "world_yaw_max_acceleration": self.world_yaw_max_acceleration,
            "world_yaw_max_jerk": self.world_yaw_max_jerk,
            "elevation_minimum": self.elevation_minimum,
            "elevation_maximum": self.elevation_maximum,
            "elevation_max_velocity": self.elevation_max_velocity,
            "elevation_max_acceleration": self.elevation_max_acceleration,
            "elevation_max_jerk": self.elevation_max_jerk,
            "body_yaw_minimum": self.body_yaw_minimum,
            "body_yaw_maximum": self.body_yaw_maximum,
            "body_yaw_max_velocity": self.body_yaw_max_velocity,
            "body_yaw_max_acceleration": self.body_yaw_max_acceleration,
            "body_yaw_max_jerk": self.body_yaw_max_jerk,
            "world_yaw_position_limited": self.world_yaw_position_limited,
            "world_yaw_velocity_limited": self.world_yaw_velocity_limited,
            "world_yaw_acceleration_limited": self.world_yaw_acceleration_limited,
            "world_yaw_jerk_limited": self.world_yaw_jerk_limited,
            "elevation_position_limited": self.elevation_position_limited,
            "elevation_velocity_limited": self.elevation_velocity_limited,
            "elevation_acceleration_limited": self.elevation_acceleration_limited,
            "elevation_jerk_limited": self.elevation_jerk_limited,
            "body_yaw_position_limited": self.body_yaw_position_limited,
            "body_yaw_velocity_limited": self.body_yaw_velocity_limited,
            "body_yaw_acceleration_limited": self.body_yaw_acceleration_limited,
            "body_yaw_jerk_limited": self.body_yaw_jerk_limited,
            "emitted": self.emitted,
            "command_accepted": self.command_accepted,
        }


class ControllerDiagnostics:
    """Fixed-capacity deterministic ring with oldest-first snapshots."""

    def __init__(self, capacity: int = _DEFAULT_CAPACITY) -> None:
        """Create an empty ring with deterministic left-edge eviction."""
        self._events: deque[ControllerEvent] = deque(maxlen=_bounded_capacity(capacity))

    @property
    def capacity(self) -> int:
        """Return the immutable maximum number of retained events."""
        maximum = self._events.maxlen
        if maximum is None:
            raise AssertionError("controller diagnostics are always bounded")
        return maximum

    def append(self, event: ControllerEvent) -> None:
        """Append one event, evicting exactly the oldest event when full."""
        self._events.append(event)

    def record(
        self,
        step: ControllerStep,
        *,
        config: ControllerConfig,
        at: float,
        observation_age: float | None,
        emitted: bool,
        command_accepted: bool | None = None,
    ) -> None:
        """Record commands, allocation and configured/active limit evidence."""
        sample = step.sample
        previous = self._events[-1] if self._events else None
        dt = 0.0 if previous is None else at - previous.at
        world = _axis_evidence(
            step.state.world_yaw,
            config.yaw_limits,
            previous_acceleration=(
                None if previous is None else previous.world_yaw_acceleration
            ),
            dt=dt,
        )
        elevation = _axis_evidence(
            step.state.elevation,
            config.elevation_limits,
            previous_acceleration=(
                None if previous is None else previous.elevation_acceleration
            ),
            dt=dt,
        )
        body = _axis_evidence(
            step.state.body_yaw,
            config.body_limits,
            previous_acceleration=(
                None if previous is None else previous.body_yaw_acceleration
            ),
            dt=dt,
        )
        allocation_share = (
            0.0
            if abs(sample.world_yaw) <= 1e-12
            else sample.body_yaw / sample.world_yaw
        )
        self.append(
            ControllerEvent(
                at=at,
                mode=step.mode,
                fault=step.state.fault,
                safe_hold=step.safe_hold,
                observation_age=observation_age,
                observation_consumed=step.observation_consumed,
                estimator_reset=step.estimator_reset,
                prediction_horizon=step.prediction_horizon,
                deadband_active=step.deadband_active,
                world_yaw=sample.world_yaw,
                elevation=sample.elevation,
                body_yaw=sample.body_yaw,
                head_yaw=sample.head_yaw,
                allocation_body_share=allocation_share,
                world_yaw_velocity=step.state.world_yaw.velocity,
                world_yaw_acceleration=step.state.world_yaw.acceleration,
                world_yaw_jerk=world.jerk,
                elevation_velocity=step.state.elevation.velocity,
                elevation_acceleration=step.state.elevation.acceleration,
                elevation_jerk=elevation.jerk,
                body_yaw_velocity=step.state.body_yaw.velocity,
                body_yaw_acceleration=step.state.body_yaw.acceleration,
                body_yaw_jerk=body.jerk,
                world_yaw_minimum=config.yaw_limits.minimum,
                world_yaw_maximum=config.yaw_limits.maximum,
                world_yaw_max_velocity=config.yaw_limits.max_velocity,
                world_yaw_max_acceleration=config.yaw_limits.max_acceleration,
                world_yaw_max_jerk=config.yaw_limits.max_jerk,
                elevation_minimum=config.elevation_limits.minimum,
                elevation_maximum=config.elevation_limits.maximum,
                elevation_max_velocity=config.elevation_limits.max_velocity,
                elevation_max_acceleration=config.elevation_limits.max_acceleration,
                elevation_max_jerk=config.elevation_limits.max_jerk,
                body_yaw_minimum=config.body_limits.minimum,
                body_yaw_maximum=config.body_limits.maximum,
                body_yaw_max_velocity=config.body_limits.max_velocity,
                body_yaw_max_acceleration=config.body_limits.max_acceleration,
                body_yaw_max_jerk=config.body_limits.max_jerk,
                world_yaw_position_limited=world.position_limited,
                world_yaw_velocity_limited=world.velocity_limited,
                world_yaw_acceleration_limited=world.acceleration_limited,
                world_yaw_jerk_limited=world.jerk_limited,
                elevation_position_limited=elevation.position_limited,
                elevation_velocity_limited=elevation.velocity_limited,
                elevation_acceleration_limited=elevation.acceleration_limited,
                elevation_jerk_limited=elevation.jerk_limited,
                body_yaw_position_limited=body.position_limited,
                body_yaw_velocity_limited=body.velocity_limited,
                body_yaw_acceleration_limited=body.acceleration_limited,
                body_yaw_jerk_limited=body.jerk_limited,
                emitted=emitted,
                command_accepted=command_accepted,
            )
        )

    def snapshot(self) -> tuple[dict[str, DiagnosticScalar], ...]:
        """Return an immutable oldest-first copy of every retained event."""
        return tuple(event.snapshot() for event in self._events)

    def reset(self) -> None:
        """Clear diagnostics and nothing else."""
        self._events.clear()
