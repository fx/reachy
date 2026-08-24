"""Pure predictive-gaze estimation, deadband, allocation and servo contracts.

Every clock reading and observation is a value chosen here. The controller is
synchronous and performs no input, output or sleeping.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from itertools import pairwise

import pytest

from reachy_contracts import FaceDetection, NormalisedPoint
from reachy_mini_ha_satellite.behaviour.gaze_controller import (
    AxisLimits,
    AxisState,
    ControllerConfig,
    ControllerMode,
    DeadbandState,
    EstimatorReset,
    EstimatorState,
    GazeObservation,
    GazeSample,
    ImagePoint,
    allocate_body,
    apply_deadband,
    initial_controller_state,
    predict_error,
    step_axis,
    step_controller,
    update_estimator,
)

_DEGREES = math.pi / 180.0


def _observation(
    *,
    source: str = "remote",
    generation: int = 0,
    sequence: int = 0,
    captured_at: float = 0.0,
    received_at: float = 0.1,
    target_key: int = 0,
    x: float = 0.4,
    y: float = 0.0,
) -> GazeObservation:
    """Build one complete face observation in the controller vocabulary."""
    return GazeObservation(
        source=source,
        generation=generation,
        sequence=sequence,
        captured_at=captured_at,
        received_at=received_at,
        target_key=target_key,
        face=FaceDetection(
            centre=NormalisedPoint(x=x, y=y),
            confidence=0.9,
        ),
    )


def _change_source(observation: GazeObservation) -> GazeObservation:
    """Start a local source stream at its first sequence."""
    return replace(observation, source="local", sequence=0)


def _change_generation(observation: GazeObservation) -> GazeObservation:
    """Start a new generation at its first sequence."""
    return replace(observation, generation=1, sequence=0)


def _change_target(observation: GazeObservation) -> GazeObservation:
    """Select a different associated target."""
    return replace(observation, target_key=1)


def _reverse_capture_time(observation: GazeObservation) -> GazeObservation:
    """Move capture behind the preceding estimator sample."""
    return replace(observation, captured_at=0.0, received_at=0.3)


def _open_supported_gap(observation: GazeObservation) -> GazeObservation:
    """Move capture beyond the estimator's supported gap."""
    return replace(observation, captured_at=1.0, received_at=1.1)


class TestImmutableValues:
    """Configuration, state and samples are frozen values rather than owners."""

    def test_config_state_and_observation_are_frozen_and_slotted(self) -> None:
        """A caller advances by replacement, never hidden mutation."""
        config = ControllerConfig()
        state = initial_controller_state(config)
        observation = _observation()

        with pytest.raises(FrozenInstanceError):
            state.mode = ControllerMode.ACTIVE  # type: ignore[misc]  # deliberately proves the public value is frozen
        with pytest.raises(FrozenInstanceError):
            config.feedback_gain = 99.0  # type: ignore[misc]  # deliberately proves the public value is frozen
        with pytest.raises(FrozenInstanceError):
            observation.sequence = 4  # type: ignore[misc]  # deliberately proves the public value is frozen
        assert not hasattr(state, "__dict__")


class TestObservationValidation:
    """Malformed timed observations are refused before arithmetic can use them."""

    def test_source_and_identity_parts_are_validated(self) -> None:
        """An identity is non-empty and never runs backwards below zero."""
        with pytest.raises(ValueError, match="source must not be empty"):
            replace(_observation(), source="")
        with pytest.raises(ValueError, match="must not be negative"):
            replace(_observation(), generation=-1)

    def test_receipt_cannot_precede_capture(self) -> None:
        """Negative observation age is not a prediction input."""
        with pytest.raises(ValueError, match="received before capture"):
            replace(_observation(), captured_at=0.2, received_at=0.1)

    def test_world_anchor_is_atomic_and_belongs_only_to_a_face(self) -> None:
        """An empty result or half an anchor cannot become a world target."""
        with pytest.raises(ValueError, match="empty observation"):
            replace(
                _observation(),
                face=None,
                world_yaw=0.0,
                world_elevation=0.0,
            )
        with pytest.raises(ValueError, match="supplied together"):
            replace(_observation(), world_yaw=0.0)


