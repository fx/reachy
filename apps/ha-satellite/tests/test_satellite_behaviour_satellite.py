"""Events and detections in, motion intents out — the whole of the decision layer.

This is where the two pure pieces meet and where the one decision neither can
take on its own is taken: **who owns the head**. Face tracking wins it whenever
there is a face, so the pipeline's own head movements are what happens in the
head's spare time, and the antennas — never contended for — are what carry the
distinguishable-movement requirement.

Everything below runs on numbers a test chooses. There is no clock, no sleep and
no port: `SatelliteBehaviour` is handed a `Detections` value and hands back
intents, which is ha-satellite REQ-042 in the shape a test can hold.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import pytest
from satellite_support import face

from reachy_mini_ha_satellite.behaviour import (
    GazeOutcome,
    LookAhead,
    LookAt,
    MoveAntennas,
    MoveHead,
    PipelineEvent,
    PipelineState,
    SatelliteBehaviour,
)
from reachy_mini_ha_satellite.ports import Detections, DetectionSource

if TYPE_CHECKING:
    from reachy_contracts import FaceDetection
    from reachy_mini_ha_satellite.behaviour import MotionIntent

NOTHING_SEEN_YET = Detections()

# Which detector answered, for the stale view below. Bound once rather than
# spelled inline, because this repository's leak scanner reads the dotted form as
# an mDNS hostname suffix — the same reason
# `adapters/perception_source.py` binds it.
_ON_THE_ROBOT: Final = DetectionSource.LOCAL  # leak-scan:allow


def _seen(*faces: FaceDetection) -> Detections:
    """Build a fresh view carrying these faces.

    Args:
        faces: What the detector reported.

    Returns:
        The view.
    """
    return Detections(
        faces=faces,
        fresh=True,
        source=DetectionSource.REMOTE,
        age_seconds=0.0,
    )


def _stale() -> Detections:
    """Build the view the staleness window produces.

    Returns:
        A view that is not fresh but names the source that had been answering.
    """
    return Detections(fresh=False, source=_ON_THE_ROBOT, age_seconds=99.0)


def _antennas(intents: tuple[MotionIntent, ...]) -> MoveAntennas | None:
    """Pick the antenna command out of a tick's intents.

    Args:
        intents: What the behaviour layer asked for.

    Returns:
        The command, or `None` when the tick asked for none.
    """
    for intent in intents:
        if isinstance(intent, MoveAntennas):
            return intent
    return None


def _heads(intents: tuple[MotionIntent, ...]) -> MoveHead | None:
    """Pick the head command out of a tick's intents.

    Args:
        intents: What the behaviour layer asked for.

    Returns:
        The command, or `None` when the tick asked for none.
    """
    for intent in intents:
        if isinstance(intent, MoveHead):
            return intent
    return None


class TestAPipelineEventIsImmediatelyVisible:
    """REQ-046 is about a person in the room, so the movement cannot wait."""

    @pytest.mark.parametrize(
        ("event", "state"),
        [
            (PipelineEvent.WAKE_WORD_DETECTED, PipelineState.LISTENING),
            (PipelineEvent.PROCESSING, PipelineState.PROCESSING),
            (PipelineEvent.RESPONDING, PipelineState.RESPONDING),
            (PipelineEvent.ERROR, PipelineState.ERROR),
            (PipelineEvent.MUTED, PipelineState.MUTED),
            (PipelineEvent.DISCONNECTED, PipelineState.DISCONNECTED),
        ],
    )
    def test_entering_a_state_commands_a_movement_at_once(
        self,
        event: PipelineEvent,
        state: PipelineState,
    ) -> None:
        """Rather than at whatever point an animation crosses a threshold.

        Args:
            event: What arrives.
            state: What it puts the machine in.
        """
        behaviour = SatelliteBehaviour(now=0.0)

        intents = behaviour.handle(event, 1.0)

        assert behaviour.state is state
        assert _antennas(intents) is not None

    def test_an_event_that_changes_nothing_commands_nothing(self) -> None:
        """A re-sent `listening` does not restart the listening movement."""
        behaviour = SatelliteBehaviour(now=0.0)
        behaviour.handle(PipelineEvent.LISTENING, 1.0)

        assert behaviour.handle(PipelineEvent.LISTENING, 2.0) == ()

    def test_the_three_states_command_three_different_antenna_poses(self) -> None:
        """The requirement, taken through the layer that actually issues them."""
        poses = []
        for event in (
            PipelineEvent.LISTENING,
            PipelineEvent.PROCESSING,
            PipelineEvent.RESPONDING,
        ):
            behaviour = SatelliteBehaviour(now=0.0)
            commanded = _antennas(behaviour.handle(event, 0.0))
            assert commanded is not None
            poses.append((commanded.pose.left, commanded.pose.right))

        assert len(set(poses)) == len(poses)


class TestWhoOwnsTheHead:
    """Face tracking wins it; the pipeline gets it when nobody is in frame."""

    def test_a_visible_face_takes_the_head(self) -> None:
        """And the pipeline's own head pose is not commanded over it."""
        behaviour = SatelliteBehaviour(now=0.0)
        behaviour.handle(PipelineEvent.PROCESSING, 0.0)

        intents = behaviour.tick(_seen(face(0.5, 0.1)), 1.0)

        assert any(isinstance(intent, LookAt) for intent in intents)
        assert _heads(intents) is None

    def test_the_antennas_still_express_the_state_while_tracking(self) -> None:
        """Which is why the antennas rather than the head carry REQ-046."""
        behaviour = SatelliteBehaviour(now=0.0)
        behaviour.handle(PipelineEvent.PROCESSING, 0.0)

        intents = behaviour.tick(_seen(face(0.5, 0.1)), 0.5)

        assert _antennas(intents) is not None

    def test_with_nobody_in_frame_the_pipeline_moves_the_head(self) -> None:
        """The secondary channel, which only runs when the primary is free."""
        behaviour = SatelliteBehaviour(now=0.0)
        behaviour.handle(PipelineEvent.PROCESSING, 0.0)

        intents = behaviour.tick(_seen(), 0.3)

        assert _heads(intents) is not None

    def test_tracking_switched_off_leaves_the_head_to_the_pipeline(self) -> None:
        """The configuration for a robot with no detector at all."""
        behaviour = SatelliteBehaviour(tracking_enabled=False, now=0.0)
        behaviour.handle(PipelineEvent.PROCESSING, 0.0)

        intents = behaviour.tick(_seen(face(0.5, 0.0)), 0.3)

        assert not any(isinstance(intent, LookAt) for intent in intents)
        assert _heads(intents) is not None


