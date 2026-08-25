"""Unified predictive-controller safety, recovery evidence and atomic validation."""

from __future__ import annotations

import math
from dataclasses import replace
from itertools import pairwise

import pytest

from reachy_contracts import FaceDetection, NormalisedPoint
from reachy_mini_ha_satellite.behaviour.gaze_controller import (
    AxisState,
    ControllerConfig,
    ControllerFault,
    ControllerMode,
    GazeObservation,
    HeadMeasurement,
    initial_controller_state,
    reduce_command_result,
    step_controller,
)
from reachy_mini_ha_satellite.motion_validation import SampleFault, validate_gaze_sample
from reachy_mini_ha_satellite.ports import (
    GazeSample,
    MotionCommandResult,
    MotionCommandStatus,
    MotionFault,
)


def _observation(sequence: int, at: float, *, yaw: float = 0.3) -> GazeObservation:
    """Build one source-qualified calibrated observation."""
    return GazeObservation(
        source="remote",
        generation=0,
        sequence=sequence,
        captured_at=at - 0.05,
        received_at=at,
        target_key=0,
        face=FaceDetection(
            centre=NormalisedPoint(x=0.4, y=0.0),
            confidence=0.9,
        ),
        world_yaw=yaw,
        world_elevation=0.0,
    )


def test_fault_is_separate_from_mode_and_safe_hold_is_derived() -> None:
    """Workspace rejection keeps active lifecycle while exposing one safety fault."""
    config = ControllerConfig()
    result = step_controller(
        initial_controller_state(config),
        _observation(0, 0.1),
        now=0.1,
        dt=0.05,
        config=config,
        workspace_accepts=lambda _sample: False,
    )

    assert result.mode.value == "active"
    assert result.state.fault is ControllerFault.WORKSPACE
    assert result.safe_hold
    assert result.sample == initial_controller_state(config).last_safe_sample


def test_timing_fault_brakes_and_requires_distinct_valid_ticks() -> None:
    """Regressed or incoherent time cannot advance a candidate or duplicate recovery."""
    config = ControllerConfig(workspace_recovery_samples=2)
    active = step_controller(
        initial_controller_state(config),
        _observation(0, 0.1),
        now=0.1,
        dt=0.05,
        config=config,
    )
    invalid = step_controller(active.state, None, now=0.1, dt=0.05, config=config)
    first = step_controller(invalid.state, None, now=0.15, dt=0.05, config=config)
    duplicate = step_controller(first.state, None, now=0.15, dt=0.0, config=config)
    recovered = step_controller(
        duplicate.state,
        None,
        now=0.2,
        dt=0.05,
        config=config,
    )

    assert invalid.state.fault is ControllerFault.TIMING
    assert invalid.sample == active.sample
    assert first.state.recovery_valid_streak == 1
    assert duplicate.state.recovery_valid_streak == 1
    assert recovered.state.fault is ControllerFault.NONE


def test_pose_recovery_counts_new_measurement_timestamps_only() -> None:
    """A cached valid pose cannot satisfy a consecutive independent evidence gate."""
    config = ControllerConfig(
        require_motion_measurements=True,
        workspace_recovery_samples=2,
    )
    initial_measurement = HeadMeasurement(0.0, 0.0, 0.1)
    active = step_controller(
        initial_controller_state(config),
        _observation(0, 0.1),
        now=0.1,
        dt=0.05,
        config=config,
        head_measurement=initial_measurement,
    )
    failed = step_controller(
        active.state,
        _observation(1, 0.15),
        now=0.15,
        dt=0.05,
        config=config,
        input_fault=ControllerFault.POSE,
    )
    first = step_controller(
        failed.state,
        _observation(2, 0.2),
        now=0.2,
        dt=0.05,
        config=config,
        head_measurement=HeadMeasurement(0.0, 0.0, 0.2),
    )
    duplicate = step_controller(
        first.state,
        _observation(2, 0.25),
        now=0.25,
        dt=0.05,
        config=config,
        head_measurement=HeadMeasurement(0.0, 0.0, 0.2),
    )
    recovered = step_controller(
        duplicate.state,
        _observation(3, 0.3),
        now=0.3,
        dt=0.05,
        config=config,
        head_measurement=HeadMeasurement(0.0, 0.0, 0.3),
    )

    assert failed.state.fault is ControllerFault.POSE
    assert first.state.recovery_valid_streak == 1
    assert duplicate.state.recovery_valid_streak == 1
    assert recovered.state.fault is ControllerFault.NONE