class TestObservationIdentityAndEstimation:
    """One source-qualified result changes the estimator at most once."""

    def test_replayed_identity_advances_only_the_existing_trajectory(self) -> None:
        """Faster behavior polling cannot integrate one image result repeatedly."""
        config = ControllerConfig()
        observation = _observation()
        first = step_controller(
            initial_controller_state(config),
            observation,
            now=0.1,
            dt=0.05,
            config=config,
        )
        second = step_controller(
            first.state, observation, now=0.15, dt=0.05, config=config
        )

        assert first.observation_consumed
        assert not second.observation_consumed
        assert second.state.estimator == first.state.estimator
        assert second.state.world_yaw.position != first.state.world_yaw.position

    def test_cached_remote_result_is_not_reconsumed_after_local_fallback(self) -> None:
        """Remote-to-local-to-cached-remote switching consumes each identity once."""
        config = ControllerConfig()
        remote = _observation(source="remote", generation=0, sequence=7)
        local = _observation(
            source="local",
            generation=0,
            sequence=3,
            captured_at=0.1,
            received_at=0.2,
            x=-0.3,
        )
        first = step_controller(
            initial_controller_state(config),
            remote,
            now=0.1,
            dt=0.05,
            config=config,
        )
        fallback = step_controller(
            first.state,
            local,
            now=0.2,
            dt=0.05,
            config=config,
        )
        resurfaced = step_controller(
            fallback.state,
            remote,
            now=0.25,
            dt=0.05,
            config=config,
        )

        assert first.observation_consumed
        assert fallback.observation_consumed
        assert not resurfaced.observation_consumed
        assert resurfaced.state.estimator == fallback.state.estimator

    def test_watermarks_stay_bounded_by_source_and_reject_old_generations(self) -> None:
        """Reconnect history costs one fixed watermark per detection source."""
        config = ControllerConfig()
        state = initial_controller_state(config)
        now = 0.1
        for generation in range(100):
            for source in ("remote", "local"):
                result = step_controller(
                    state,
                    _observation(
                        source=source,
                        generation=generation,
                        sequence=10,
                        captured_at=now - 0.05,
                        received_at=now,
                    ),
                    now=now,
                    dt=0.05,
                    config=config,
                )
                assert result.observation_consumed
                state = result.state
                now += 0.1

        watermarks = state.consumption_watermarks
        assert len(watermarks) == 2
        assert set(watermarks) == {
            ("remote", 99, 10),
            ("local", 99, 10),
        }

        for source in ("remote", "local"):
            replayed = step_controller(
                state,
                _observation(
                    source=source,
                    generation=50,
                    sequence=999,
                    captured_at=now - 0.05,
                    received_at=now,
                ),
                now=now,
                dt=0.05,
                config=config,
            )
            assert not replayed.observation_consumed
            assert replayed.state.consumption_watermarks == watermarks
            state = replayed.state
            now += 0.1

    def test_fresh_empty_result_is_consumed_once_as_explicit_loss(self) -> None:
        """Nobody seen is an observation, not a missing or stale turn."""
        config = ControllerConfig()
        active = step_controller(
            initial_controller_state(config),
            _observation(),
            now=0.1,
            dt=0.05,
            config=config,
        )
        empty = replace(
            _observation(sequence=1, captured_at=0.1, received_at=0.2),
            face=None,
        )
        lost = step_controller(
            active.state,
            empty,
            now=0.2,
            dt=0.05,
            config=config,
        )
        replayed = step_controller(
            lost.state,
            empty,
            now=0.25,
            dt=0.05,
            config=config,
        )

        assert lost.observation_consumed
        assert lost.mode is ControllerMode.HOLD
        assert not replayed.observation_consumed

    def test_reacquisition_after_fresh_empty_starts_with_zero_velocity(self) -> None:
        """An association loss breaks estimator continuity even inside its gap."""
        config = ControllerConfig(estimator_gap=0.5)
        first = step_controller(
            initial_controller_state(config),
            _observation(x=0.1),
            now=0.1,
            dt=0.05,
            config=config,
        )
        moving = step_controller(
            first.state,
            _observation(
                sequence=1,
                captured_at=0.1,
                received_at=0.2,
                x=0.5,
            ),
            now=0.2,
            dt=0.05,
            config=config,
        )
        assert moving.state.estimator is not None
        assert moving.state.estimator.velocity.x != 0.0
        empty = step_controller(
            moving.state,
            replace(
                _observation(sequence=2, captured_at=0.2, received_at=0.3),
                face=None,
            ),
            now=0.3,
            dt=0.05,
            config=config,
        )
        reacquired = step_controller(
            empty.state,
            _observation(
                sequence=3,
                captured_at=0.3,
                received_at=0.4,
                x=0.6,
            ),
            now=0.4,
            dt=0.05,
            config=config,
        )

        assert reacquired.observation_consumed
        assert reacquired.estimator_reset is EstimatorReset.FIRST
        assert reacquired.state.estimator is not None
        assert reacquired.state.estimator.velocity == ImagePoint(0.0, 0.0)

    @pytest.mark.parametrize(
        ("dt", "before"),
        [
            (0.001, AxisState(position=0.24 * _DEGREES)),
            (
                0.02,
                AxisState(
                    position=0.1 * _DEGREES,
                    velocity=0.3 * _DEGREES,
                    acceleration=15.0 * _DEGREES,
                ),
            ),
        ],
    )
    def test_idle_transition_preserves_near_threshold_trajectory_continuity(
        self,
        dt: float,
        before: AxisState,
    ) -> None:
        """Settling to IDLE cannot snap bounded residual trajectory state to zero."""
        config = ControllerConfig()
        state = replace(
            initial_controller_state(config),
            mode=ControllerMode.RETURNING,
            world_yaw=before,
            target_visible=False,
            loss_started_at=0.0,
            last_safe_sample=GazeSample(
                world_yaw=before.position,
                elevation=0.0,
                body_yaw=0.0,
                head_yaw=before.position,
                body_enabled=False,
            ),
        )

        settled = step_controller(
            state,
            None,
            now=1.0,
            dt=dt,
            config=config,
        )
        after = settled.state.world_yaw

        assert settled.mode is ControllerMode.IDLE
        assert (
            abs(after.position - before.position) <= config.yaw_limits.max_velocity * dt
        )
        assert (
            abs(after.velocity - before.velocity)
            <= config.yaw_limits.max_acceleration * dt
        )
        assert (
            abs(after.acceleration - before.acceleration)
            <= config.yaw_limits.max_jerk * dt
        )
        assert after.position >= -1.0 * _DEGREES
        assert settled.sample.world_yaw == after.position

    def test_settled_loss_remains_idle_without_new_observations(self) -> None:
        """Idle after empty-face return cannot restart its own loss lifecycle."""
        config = ControllerConfig(loss_hold_seconds=0.1)
        observation = replace(
            _observation(),
            world_yaw=25.0 * _DEGREES,
            world_elevation=10.0 * _DEGREES,
        )
        now = 0.1
        result = step_controller(
            initial_controller_state(config),
            observation,
            now=now,
            dt=0.05,
            config=config,
        )
        for _ in range(30):
            now += 0.05
            result = step_controller(
                result.state,
                observation,
                now=now,
                dt=0.05,
                config=config,
            )
        assert result.sample.world_yaw != 0.0

        now += 0.05
        result = step_controller(
            result.state,
            replace(
                _observation(
                    sequence=1,
                    captured_at=now - 0.05,
                    received_at=now,
                ),
                face=None,
            ),
            now=now,
            dt=0.05,
            config=config,
        )
        for _ in range(400):
            if result.mode is ControllerMode.IDLE:
                break
            now += 0.05
            result = step_controller(
                result.state,
                None,
                now=now,
                dt=0.05,
                config=config,
            )
        assert result.mode is ControllerMode.IDLE

        axes = (
            ("world_yaw", config.yaw_limits),
            ("elevation", config.elevation_limits),
            ("body_yaw", config.body_limits),
        )
        for _ in range(8):
            before = result.state
            now += 0.05
            result = step_controller(
                before,
                None,
                now=now,
                dt=0.05,
                config=config,
            )
            assert result.mode is ControllerMode.IDLE
            for name, limits in axes:
                previous_axis = getattr(before, name)
                axis = getattr(result.state, name)
                assert abs(axis.position) <= 1.0 * _DEGREES
                assert abs(axis.velocity) <= limits.max_velocity
                assert abs(axis.acceleration) <= limits.max_acceleration
                assert (
                    abs(axis.position - previous_axis.position)
                    <= limits.max_velocity * 0.05
                )
                assert (
                    abs(axis.velocity - previous_axis.velocity)
                    <= limits.max_acceleration * 0.05
                )
                assert (
                    abs(axis.acceleration - previous_axis.acceleration)
                    <= limits.max_jerk * 0.05
                )
            assert result.sample.world_yaw == result.state.world_yaw.position
            assert result.sample.elevation == result.state.elevation.position
            assert result.sample.body_yaw == result.state.body_yaw.position

    @pytest.mark.parametrize(
        ("change", "expected"),
        [
            (_change_source, EstimatorReset.SOURCE),
            (_change_generation, EstimatorReset.GENERATION),
            (_change_target, EstimatorReset.TARGET),
            (_reverse_capture_time, EstimatorReset.TIME_ORDER),
            (_open_supported_gap, EstimatorReset.GAP),
        ],
    )
    def test_every_stream_discontinuity_resets_velocity(
        self,
        change: Callable[[GazeObservation], GazeObservation],
        expected: EstimatorReset,
    ) -> None:
        """No old target velocity crosses a source, target or time boundary."""
        config = ControllerConfig(estimator_gap=0.5)
        first, _reset = update_estimator(None, _observation(), config)
        moving, _reset = update_estimator(
            first,
            _observation(sequence=1, captured_at=0.1, received_at=0.2, x=0.6),
            config,
        )
        assert moving.velocity.x != 0.0
        next_observation = change(
            _observation(sequence=2, captured_at=0.2, received_at=0.3, x=-0.4)
        )

        reset, reason = update_estimator(moving, next_observation, config)

        assert reason is expected
        assert reset.velocity == ImagePoint(0.0, 0.0)

    def test_horizontal_and_vertical_velocity_are_estimated_independently(self) -> None:
        """Pure motion on one image axis cannot borrow activity from the other."""
        config = ControllerConfig()
        first, _reset = update_estimator(None, _observation(x=0.1, y=0.2), config)
        second, _reset = update_estimator(
            first,
            _observation(sequence=1, captured_at=0.1, received_at=0.2, x=0.4, y=0.2),
            config,
        )

        assert second.velocity.x > 0.0
        assert second.velocity.y == pytest.approx(0.0)


