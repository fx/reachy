"""Pure source-qualified face selection for predictive gaze.

The selector does not smooth, trim or command a head. It turns completed
perception results into immutable directives and assigns a process-local target
epoch whenever estimator continuity must be broken. The epoch is association
state only: it is neither persistent identity nor part of the wire protocol.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Final

from reachy_mini_ha_satellite.ports import GazeDirective, GazeOutcome

if TYPE_CHECKING:
    from collections.abc import Sequence

    from reachy_contracts import FaceDetection
    from reachy_mini_ha_satellite.ports import Detections, DetectionSource

__all__ = [
    "GazeDirective",
    "GazeOutcome",
    "GazeSelector",
    "choose_face",
]

_ASSOCIATION_LIMIT: Final = 0.60
_DEFAULT_MAXIMUM_GAP: Final = 0.50


def _initial_key(face: FaceDetection) -> tuple[float, float, float, float]:
    """Order an initial selection independently of detector list order."""
    return (
        -face.confidence,
        math.hypot(face.centre.x, face.centre.y),
        face.centre.x,
        face.centre.y,
    )


def choose_face(faces: Sequence[FaceDetection]) -> FaceDetection | None:
    """Choose the most confident face, then the deterministic nearest centre."""
    return min(faces, key=_initial_key) if faces else None


def _associated_face(
    faces: Sequence[FaceDetection],
    previous: FaceDetection,
) -> tuple[FaceDetection | None, bool]:
    """Choose the deterministic candidate nearest the preceding target."""
    if not faces:
        return None, False

    def key(face: FaceDetection) -> tuple[float, float, float, float, float]:
        distance = math.hypot(
            face.centre.x - previous.centre.x,
            face.centre.y - previous.centre.y,
        )
        return (
            distance,
            -face.confidence,
            math.hypot(face.centre.x, face.centre.y),
            face.centre.x,
            face.centre.y,
        )

    selected = min(faces, key=key)
    distance = math.hypot(
        selected.centre.x - previous.centre.x,
        selected.centre.y - previous.centre.y,
    )
    return selected, distance <= _ASSOCIATION_LIMIT


class GazeSelector:
    """Select one face and qualify every estimator-continuity boundary."""

    def __init__(self, *, maximum_gap: float = _DEFAULT_MAXIMUM_GAP) -> None:
        """Create an empty selector with a bounded supported capture gap."""
        if not math.isfinite(maximum_gap) or maximum_gap <= 0.0:
            message = f"maximum gap must be finite and positive, not {maximum_gap}"
            raise ValueError(message)
        self._maximum_gap = maximum_gap
        self._epoch = 0
        self._current: GazeDirective | None = None
        self._watermarks: dict[DetectionSource, tuple[int, int]] = {}

    def select(self, detections: Detections) -> GazeDirective:
        """Return the directive represented by one perception-port snapshot."""
        identity = detections.identity
        if identity is None:
            return GazeDirective(GazeOutcome.UNKNOWN, target_epoch=self._epoch)

        source, generation, sequence = identity
        watermark = self._watermarks.get(source)
        if watermark is not None and (generation, sequence) < watermark:
            return self._current_or_unknown()

        current = self._current
        if (
            not detections.fresh
            and current is not None
            and current.source is not source
        ):
            return current
        if not detections.fresh:
            if current is not None and current.outcome is GazeOutcome.STALE:
                return current
            self._break_continuity()
            directive = GazeDirective(
                GazeOutcome.STALE,
                source=source,
                generation=generation,
                sequence=sequence,
                captured_at=detections.captured_at,
                received_at=detections.received_at,
                target_epoch=self._epoch,
            )
            self._current = directive
            return directive

        if watermark is not None and (generation, sequence) == watermark:
            return self._current_or_unknown()

        captured_at = detections.captured_at
        received_at = detections.received_at
        if captured_at is None or received_at is None:
            return GazeDirective(GazeOutcome.UNKNOWN, target_epoch=self._epoch)

        discontinuity = False
        if current is not None and current.identity is not None:
            discontinuity = source != current.source or generation != current.generation
            if (
                not discontinuity
                and current.captured_at is not None
                and captured_at - current.captured_at > self._maximum_gap
            ):
                discontinuity = True

        previous_face = current.face if current is not None else None
        selected: FaceDetection | None
        associated = False
        if previous_face is None:
            selected = choose_face(detections.faces)
        else:
            selected, associated = _associated_face(detections.faces, previous_face)

        if selected is None:
            discontinuity = previous_face is not None
        elif (previous_face is not None and not associated) or (
            current is not None
            and current.outcome
            in {
                GazeOutcome.NOBODY,
                GazeOutcome.STALE,
            }
        ):
            discontinuity = True

        if discontinuity:
            self._break_continuity()

        self._watermarks[source] = generation, sequence
        directive = GazeDirective(
            GazeOutcome.TRACKING if selected is not None else GazeOutcome.NOBODY,
            source=source,
            generation=generation,
            sequence=sequence,
            captured_at=captured_at,
            received_at=received_at,
            face=selected,
            target_epoch=self._epoch,
        )
        self._current = directive
        return directive

    def stand_down(self) -> GazeDirective:
        """Return unknown after tracking is disabled, breaking continuity once."""
        if self._current is not None and self._current.face is not None:
            self._break_continuity()
        directive = GazeDirective(GazeOutcome.UNKNOWN, target_epoch=self._epoch)
        self._current = directive
        return directive

    def _break_continuity(self) -> None:
        """Advance the process-local target epoch."""
        self._epoch += 1

    def _current_or_unknown(self) -> GazeDirective:
        """Return current state without allowing a replay to mutate it."""
        if self._current is not None:
            return self._current
        return GazeDirective(GazeOutcome.UNKNOWN, target_epoch=self._epoch)
