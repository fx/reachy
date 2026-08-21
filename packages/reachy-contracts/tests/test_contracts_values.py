"""The shared vocabulary: coordinates, confidences, tokens and payloads.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`. These tests touch no socket, no clock and no file.
"""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from reachy_contracts.values import (
    CAPABILITY_PAYLOADS,
    FACE_CAPABILITY,
    GESTURE_CAPABILITY,
    CaptureTimestamp,
    FaceDetection,
    FaceDetections,
    GestureDetection,
    GestureDetections,
    NormalisedPoint,
)


@pytest.mark.parametrize(
    ("x", "y"),
    [
        (0.0, 0.0),
        (-1.0, -1.0),
        (1.0, 1.0),
        (-1.0, 1.0),
        (0.5, -0.25),
    ],
)
def test_a_normalised_point_accepts_the_whole_range_including_its_edges(
    x: float,
    y: float,
) -> None:
    """The bounds are inclusive: a detection touching the frame edge is real."""
    point = NormalisedPoint(x=x, y=y)

    assert (point.x, point.y) == (x, y)


@pytest.mark.parametrize(
    ("x", "y"),
    [
        (1.0000001, 0.0),
        (-1.0000001, 0.0),
        (0.0, 1.0000001),
        (0.0, -1.0000001),
        (320.0, 240.0),
    ],
)
def test_a_point_outside_the_range_is_refused_at_the_boundary(
    x: float,
    y: float,
) -> None:
    """An un-normalised pixel position fails loudly rather than steering a head."""
    with pytest.raises(ValidationError):
        NormalisedPoint(x=x, y=y)


@pytest.mark.parametrize(
    "value",
    [math.nan, math.inf, -math.inf],
)
def test_a_point_refuses_nan_and_infinity(value: float) -> None:
    """NaN compares false against every bound, so it is rejected by name."""
    with pytest.raises(ValidationError, match=r"allow_inf_nan|finite number"):
        NormalisedPoint(x=value, y=0.0)


@pytest.mark.parametrize("confidence", [0.0, 0.5, 1.0])
def test_a_confidence_spans_the_unit_interval(confidence: float) -> None:
    """Certainty and total doubt are both expressible."""
    detection = FaceDetection(
        centre=NormalisedPoint(x=0.0, y=0.0),
        confidence=confidence,
    )

    assert detection.confidence == confidence


@pytest.mark.parametrize("confidence", [-0.01, 1.01, math.nan])
def test_a_confidence_outside_the_unit_interval_is_refused(confidence: float) -> None:
    """A confidence is a proportion, not an arbitrary score."""
    with pytest.raises(ValidationError):
        FaceDetection(centre=NormalisedPoint(x=0.0, y=0.0), confidence=confidence)


def test_a_face_detection_reports_a_centre_and_a_confidence() -> None:
    """An off-centre face reads negative left of centre and positive above it."""
    detection = FaceDetection(
        centre=NormalisedPoint(x=-0.4, y=0.6),
        confidence=0.9,
    )

    assert detection.centre.x < 0
    assert detection.centre.y > 0
    assert detection.confidence == pytest.approx(0.9)


def test_no_detections_is_an_ordinary_constructible_message() -> None:
    """Nothing in this frame is a result, not an error and not an absence."""
    assert FaceDetections().faces == ()
    assert GestureDetections().gestures == ()


def test_a_gesture_label_is_a_value_rather_than_an_enumerator() -> None:
    """A new gesture is new data; no type here enumerates the known ones."""
    gesture = GestureDetection(label="thumbs_up", confidence=0.7)

    assert gesture.label == "thumbs_up"


@pytest.mark.parametrize("label", ["", "Thumbs Up", "1st", "thumbs-up", "a" * 33])
def test_a_gesture_label_must_be_a_lowercase_identifier(label: str) -> None:
    """The shape is constrained even though the set of values is not."""
    with pytest.raises(ValidationError):
        GestureDetection(label=label, confidence=0.7)


def test_an_unrecognised_field_is_a_parse_failure() -> None:
    """A field one side added and the other has not is loud, not dropped."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        FaceDetections.from_wire(b'{"faces":[],"landmarks":[]}')


def test_a_message_cannot_be_edited_after_it_is_built() -> None:
    """Messages are values: nothing received can be altered and re-sent."""
    detections = FaceDetections()

    with pytest.raises(ValidationError, match="frozen"):
        detections.faces = ()  # type: ignore[misc]  # the point of the assertion


def test_a_capture_timestamp_keeps_the_characters_it_was_given() -> None:
    """The token is stored, not interpreted — there is nothing else to do."""
    token = CaptureTimestamp("00003894112233445566")

    assert token.root == "00003894112233445566"
    assert str(token) == "00003894112233445566"


def test_two_capture_timestamps_compare_by_their_characters() -> None:
    """Equality is textual, which is what "copied through unaltered" means."""
    assert CaptureTimestamp("41.5") == CaptureTimestamp("41.5")
    assert CaptureTimestamp("41.5") != CaptureTimestamp("41.50")


def test_a_capture_timestamp_refuses_to_be_empty() -> None:
    """A frame with no capture token is a frame nobody can age."""
    with pytest.raises(ValidationError):
        CaptureTimestamp("")


def test_the_capability_registry_is_data_and_read_only() -> None:
    """Adding a capability adds a row here and changes no type."""
    assert CAPABILITY_PAYLOADS[FACE_CAPABILITY] is FaceDetections
    assert CAPABILITY_PAYLOADS[GESTURE_CAPABILITY] is GestureDetections

    with pytest.raises(TypeError):
        CAPABILITY_PAYLOADS["audio"] = FaceDetections  # type: ignore[index]  # read-only by design
