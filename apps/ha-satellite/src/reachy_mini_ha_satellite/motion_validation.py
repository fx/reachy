"""Shared pure validation for complete coordinated motion samples."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from reachy_mini_ha_satellite.ports import GazeSample

__all__ = ["SampleFault", "validate_gaze_sample"]

_EPSILON = 1e-12


class _AxisEnvelope(Protocol):
    """The scalar envelope facts shared config exposes to the validator."""

    @property
    def minimum(self) -> float: ...

    @property
    def maximum(self) -> float: ...

    @property
    def max_velocity(self) -> float: ...

    @property
    def max_acceleration(self) -> float: ...


class _GazeEnvelope(Protocol):
    """The subset of controller configuration used by atomic validation."""

    @property
    def body_enabled(self) -> bool: ...

    @property
    def yaw_limits(self) -> _AxisEnvelope: ...

    @property
    def elevation_limits(self) -> _AxisEnvelope: ...

    @property
    def body_limits(self) -> _AxisEnvelope: ...


class SampleFault(StrEnum):
    """Pure sample validation outcome shared by controller and adapter."""

    NONE = "none"
    DERIVATIVE = "derivative"
    WORKSPACE = "workspace"


def validate_gaze_sample(
    sample: GazeSample,
    config: _GazeEnvelope,
) -> SampleFault:
    """Validate body-mode agreement and configured position/derivative bounds."""
    if sample.body_enabled is not config.body_enabled:
        return SampleFault.WORKSPACE
    axes = (
        (
            sample.world_yaw,
            sample.world_yaw_velocity,
            sample.world_yaw_acceleration,
            config.yaw_limits,
        ),
        (
            sample.elevation,
            sample.elevation_velocity,
            sample.elevation_acceleration,
            config.elevation_limits,
        ),
        (
            sample.body_yaw,
            sample.body_yaw_velocity,
            sample.body_yaw_acceleration,
            config.body_limits,
        ),
    )
    if (
        any(
            not limits.minimum <= position <= limits.maximum
            for position, _velocity, _acceleration, limits in axes
        )
        or not config.yaw_limits.minimum <= sample.head_yaw <= config.yaw_limits.maximum
    ):
        return SampleFault.WORKSPACE
    if any(
        abs(velocity) > limits.max_velocity + _EPSILON
        or abs(acceleration) > limits.max_acceleration + _EPSILON
        for _position, velocity, acceleration, limits in axes
    ):
        return SampleFault.DERIVATIVE
    return SampleFault.NONE