def test_calibration_recovery_counts_new_observation_identities_only() -> None:
    """Failed calibration retains last safe and cannot recover by replaying one result."""
    config = ControllerConfig(workspace_recovery_samples=2)
    active = step_controller(
        initial_controller_state(config),
        _observation(0, 0.1),
        now=0.1,
        dt=0.05,
        config=config,
    )
    failed = step_controller(
        active.state,
        _observation(1, 0.15),
        now=0.15,
        dt=0.05,
        config=config,
        input_fault=ControllerFault.CALIBRATION,
        input_evidence=("calibration", ("remote", 0, 1)),
    )
    first = step_controller(
        failed.state,
        _observation(2, 0.2),
        now=0.2,
        dt=0.05,
        config=config,
    )
    duplicate = step_controller(
        first.state,
        _observation(2, 0.2),
        now=0.25,
        dt=0.05,
        config=config,
    )
    recovered = step_controller(
        duplicate.state,
        _observation(3, 0.3),
        now=0.3,
        dt=0.05,
        config=config,
    )

    assert failed.sample == active.sample
    assert failed.state.fault is ControllerFault.CALIBRATION
    assert first.state.recovery_valid_streak == 1
    assert duplicate.state.recovery_valid_streak == 1
    assert recovered.state.fault is ControllerFault.NONE


def test_shared_sample_validator_covers_derivatives_workspace_and_body_coherence() -> (
    None
):
    """The pure validator used by controller and adapter checks the whole atomic sample."""
    config = ControllerConfig()
    derivative = replace(
        initial_controller_state(config).last_safe_sample,
        world_yaw_velocity=config.yaw_limits.max_velocity * 2.0,
    )
    workspace = GazeSample(
        world_yaw=config.yaw_limits.maximum + 0.1,
        elevation=0.0,
        body_yaw=0.0,
        head_yaw=config.yaw_limits.maximum + 0.1,
        body_enabled=False,
    )

    assert validate_gaze_sample(derivative, config) is SampleFault.DERIVATIVE
    assert validate_gaze_sample(workspace, config) is SampleFault.WORKSPACE
    with pytest.raises(ValueError, match="body-disabled"):
        GazeSample(0.1, 0.0, 0.1, 0.0, False)
    with pytest.raises(ValueError, match="finite"):
        GazeSample(math.nan, 0.0, 0.0, math.nan, False)


def test_repeated_command_rejection_preserves_monotonic_hidden_braking() -> None:
    """Retry rejection holds emitted q while hidden velocity and acceleration brake."""
    config = ControllerConfig(workspace_recovery_samples=2)
    hidden = AxisState(position=0.2, velocity=0.3, acceleration=0.4)
    safe_sample = GazeSample(
        0.2,
        0.0,
        0.0,
        0.2,
        False,
        world_yaw_velocity=hidden.velocity,
        world_yaw_acceleration=hidden.acceleration,
    )
    prior = replace(
        initial_controller_state(config),
        mode=ControllerMode.ACTIVE,
        head_initialized=True,
        world_yaw=hidden,
        target_visible=True,
        last_safe_sample=safe_sample,
        last_step_at=0.1,
    )
    unsafe_candidate = replace(
        prior,
        world_yaw=AxisState(position=0.22, velocity=0.35, acceleration=0.5),
        last_safe_sample=replace(safe_sample, world_yaw=0.22, head_yaw=0.22),
        last_step_at=0.15,
    )
    rejected = reduce_command_result(
        unsafe_candidate,
        prior,
        MotionCommandResult(
            MotionCommandStatus.REJECTED,
            MotionFault.COMMAND,
            call=1,
        ),
        config,
    )

    held = rejected
    derivatives = []
    for call in (2, 3, 4):
        braking = step_controller(
            held,
            None,
            now=0.1 + call * 0.05,
            dt=0.05,
            config=config,
        )
        held = reduce_command_result(
            braking.state,
            held,
            MotionCommandResult(
                MotionCommandStatus.REJECTED,
                MotionFault.COMMAND,
                call=call,
            ),
            config,
        )
        derivatives.append(
            (abs(held.world_yaw.velocity), abs(held.world_yaw.acceleration))
        )
        assert held.last_safe_sample == safe_sample
        assert held.fault is ControllerFault.COMMAND

    assert all(
        later_velocity <= earlier_velocity
        for (earlier_velocity, _earlier_acceleration), (
            later_velocity,
            _later_acceleration,
        ) in pairwise(derivatives)
    )
    assert derivatives[-1][0] < derivatives[0][0]
    assert len({acceleration for _velocity, acceleration in derivatives}) > 1
    assert all(
        abs(later_acceleration - earlier_acceleration)
        <= config.yaw_limits.max_jerk * 0.05 + 1e-12
        for (_earlier_velocity, earlier_acceleration), (
            _later_velocity,
            later_acceleration,
        ) in pairwise(derivatives)
    )
