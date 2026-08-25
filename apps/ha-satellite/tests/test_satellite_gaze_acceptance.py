"""Named deterministic acceptance matrix for gaze-control REQ-074 through REQ-092."""

from __future__ import annotations

import math
from dataclasses import replace
from itertools import pairwise
from typing import Final

import numpy as np
import pytest
from gaze_simulation import (
    DEFAULT_NOISE,
    GazePlant,
    PlantConfig,
    PlantFaults,
    constant_target,
    moving_target,
)

from reachy_contracts import FaceDetection, NormalisedPoint
from reachy_mini_ha_satellite.adapters.motion_reachy import (
    head_pose_matrix,
    rebase_calibrated_rotation,
)
from reachy_mini_ha_satellite.behaviour.controller_diagnostics import (
    ControllerDiagnostics,
)
from reachy_mini_ha_satellite.behaviour.gaze_controller import (
    AxisState,
    ControllerConfig,
    ControllerFault,
    ControllerMode,
    DeadbandState,
    EstimatorReset,
    GazeObservation,
    ImagePoint,
    allocate_body,
    apply_deadband,
    initial_controller_state,
    step_controller,
    update_estimator,
)
from reachy_mini_ha_satellite.behaviour.satellite import SatelliteBehaviour
from reachy_mini_ha_satellite.motion_validation import validate_gaze_sample
from reachy_mini_ha_satellite.ports import HeadPose

_DEGREES: Final = math.pi / 180.0


def _observation(
    sequence: int,
    at: float,
    *,
    x: float = 0.4,
    y: float = 0.0,
    world_yaw: float | None = None,
    world_elevation: float | None = None,
) -> GazeObservation:
    """Build one deterministic completed camera result."""
    return GazeObservation(
        source="remote",
        generation=0,
        sequence=sequence,
        captured_at=at - 0.05,
        received_at=at,
        target_key=0,
        face=FaceDetection(
            centre=NormalisedPoint(x=x, y=y),
            confidence=0.9,
        ),
        world_yaw=world_yaw,
        world_elevation=world_elevation,
    )


#:= docs/specs/gaze-control/index.md#req-074-each-observation-is-consumed-once
#:% Repeated reads of one source-qualified face-detection result MUST NOT compound
#:% its correction while the previously established trajectory continues.
def test_req_074_each_observation_is_consumed_once() -> None:
    """A cached identity advances trajectory but never the estimator twice."""
    config = ControllerConfig()
    observation = _observation(0, 0.1)
    first = step_controller(
        initial_controller_state(config), observation, now=0.1, dt=0.05, config=config
    )
    replay = step_controller(first.state, observation, now=0.15, dt=0.05, config=config)

    assert first.observation_consumed
    assert not replay.observation_consumed
    assert replay.state.estimator == first.state.estimator


