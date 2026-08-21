"""Following a face, and the three different ways of not following one.

The distinction this module exists for is the one `Detections.source` carries:
`None` until something has actually produced a result. "Nothing has ever
answered" and "a live source answered and there is nobody there" are different
facts, and only the second means the robot has truthfully been told the room is
empty — so only the second returns the head to neutral on that account. The
third way, results having stopped arriving, is ha-satellite REQ-048 and returns
it for a different reason.

Nothing here reads a clock or sleeps; `now` is a number the test picks.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from satellite_support import face

from reachy_mini_ha_satellite.behaviour import (
    FaceTracker,
    GazeOutcome,
    LookAhead,
    LookAt,
    choose_face,
)
from reachy_mini_ha_satellite.ports import Detections, DetectionSource

if TYPE_CHECKING:
    from reachy_contracts import FaceDetection


def _seen(*faces: FaceDetection) -> Detections:
    """Build a fresh view carrying these faces.

    Args:
        faces: What the detector reported.

    Returns:
        The view, marked fresh and attributed to a source.
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
    return Detections(fresh=False, source=DetectionSource.REMOTE, age_seconds=99.0)


class TestFollowingAFace:
    """The ordinary case, and how little it commands."""

    def test_the_first_sighting_aims_straight_at_it(self) -> None:
        """Nothing to smooth from, so the head goes where the face is."""
        tracker = FaceTracker(now=0.0)

        decision = tracker.update(_seen(face(0.4, 0.2)), 1.0)

        assert decision.outcome is GazeOutcome.TRACKING
        assert isinstance(decision.intent, LookAt)
        assert decision.intent.target.x == pytest.approx(0.4)

    def test_a_face_that_has_not_moved_commands_nothing(self) -> None:
        """A command every tick for a stationary robot is a busy motor bus."""
        tracker = FaceTracker(now=0.0)
        tracker.update(_seen(face(0.4, 0.2)), 1.0)

        decision = tracker.update(_seen(face(0.4, 0.2)), 2.0)

        assert decision.outcome is GazeOutcome.TRACKING
        assert decision.intent is None

    def test_a_face_that_moved_a_little_stays_inside_the_deadzone(self) -> None:
        """Detector noise is not a reason to move a head."""
        tracker = FaceTracker(deadzone=0.05, smoothing=1.0, now=0.0)
        tracker.update(_seen(face(0.4, 0.2)), 1.0)

        decision = tracker.update(_seen(face(0.41, 0.2)), 2.0)

        assert decision.intent is None

    def test_a_face_that_moved_is_followed_part_of_the_way(self) -> None:
        """Smoothing is what stops the head snapping between detector frames."""
        tracker = FaceTracker(deadzone=0.0, smoothing=0.5, now=0.0)
        tracker.update(_seen(face(0.0, 0.0)), 1.0)

        decision = tracker.update(_seen(face(1.0, 0.0)), 2.0)

        assert isinstance(decision.intent, LookAt)
        assert decision.intent.target.x == pytest.approx(0.5)

    def test_repeated_updates_converge_on_the_target(self) -> None:
        """And then stop commanding, because the step falls inside the deadzone."""
        tracker = FaceTracker(deadzone=0.01, smoothing=0.5, now=0.0)
        for tick in range(20):
            tracker.update(_seen(face(1.0, 0.0)), float(tick))

        assert tracker.committed is not None
        assert tracker.committed.x == pytest.approx(1.0, abs=0.02)

    def test_a_smoothed_aim_stays_inside_the_coordinate_range(self) -> None:
        """The wire type refuses anything outside it, so an overshoot would raise."""
        tracker = FaceTracker(deadzone=0.0, smoothing=1.0, now=0.0)
        tracker.update(_seen(face(-1.0, -1.0)), 1.0)

        decision = tracker.update(_seen(face(1.0, 1.0)), 2.0)

        assert isinstance(decision.intent, LookAt)
        assert -1.0 <= decision.intent.target.x <= 1.0

    def test_seeing_a_face_clears_the_idle_count(self) -> None:
        """Idle behaviour is about the room being empty, not about time passing."""
        tracker = FaceTracker(now=0.0)

        decision = tracker.update(_seen(face(0.0, 0.0)), 10.0)

        assert decision.idle_since is None


class TestChoosingBetweenFaces:
    """One head, so one face."""

    def test_the_most_confident_wins(self) -> None:
        """The detector's own belief is the best signal available."""
        chosen = choose_face([face(0.8, 0.0, 0.5), face(-0.8, 0.0, 0.9)])

        assert chosen is not None
        assert chosen.centre.x == pytest.approx(-0.8)

    def test_a_tie_goes_to_whichever_is_nearer_the_centre(self) -> None:
        """So two equally-believed faces do not make the head oscillate."""
        chosen = choose_face([face(0.9, 0.0, 0.9), face(0.1, 0.0, 0.9)])

        assert chosen is not None
        assert chosen.centre.x == pytest.approx(0.1)

    def test_no_faces_chooses_none(self) -> None:
        """Which is what an empty result means."""
        assert choose_face([]) is None


