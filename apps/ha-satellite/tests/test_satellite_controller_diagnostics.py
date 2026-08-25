"""Bounded private scalar diagnostics for predictive gaze."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import replace

import pytest
from satellite_support import assert_public_controller_diagnostic_event

from reachy_mini_ha_satellite.behaviour.controller_diagnostics import (
    ControllerDiagnostics,
    ControllerEvent,
)
from reachy_mini_ha_satellite.behaviour.gaze_controller import (
    AxisState,
    ControllerConfig,
    ControllerFault,
    ControllerMode,
    ControllerStep,
    EstimatorReset,
    allocate_body,
    initial_controller_state,
    step_controller,
)
from reachy_mini_ha_satellite.ports import GazeSample


def test_diagnostics_ring_evicts_oldest_and_snapshots_deterministically() -> None:
    """Fixed capacity retains the newest events in stable oldest-first order."""
    diagnostics = ControllerDiagnostics(capacity=2)
    config = ControllerConfig()
    state = initial_controller_state(config)

    for index in range(3):
        at = index * 0.05
        step = step_controller(
            state,
            None,
            now=at,
            dt=0.0 if index == 0 else 0.05,
            config=config,
        )
        state = step.state
        diagnostics.record(
            step,
            config=config,
            at=at,
            observation_age=None,
            emitted=False,
        )

    first = diagnostics.snapshot()
    second = diagnostics.snapshot()
    assert first == second
    assert [event["at"] for event in first] == [0.05, 0.1]


def test_diagnostics_privacy_guard_rejects_unknown_scalar_identifiers() -> None:
    """A differently named scalar identifier must not extend the public schema."""
    diagnostics = ControllerDiagnostics(capacity=1)
    config = ControllerConfig()
    step = step_controller(
        initial_controller_state(config),
        None,
        now=0.0,
        dt=0.0,
        config=config,
    )
    diagnostics.record(
        step,
        config=config,
        at=0.0,
        observation_age=None,
        emitted=False,
    )
    mutated = {**diagnostics.snapshot()[0], "robot_serial": 7.0}

    with pytest.raises(AssertionError):
        assert_public_controller_diagnostic_event(mutated)


def test_diagnostics_schema_matches_exact_unversioned_public_allowlist() -> None:
    """Every required key and only those keys retain documented scalar/null types."""
    diagnostics = ControllerDiagnostics(capacity=1)
    config = ControllerConfig()
    step = step_controller(
        initial_controller_state(config),
        None,
        now=0.0,
        dt=0.0,
        config=config,
    )
    diagnostics.record(
        step,
        config=config,
        at=0.0,
        observation_age=None,
        emitted=False,
    )

    event = diagnostics.snapshot()[0]
    assert event["observation_age"] is None
    assert event["command_accepted"] is None
    assert_public_controller_diagnostic_event(event)
    assert_public_controller_diagnostic_event(
        {**event, "observation_age": 0.1, "command_accepted": True}
    )


@pytest.mark.parametrize("capacity", [True, 0])
def test_diagnostics_capacity_is_a_positive_integer_excluding_bool(
    capacity: int,
) -> None:
    """The ring can never become unbounded or silently use bool as one."""
    with pytest.raises(ValueError, match="positive integer"):
        ControllerDiagnostics(capacity)


def test_diagnostics_reject_nonfinite_scalar_payloads() -> None:
    """Invalid JSON numbers never enter the operator surface."""
    with pytest.raises(ValueError, match="finite scalars"):
        ControllerEvent(
            at=math.nan,
            mode=ControllerMode.ACTIVE,
            fault=ControllerFault.NONE,
            safe_hold=False,
            observation_age=None,
            observation_consumed=False,
            estimator_reset=EstimatorReset.NONE,
            prediction_horizon=0.0,
            deadband_active=False,
            world_yaw=0.0,
            elevation=0.0,
            body_yaw=0.0,
            emitted=False,
        )


def test_unbounded_internal_ring_is_refused_even_if_invariant_is_corrupted() -> None:
    """The capacity property never reports an unbounded diagnostic history."""
    diagnostics = ControllerDiagnostics()
    diagnostics._events = deque()

    with pytest.raises(AssertionError, match="always bounded"):
        _ = diagnostics.capacity


def test_saturated_event_explains_allocation_workspace_and_derivative_limits() -> None:
    """REQ-091 evidence names configured envelopes and which limits are active."""
    config = replace(ControllerConfig(), body_enabled=True)
    world = AxisState(
        position=config.yaw_limits.maximum,
        velocity=config.yaw_limits.max_velocity,
        acceleration=config.yaw_limits.max_acceleration,
    )
    body_position = allocate_body(world.position, config)
    body = AxisState(position=body_position)
    sample = GazeSample(
        world.position,
        0.0,
        body.position,
        world.position - body.position,
        True,
        world_yaw_velocity=world.velocity,
        world_yaw_acceleration=world.acceleration,
    )
    state = replace(
        initial_controller_state(config),
        mode=ControllerMode.ACTIVE,
        world_yaw=world,
        body_yaw=body,
        last_safe_sample=sample,
    )
    step = ControllerStep(state, True, EstimatorReset.NONE, 0.2, False)
    diagnostics = ControllerDiagnostics(capacity=2)

    diagnostics.record(
        step,
        config=config,
        at=1.0,
        observation_age=0.1,
        emitted=True,
    )
    event = diagnostics.snapshot()[0]

    assert event["allocation_body_share"] == pytest.approx(
        body.position / world.position
    )
    assert event["head_yaw"] == pytest.approx(sample.head_yaw)
    assert event["world_yaw_minimum"] == config.yaw_limits.minimum
    assert event["world_yaw_maximum"] == config.yaw_limits.maximum
    assert event["world_yaw_max_velocity"] == config.yaw_limits.max_velocity
    assert event["world_yaw_max_acceleration"] == config.yaw_limits.max_acceleration
    assert event["world_yaw_max_jerk"] == config.yaw_limits.max_jerk
    assert event["world_yaw_position_limited"] is True
    assert event["world_yaw_velocity_limited"] is True
    assert event["world_yaw_acceleration_limited"] is True
    assert_public_controller_diagnostic_event(event)


def test_reset_clears_only_ring_contents() -> None:
    """The diagnostics owner has no reference through which it could move a robot."""
    diagnostics = ControllerDiagnostics(capacity=1)
    config = ControllerConfig()
    state = initial_controller_state(config)
    step = step_controller(state, None, now=0.0, dt=0.0, config=config)
    diagnostics.record(
        step,
        config=config,
        at=0.0,
        observation_age=None,
        emitted=False,
    )
    controller_before = step.state

    diagnostics.reset()

    assert diagnostics.snapshot() == ()
    assert step.state == controller_before
