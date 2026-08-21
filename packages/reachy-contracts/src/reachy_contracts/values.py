"""The shared vocabulary every robot link message is built from.

Three kinds of thing live here, and they share a module because every one of
them is a *value* rather than a step in the session's lifecycle: the base class
that fixes how a message becomes bytes, the validated scalars those messages
carry, and the per-capability payloads a result delivers.

Two of the values are deliberately awkward to misuse.

`NormalisedCoordinate` refuses anything outside `[-1, 1]`, so a position that
was never divided by the frame's dimensions fails at the boundary instead of
travelling on as a plausible-looking head movement — the one geometry bug in
this protocol that is otherwise silent.

`CaptureTimestamp` is an opaque token. It holds the characters the capturing
side wrote and offers no way to compare, adjust or interpret them, because only
the machine that minted the value owns a clock it means anything against. A
type that parsed the token into an instant would invite exactly the cross-clock
comparison the protocol is built to avoid.

The capability payloads are declared here rather than beside the session types
so that adding a capability touches this module and nothing else. Which
capabilities exist is data — `CAPABILITY_PAYLOADS` — not a shape in the
negotiation or result types.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Final, Self

# TID251 bans pydantic's model bases everywhere so that a consumer cannot
# declare its own copy of a wire type; the ban is lifted for this package in the
# root `pyproject.toml`, because this is the declaration site it points at.
from pydantic import BaseModel, ConfigDict, Field, RootModel, StringConstraints

__all__ = [
    "CAPABILITY_PAYLOADS",
    "FACE_CAPABILITY",
    "GESTURE_CAPABILITY",
    "CapabilityName",
    "CaptureTimestamp",
    "Confidence",
    "FaceDetection",
    "FaceDetections",
    "GestureDetection",
    "GestureDetections",
    "GestureLabel",
    "NormalisedCoordinate",
    "NormalisedPoint",
    "WireModel",
]


class WireModel(BaseModel):
    """The base every robot link message shares.

    The configuration is the contract, not a preference. `extra="forbid"` makes
    an unrecognised field a loud parse failure rather than a value silently
    dropped on one side of the link, and `frozen=True` makes a message a value:
    nothing that has been received can be edited and re-sent as though it were
    the original.

    `strict=True` is the third, and it is what keeps the published schema honest.
    Left lax, pydantic reads JSON `true` as the float `1.0` and the string
    `"0.5"` as `0.5` — so a message this package accepts would be one the schema
    in `docs/contracts/` says is invalid, and a second implementation written
    against that schema would disagree with this one about what arrived. JSON
    integers are still accepted where a number is wanted, which is the one
    coercion the format itself requires.

    `to_wire` is the single canonical serialisation. Every component uses it, so
    the golden fixtures pin the same bytes for all of them rather than pinning
    whichever dump options each consumer happened to pass.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    def to_wire(self) -> bytes:
        """Serialise to the canonical wire bytes.

        Returns:
            Compact UTF-8 JSON with fields in declaration order.
        """
        return self.model_dump_json().encode("utf-8")

    @classmethod
    def from_wire(cls, payload: bytes) -> Self:
        """Parse canonical wire bytes back into a message.

        Args:
            payload: The bytes as they arrived.

        Returns:
            The validated message.

        Raises:
            pydantic.ValidationError: If the bytes are not this message type.
        """
        return cls.model_validate_json(payload)