#:= docs/specs/gaze-control/index.md#req-075-observation-time-uses-the-capture-clock
#:% Changing nominal detector or behavior-loop cadence MUST NOT change observation
#:% age or the trajectory produced from an otherwise identical timed observation
#:% sequence.
def test_req_075_capture_clock_is_invariant_to_nominal_cadence() -> None:
    """Independent common-time values reject receipt interpreted as capture."""
    config = ControllerConfig(prediction_horizon=0.8, staleness_seconds=0.9)
    observation_sequences = (
        (
            replace(
                _observation(0, 0.14, x=0.12),
                captured_at=0.03,
                received_at=0.14,
            ),
            replace(
                _observation(1, 0.31, x=0.34),
                captured_at=0.17,
                received_at=0.31,
            ),
            replace(
                _observation(2, 0.52, x=0.61),
                captured_at=0.33,
                received_at=0.52,
            ),
        ),
        (
            replace(
                _observation(0, 0.14, x=0.12),
                captured_at=0.01,
                received_at=0.14,
            ),
            replace(
                _observation(1, 0.31, x=0.34),
                captured_at=0.12,
                received_at=0.31,
            ),
            replace(
                _observation(2, 0.52, x=0.61),
                captured_at=0.29,
                received_at=0.52,
            ),
        ),
    )
    schedules = (
        (0.14, 0.19, 0.24, 0.29, 0.31, 0.36, 0.41, 0.46, 0.51, 0.52, 0.57, 0.62),
        (0.14, 0.24, 0.31, 0.41, 0.52, 0.62),
    )
    common_times = frozenset((0.14, 0.24, 0.31, 0.41, 0.52, 0.62))
    expected = (
        {
            0.14: (
                0.11,
                0.21,
                AxisState(-0.0007635815477, -0.0305432619099, -0.6108652381980),
            ),
            0.24: (
                0.21,
                0.31,
                AxisState(-0.0106901416685, -0.1832595714594, -1.8325957145940),
            ),
            0.31: (
                0.14,
                0.24,
                AxisState(-0.0295536602240, -0.3591887600604, -2.6878070480713),
            ),
            0.41: (
                0.24,
                0.34,
                AxisState(-0.0794351702460, -0.6384414403795, -2.7925268031909),
            ),
            0.52: (
                0.19,
                0.29,
                AxisState(-0.1665585158471, -0.9456193887305, -2.7925268031909),
            ),
            0.62: (
                0.29,
                0.39,
                AxisState(-0.2621938322101, -0.9599310885969, -1.5707963267949),
            ),
        },
        {
            0.14: (
                0.13,
                0.23,
                AxisState(-0.0007635815477, -0.0305432619099, -0.6108652381980),
            ),
            0.24: (
                0.23,
                0.33,
                AxisState(-0.0129808863117, -0.2138028333693, -1.8325957145940),
            ),
            0.31: (
                0.19,
                0.29,
                AxisState(-0.0345322119153, -0.4019493267343, -2.6878070480713),
            ),
            0.41: (
                0.29,
                0.39,
                AxisState(-0.0886897786047, -0.6812020070534, -2.7925268031909),
            ),
            0.52: (
                0.23,
                0.33,
                AxisState(-0.1789520988655, -0.9599310885969, -2.5339007413045),
            ),
            0.62: (
                0.33,
                0.43,
                AxisState(-0.2749452077252, -0.9599310885969, -1.3121702649085),
            ),
        },
    )

    def run(
        sequence: tuple[GazeObservation, ...],
        schedule: tuple[float, ...],
    ) -> dict[float, tuple[float, float, AxisState]]:
        state = initial_controller_state(config)
        latest: GazeObservation | None = None
        prior_at = schedule[0] - 0.05
        records: dict[float, tuple[float, float, AxisState]] = {}
        for at in schedule:
            available = [item for item in sequence if item.received_at <= at]
            if available:
                latest = available[-1]
            result = step_controller(
                state,
                latest,
                now=at,
                dt=at - prior_at,
                config=config,
            )
            state = result.state
            if at in common_times:
                records[at] = (
                    result.prediction_horizon - config.actuator_delay,
                    result.prediction_horizon,
                    state.world_yaw,
                )
            prior_at = at
        return records

    def assert_matches_oracle(
        actual: dict[float, tuple[float, float, AxisState]],
        oracle: dict[float, tuple[float, float, AxisState]],
    ) -> None:
        assert actual.keys() == oracle.keys()
        time_epsilon = 1e-9
        for at, (expected_age, expected_horizon, expected_axis) in oracle.items():
            age, horizon, axis = actual[at]
            assert age == pytest.approx(expected_age, abs=1e-12)
            assert horizon == pytest.approx(expected_horizon, abs=1e-12)
            assert axis.position == pytest.approx(
                expected_axis.position,
                abs=config.yaw_limits.max_velocity * time_epsilon,
            )
            assert axis.velocity == pytest.approx(
                expected_axis.velocity,
                abs=config.yaw_limits.max_acceleration * time_epsilon,
            )
            assert axis.acceleration == pytest.approx(
                expected_axis.acceleration,
                abs=config.yaw_limits.max_jerk * time_epsilon,
            )

    for sequence, schedule, oracle in zip(
        observation_sequences,
        schedules,
        expected,
        strict=True,
    ):
        assert_matches_oracle(run(sequence, schedule), oracle)

    receipt_as_capture = tuple(
        replace(observation, captured_at=observation.received_at)
        for observation in observation_sequences[0]
    )
    mutated = run(receipt_as_capture, schedules[0])
    assert mutated[0.14][0] != pytest.approx(expected[0][0.14][0], abs=1e-12)
    with pytest.raises(AssertionError):
        assert_matches_oracle(mutated, expected[0])


