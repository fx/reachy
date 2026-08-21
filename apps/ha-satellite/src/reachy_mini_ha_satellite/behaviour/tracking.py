"""Following a face, and knowing the difference between the ways of not doing it.

`Detections` carries four facts and this module turns them into one decision.
Three of those facts are easy — a face is visible, or none is, or the results
have stopped. The fourth is the one that is usually got wrong: **`source` is
`None` until something has actually produced a result**, and "nothing has ever
answered" is not the same event as "something answered and there is nobody
there".

The difference has a consequence at the motor. A live source reporting an empty
frame has truthfully told the robot that nobody is in the room, so this tracker
gives up the head and says so. A source that has never produced anything has told
the robot nothing at all, so **this tracker produces no movement whatever** —
acting on a fact nobody has supplied is how a robot ends up twitching away from
somebody standing in front of it while its detector is still warming up.

That is a statement about what *this* module decides, and it is worth being
precise: the head is not the tracker's alone. When nothing is being tracked the
voice pipeline's own pose has it — see `behaviour.satellite` — and at startup
that pose is neutral, so a robot that has just been given control does centre its
head. The distinction survives that, because it is about acting on a *detection*:
`LookAhead`, the movement REQ-048 names, is produced only where something
actually stopped reporting.

Staleness is the third case and ha-satellite REQ-048 owns it: results were
arriving and have stopped, so the head returns to neutral rather than holding
its last commanded pose. Holding looks like successful tracking of a person who
has left, which is worse than visibly giving up.

Everything here is pure. Time arrives as a parameter and there is no clock.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from reachy_contracts import NormalisedPoint
from reachy_mini_ha_satellite.behaviour.intents import LookAhead, LookAt

if TYPE_CHECKING:
    from collections.abc import Sequence

    from reachy_contracts import FaceDetection
    from reachy_mini_ha_satellite.behaviour.intents import MotionIntent
    from reachy_mini_ha_satellite.ports import Detections

__all__ = [
    "FaceTracker",
    "GazeOutcome",
    "TrackingDecision",
    "choose_face",
]


class GazeOutcome(StrEnum):
    """Why the head is where it is.

    Reported rather than inferred, because a still robot looks the same in
    three of these four cases and an operator deserves to be told which one it
    is. `NOBODY` and `STALE` return the head to neutral; `UNKNOWN` commands
    nothing at all, which leaves the head wherever it already was — the same
    place, since nothing can have aimed it before a source has answered, but
    arrived at by not moving rather than by moving back.

    Attributes:
        TRACKING: A face is visible and the head is following it.
        NOBODY: A live source produced an empty result. Nobody is there.
        STALE: Results were arriving and have stopped — REQ-048's case.
        UNKNOWN: Nothing has produced a result yet, so nothing is known and
            nothing is commanded.
    """

    TRACKING = "tracking"
    NOBODY = "nobody"
    STALE = "stale"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TrackingDecision:
    """What to do with the head, and why.

    Attributes:
        outcome: Which of the four situations this is.
        intent: The movement to command, or `None` when the head is already
            where this decision wants it. A tracker that re-commanded an
            unchanged pose every tick would put a message on the motor bus
            twenty times a second for a robot that is not moving.
        idle_since: When the robot last had a face to look at, or `None` while
            it has one. Idle behaviour is measured from here.
    """

    outcome: GazeOutcome
    intent: MotionIntent | None
    idle_since: float | None


def choose_face(faces: Sequence[FaceDetection]) -> FaceDetection | None:
    """Pick the one face to follow.

    Most confident first, and the tie is broken by whichever is nearer the
    centre of the frame — so a robot looking at two equally-believed faces
    keeps the one it is already closest to facing rather than swinging between
    them on floating-point noise.

    Args:
        faces: Every face the detector reported.

    Returns:
        The face to follow, or `None` when there are none.
    """
    if not faces:
        return None
    return max(
        faces,
        key=lambda face: (
            face.confidence,
            -math.hypot(face.centre.x, face.centre.y),
        ),
    )


def _clamp(value: float) -> float:
    """Keep a coordinate inside the range the wire type accepts.

    Args:
        value: The coordinate.

    Returns:
        The value, bounded to [-1, 1].
    """
    return max(-1.0, min(1.0, value))


def _check_following(deadzone: float, smoothing: float) -> None:
    """Refuse a tuning that would look like tracking not working at all.

    Args:
        deadzone: How far a face must move before the head follows.
        smoothing: How much of the way towards a new target one update moves.

    Raises:
        ValueError: If the deadzone is negative or the smoothing is not a
            fraction greater than zero. A smoothing of zero never arrives
            anywhere, which is a robot that appears to have no tracking, and
            that is the least debuggable way to be told about a mistyped
            setting.
    """
    if deadzone < 0.0:
        message = f"the gaze deadzone must not be negative, not {deadzone}"
        raise ValueError(message)
    if not 0.0 < smoothing <= 1.0:
        message = (
            f"the gaze smoothing must be greater than zero and at most one, "
            f"not {smoothing}"
        )
        raise ValueError(message)


class FaceTracker:
    """Turns a stream of detections into as few head movements as will do.

    Holds two pieces of state and nothing else: where the head was last
    commanded to look, and when the robot last had somebody to look at.
    """

    def __init__(
        self,
        *,
        deadzone: float = 0.02,
        smoothing: float = 0.35,
        now: float = 0.0,
    ) -> None:
        """Describe how the head follows.

        Args:
            deadzone: How far the aim must move, in normalised image
                coordinates, before the head is re-commanded.
            smoothing: How much of the way to a new target one update moves.
                One follows instantly and jitters with the detector; a small
                value lags visibly. It is configuration because the right value
                depends on how often detections arrive.
            now: The caller's clock at construction, which is when idleness
                starts being measured from — a robot that has just started and
                seen nobody is idle, not indefinitely attentive.

        Raises:
            ValueError: On the values `_check_following` refuses.
        """
        _check_following(deadzone, smoothing)
        self._deadzone = deadzone
        self._smoothing = smoothing
        self._committed: NormalisedPoint | None = None
        self._idle_since: float | None = now

    def retune(self, *, deadzone: float, smoothing: float) -> None:
        """Change how the head follows, without forgetting where it is pointing.

        This is what the settings interface calls when an operator adjusts the
        tracking. Rebuilding the tracker instead would drop the committed aim
        and make the head jump back to whatever it was looking at, which is a
        visible artefact of an invisible edit.

        Args:
            deadzone: The new deadzone.
            smoothing: The new smoothing.

        Raises:
            ValueError: On the same values the constructor refuses.
        """
        _check_following(deadzone, smoothing)
        self._deadzone = deadzone
        self._smoothing = smoothing

    @property
    def committed(self) -> NormalisedPoint | None:
        """Where the head was last commanded to look.

        Returns:
            The aim, or `None` when the head is at neutral.
        """
        return self._committed

    #:= docs/specs/ha-satellite/index.md#req-048-the-head-returns-to-neutral-when-tracking-data-goes-stale
    #:% When results stop arriving within the staleness window, the application MUST
    #:% return the head to its neutral position rather than holding its last commanded
    #:% pose.
    def update(self, detections: Detections, now: float) -> TrackingDecision:
        """Decide what the head should do about what is in front of the robot.

        Args:
            detections: What the perception source last saw, and whether that
                is still true.
            now: The caller's clock.

        Returns:
            The decision, carrying a movement only when one is needed.
        """
        if not detections.fresh:
            if detections.source is None:
                # Nothing has ever answered. The robot has not been told
                # anybody is there and has not been told nobody is; commanding
                # anything would be acting on a fact that does not exist.
                return TrackingDecision(
                    outcome=GazeOutcome.UNKNOWN,
                    intent=None,
                    idle_since=self._idle_since,
                )
            return self._give_up(GazeOutcome.STALE, now)

        face = choose_face(detections.faces)
        if face is None:
            return self._give_up(GazeOutcome.NOBODY, now)

        self._idle_since = None
        return TrackingDecision(
            outcome=GazeOutcome.TRACKING,
            intent=self._aim_at(face.centre),
            idle_since=None,
        )

    def stand_down(self, now: float) -> TrackingDecision:
        """Give the head up because nothing is going to ask for it again.

        What the caller does when face tracking is switched off while the robot
        is running. Without it the committed aim would survive the switch and
        the head would hold the pose of the last face it saw for the life of the
        process — which is exactly the "successful tracking of a person who has
        left" this module's docstring is about, arrived at from the other
        direction.

        Args:
            now: The caller's clock.

        Returns:
            The decision, carrying `LookAhead` only if the head is not already
            back at neutral.
        """
        return self._give_up(GazeOutcome.UNKNOWN, now)

    def _give_up(self, outcome: GazeOutcome, now: float) -> TrackingDecision:
        """Return the head to neutral, once, and start counting idleness.

        Args:
            outcome: Which way of having nobody to look at this is — `STALE`
                and `NOBODY` from `update`, `UNKNOWN` from `stand_down`.
            now: The caller's clock.

        Returns:
            The decision, carrying `LookAhead` only if the head is not already
            back at neutral.
        """
        if self._idle_since is None:
            self._idle_since = now
        intent: MotionIntent | None = None
        if self._committed is not None:
            self._committed = None
            intent = LookAhead()
        return TrackingDecision(
            outcome=outcome,
            intent=intent,
            idle_since=self._idle_since,
        )

    def _aim_at(self, target: NormalisedPoint) -> MotionIntent | None:
        """Move part of the way towards a target, if that is far enough to bother.

        Args:
            target: Where the face is.

        Returns:
            The movement, or `None` when the head is already aimed there.
        """
        if self._committed is None:
            self._committed = target
            return LookAt(target)

        aim = NormalisedPoint(
            x=_clamp(
                self._committed.x + self._smoothing * (target.x - self._committed.x),
            ),
            y=_clamp(
                self._committed.y + self._smoothing * (target.y - self._committed.y),
            ),
        )
        if math.hypot(aim.x - self._committed.x, aim.y - self._committed.y) <= (
            self._deadzone
        ):
            return None
        self._committed = aim
        return LookAt(aim)