class TestTheThreeWaysOfNotFollowing:
    """`Detections.source` is what tells them apart."""

    def test_nothing_produced_yet_commands_nothing_at_all(self) -> None:
        """The robot has not been told anybody is there or that nobody is."""
        tracker = FaceTracker(now=0.0)

        decision = tracker.update(Detections(), 1.0)

        assert decision.outcome is GazeOutcome.UNKNOWN
        assert decision.intent is None

    def test_nothing_produced_yet_never_moves_the_head_however_long_it_lasts(
        self,
    ) -> None:
        """A robot started in front of somebody must not twitch to neutral."""
        tracker = FaceTracker(now=0.0)

        decisions = [tracker.update(Detections(), float(tick)) for tick in range(10)]

        assert all(decision.intent is None for decision in decisions)

    def test_an_empty_live_result_returns_the_head_to_neutral(self) -> None:
        """A live source has truthfully said the room is empty."""
        tracker = FaceTracker(now=0.0)
        tracker.update(_seen(face(0.5, 0.0)), 1.0)

        decision = tracker.update(_seen(), 2.0)

        assert decision.outcome is GazeOutcome.NOBODY
        assert isinstance(decision.intent, LookAhead)

    def test_stale_results_return_the_head_to_neutral(self) -> None:
        """ha-satellite REQ-048, which is what this whole distinction is for."""
        tracker = FaceTracker(now=0.0)
        tracker.update(_seen(face(0.5, 0.0)), 1.0)

        decision = tracker.update(_stale(), 2.0)

        assert decision.outcome is GazeOutcome.STALE
        assert isinstance(decision.intent, LookAhead)

    def test_the_head_is_returned_to_neutral_once_rather_than_every_tick(self) -> None:
        """It is already there; saying so again is noise on the motor bus."""
        tracker = FaceTracker(now=0.0)
        tracker.update(_seen(face(0.5, 0.0)), 1.0)
        tracker.update(_stale(), 2.0)

        decision = tracker.update(_stale(), 3.0)

        assert decision.intent is None

    def test_giving_up_forgets_where_the_head_was_pointing(self) -> None:
        """So the next sighting aims rather than smoothing from a stale target."""
        tracker = FaceTracker(now=0.0)
        tracker.update(_seen(face(0.9, 0.0)), 1.0)
        tracker.update(_stale(), 2.0)

        assert tracker.committed is None

    def test_a_face_seen_again_after_staleness_is_aimed_at_directly(self) -> None:
        """The complement of the above, at the port rather than the attribute."""
        tracker = FaceTracker(deadzone=0.0, smoothing=0.1, now=0.0)
        tracker.update(_seen(face(0.9, 0.0)), 1.0)
        tracker.update(_stale(), 2.0)

        decision = tracker.update(_seen(face(-0.9, 0.0)), 3.0)

        assert isinstance(decision.intent, LookAt)
        assert decision.intent.target.x == pytest.approx(-0.9)

    def test_giving_up_starts_the_idle_count(self) -> None:
        """From the moment the robot stopped having somebody to look at."""
        tracker = FaceTracker(now=0.0)
        tracker.update(_seen(face(0.5, 0.0)), 1.0)

        decision = tracker.update(_seen(), 4.0)

        assert decision.idle_since == 4.0

    def test_the_idle_count_is_not_restarted_while_it_is_running(self) -> None:
        """Otherwise the robot would never reach its idle behaviour."""
        tracker = FaceTracker(now=0.0)
        tracker.update(_seen(face(0.5, 0.0)), 1.0)
        tracker.update(_seen(), 4.0)

        decision = tracker.update(_seen(), 9.0)

        assert decision.idle_since == 4.0

    def test_a_robot_that_has_seen_nobody_is_idle_from_the_start(self) -> None:
        """Rather than indefinitely attentive to a room it has never looked at."""
        tracker = FaceTracker(now=5.0)

        decision = tracker.update(Detections(), 6.0)

        assert decision.idle_since == 5.0


class TestTuning:
    """What the settings interface may change while the robot is running."""

    def test_a_negative_deadzone_is_refused(self) -> None:
        """It would mean "move for a movement smaller than nothing"."""
        with pytest.raises(ValueError, match="deadzone"):
            FaceTracker(deadzone=-0.1)

    def test_a_smoothing_of_zero_is_refused(self) -> None:
        """It never arrives, which looks exactly like tracking not working."""
        with pytest.raises(ValueError, match="smoothing"):
            FaceTracker(smoothing=0.0)

    def test_a_smoothing_above_one_is_refused(self) -> None:
        """It overshoots, which looks like the head hunting."""
        with pytest.raises(ValueError, match="smoothing"):
            FaceTracker(smoothing=1.5)

    def test_retuning_keeps_the_committed_aim(self) -> None:
        """An invisible edit must not make the head jump."""
        tracker = FaceTracker(deadzone=0.0, smoothing=1.0, now=0.0)
        tracker.update(_seen(face(0.5, 0.0)), 1.0)

        tracker.retune(deadzone=0.1, smoothing=0.2)

        assert tracker.committed is not None
        assert tracker.committed.x == pytest.approx(0.5)

    def test_retuning_takes_effect(self) -> None:
        """The whole reason it exists."""
        tracker = FaceTracker(deadzone=0.0, smoothing=1.0, now=0.0)
        tracker.update(_seen(face(0.0, 0.0)), 1.0)

        tracker.retune(deadzone=0.0, smoothing=0.25)
        decision = tracker.update(_seen(face(1.0, 0.0)), 2.0)

        assert isinstance(decision.intent, LookAt)
        assert decision.intent.target.x == pytest.approx(0.25)

    def test_retuning_refuses_what_the_constructor_refuses(self) -> None:
        """One rule, in one place, for both ways in."""
        tracker = FaceTracker()

        with pytest.raises(ValueError, match="smoothing"):
            tracker.retune(deadzone=0.0, smoothing=0.0)