#:= docs/specs/gaze-control/index.md#req-076-observation-discontinuities-reset-prediction
#:% An observation after a detection-source, source-generation, selected-target,
#:% timestamp-order or supported-gap discontinuity MUST NOT inherit target velocity
#:% from the preceding observation stream.
def test_req_076_every_observation_discontinuity_resets_prediction() -> None:
    """All declared discontinuity classes start with zero target velocity."""
    config = ControllerConfig()
    first, _reset = update_estimator(None, _observation(0, 0.1), config)
    moving, _reset = update_estimator(first, _observation(1, 0.2, x=0.7), config)
    candidates = (
        replace(_observation(0, 0.3), source="local"),
        replace(_observation(0, 0.3), generation=1),
        replace(_observation(2, 0.3), target_key=1),
        replace(_observation(2, 0.3), captured_at=moving.captured_at),
        _observation(2, 1.0),
    )

    for candidate in candidates:
        reset, reason = update_estimator(moving, candidate, config)
        assert reason is not EstimatorReset.NONE
        assert reset.velocity == ImagePoint(0.0, 0.0)


#:= docs/specs/gaze-control/index.md#req-077-prediction-is-bounded-separately-from-staleness
#:% The controller MUST bound predicted image position, target velocity and
#:% prediction horizon while treating receipt-time staleness as the separate trigger
#:% for loss handling.
def test_req_077_prediction_bounds_are_separate_from_receipt_staleness() -> None:
    """A late fresh result clamps prediction and stays active."""
    config = ControllerConfig(prediction_horizon=0.2, staleness_seconds=1.0)
    result = step_controller(
        initial_controller_state(config),
        _observation(0, 0.8),
        now=0.9,
        dt=0.05,
        config=config,
    )

    assert result.prediction_horizon == pytest.approx(0.2)
    assert not result.stale
    assert result.mode is ControllerMode.ACTIVE


#:= docs/specs/gaze-control/index.md#req-078-calibrated-gaze-accounts-for-camera-motion
#:% Head motion between image capture and calibration MUST neither shift a
#:% stationary target's world-gaze anchor nor be counted more than once.
def test_req_078_ego_motion_is_rebased_exactly_once() -> None:
    """Capture @ query.T @ target removes only the intervening camera turn."""
    capture = head_pose_matrix(HeadPose(yaw=10.0 * _DEGREES))[:3, :3]
    query = head_pose_matrix(HeadPose(yaw=30.0 * _DEGREES))[:3, :3]
    target = head_pose_matrix(HeadPose(yaw=55.0 * _DEGREES))[:3, :3]

    rebased = rebase_calibrated_rotation(capture, query, target)
    forward = rebased @ np.array([1.0, 0.0, 0.0])
    assert math.atan2(forward[1], forward[0]) == pytest.approx(35.0 * _DEGREES)


def test_simulation_calibration_oracle_is_independent_and_mutation_sensitive() -> None:
    """Inverting camera-to-world calibration breaks fixation instead of self-confirming."""

    def inverted_calibration(
        image: ImagePoint,
        capture_yaw: float,
        capture_elevation: float,
        horizontal_fov: float,
        vertical_fov: float,
    ) -> tuple[float, float]:
        yaw_error = math.atan(image.x * math.tan(horizontal_fov / 2.0))
        elevation_error = math.atan(image.y * math.tan(vertical_fov / 2.0))
        return capture_yaw + yaw_error, capture_elevation - elevation_error

    target = constant_target(yaw=35.0 * _DEGREES, elevation=10.0 * _DEGREES)
    correct = GazePlant().run(4.0, target)
    inverted = GazePlant(PlantConfig(calibration_oracle=inverted_calibration)).run(
        4.0, target
    )

    assert abs(correct[-1].image_error.x) <= 0.025
    assert abs(correct[-1].image_error.y) <= 0.025
    assert (
        max(
            abs(inverted[-1].image_error.x),
            abs(inverted[-1].image_error.y),
        )
        > 0.025
    )


