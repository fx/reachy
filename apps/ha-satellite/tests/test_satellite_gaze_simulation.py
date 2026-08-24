"""Deterministic nonlinear plant gates for the pure gaze controller.

The plant has explicit time, detector cadence and latency, command delay,
actuator lag, projection, distortion, dropout and faults. It performs no input,
output or sleeping and shares no projection implementation with the controller.
"""

from __future__ import annotations

import math
from dataclasses import replace
from itertools import pairwise
from typing import Final

import pytest
from gaze_simulation import (
    DEFAULT_NOISE,
    GazePlant,
    PlantConfig,
    PlantFaults,
    PlantSample,
    constant_target,
    moving_target,
)

from reachy_mini_ha_satellite.behaviour.gaze_controller import (
    ControllerConfig,
    ControllerMode,
)

_DEGREES: Final = math.pi / 180.0


def _nearest(trace: list[PlantSample], at: float) -> PlantSample:
    """Return the deterministic plant sample nearest one explicit time."""
    return min(trace, key=lambda sample: abs(sample.at - at))


def _controller_samples(trace: list[PlantSample]) -> list[PlantSample]:
    """Select one plant record for each controller tick."""
    return [sample for sample in trace if sample.controller_tick]


class TestAccuracyEnvelopes:
    """The approved step and moving-target scenarios gate the foundation."""

    @pytest.mark.parametrize("controller_interval", [0.02, 0.025, 0.05, 0.10])
    def test_thirty_five_degree_step_settles_without_overshoot(
        self,
        controller_interval: float,
    ) -> None:
        """Error is at most 0.025 by three seconds and overshoot at most 2°."""
        plant = GazePlant(
            PlantConfig(controller_interval=lambda _index, _at: controller_interval)
        )
        trace = plant.run(
            4.0,
            lambda at: (0.0, 0.0) if at < 0.5 else (35.0 * _DEGREES, 0.0),
        )
        after_step = [sample for sample in trace if sample.at >= 0.5]

        assert abs(_nearest(trace, 3.5).image_error.x) <= 0.025
        assert (
            max(
                max(0.0, sample.plant_world_yaw - sample.target_yaw)
                for sample in after_step
            )
            <= 2.0 * _DEGREES
        )
        consumed = [
            sample.observation_identity
            for sample in _controller_samples(trace)
            if sample.observation_consumed
        ]
        assert len(consumed) == len(set(consumed))

    @pytest.mark.parametrize("axis", ["horizontal", "vertical"])
    def test_five_degree_per_second_motion_has_bounded_lag(self, axis: str) -> None:
        """Both independent axes meet acquisition, mean, max and stop envelopes."""
        path = moving_target(
            axis=axis,
            starts_at=0.5,
            stops_at=6.0,
            speed=5.0 * _DEGREES,
        )
        trace = GazePlant().run(6.5, path)
        acquired = [sample for sample in trace if 2.0 <= sample.at <= 6.0]
        lags = [
            abs(
                sample.target_yaw - sample.plant_world_yaw
                if axis == "horizontal"
                else sample.target_elevation - sample.plant_elevation
            )
            for sample in acquired
        ]
        stopped = _nearest(trace, 6.5)
        final_lag = abs(
            stopped.target_yaw - stopped.plant_world_yaw
            if axis == "horizontal"
            else stopped.target_elevation - stopped.plant_elevation
        )

        assert sum(lags) / len(lags) <= 1.5 * _DEGREES
        assert max(lags) <= 2.0 * _DEGREES
        assert final_lag <= 1.5 * _DEGREES

    def test_static_bounded_noise_never_commands_tracking_motion(self) -> None:
        """Raw-error deadband prevents predicted noise from activating motion."""
        plant = GazePlant(PlantConfig(noise=DEFAULT_NOISE))
        trace = plant.run(8.0, constant_target())

        assert all(not sample.deadband_active for sample in trace)
        assert max(abs(sample.command.world_yaw) for sample in trace) == 0.0
        assert max(abs(sample.command.elevation) for sample in trace) == 0.0
        assert all(sample.command.body_yaw == 0.0 for sample in trace)