class TestStalenessAndEmptiness:
    """Two ways of ending at neutral, and one way of not going there."""

    def test_stale_results_return_the_head_to_neutral(self) -> None:
        """ha-satellite REQ-048, at the layer that issues the command."""
        behaviour = SatelliteBehaviour(now=0.0)
        behaviour.tick(_seen(face(0.6, 0.0)), 1.0)

        intents = behaviour.tick(_stale(), 2.0)

        assert any(isinstance(intent, LookAhead) for intent in intents)

    def test_returning_to_neutral_is_not_followed_by_a_second_head_command(
        self,
    ) -> None:
        """`look_ahead` already put it there; saying so twice is motor traffic."""
        behaviour = SatelliteBehaviour(now=0.0)
        behaviour.tick(_seen(face(0.6, 0.0)), 1.0)

        intents = behaviour.tick(_stale(), 2.0)

        assert _heads(intents) is None

    def test_nothing_produced_yet_never_commands_the_head_to_neutral(self) -> None:
        """The robot has been told nothing, so it acts on nothing."""
        behaviour = SatelliteBehaviour(now=0.0)

        intents = behaviour.tick(NOTHING_SEEN_YET, 1.0)

        assert not any(isinstance(intent, LookAhead) for intent in intents)

    def test_the_reason_the_head_is_where_it_is_is_reported(self) -> None:
        """An operator looking at a still robot deserves to know which case it is."""
        behaviour = SatelliteBehaviour(now=0.0)
        behaviour.tick(_seen(face(0.6, 0.0)), 1.0)
        behaviour.tick(_stale(), 2.0)

        assert behaviour.status(2.0).outcome is GazeOutcome.STALE


class TestIdleBehaviour:
    """What the robot does when it has been left alone."""

    def test_it_holds_still_while_somebody_was_recently_there(self) -> None:
        """Calm attention rather than a sway that would read as fidgeting."""
        behaviour = SatelliteBehaviour(idle_seconds=5.0, now=0.0)

        first = _antennas(behaviour.tick(_seen(face(0.0, 0.0)), 1.0))
        later = [_antennas(behaviour.tick(_seen(), 2.0 + step)) for step in range(3)]

        assert first is not None
        assert first.pose.left == pytest.approx(0.0)
        assert all(command is None for command in later)

    def test_the_sway_starts_once_the_room_has_been_empty_long_enough(self) -> None:
        """Present enough not to look switched off."""
        behaviour = SatelliteBehaviour(idle_seconds=5.0, now=0.0)
        behaviour.tick(_seen(), 0.0)

        moved = [
            _antennas(behaviour.tick(_seen(), 5.0 + step * 0.5)) for step in range(1, 6)
        ]

        assert any(command is not None for command in moved)

    def test_idleness_is_reported(self) -> None:
        """The settings page says whether the robot is waiting or attending."""
        behaviour = SatelliteBehaviour(idle_seconds=5.0, now=0.0)
        behaviour.tick(_seen(), 0.0)

        assert not behaviour.status(1.0).idle
        assert behaviour.status(10.0).idle

    def test_a_face_ends_the_idle_state(self) -> None:
        """And the robot goes back to holding still."""
        behaviour = SatelliteBehaviour(idle_seconds=5.0, now=0.0)
        behaviour.tick(_seen(), 0.0)
        behaviour.tick(_seen(), 10.0)

        behaviour.tick(_seen(face(0.0, 0.0)), 11.0)

        assert not behaviour.status(11.0).idle