#:= docs/specs/gaze-control/index.md#req-079-centering-has-continuous-hysteresis
#:% Crossing configured tracking activation and release regions MUST suppress
#:% bounded image noise without causing command chatter or a command discontinuity.
def test_req_079_hysteresis_is_continuous_across_bounded_noise() -> None:
    """Static noise never activates and an active command reaches zero continuously."""
    config = ControllerConfig()
    state = DeadbandState()
    for x, y in DEFAULT_NOISE:
        output, state = apply_deadband(
            ImagePoint(x, y), activation=ImagePoint(x, y), state=state, config=config
        )
        assert output == ImagePoint(0.0, 0.0)
        assert not state.active


#:= docs/specs/gaze-control/index.md#req-080-step-tracking-settles-without-whiplash
#:% For a stationary target that makes a 35-degree horizontal yaw step inside the
#:% supported operating envelope, predictive gaze MUST enter a normalized image
#:% error of 0.025 within 3 seconds and limit angular overshoot to 2 degrees.
def test_req_080_thirty_five_degree_step_meets_settle_and_overshoot_envelopes() -> None:
    """The independent delayed plant enforces both numeric step thresholds."""
    trace = GazePlant().run(
        4.0,
        lambda at: (0.0, 0.0) if at < 0.5 else (35.0 * _DEGREES, 0.0),
    )
    settled = min(trace, key=lambda sample: abs(sample.at - 3.5))
    after = [sample for sample in trace if sample.at >= 0.5]

    assert abs(settled.image_error.x) <= 0.025
    assert (
        max(max(0.0, sample.plant_world_yaw - sample.target_yaw) for sample in after)
        <= 2.0 * _DEGREES
    )


#:= docs/specs/gaze-control/index.md#req-081-moving-target-lag-is-bounded
#:% For a target moving at 5 degrees per second inside the supported operating
#:% envelope, predictive gaze MUST after at most 1.5 seconds of acquisition limit
#:% mean lag over the next 4 seconds to 1.5 degrees, maximum lag to 2 degrees and lag
#:% 0.5 seconds after motion stops to 1.5 degrees on both horizontal and vertical
#:% axes.
@pytest.mark.parametrize("axis", ["horizontal", "vertical"])
def test_req_081_five_degree_per_second_lag_is_bounded_on_both_axes(axis: str) -> None:
    """Horizontal and vertical paths independently enforce mean, max and stop lag."""
    trace = GazePlant().run(
        6.5,
        moving_target(axis=axis, starts_at=0.5, stops_at=6.0, speed=5.0 * _DEGREES),
    )
    acquired = [sample for sample in trace if 2.0 <= sample.at <= 6.0]
    lags = [
        abs(
            sample.target_yaw - sample.plant_world_yaw
            if axis == "horizontal"
            else sample.target_elevation - sample.plant_elevation
        )
        for sample in acquired
    ]
    final = trace[-1]
    final_lag = abs(
        final.target_yaw - final.plant_world_yaw
        if axis == "horizontal"
        else final.target_elevation - final.plant_elevation
    )

    assert sum(lags) / len(lags) <= 1.5 * _DEGREES
    assert max(lags) <= 2.0 * _DEGREES
    assert final_lag <= 1.5 * _DEGREES


#:= docs/specs/gaze-control/index.md#req-082-static-noise-does-not-move-the-robot
#:% For bounded static detection noise inside the configured non-activation
#:% envelope, predictive gaze MUST issue no tracking motion after settling.
def test_req_082_static_noise_issues_no_tracking_motion() -> None:
    """The complete delayed plant stays at neutral under the accepted noise corpus."""
    trace = GazePlant(PlantConfig(noise=DEFAULT_NOISE)).run(6.0, constant_target())
    assert all(sample.command.world_yaw == 0.0 for sample in trace)
    assert all(sample.command.elevation == 0.0 for sample in trace)
    assert all(sample.command.body_yaw == 0.0 for sample in trace)