class TestDelayDropoutCadenceAndFaults:
    """Timing and fault channels are independent deterministic injections."""

    def test_late_fresh_observation_uses_clamped_capture_prediction(self) -> None:
        """Seven hundred milliseconds of latency is not two seconds of staleness."""
        controller = replace(
            ControllerConfig(),
            actuator_delay=0.20,
            prediction_horizon=0.25,
        )
        plant = GazePlant(
            PlantConfig(observation_latency=lambda _index, _at: 0.70),
            controller=controller,
        )
        trace = plant.run(4.5, constant_target(yaw=35.0 * _DEGREES))
        after_receipt = [sample for sample in trace if sample.at >= 0.70]

        assert any(sample.observation_consumed for sample in after_receipt)
        assert max(
            sample.prediction_horizon for sample in after_receipt
        ) == pytest.approx(0.25)
        assert any(sample.mode is ControllerMode.ACTIVE for sample in after_receipt)
        assert abs(trace[-1].image_error.x) <= 0.025

    def test_dropout_then_loss_holds_returns_and_reacquires(self) -> None:
        """Short dropout does not replay; persistent loss returns through servos."""
        faults = PlantFaults(
            drop_observation=lambda index, _at: 8 <= index < 13 or 25 <= index < 60
        )
        controller = replace(
            ControllerConfig(),
            staleness_seconds=0.5,
            loss_hold_seconds=0.2,
        )
        trace = GazePlant(faults=faults, controller=controller).run(
            7.0,
            constant_target(yaw=25.0 * _DEGREES),
        )
        ticks = _controller_samples(trace)
        consumed = [
            sample.observation_identity
            for sample in ticks
            if sample.observation_consumed
        ]

        assert len(consumed) == len(set(consumed))
        assert any(sample.mode is ControllerMode.HOLD for sample in ticks)
        assert any(sample.mode is ControllerMode.RETURNING for sample in ticks)
        assert any(
            sample.mode is ControllerMode.ACTIVE and sample.at > 6.0 for sample in ticks
        )
        assert all(sample.command.body_yaw == 0.0 for sample in ticks)

    def test_cadence_change_and_stall_keep_derivatives_bounded(self) -> None:
        """A long tick integrates only the configured safe stall interval."""

        def cadence(index: int, _at: float) -> float:
            if index == 20:
                return 0.30
            return 0.02 if index < 20 else 0.10

        trace = GazePlant(PlantConfig(controller_interval=cadence)).run(
            4.0, constant_target(yaw=30.0 * _DEGREES)
        )
        ticks = _controller_samples(trace)
        limits = ControllerConfig().yaw_limits

        assert any(later.at - earlier.at >= 0.299 for earlier, later in pairwise(ticks))
        assert all(
            abs(sample.state.world_yaw.velocity) <= limits.max_velocity
            for sample in ticks
        )
        assert all(
            abs(sample.state.world_yaw.acceleration) <= limits.max_acceleration
            for sample in ticks
        )
        for earlier, later in pairwise(ticks):
            dt = later.at - earlier.at
            assert (
                abs(
                    later.state.world_yaw.acceleration
                    - earlier.state.world_yaw.acceleration
                )
                <= limits.max_jerk * dt + 1e-9
            )

    def test_projection_distortion_workspace_and_fault_injections_are_exercised(
        self,
    ) -> None:
        """The plant exposes independent nonlinear and fault seams, not fixtures in name."""
        projected: list[float] = []
        distorted: list[float] = []

        def projection(angle: float, field_of_view: float) -> float:
            projected.append(angle)
            return math.tan(angle) / math.tan(field_of_view / 2.0)

        def distortion(value: float) -> float:
            distorted.append(value)
            return value * (1.0 + 0.03 * value * value)

        faults = PlantFaults(
            reject_workspace=lambda _sample, tick, _at: 6 <= tick < 9,
            corrupt_observation=lambda observation, index: (
                replace(
                    observation,
                    confidence=0.8,
                )
                if index == 2
                else observation
            ),
        )
        plant = GazePlant(
            PlantConfig(projection=projection, distortion=distortion),
            faults=faults,
        )
        trace = plant.run(2.0, constant_target(yaw=20.0 * _DEGREES))
        ticks = _controller_samples(trace)

        assert projected
        assert distorted
        assert any(sample.mode is ControllerMode.WORKSPACE_HOLD for sample in ticks)
        assert any(
            sample.mode is ControllerMode.ACTIVE
            for sample in ticks
            if sample.at > ticks[9].at
        )


class TestReversalEnvelope:
    """A nonlinear delayed plant confirms bounded reversal at scenario altitude."""

    def test_large_reversal_has_no_position_jump_and_bounded_derivatives(self) -> None:
        """A plus-to-minus 35° target reverses through the configured limits."""
        reversal_at = 2.5
        trace = GazePlant().run(
            6.0,
            lambda at: (
                35.0 * _DEGREES if at < reversal_at else -35.0 * _DEGREES,
                0.0,
            ),
        )
        ticks = _controller_samples(trace)
        limits = ControllerConfig().yaw_limits

        assert all(
            abs(sample.state.world_yaw.velocity) <= limits.max_velocity
            for sample in ticks
        )
        assert all(
            abs(sample.state.world_yaw.acceleration) <= limits.max_acceleration
            for sample in ticks
        )
        for earlier, later in pairwise(ticks):
            dt = later.at - earlier.at
            assert (
                abs(later.state.world_yaw.position - earlier.state.world_yaw.position)
                <= limits.max_velocity * dt + 1e-9
            )
            assert (
                abs(
                    later.state.world_yaw.acceleration
                    - earlier.state.world_yaw.acceleration
                )
                <= limits.max_jerk * dt + 1e-9
            )