class TestTheErrorStateClearsItself:
    """The one thing a tick decides on its own."""

    def test_it_returns_to_idle_and_commands_the_change(self) -> None:
        """A robot that stayed cross would look broken rather than informative."""
        behaviour = SatelliteBehaviour(now=0.0)
        behaviour.handle(PipelineEvent.ERROR, 0.0)

        intents = behaviour.tick(NOTHING_SEEN_YET, 10.0)

        assert behaviour.state is PipelineState.IDLE
        assert _antennas(intents) is not None


class TestTuning:
    """What the settings interface changes without restarting anything."""

    def test_an_idle_interval_of_zero_is_refused(self) -> None:
        """It would mean the robot is idle before it has looked."""
        with pytest.raises(ValueError, match="idle interval"):
            SatelliteBehaviour(idle_seconds=0.0)

    def test_retuning_keeps_the_pipeline_state(self) -> None:
        """An operator adjusting a threshold must not reset a conversation."""
        behaviour = SatelliteBehaviour(now=0.0)
        behaviour.handle(PipelineEvent.PROCESSING, 1.0)

        behaviour.retune(
            deadzone=0.1,
            smoothing=0.2,
            idle_seconds=10.0,
            tracking_enabled=True,
        )

        assert behaviour.state is PipelineState.PROCESSING

    def test_retuning_can_switch_tracking_off(self) -> None:
        """Which is the live half of the face-tracking setting."""
        behaviour = SatelliteBehaviour(now=0.0)
        behaviour.tick(_seen(face(0.5, 0.0)), 1.0)

        behaviour.retune(
            deadzone=0.02,
            smoothing=0.35,
            idle_seconds=6.0,
            tracking_enabled=False,
        )
        intents = behaviour.tick(_seen(face(-0.5, 0.0)), 2.0)

        assert not any(isinstance(intent, LookAt) for intent in intents)

    def test_retuning_refuses_an_idle_interval_of_zero(self) -> None:
        """One rule, in one place, for both ways in."""
        behaviour = SatelliteBehaviour(now=0.0)

        with pytest.raises(ValueError, match="idle interval"):
            behaviour.retune(
                deadzone=0.02,
                smoothing=0.35,
                idle_seconds=0.0,
                tracking_enabled=True,
            )


class TestStartupTakesTheHeadWithoutActingOnADetection:
    """The distinction `behaviour.tracking` protects, stated where it composes."""

    def test_the_first_tick_centres_the_head_from_the_idle_expression(self) -> None:
        """The application taking control, not a conclusion about the room."""
        behaviour = SatelliteBehaviour(now=0.0)

        intents = behaviour.tick(NOTHING_SEEN_YET, 1.0)

        assert _heads(intents) is not None
        assert not any(isinstance(intent, LookAhead) for intent in intents)

    def test_a_detector_that_never_answers_never_produces_a_giving_up_movement(
        self,
    ) -> None:
        """`LookAhead` is REQ-048's movement and it needs something to have stopped."""
        behaviour = SatelliteBehaviour(now=0.0)

        produced = [
            intent
            for tick in range(20)
            for intent in behaviour.tick(NOTHING_SEEN_YET, float(tick))
        ]

        assert not any(isinstance(intent, LookAhead) for intent in produced)


class TestSwitchingTrackingOffWhileRunning:
    """The settings page can do it, so the head has to survive it."""

    def test_the_head_is_given_up_rather_than_left_claimed(self) -> None:
        """Otherwise it holds the last face's pose for the life of the process."""
        behaviour = SatelliteBehaviour(now=0.0)
        behaviour.tick(_seen(face(0.6, 0.2)), 1.0)

        behaviour.retune(
            deadzone=0.02,
            smoothing=0.35,
            idle_seconds=6.0,
            tracking_enabled=False,
        )
        intents = behaviour.tick(_seen(face(0.6, 0.2)), 2.0)

        assert any(isinstance(intent, LookAhead) for intent in intents)

    def test_the_pipeline_gets_the_head_back_afterwards(self) -> None:
        """The failure this guards against is the head never moving again."""
        behaviour = SatelliteBehaviour(now=0.0)
        behaviour.tick(_seen(face(0.6, 0.2)), 1.0)
        behaviour.retune(
            deadzone=0.02,
            smoothing=0.35,
            idle_seconds=6.0,
            tracking_enabled=False,
        )
        behaviour.tick(_seen(face(0.6, 0.2)), 2.0)

        behaviour.handle(PipelineEvent.DISCONNECTED, 3.0)
        intents = behaviour.tick(_seen(face(0.6, 0.2)), 3.1)

        assert _heads(behaviour.handle(PipelineEvent.PROCESSING, 4.0)) is not None
        assert not any(isinstance(intent, LookAt) for intent in intents)