class TestPredictionAndStaleness:
    """Capture-age prediction and receipt freshness are separate decisions."""

    def test_prediction_bounds_velocity_position_and_capture_horizon(self) -> None:
        """A late fast estimate cannot extrapolate without limit."""
        config = ControllerConfig(
            prediction_horizon=0.35,
            image_position_limit=1.5,
            image_velocity_limit=2.0,
        )
        estimator = EstimatorState(
            identity=("remote", 0, 2),
            target_key=0,
            measured=ImagePoint(1.0, -1.0),
            position=ImagePoint(1.4, -1.4),
            velocity=ImagePoint(2.0, -2.0),
            captured_at=0.0,
            received_at=0.8,
        )

        predicted, horizon = predict_error(estimator, now=1.0, config=config)

        assert horizon == pytest.approx(0.35)
        assert predicted == ImagePoint(1.5, -1.5)

    def test_late_but_fresh_result_tracks_with_clamped_prediction(self) -> None:
        """Capture age beyond the horizon is not receipt-time loss."""
        config = ControllerConfig(prediction_horizon=0.2, staleness_seconds=2.0)
        result = step_controller(
            initial_controller_state(config),
            _observation(captured_at=0.0, received_at=0.9, x=0.5),
            now=1.0,
            dt=0.05,
            config=config,
        )

        assert result.mode is ControllerMode.ACTIVE
        assert result.prediction_horizon == pytest.approx(0.2)
        assert not result.stale

    def test_receipt_age_enters_loss_even_when_capture_prediction_is_bounded(
        self,
    ) -> None:
        """No extrapolation clamp can keep an old receipt live forever."""
        config = ControllerConfig(staleness_seconds=0.5)
        active = step_controller(
            initial_controller_state(config),
            _observation(captured_at=0.0, received_at=0.1),
            now=0.1,
            dt=0.05,
            config=config,
        )

        stale = step_controller(active.state, None, now=0.6, dt=0.05, config=config)

        assert stale.stale
        assert stale.mode in {ControllerMode.HOLD, ControllerMode.RETURNING}


