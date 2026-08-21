"""Gesture recognition: two stages, a configurable sampling rate, no model yet.

The arrangement is a hand detector followed by a classifier over the crop it
found, and both stages are interfaces this module declares rather than models it
loads. That is the whole design decision, and it is the perception spec's:

**No gesture model ships in this build, and that is deliberate.** The
predecessor's classifier reported `fist` and `mute` at 0.9 confidence in an empty
room — a confident wrong answer, which survives any threshold that keeps the true
positives — and its weights never had the provenance and licence check
perception REQ-032 and REQ-033 apply to everything else. Carrying it forward
because it is what exists would make a known defect the out-of-box behaviour, and
blocking the capability on choosing a replacement would block the change. So the
capability ships whole, switched off by default, with no stages wired; the
negatives evaluation the test suite runs reports its false-positive rate as a
number, which is what REQ-037 asks for and what lets a candidate be compared
rather than argued about.

Wiring a model later is passing `hands` and `gestures` to the capability. Nothing
else here changes, and the evaluation reports a real number the moment there is a
model to report one about.

**The sampling rate is configuration.** The predecessor classified every fourth
frame to bound the cost; here that interval is a setting, because the right value
depends on the classifier eventually chosen. It is measured against the frame's
own sequence number rather than a counter of frames this capability has seen, so
a session that shed frames under load samples the same frames it would have
sampled without shedding them.

**An unsampled frame answers with nothing, not with the last answer.** Repeating
a previous classification would put a stale conclusion into a result carrying the
current frame's capture token, which is exactly the cross-clock illusion the
opaque token exists to prevent. An empty payload is an ordinary successful result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

from reachy_contracts import (
    GESTURE_CAPABILITY,
    Capability,
    GestureDetections,
)
from reachy_groundstation.capabilities.base import CapabilityBase
from reachy_groundstation.capabilities.registry import CapabilityDisabledError, register

if TYPE_CHECKING:
    from reachy_contracts import GestureDetection, WireModel
    from reachy_groundstation.config import Settings
    from reachy_groundstation.ports import CapabilityPort, DecodedFrame, ImageArray

__all__ = [
    "GESTURE_VERSION",
    "GestureCapability",
    "GestureClassifier",
    "HandDetector",
    "HandRegion",
    "build_gesture_capability",
    "crop",
]

# The revision of this capability's behaviour that negotiation agrees on.
GESTURE_VERSION: Final = 1


@dataclass(frozen=True, slots=True)
class HandRegion:
    """Where a hand is, in the pixel coordinates of the frame.

    Attributes:
        x: Left edge of the region.
        y: Top edge of the region.
        width: Region width.
        height: Region height.
    """

    x: float
    y: float
    width: float
    height: float


@runtime_checkable
class HandDetector(Protocol):
    """The first stage: finds the regions worth classifying."""

    async def detect(self, image: ImageArray) -> tuple[HandRegion, ...]:
        """Locate every hand in a frame.

        Args:
            image: The decoded frame, to be treated as read-only.

        Returns:
            The regions found, possibly none.
        """
        ...

    async def aclose(self) -> None:
        """Release whatever the stage holds."""
        ...


@runtime_checkable
class GestureClassifier(Protocol):
    """The second stage: names the signal in a cropped hand region."""

    async def classify(self, image: ImageArray) -> GestureDetection | None:
        """Name the gesture in a crop.

        Args:
            image: The cropped hand region.

        Returns:
            The gesture and its confidence, or `None` when the crop is not one
            the classifier recognises.
        """
        ...

    async def aclose(self) -> None:
        """Release whatever the stage holds."""
        ...


def crop(image: ImageArray, region: HandRegion) -> ImageArray | None:
    """Cut a region out of a frame, held inside its bounds.

    A detector can report a region that runs off the edge of the frame, and a
    numpy slice with a negative start silently means something else entirely —
    counting from the far edge — so the bounds are applied here rather than
    trusted.

    Args:
        image: The decoded frame, treated as read-only.
        region: Where to cut.

    Returns:
        A view of the requested pixels, or `None` when the region lies wholly
        outside the frame or has no area inside it.
    """
    height, width = int(image.shape[0]), int(image.shape[1])
    left = max(0, int(region.x))
    top = max(0, int(region.y))
    right = min(width, int(region.x + region.width))
    bottom = min(height, int(region.y + region.height))
    if right <= left or bottom <= top:
        return None
    cut: ImageArray = image[top:bottom, left:right]
    return cut


#:= docs/specs/perception/index.md#req-037-gesture-accuracy-is-measured-against-a-negatives-fixture-set
#:% The gesture capability MUST be evaluated against a fixture set containing scenes
#:% with no hands present, and its false-positive rate on that set MUST be reported
#:% by the test suite.
class GestureCapability(CapabilityBase):
    """Answers a sampled frame with the hand signals in it, or with nothing."""

    def __init__(
        self,
        settings: Settings,
        hands: HandDetector | None = None,
        gestures: GestureClassifier | None = None,
    ) -> None:
        """Assemble the two stages, either of which may be absent.

        Args:
            settings: The settings in effect.
            hands: The hand detector, or `None` when no hand model is wired.
            gestures: The classifier, or `None` when no gesture model is wired.
        """
        super().__init__(Capability(name=GESTURE_CAPABILITY, version=GESTURE_VERSION))
        self._hands = hands
        self._gestures = gestures
        self._threshold = settings.gesture_score_threshold
        self._interval = settings.gesture_sample_interval

    @property
    def wired(self) -> bool:
        """Whether both stages have a model behind them.

        Returns:
            True only when a hand detector and a classifier are both present.
            False is the state this build ships in, and it means every frame is
            answered with no gestures.
        """
        return self._hands is not None and self._gestures is not None

    def samples(self, sequence: int) -> bool:
        """Say whether a frame is one this capability classifies.

        Args:
            sequence: The frame's number within its session.

        Returns:
            True when the frame falls on the configured sampling interval.
        """
        return sequence % self._interval == 0

    async def process(self, frame: DecodedFrame) -> WireModel:
        """Answer one frame.

        Args:
            frame: The decoded frame, shared with every other agreed capability
                and treated as read-only.

        Returns:
            The gestures recognised in it: none when the frame is between
            samples, none when no model is wired, and none when nothing in the
            frame cleared the configured threshold. All three are ordinary
            successful results.
        """
        if not self.samples(frame.sequence):
            return GestureDetections()
        if self._hands is None or self._gestures is None:
            return GestureDetections()
        found: list[GestureDetection] = []
        for region in await self._hands.detect(frame.image):
            cut = crop(frame.image, region)
            if cut is None:
                continue
            gesture = await self._gestures.classify(cut)
            #:= docs/specs/perception/index.md#req-039-detection-thresholds-are-configuration
            #:% The confidence threshold for each detector MUST be settable without rebuilding
            #:% the artifact.
            if gesture is not None and gesture.confidence >= self._threshold:
                found.append(gesture)
        return GestureDetections(gestures=tuple(found))

    async def aclose(self) -> None:
        """Release both stages, whichever of them exist.

        The second close is in a `finally` because the registry logs whatever a
        capability raises on the way out and moves on to the next one: a first
        stage that failed to close would otherwise take the second stage's
        release with it, and nothing would ever try again.
        """
        try:
            if self._hands is not None:
                await self._hands.aclose()
        finally:
            if self._gestures is not None:
                await self._gestures.aclose()


@register
def build_gesture_capability(settings: Settings) -> CapabilityPort:
    """Build the gesture capability, unless this deployment switched it off.

    No stages are passed, because no gesture model clears this repository's
    licence and provenance bar yet — see the module docstring and the perception
    spec's decision records. The capability is still whole: it negotiates, it
    samples, it answers every frame with an empty payload, and the negatives
    evaluation measures it.

    Args:
        settings: The settings in effect.

    Returns:
        The capability.

    Raises:
        CapabilityDisabledError: If gesture detection is switched off, which is the
            default. The registry records it as disabled rather than failed, and
            face detection is unaffected.
    """
    if not settings.gesture_enabled:
        raise CapabilityDisabledError(GESTURE_CAPABILITY)
    return GestureCapability(settings)
