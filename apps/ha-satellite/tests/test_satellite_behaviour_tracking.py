"""Source-qualified face selection and loss directives."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Final

import pytest
from satellite_support import face

from reachy_mini_ha_satellite.behaviour.tracking import GazeSelector, choose_face
from reachy_mini_ha_satellite.ports import Detections, DetectionSource, GazeOutcome

if TYPE_CHECKING:
    from reachy_contracts import FaceDetection


_LOCAL_SOURCE: Final = DetectionSource.LOCAL  # leak-scan:allow


def _result(
    *faces: FaceDetection,
    source: DetectionSource = DetectionSource.REMOTE,
    generation: int = 0,
    sequence: int = 0,
    captured_at: float = 1.0,
    received_at: float = 1.1,
    fresh: bool = True,
) -> Detections:
    """Build one fully qualified completed result."""
    return Detections(
        faces=faces,
        fresh=fresh,
        source=source,
        age_seconds=max(0.0, received_at - captured_at),
        generation=generation,
        sequence=sequence,
        captured_at=captured_at,
        received_at=received_at,
    )


class TestDeterministicSelection:
    """Confidence establishes a target; centre distance associates it thereafter."""

    def test_highest_confidence_wins_then_centre_breaks_a_tie(self) -> None:
        """Use detector confidence before centrality."""
        selected = choose_face(
            [face(0.8, 0.0, 0.8), face(-0.4, 0.0, 0.9), face(0.1, 0.0, 0.9)]
        )

        assert selected is not None
        assert selected.centre.x == pytest.approx(0.1)

    def test_exact_ties_are_independent_of_input_order(self) -> None:
        """Do not let detector list order choose the person."""
        left = face(-0.2, 0.0, 0.9)
        right = face(0.2, 0.0, 0.9)

        assert choose_face([left, right]) == choose_face([right, left])

    def test_the_previous_target_is_associated_by_nearest_centre(self) -> None:
        """Retain a nearby target despite another confidence lead."""
        selector = GazeSelector()
        first = selector.select(_result(face(-0.4, 0.0, 0.9), sequence=1))
        followed = selector.select(
            _result(
                face(0.45, 0.0, 0.99),
                face(-0.35, 0.0, 0.7),
                sequence=2,
                captured_at=1.1,
                received_at=1.2,
            )
        )

        assert first.face is not None
        assert followed.face is not None
        assert followed.face.centre.x == pytest.approx(-0.35)
        assert followed.target_epoch == first.target_epoch


class TestQualifiedDirectives:
    """Only completed source-qualified results can activate predictive gaze."""

    def test_actionable_directive_carries_all_source_and_time_facts(self) -> None:
        """Preserve identity, timing and selected face unchanged."""
        selector = GazeSelector()

        directive = selector.select(
            _result(
                face(0.4, -0.2),
                generation=3,
                sequence=7,
                captured_at=8.0,
                received_at=8.4,
            )
        )

        assert directive.outcome is GazeOutcome.TRACKING
        assert directive.identity == (DetectionSource.REMOTE, 3, 7)
        assert directive.captured_at == 8.0
        assert directive.received_at == 8.4
        assert directive.face == face(0.4, -0.2)
        assert directive.actionable

    def test_metadata_free_legacy_input_is_unknown_and_never_actionable(self) -> None:
        """Compatibility construction cannot activate motion."""
        selector = GazeSelector()

        directive = selector.select(Detections(faces=(face(0.8, 0.0),), fresh=True))

        assert directive.outcome is GazeOutcome.UNKNOWN
        assert directive.identity is None
        assert directive.face is None
        assert not directive.actionable

    def test_never_observed_input_is_unknown(self) -> None:
        """Do not infer a loss or a target before any result."""
        directive = GazeSelector().select(Detections())

        assert directive.outcome is GazeOutcome.UNKNOWN
        assert directive.identity is None

    def test_fresh_empty_result_is_an_explicit_qualified_loss(self) -> None:
        """Consume a completed empty frame as loss once."""
        selector = GazeSelector()

        directive = selector.select(_result(sequence=4))

        assert directive.outcome is GazeOutcome.NOBODY
        assert directive.identity == (DetectionSource.REMOTE, 0, 4)
        assert directive.face is None
        assert directive.actionable

    def test_stale_result_is_an_explicit_loss_but_not_a_new_observation(self) -> None:
        """Signal staleness without replaying image data."""
        selector = GazeSelector()
        selector.select(_result(face(0.5, 0.0), sequence=4))

        directive = selector.select(
            _result(sequence=4, fresh=False, captured_at=1.0, received_at=1.1)
        )

        assert directive.outcome is GazeOutcome.STALE
        assert directive.face is None
        assert not directive.actionable


class TestIdentityCacheAndDiscontinuities:
    """Replays cannot mutate selection state; real discontinuities advance its epoch."""

    def test_cached_identity_returns_the_same_directive(self) -> None:
        """Make repeated polling observationally inert."""
        selector = GazeSelector()
        result = _result(face(0.3, 0.0), sequence=5)

        first = selector.select(result)
        cached = selector.select(result)

        assert cached is first

    def test_reordered_sequence_returns_the_current_cache_without_reselection(
        self,
    ) -> None:
        """Reject old frames before target selection can mutate."""
        selector = GazeSelector()
        current = selector.select(_result(face(0.3, 0.0), sequence=5))

        replayed = selector.select(
            _result(face(-0.9, 0.0), sequence=4, captured_at=0.9, received_at=1.0)
        )

        assert replayed is current
        assert replayed.face == face(0.3, 0.0)

    def test_obsolete_inactive_source_staleness_cannot_replace_current_source(
        self,
    ) -> None:
        """A cached remote view going stale after local fallback is not active loss."""
        selector = GazeSelector()
        remote = _result(face(0.4, 0.0), generation=1, sequence=9)
        selector.select(remote)
        local = selector.select(
            _result(
                face(-0.3, 0.0),
                source=_LOCAL_SOURCE,
                generation=0,
                sequence=2,
                captured_at=1.1,
                received_at=1.2,
            )
        )

        replayed = selector.select(
            replace(remote, fresh=False, faces=(), age_seconds=99.0)
        )

        assert replayed is local
        assert replayed.outcome is GazeOutcome.TRACKING

    def test_old_generation_cannot_resurface_after_fallback(self) -> None:
        """Keep bounded per-source watermarks across fallback."""
        selector = GazeSelector()
        old_remote = _result(face(0.4, 0.0), generation=1, sequence=9)
        selector.select(old_remote)
        local = selector.select(
            _result(
                face(-0.3, 0.0),
                source=_LOCAL_SOURCE,
                generation=0,
                sequence=2,
                captured_at=1.1,
                received_at=1.2,
            )
        )

        assert selector.select(old_remote) is local

    @pytest.mark.parametrize(
        "next_result",
        [
            _result(
                face(0.2, 0.0),
                source=_LOCAL_SOURCE,
                sequence=0,
                captured_at=1.1,
                received_at=1.2,
            ),
            _result(
                face(0.2, 0.0),
                generation=1,
                sequence=0,
                captured_at=1.1,
                received_at=1.2,
            ),
            _result(
                face(-0.9, 0.0),
                sequence=2,
                captured_at=1.1,
                received_at=1.2,
            ),
            _result(
                face(0.2, 0.0),
                sequence=2,
                captured_at=2.0,
                received_at=2.1,
            ),
        ],
        ids=["source", "generation", "selection", "gap"],
    )
    def test_stream_discontinuity_advances_target_epoch(
        self,
        next_result: Detections,
    ) -> None:
        """Reset association state across every stream discontinuity."""
        selector = GazeSelector(maximum_gap=0.5)
        first = selector.select(_result(face(0.2, 0.0), sequence=1))

        changed = selector.select(next_result)

        assert changed.target_epoch == first.target_epoch + 1

    def test_empty_advances_epoch_once_and_reacquisition_advances_again(self) -> None:
        """Break continuity on loss and again on reacquisition."""
        selector = GazeSelector()
        first = selector.select(_result(face(0.2, 0.0), sequence=1))
        empty_result = _result(
            sequence=2,
            captured_at=1.1,
            received_at=1.2,
        )

        empty = selector.select(empty_result)
        cached = selector.select(empty_result)
        reacquired = selector.select(
            replace(
                empty_result,
                faces=(face(0.2, 0.0),),
                sequence=3,
                captured_at=1.2,
                received_at=1.3,
            )
        )

        assert empty.target_epoch == first.target_epoch + 1
        assert cached is empty
        assert reacquired.target_epoch == empty.target_epoch + 1