class TestDeadbandAndServo:
    """Raw-error hysteresis is continuous and every trajectory derivative is bounded."""

    def test_raw_error_alone_controls_activation(self) -> None:
        """Predicted velocity cannot activate centered detector noise."""
        config = ControllerConfig()
        predicted = ImagePoint(0.8, 0.0)
        filtered, state = apply_deadband(
            predicted,
            activation=ImagePoint(0.0, 0.0),
            state=DeadbandState(),
            config=config,
        )

        assert not state.active
        assert filtered == ImagePoint(0.0, 0.0)

    def test_hysteresis_retains_mid_band_state_and_releases_continuously(self) -> None:
        """The same raw radius differs before and after crossing the start edge."""
        config = ControllerConfig(deadband_start=1.1, deadband_stop=0.7)
        mid = ImagePoint(config.deadband_x * 0.9, 0.0)
        _filtered, inactive = apply_deadband(
            mid, activation=mid, state=DeadbandState(), config=config
        )
        _filtered, started = apply_deadband(
            ImagePoint(config.deadband_x * 1.2, 0.0),
            activation=ImagePoint(config.deadband_x * 1.2, 0.0),
            state=inactive,
            config=config,
        )
        retained, active = apply_deadband(
            mid, activation=mid, state=started, config=config
        )
        edge = ImagePoint(config.deadband_x * config.deadband_stop, 0.0)
        released_output, released = apply_deadband(
            edge, activation=edge, state=active, config=config
        )

        assert not inactive.active
        assert started.active
        assert active.active
        assert retained.x > 0.0
        assert not released.active
        assert released_output == ImagePoint(0.0, 0.0)

    def test_reversal_respects_position_velocity_acceleration_and_jerk(self) -> None:
        """A sign change crosses bounded derivatives without a position jump."""
        limits = AxisLimits(
            minimum=-1.0,
            maximum=1.0,
            max_velocity=0.8,
            max_acceleration=1.5,
            max_jerk=4.0,
        )
        state = AxisState()
        states = [state]
        for tick in range(120):
            state = step_axis(state, 0.8 if tick < 50 else -0.8, 0.02, limits)
            states.append(state)

        assert all(limits.minimum <= item.position <= limits.maximum for item in states)
        assert all(abs(item.velocity) <= limits.max_velocity for item in states)
        assert all(abs(item.acceleration) <= limits.max_acceleration for item in states)
        assert all(
            abs(later.acceleration - earlier.acceleration) / 0.02
            <= limits.max_jerk + 1e-12
            for earlier, later in pairwise(states)
        )

    @pytest.mark.parametrize("acceleration", [-1.5, 1.5])
    def test_crossing_stall_threshold_preserves_acceleration_jerk(
        self,
        acceleration: float,
    ) -> None:
        """A tiny threshold crossing cannot reset either acceleration sign."""
        limits = AxisLimits(
            minimum=-2.0,
            maximum=2.0,
            max_velocity=1.0,
            max_acceleration=2.0,
            max_jerk=4.0,
        )
        state = AxisState(
            position=0.0,
            velocity=math.copysign(0.4, acceleration),
            acceleration=acceleration,
        )
        dt = 0.200001

        advanced = step_axis(
            state,
            velocity_goal=0.0,
            dt=dt,
            limits=limits,
            maximum_dt=0.2,
            stall_dt=0.05,
        )

        assert abs(advanced.acceleration - state.acceleration) <= limits.max_jerk * dt