#:= docs/specs/gaze-control/index.md#req-083-motion-derivatives-remain-bounded
#:% Every commanded head and body trajectory MUST remain within its configured
#:% position, velocity, acceleration and jerk envelopes across acquisition,
#:% tracking, reversal, workspace limiting, loss, return and recovery.
def test_req_083_reversal_and_safety_paths_keep_all_derivatives_bounded() -> None:
    """A reversal crossing workspace rejection remains inside every q/v/a/jerk bound."""
    config = ControllerConfig()
    trace = GazePlant(
        faults=PlantFaults(reject_workspace=lambda _sample, tick, _at: 35 <= tick < 40)
    ).run(
        6.0,
        lambda at: (30.0 * _DEGREES if at < 2.5 else -30.0 * _DEGREES, 0.0),
    )
    ticks = [sample for sample in trace if sample.controller_tick]
    for name, limits in (
        ("world_yaw", config.yaw_limits),
        ("elevation", config.elevation_limits),
        ("body_yaw", config.body_limits),
    ):
        axes = [getattr(sample.state, name) for sample in ticks]
        assert all(limits.minimum <= axis.position <= limits.maximum for axis in axes)
        assert all(abs(axis.velocity) <= limits.max_velocity for axis in axes)
        assert all(abs(axis.acceleration) <= limits.max_acceleration for axis in axes)
        assert all(
            abs(later.acceleration - earlier.acceleration)
            <= limits.max_jerk * (later_sample.at - earlier_sample.at) + 1e-9
            for (earlier_sample, later_sample), (earlier, later) in zip(
                pairwise(ticks), pairwise(axes), strict=True
            )
        )


#:= docs/specs/gaze-control/index.md#req-084-tracking-uses-canonical-head-poses
#:% Tracking commands MUST use zero head roll and canonical head translation so
#:% expressive pose offsets cannot distort fixation or consume the parallel head's
#:% tracking workspace.
def test_req_084_tracking_pose_is_canonical() -> None:
    """The generated tracking pose has no translation and no roll contribution."""
    pose = head_pose_matrix(HeadPose(yaw=0.3, pitch=0.2, roll=0.0))
    assert np.allclose(pose[:3, 3], np.zeros(3))
    assert np.allclose(pose[3], [0.0, 0.0, 0.0, 1.0])


#:= docs/specs/gaze-control/index.md#req-085-head-and-body-preserve-one-world-gaze
#:% When body motion is enabled, every coordinated command MUST preserve the
#:% identity that world-gaze yaw equals body yaw plus head-on-body yaw.
def test_req_085_body_catch_up_preserves_exact_world_gaze_identity() -> None:
    """Every emitted coordinated plant command preserves the atomic yaw equation."""
    config = replace(ControllerConfig(), body_enabled=True)
    trace = GazePlant(controller=config).run(5.0, constant_target(yaw=45.0 * _DEGREES))
    assert all(
        sample.command.world_yaw
        == pytest.approx(sample.command.body_yaw + sample.command.head_yaw)
        for sample in trace
    )


#:= docs/specs/gaze-control/index.md#req-086-body-allocation-is-continuous
#:% When body motion is enabled, the controller MUST allocate a continuous,
#:% odd-symmetric and monotonic body share that ignores the configured noise floor,
#:% uses a modest body contribution for small lateral gaze shifts and leaves the
#:% head inside its configured comfort region for large shifts.
def test_req_086_body_allocation_is_continuous_symmetric_and_monotonic() -> None:
    """Dense samples pin continuity, symmetry, noise floor and head comfort."""
    config = replace(ControllerConfig(), body_enabled=True)
    values = [index * 0.1 * _DEGREES for index in range(701)]
    allocated = [allocate_body(value, config) for value in values]

    assert all(later >= earlier for earlier, later in pairwise(allocated))
    assert allocate_body(config.body_noise_floor, config) == 0.0
    assert all(
        allocate_body(-value, config) == pytest.approx(-body)
        for value, body in zip(values, allocated, strict=True)
    )
    assert values[-1] - allocated[-1] <= config.body_head_comfort


