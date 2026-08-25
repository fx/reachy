"""Cross-envelope validation for one shared predictive controller configuration."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import replace
from typing import Any, cast

import pytest

from reachy_mini_ha_satellite.behaviour.gaze_controller import (
    AxisLimits,
    ControllerConfig,
)


def _invalid_neutral(config: ControllerConfig) -> ControllerConfig:
    return replace(
        config,
        yaw_limits=AxisLimits(0.1, 1.0, 1.0, 1.0, 1.0),
    )


def _invalid_observation_order(config: ControllerConfig) -> ControllerConfig:
    return replace(config, maximum_observation_dt=config.estimator_gap + 0.1)


def _invalid_prediction_order(config: ControllerConfig) -> ControllerConfig:
    return replace(config, prediction_horizon=config.staleness_seconds + 0.1)


def _invalid_deadband(config: ControllerConfig) -> ControllerConfig:
    return replace(
        config,
        deadband_x=config.image_position_limit,
        deadband_start=2.0,
    )


def _invalid_tick(config: ControllerConfig) -> ControllerConfig:
    return replace(config, stall_integration_dt=config.maximum_tick_dt + 0.1)


def _invalid_idle(config: ControllerConfig) -> ControllerConfig:
    return replace(
        config,
        idle_acceleration_epsilon=config.body_limits.max_acceleration + 0.1,
    )


def _invalid_allocation_knots(config: ControllerConfig) -> ControllerConfig:
    return replace(config, body_midpoint=config.body_large_point)


def _invalid_allocation_shares(config: ControllerConfig) -> ControllerConfig:
    return replace(config, body_mid_share=config.body_large_share + 0.1)


def _invalid_comfort(config: ControllerConfig) -> ControllerConfig:
    return replace(config, body_head_comfort=math.pi)


def _invalid_divergence(config: ControllerConfig) -> ControllerConfig:
    return replace(
        config,
        body_feedback_divergence=(
            config.body_limits.maximum - config.body_limits.minimum + 0.1
        ),
    )


def _invalid_loss_hold(config: ControllerConfig) -> ControllerConfig:
    return replace(config, staleness_seconds=1.0, loss_hold_seconds=1.1)


def _invalid_head_age(config: ControllerConfig) -> ControllerConfig:
    return replace(
        config,
        staleness_seconds=0.2,
        prediction_horizon=0.2,
        loss_hold_seconds=0.1,
        head_measurement_max_age=0.3,
        body_feedback_max_age=0.2,
    )


def _invalid_body_age(config: ControllerConfig) -> ControllerConfig:
    return replace(
        config,
        staleness_seconds=0.2,
        prediction_horizon=0.2,
        loss_hold_seconds=0.1,
        head_measurement_max_age=0.2,
        body_feedback_max_age=0.3,
    )


def _invalid_large_knot_range(config: ControllerConfig) -> ControllerConfig:
    return replace(config, body_large_point=60.0 * math.pi / 180.0)


def _invalid_body_goal(config: ControllerConfig) -> ControllerConfig:
    return replace(
        config,
        body_limits=AxisLimits(-0.2, 0.2, 1.0, 1.0, 1.0),
    )


def _invalid_large_head_residual(config: ControllerConfig) -> ControllerConfig:
    return replace(config, body_head_comfort=5.0 * math.pi / 180.0)


def _invalid_idle_position(config: ControllerConfig) -> ControllerConfig:
    return replace(config, idle_position_epsilon=40.0 * math.pi / 180.0)


def _invalid_idle_velocity(config: ControllerConfig) -> ControllerConfig:
    return replace(config, idle_velocity_epsilon=30.0 * math.pi / 180.0)


@pytest.mark.parametrize(
    "mutate",
    [
        _invalid_neutral,
        _invalid_observation_order,
        _invalid_prediction_order,
        _invalid_deadband,
        _invalid_tick,
        _invalid_idle,
        _invalid_allocation_knots,
        _invalid_allocation_shares,
        _invalid_comfort,
        _invalid_divergence,
        _invalid_loss_hold,
        _invalid_head_age,
        _invalid_body_age,
        _invalid_large_knot_range,
        _invalid_body_goal,
        _invalid_large_head_residual,
        _invalid_idle_position,
        _invalid_idle_velocity,
    ],
)
def test_cross_envelope_configuration_is_rejected(
    mutate: Callable[[ControllerConfig], ControllerConfig],
) -> None:
    """Every coupled bound is validated before controller state exists."""
    with pytest.raises(ValueError, match=r".+"):
        mutate(ControllerConfig())


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("workspace_recovery_samples", True),
        ("workspace_recovery_samples", 0),
        ("workspace_recovery_samples", 1.5),
        ("body_feedback_recovery_samples", False),
        ("body_feedback_recovery_samples", 0),
        ("body_feedback_recovery_samples", 2.5),
    ],
)
def test_recovery_gates_are_positive_integers_excluding_booleans(
    name: str,
    value: object,
) -> None:
    """Python booleans cannot silently become one-sample safety gates."""
    with pytest.raises(ValueError, match="positive integer"):
        ControllerConfig(**cast("dict[str, Any]", {name: value}))


def test_motion_enablement_flags_are_actual_booleans() -> None:
    """No truthy scalar can change a restart-bound motion safety decision."""
    with pytest.raises(ValueError, match="boolean"):
        ControllerConfig(body_enabled=1)  # type: ignore[arg-type]  # invalid runtime input is the subject
    with pytest.raises(ValueError, match="boolean"):
        ControllerConfig(require_motion_measurements=1)  # type: ignore[arg-type]  # invalid runtime input is the subject