class TestAllocationAndWorkspaceScaffolds:
    """Body output stays off while its pure bounded allocation is exercised."""

    def test_body_is_disabled_by_default(self) -> None:
        """No foundation-only calculation can move the released runtime body."""
        config = ControllerConfig()

        assert not config.body_enabled
        assert allocate_body(40.0 * _DEGREES, config) == 0.0

    def test_enabled_allocation_is_continuous_odd_symmetric_and_monotonic(self) -> None:
        """The scaffold has no threshold jump or directional bias."""
        config = replace(ControllerConfig(), body_enabled=True)
        magnitudes = [index * 0.1 * _DEGREES for index in range(701)]
        positive = [allocate_body(value, config) for value in magnitudes]

        assert all(later >= earlier for earlier, later in pairwise(positive))
        for value, allocated in zip(magnitudes, positive, strict=True):
            assert allocate_body(-value, config) == pytest.approx(-allocated)
        for knot in (
            config.body_noise_floor,
            config.body_midpoint,
            config.body_large_point,
        ):
            left = allocate_body(knot - 1e-9, config)
            exact = allocate_body(knot, config)
            right = allocate_body(knot + 1e-9, config)
            assert left == pytest.approx(exact, abs=1e-8)
            assert right == pytest.approx(exact, abs=1e-8)

    def test_workspace_validates_candidate_before_accepting_it(self) -> None:
        """An old valid sample cannot admit a newly unsafe candidate."""
        config = ControllerConfig()
        neutral = initial_controller_state(config)
        checked: list[float] = []

        def only_neutral(sample: GazeSample) -> bool:
            checked.append(sample.world_yaw)
            return math.isclose(sample.world_yaw, 0.0, abs_tol=1e-15)

        rejected = step_controller(
            neutral,
            _observation(),
            now=0.1,
            dt=0.05,
            config=config,
            workspace_accepts=only_neutral,
        )

        assert checked
        assert checked[0] != 0.0
        assert rejected.mode is ControllerMode.WORKSPACE_HOLD
        assert rejected.sample == neutral.last_safe_sample
        assert rejected.state.last_safe_sample == neutral.last_safe_sample

    def test_workspace_recovery_counts_each_validated_candidate(self) -> None:
        """Only consecutive independently checked candidates satisfy recovery."""
        config = ControllerConfig(workspace_recovery_samples=2)
        verdicts = iter([False, True, False, True, True])
        checked: list[float] = []

        def scripted_workspace(sample: GazeSample) -> bool:
            checked.append(sample.world_yaw)
            return next(verdicts)

        state = initial_controller_state(config)
        results = []
        for index in range(5):
            result = step_controller(
                state,
                _observation() if index == 0 else None,
                now=0.1 + index * 0.05,
                dt=0.05,
                config=config,
                workspace_accepts=scripted_workspace,
            )
            results.append(result)
            state = result.state

        assert len(checked) == 5
        assert [result.workspace_accepted for result in results] == [
            False,
            False,
            False,
            False,
            True,
        ]
        assert results[-1].mode is not ControllerMode.WORKSPACE_HOLD