#:= docs/specs/gaze-control/index.md#req-087-measured-body-feedback-cannot-corrupt-trajectory-limits
#:% When body motion is enabled, ordinary measured-body lag MUST NOT reset the
#:% commanded trajectory or cause its derivative envelopes to be exceeded, while
#:% missing or persistently divergent feedback enters bounded safe hold.
def test_req_087_body_lag_divergence_and_exact_new_sample_recovery() -> None:
    """Independent lag passes; persistent offset faults; fresh measurements recover."""
    ordinary_config = replace(ControllerConfig(), body_enabled=True)
    config = replace(
        ordinary_config,
        body_feedback_divergence=3.0 * _DEGREES,
        body_feedback_persistence=0.15,
        body_feedback_recovery_samples=2,
    )
    ordinary = GazePlant(
        PlantConfig(
            body_measurement_interval=lambda _index, _at: 0.1, body_measurement_lag=0.1
        ),
        controller=ordinary_config,
    ).run(3.0, constant_target(yaw=25.0 * _DEGREES))
    assert not any(
        sample.state.fault is ControllerFault.BODY_FEEDBACK for sample in ordinary
    )

    divergent = GazePlant(
        PlantConfig(body_measurement_interval=lambda _index, _at: 0.05),
        faults=PlantFaults(
            body_measurement_offset=lambda index, _at: (
                -15.0 * _DEGREES if 5 <= index < 18 else 0.0
            )
        ),
        controller=config,
    ).run(4.0, constant_target(yaw=25.0 * _DEGREES))
    faulted = [
        sample
        for sample in divergent
        if sample.state.fault is ControllerFault.BODY_FEEDBACK
    ]
    assert faulted
    assert divergent[-1].state.fault is ControllerFault.NONE
    assert faulted[-1].body_measurement is not None


#:= docs/specs/gaze-control/index.md#req-088-loss-returns-all-controlled-axes-smoothly
#:% After the configured loss hold, the controller MUST return every controlled head
#:% and body axis toward neutral through the same derivative-bounded trajectory and
#:% release gaze ownership only after position, velocity and acceleration settle.
def test_req_088_loss_returns_every_axis_before_idle_handoff() -> None:
    """Persistent loss holds, returns, then reaches settled idle without a snap."""
    config = ControllerConfig(staleness_seconds=0.5, loss_hold_seconds=0.2)
    trace = GazePlant(
        faults=PlantFaults(drop_observation=lambda index, _at: index >= 15),
        controller=config,
    ).run(12.0, constant_target(yaw=25.0 * _DEGREES, elevation=10.0 * _DEGREES))
    ticks = [sample for sample in trace if sample.controller_tick]
    assert any(sample.mode is ControllerMode.HOLD for sample in ticks)
    assert any(sample.mode is ControllerMode.RETURNING for sample in ticks)
    final = ticks[-1]
    assert final.mode is ControllerMode.IDLE
    for axis in (final.state.world_yaw, final.state.elevation, final.state.body_yaw):
        assert abs(axis.position) <= config.idle_position_epsilon
        assert abs(axis.velocity) <= config.idle_velocity_epsilon
        assert abs(axis.acceleration) <= config.idle_acceleration_epsilon


#:= docs/specs/gaze-control/index.md#req-089-unsafe-targets-never-reach-hardware
#:% A non-finite, malformed, non-canonical or out-of-workspace atomic target MUST
#:% emit no unsafe hardware command, retain derivative-bounded motion from the last
#:% safe command, report safe hold and require consecutive valid samples before
#:% recovery.
def test_req_089_every_safety_channel_is_stable_and_holds_last_safe() -> None:
    """Timing, pose, calibration, derivative, workspace, feedback and command are real."""
    config = ControllerConfig()
    initial = initial_controller_state(config)
    active = step_controller(
        initial, _observation(0, 0.1), now=0.1, dt=0.05, config=config
    )
    timing = step_controller(active.state, None, now=0.1, dt=0.05, config=config)
    pose = step_controller(
        active.state,
        None,
        now=0.15,
        dt=0.05,
        config=config,
        input_fault=ControllerFault.POSE,
    )
    calibration = step_controller(
        active.state,
        None,
        now=0.15,
        dt=0.05,
        config=config,
        input_fault=ControllerFault.CALIBRATION,
    )
    workspace = step_controller(
        active.state,
        None,
        now=0.15,
        dt=0.05,
        config=config,
        workspace_accepts=lambda _sample: False,
    )
    derivative_sample = replace(
        active.sample,
        world_yaw_velocity=config.yaw_limits.max_velocity * 2.0,
    )
    channels = {
        timing.state.fault,
        pose.state.fault,
        calibration.state.fault,
        workspace.state.fault,
        ControllerFault(validate_gaze_sample(derivative_sample, config).value),
        ControllerFault.BODY_FEEDBACK,
        ControllerFault.COMMAND,
    }

    assert channels == {
        ControllerFault.TIMING,
        ControllerFault.POSE,
        ControllerFault.CALIBRATION,
        ControllerFault.DERIVATIVE,
        ControllerFault.WORKSPACE,
        ControllerFault.BODY_FEEDBACK,
        ControllerFault.COMMAND,
    }
    assert all(
        result.sample == active.sample
        for result in (timing, pose, calibration, workspace)
    )


