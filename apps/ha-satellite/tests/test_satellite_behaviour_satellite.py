"""Two-phase predictive gaze arbitration with independent pipeline expression."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from satellite_support import face

from reachy_mini_ha_satellite.behaviour.gaze_controller import (
    BodyMeasurement,
    ControllerConfig,
    ControllerMode,
)
from reachy_mini_ha_satellite.behaviour.intents import (
    CommandGaze,
    MoveAntennas,
    MoveHead,
)
from reachy_mini_ha_satellite.behaviour.pipeline import PipelineEvent, PipelineState
from reachy_mini_ha_satellite.behaviour.satellite import SatelliteBehaviour
from reachy_mini_ha_satellite.behaviour.tracking import GazeOutcome
from reachy_mini_ha_satellite.ports import CalibratedGaze, Detections, DetectionSource

if TYPE_CHECKING:
    from reachy_contracts import FaceDetection
    from reachy_mini_ha_satellite.behaviour.intents import MotionIntent
    from reachy_mini_ha_satellite.behaviour.satellite import PreparedGazeTick


def _seen(
    *faces: FaceDetection,
    sequence: int = 1,
    captured_at: float = 0.0,
    received_at: float = 0.1,
    fresh: bool = True,
) -> Detections:
    """Build one source-qualified result."""
    return Detections(
        faces=faces,
        fresh=fresh,
        source=DetectionSource.REMOTE,
        age_seconds=max(0.0, received_at - captured_at),
        generation=0,
        sequence=sequence,
        captured_at=captured_at,
        received_at=received_at,
    )


def _calibrated(prepared: PreparedGazeTick, yaw: float = 0.4) -> CalibratedGaze:
    """Calibrate one actionable prepared directive to a chosen world target."""
    directive = prepared.directive
    assert directive.identity is not None
    assert directive.captured_at is not None
    assert directive.received_at is not None
    source, generation, sequence = directive.identity
    return CalibratedGaze(
        source=source,
        generation=generation,
        sequence=sequence,
        captured_at=directive.captured_at,
        received_at=directive.received_at,
        target_epoch=directive.target_epoch,
        world_yaw=yaw,
        world_elevation=0.1,
    )


def _wrong_epoch(calibrated: CalibratedGaze) -> CalibratedGaze:
    """Return calibration for another associated target."""
    return replace(calibrated, target_epoch=calibrated.target_epoch + 1)


def _wrong_capture(calibrated: CalibratedGaze) -> CalibratedGaze:
    """Return calibration for another capture time."""
    return replace(calibrated, captured_at=calibrated.captured_at + 0.01)


def _wrong_receipt(calibrated: CalibratedGaze) -> CalibratedGaze:
    """Return calibration for another receipt time."""
    return replace(calibrated, received_at=calibrated.received_at + 0.01)


def _finish(
    behaviour: SatelliteBehaviour,
    detections: Detections,
    *,
    now: float,
    dt: float = 0.05,
    yaw: float = 0.4,
    body: BodyMeasurement | None = None,
) -> tuple[MotionIntent, ...]:
    """Run both pure phases with calibration for an actionable face."""
    prepared = behaviour.prepare(detections, now)
    target = _calibrated(prepared, yaw) if prepared.directive.face is not None else None
    return behaviour.finish(
        prepared,
        calibrated=target,
        body_measurement=body,
        dt=dt,
    )


def _gaze(intents: tuple[MotionIntent, ...]) -> CommandGaze | None:
    """Return a coordinated gaze command from one tick, if present."""
    return next((item for item in intents if isinstance(item, CommandGaze)), None)


def _head(intents: tuple[MotionIntent, ...]) -> MoveHead | None:
    """Return a pipeline head handoff from one tick, if present."""
    return next((item for item in intents if isinstance(item, MoveHead)), None)


def _antennas(intents: tuple[MotionIntent, ...]) -> MoveAntennas | None:
    """Return an antenna expression from one tick, if present."""
    return next((item for item in intents if isinstance(item, MoveAntennas)), None)


class TestPipelineExpression:
    """Pipeline state and antennas continue independently of gaze ownership."""

    @pytest.mark.parametrize(
        ("event", "state"),
        [
            (PipelineEvent.LISTENING, PipelineState.LISTENING),
            (PipelineEvent.PROCESSING, PipelineState.PROCESSING),
            (PipelineEvent.RESPONDING, PipelineState.RESPONDING),
            (PipelineEvent.ERROR, PipelineState.ERROR),
        ],
    )
    def test_pipeline_transition_is_immediately_visible(
        self,
        event: PipelineEvent,
        state: PipelineState,
    ) -> None:
        """Entering a state commands its expression without waiting for a tick."""
        behaviour = SatelliteBehaviour(now=0.0)

        intents = behaviour.handle(event, 0.1)

        assert behaviour.state is state
        assert _antennas(intents) is not None
        assert _head(intents) is not None

    def test_pipeline_head_is_suppressed_while_gaze_owns_it(self) -> None:
        """State still changes and antennas move, but no competing head is emitted."""
        behaviour = SatelliteBehaviour(now=0.0)
        _finish(behaviour, _seen(face(0.4, 0.0)), now=0.1)

        intents = behaviour.handle(PipelineEvent.PROCESSING, 0.2)

        assert behaviour.state is PipelineState.PROCESSING
        assert _antennas(intents) is not None
        assert _head(intents) is None


class TestTwoPhasePredictiveGaze:
    """Selection is pure, calibration is supplied, and controller motion stays pure."""

    def test_metadata_free_face_never_activates_predictive_motion(self) -> None:
        """Legacy construction remains accepted but carries no actionable identity."""
        behaviour = SatelliteBehaviour(now=0.0)
        detections = Detections(faces=(face(0.7, 0.0),), fresh=True)

        intents = _finish(behaviour, detections, now=0.1)

        assert _gaze(intents) is None
        assert behaviour.status(0.1).outcome is GazeOutcome.UNKNOWN

    def test_qualified_face_owns_head_and_preserves_antennas(self) -> None:
        """A calibrated face emits gaze while pipeline head remains suppressed."""
        behaviour = SatelliteBehaviour(now=0.0)
        behaviour.handle(PipelineEvent.PROCESSING, 0.0)

        intents = _finish(behaviour, _seen(face(0.5, 0.1)), now=0.1)

        assert _gaze(intents) is not None
        assert _head(intents) is None
        assert _antennas(intents) is not None
        assert behaviour.status(0.1).tracking

    def test_cached_observation_advances_trajectory_without_new_identity(self) -> None:
        """Repeated reads retain estimator identity while jerk-limited q advances."""
        behaviour = SatelliteBehaviour(now=0.0)
        detections = _seen(face(0.5, 0.0))

        first = _gaze(_finish(behaviour, detections, now=0.1))
        second = _gaze(_finish(behaviour, detections, now=0.15))

        assert first is not None
        assert second is not None
        assert second.sample.world_yaw != first.sample.world_yaw

    def test_calibration_failure_does_not_activate_a_never_observed_controller(
        self,
    ) -> None:
        """A rejected adapter result leaves pipeline head ownership unchanged."""
        behaviour = SatelliteBehaviour(now=0.0)
        prepared = behaviour.prepare(_seen(face(0.6, 0.0)), 0.1)

        intents = behaviour.finish(
            prepared,
            calibrated=None,
            body_measurement=None,
            dt=0.05,
        )

        assert _gaze(intents) is None
        assert _head(intents) is not None

    @pytest.mark.parametrize(
        "change",
        [_wrong_epoch, _wrong_capture, _wrong_receipt],
        ids=["target", "capture", "receipt"],
    )
    def test_calibration_must_match_the_complete_prepared_directive(
        self,
        change: Callable[[CalibratedGaze], CalibratedGaze],
    ) -> None:
        """Stale adapter output cannot be joined to a different target or time."""
        behaviour = SatelliteBehaviour(now=0.0)
        prepared = behaviour.prepare(_seen(face(0.6, 0.0)), 0.1)
        mismatched = change(_calibrated(prepared))

        intents = behaviour.finish(
            prepared,
            calibrated=mismatched,
            body_measurement=None,
            dt=0.05,
        )

        assert _gaze(intents) is None
        assert _head(intents) is not None

    def test_body_measurement_is_forwarded_to_the_pure_controller(self) -> None:
        """The first valid body sample initializes commanded body state once."""
        config = replace(ControllerConfig(), body_enabled=True)
        behaviour = SatelliteBehaviour(controller_config=config, now=0.0)

        gaze = _gaze(
            _finish(
                behaviour,
                _seen(face(0.5, 0.0)),
                now=0.1,
                body=BodyMeasurement(yaw=0.2, measured_at=0.1),
            )
        )

        assert gaze is not None
        assert gaze.sample.body_enabled
        assert behaviour.controller_state.body_feedback.initialized


class TestLossReturnAndHandoff:
    """Loss retains ownership through bounded return, then yields exactly once."""

    def test_explicit_empty_returns_and_hands_current_pipeline_head_once(self) -> None:
        """Final gaze precedes one current processing head on first settled idle."""
        config = ControllerConfig(loss_hold_seconds=0.0)
        behaviour = SatelliteBehaviour(controller_config=config, now=0.0)
        behaviour.handle(PipelineEvent.PROCESSING, 0.0)
        detections = _seen(face(0.6, 0.0))
        now = 0.1
        for _ in range(30):
            _finish(behaviour, detections, now=now)
            now += 0.05
        empty = _seen(
            sequence=2,
            captured_at=now - 0.05,
            received_at=now,
        )

        handoffs = 0
        settled: tuple[MotionIntent, ...] = ()
        for _ in range(500):
            intents = _finish(behaviour, empty, now=now)
            handoffs += sum(isinstance(item, MoveHead) for item in intents)
            if behaviour.controller_state.mode is ControllerMode.IDLE:
                settled = intents
                break
            now += 0.05

        assert behaviour.controller_state.mode is ControllerMode.IDLE
        assert isinstance(settled[0], CommandGaze)
        assert any(isinstance(item, MoveHead) for item in settled[1:])
        assert handoffs == 1
        repeated = _finish(behaviour, empty, now=now + 0.05)
        assert _gaze(repeated) is None

    def test_reacquisition_during_return_cancels_handoff(self) -> None:
        """A new calibrated face keeps ownership before pipeline head can resume."""
        config = ControllerConfig(loss_hold_seconds=0.0)
        behaviour = SatelliteBehaviour(controller_config=config, now=0.0)
        active = _seen(face(0.6, 0.0))
        now = 0.1
        for _ in range(20):
            _finish(behaviour, active, now=now)
            now += 0.05
        empty = _seen(sequence=2, captured_at=now - 0.05, received_at=now)
        _finish(behaviour, empty, now=now)
        now += 0.05
        reacquired = _seen(
            face(-0.5, 0.0),
            sequence=3,
            captured_at=now - 0.05,
            received_at=now,
        )

        intents = _finish(behaviour, reacquired, now=now, yaw=-0.4)

        assert _gaze(intents) is not None
        assert _head(intents) is None
        assert behaviour.controller_state.mode is ControllerMode.ACTIVE

    def test_stale_input_reports_stale_while_controller_returns(self) -> None:
        """Receipt staleness starts hold/return without replaying image data."""
        config = ControllerConfig(staleness_seconds=0.2, loss_hold_seconds=0.0)
        behaviour = SatelliteBehaviour(controller_config=config, now=0.0)
        active = _seen(face(0.4, 0.0), received_at=0.1)
        _finish(behaviour, active, now=0.1)
        stale = replace(active, fresh=False, faces=(), age_seconds=0.3)

        intents = _finish(behaviour, stale, now=0.4)

        assert behaviour.status(0.4).outcome is GazeOutcome.STALE
        assert behaviour.controller_state.mode is ControllerMode.RETURNING
        assert _gaze(intents) is not None


class TestConfigurationAndIdleExpression:
    """Restart-bound tracking and live idle timing remain separate concerns."""

    def test_tracking_disabled_leaves_head_to_pipeline(self) -> None:
        """No detector result can claim a controller disabled at construction."""
        behaviour = SatelliteBehaviour(tracking_enabled=False, now=0.0)

        intents = _finish(behaviour, _seen(face(0.5, 0.0)), now=0.1)

        assert _gaze(intents) is None
        assert _head(intents) is not None

    def test_idle_interval_can_retune_without_resetting_pipeline(self) -> None:
        """The one live behavior value changes without rebuilding state."""
        behaviour = SatelliteBehaviour(now=0.0)
        behaviour.handle(PipelineEvent.PROCESSING, 0.1)

        behaviour.retune(idle_seconds=10.0)

        assert behaviour.state is PipelineState.PROCESSING

    def test_idle_sway_still_moves_antennas(self) -> None:
        """Predictive head integration does not remove independent idle character."""
        behaviour = SatelliteBehaviour(idle_seconds=0.1, now=0.0)
        empty = _seen(sequence=1, captured_at=0.0, received_at=0.0)
        _finish(behaviour, empty, now=0.0)

        moved = [
            _antennas(_finish(behaviour, empty, now=0.15 + index * 0.1))
            for index in range(5)
        ]

        assert any(intent is not None for intent in moved)