#:= docs/specs/robot-link/index.md#req-016-results-return-the-capture-timestamp-unaltered
#:% Every result MUST carry the capture timestamp of the frame it derives from,
#:% byte-for-byte as the capturing side supplied it, so that the capturing side can
#:% compute the result's age against the same clock that produced it.
class CaptureTimestamp(RootModel[str]):
    """A frame's capture instant, as the capturing side wrote it.

    The value is an opaque token everywhere except on the machine that minted
    it. This type therefore stores characters and nothing else: it will not
    parse, compare, order or re-render them, so copying a token from a frame
    onto a result is the only thing that is easy to do with one. The producing
    side reads `root` and interprets it against the monotonic clock it came
    from; nobody else has a clock the value means anything against.

    Attributes:
        root: The token exactly as the capturing side supplied it.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    root: Annotated[str, StringConstraints(min_length=1, max_length=64)]

    def __str__(self) -> str:
        """Render the token unchanged.

        Returns:
            The characters the capturing side supplied.
        """
        return self.root


#:= docs/specs/robot-link/index.md#req-021-detection-geometry-is-resolution-independent
#:% Positions in results MUST be expressed in normalised image coordinates rather
#:% than pixels.
#
# The origin is the image centre and both axes run to the edges at ±1, which is
# what the robot's motion layer consumes directly. `allow_inf_nan=False` is not
# redundant beside the bounds: it turns a NaN — which compares false against
# every bound and would otherwise be rejected only incidentally — into a stated
# rejection with a readable message.
NormalisedCoordinate = Annotated[float, Field(ge=-1.0, le=1.0, allow_inf_nan=False)]

# A detector's own estimate of how much it believes itself, on the unit
# interval. Thresholding it is configuration, and belongs to the detector.
Confidence = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]

# Capability names and gesture labels are lowercase identifiers rather than
# members of an enumeration, because both are values in the contract: a new
# capability or a new gesture is new data, not a new type. See groundstation
# REQ-022, which requires adding a capability without touching the transport.
_IDENTIFIER: Final = r"^[a-z][a-z0-9_]*$"

CapabilityName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=32, pattern=_IDENTIFIER),
]

GestureLabel = Annotated[
    str,
    StringConstraints(min_length=1, max_length=32, pattern=_IDENTIFIER),
]


class NormalisedPoint(WireModel):
    """A position in the frame, independent of the capture resolution.

    Attributes:
        x: Horizontal position, negative to the left of centre.
        y: Vertical position, negative below centre.
    """

    x: NormalisedCoordinate
    y: NormalisedCoordinate


#:= docs/specs/perception/index.md#req-034-face-detections-report-a-normalised-centre-and-a-confidence
#:% Each face detection MUST report the face's centre in normalised image
#:% coordinates together with a confidence value.
class FaceDetection(WireModel):
    """One face the detector found.

    A centre and a confidence, and nothing else. Landmarks and bounding boxes
    are absent deliberately: the perception spec leaves the richer payload open
    and nothing consumes one yet, so widening this type is the job of the change
    that introduces a consumer for the extra fields.

    Attributes:
        centre: Where the face is, in normalised image coordinates.
        confidence: How much the detector believes itself.
    """

    centre: NormalisedPoint
    confidence: Confidence


#:= docs/specs/robot-link/index.md#req-013-an-empty-result-is-a-valid-result
#:% A result message carrying no detections MUST be treated as a successful result
#:% for that frame.
class FaceDetections(WireModel):
    """The face capability's payload for one frame.

    `faces` defaults to empty and is never optional, so "this frame contained no
    face" is an ordinary message that constructs with no arguments at all. The
    predecessor made the opposite choice — it posted nothing when every detector
    was switched off, and got a 400 back for it.

    Attributes:
        faces: Every face found in the frame, possibly none.
    """

    faces: tuple[FaceDetection, ...] = ()


class GestureDetection(WireModel):
    """One hand signal the classifier recognised.

    Attributes:
        label: The gesture's name, a value in the contract rather than a type.
        confidence: How much the classifier believes itself.
    """

    label: GestureLabel
    confidence: Confidence


class GestureDetections(WireModel):
    """The gesture capability's payload for one frame.

    Empty for the same reason `FaceDetections` is: a frame with no hand in it is
    a successful result, not a failure to report one.

    Attributes:
        gestures: Every gesture recognised in the frame, possibly none.
    """

    gestures: tuple[GestureDetection, ...] = ()


FACE_CAPABILITY: Final[CapabilityName] = "face"
GESTURE_CAPABILITY: Final[CapabilityName] = "gesture"

# Which capability produces which payload, as data. A consumer routes a result
# by looking its capability name up here; adding a capability adds a row and
# changes no type in `reachy_contracts.session`.
CAPABILITY_PAYLOADS: Final[Mapping[CapabilityName, type[WireModel]]] = MappingProxyType(
    {
        FACE_CAPABILITY: FaceDetections,
        GESTURE_CAPABILITY: GestureDetections,
    },
)