#:= docs/specs/gaze-control/index.md#req-090-tracking-ownership-is-explicit
#:% Predictive gaze MUST exclusively own the head during acquisition, tracking,
#:% loss hold, neutral return and safety clamp while leaving antenna expression
#:% independent and yielding the head once on return to idle.
def test_req_090_head_ownership_covers_lifecycle_and_safety_then_yields() -> None:
    """Lifecycle mode and independent safe hold jointly decide the contended channel."""
    behaviour = SatelliteBehaviour(now=0.0)
    for mode in (ControllerMode.ACTIVE, ControllerMode.HOLD, ControllerMode.RETURNING):
        behaviour._controller = replace(behaviour.controller_state, mode=mode)
        assert behaviour._owns_head()
    behaviour._controller = replace(
        behaviour.controller_state,
        mode=ControllerMode.IDLE,
        fault=ControllerFault.WORKSPACE,
    )
    assert behaviour._owns_head()
    behaviour._controller = replace(
        behaviour.controller_state,
        mode=ControllerMode.IDLE,
        fault=ControllerFault.NONE,
    )
    assert not behaviour._owns_head()


#:= docs/specs/gaze-control/index.md#req-091-controller-diagnostics-are-bounded-and-private
#:% The operator diagnostics surface MUST report bounded scalar controller state,
#:% observation ages, estimator transitions, limits, commands and fault categories
#:% without retaining images, face crops, credentials or installation identifiers.
def test_req_091_diagnostics_are_bounded_private_and_reset_only_the_ring() -> None:
    """Fixed schema, deterministic eviction and reset retain no forbidden payload."""
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
    snapshot = diagnostics.snapshot()
    assert len(snapshot) == 2
    assert [event["at"] for event in snapshot] == [0.05, 0.1]
    assert all(
        key not in event
        for event in snapshot
        for key in ("source", "generation", "sequence", "identity", "image", "face")
    )
    controller_before = state
    diagnostics.reset()
    assert diagnostics.snapshot() == ()
    assert state == controller_before


#:= docs/specs/gaze-control/index.md#req-092-deterministic-simulation-gates-the-controller
#:% Controller acceptance MUST be demonstrated without hardware by deterministic
#:% simulation covering step targets, sustained horizontal and vertical motion,
#:% reversal, bounded noise, observation delay and loss, cadence stalls, workspace
#:% rejection, coordinated body catch-up, feedback divergence and neutral return.
def test_req_092_cadence_and_stalls_deterministically_abort_on_any_envelope_failure() -> (
    None
):
    """Supported cadences plus one stall produce identical reruns and bounded state."""
    config = ControllerConfig()

    def cadence(index: int, _at: float) -> float:
        if index == 20:
            return config.maximum_tick_dt + 0.01
        return 0.02 if index < 20 else 0.1

    plant = GazePlant(PlantConfig(controller_interval=cadence), controller=config)
    first = plant.run(4.0, constant_target(yaw=30.0 * _DEGREES))
    second = plant.run(4.0, constant_target(yaw=30.0 * _DEGREES))
    assert first == second
    ticks = [sample for sample in first if sample.controller_tick]
    assert any(
        later.at - earlier.at > config.maximum_tick_dt
        for earlier, later in pairwise(ticks)
    )
    assert all(
        abs(sample.state.world_yaw.velocity) <= config.yaw_limits.max_velocity
        and abs(sample.state.world_yaw.acceleration)
        <= config.yaw_limits.max_acceleration
        for sample in ticks
    )
