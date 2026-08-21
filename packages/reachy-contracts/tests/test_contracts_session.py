"""The session envelope: negotiation, framing, results, errors and close.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`. These tests touch no socket, no clock and no file.
"""

from __future__ import annotations

import json

import pytest
from pydantic import SecretStr, ValidationError

from reachy_contracts.session import (
    Capability,
    CloseReason,
    ErrorCode,
    FrameHeader,
    ResultEnvelope,
    SessionAgreement,
    SessionClose,
    SessionError,
    SessionOffer,
    negotiate,
)
from reachy_contracts.values import (
    CaptureTimestamp,
    FaceDetection,
    FaceDetections,
    GestureDetection,
    GestureDetections,
    NormalisedPoint,
)

FACE_V1 = Capability(name="face", version=1)
FACE_V2 = Capability(name="face", version=2)
GESTURE_V1 = Capability(name="gesture", version=1)

CREDENTIAL = SecretStr("example-credential")


def _offer(*capabilities: Capability) -> SessionOffer:
    """Build an offer carrying the standard placeholder credential.

    Args:
        capabilities: What the client claims it can speak.

    Returns:
        The offer.
    """
    return SessionOffer(credential=CREDENTIAL, capabilities=capabilities)


def test_a_capability_is_a_name_and_a_version() -> None:
    """Both halves are values, so a new capability is new data."""
    assert (FACE_V1.name, FACE_V1.version) == ("face", 1)


@pytest.mark.parametrize("name", ["", "Face", "1face", "face-detect", "a" * 33])
def test_a_capability_name_must_be_a_lowercase_identifier(name: str) -> None:
    """The shape is constrained; the set of names deliberately is not."""
    with pytest.raises(ValidationError):
        Capability(name=name, version=1)


@pytest.mark.parametrize("version", [0, -1])
def test_a_capability_version_starts_at_one(version: int) -> None:
    """There is no zeroth revision of anything."""
    with pytest.raises(ValidationError):
        Capability(name="face", version=version)


def test_a_client_that_can_speak_nothing_still_makes_a_valid_offer() -> None:
    """Presenting a credential and offering nothing is a legitimate session."""
    assert _offer().capabilities == ()


def test_an_offer_naming_one_capability_twice_is_refused() -> None:
    """Two versions of one name leaves no stated rule for which is agreed."""
    with pytest.raises(ValidationError, match="named more than once"):
        _offer(FACE_V1, FACE_V2)


def test_an_agreement_naming_one_capability_twice_is_refused() -> None:
    """The same ambiguity is refused on the answering side."""
    with pytest.raises(ValidationError, match="named more than once"):
        SessionAgreement(capabilities=(FACE_V1, FACE_V2))


def test_the_credential_is_hidden_in_a_repr_and_present_on_the_wire() -> None:
    """Logging a message must not print the credential; sending it must."""
    offer = _offer(FACE_V1)

    assert "example-credential" not in repr(offer)
    assert json.loads(offer.to_wire())["credential"] == "example-credential"


def test_the_credential_survives_a_round_trip() -> None:
    """What the groundstation authenticates against is what the client sent."""
    parsed = SessionOffer.from_wire(_offer().to_wire())

    assert parsed.credential.get_secret_value() == "example-credential"


def test_negotiation_keeps_only_what_both_sides_speak() -> None:
    """A capability the other side lacks is absent, and the rest continues."""
    agreement = negotiate(_offer(FACE_V1, GESTURE_V1), [FACE_V1])

    assert agreement.capabilities == (FACE_V1,)


def test_negotiation_compares_versions_exactly() -> None:
    """A version the other side does not offer drops out rather than degrading."""
    agreement = negotiate(_offer(FACE_V2), [FACE_V1])

    assert agreement.capabilities == ()


def test_negotiation_can_agree_on_nothing_without_failing() -> None:
    """An empty agreed set is an outcome, not an error."""
    assert negotiate(_offer(GESTURE_V1), [FACE_V1]) == SessionAgreement()


def test_negotiation_preserves_the_order_the_client_offered() -> None:
    """The agreed set reads back in the offer's order, not the server's."""
    agreement = negotiate(_offer(GESTURE_V1, FACE_V1), [FACE_V1, GESTURE_V1])

    assert agreement.capabilities == (GESTURE_V1, FACE_V1)


def test_negotiation_ignores_a_capability_only_the_other_side_speaks() -> None:
    """Nothing is agreed that the client never asked for."""
    agreement = negotiate(_offer(FACE_V1), [FACE_V1, GESTURE_V1])

    assert agreement.capabilities == (FACE_V1,)


def test_a_frame_header_numbers_the_frame_and_stamps_it() -> None:
    """Sequence number and capture token are the whole of a frame's header."""
    header = FrameHeader(sequence=7, captured_at=CaptureTimestamp("3894112233445566"))

    assert header.sequence == 7
    assert header.captured_at.root == "3894112233445566"


def test_a_sequence_number_cannot_be_negative() -> None:
    """Frames are numbered from zero for the life of one session."""
    with pytest.raises(ValidationError):
        FrameHeader(sequence=-1, captured_at=CaptureTimestamp("1"))


def test_a_result_carries_the_capture_token_through_unaltered() -> None:
    """The groundstation copies the token; it has no clock to compare it with."""
    token = CaptureTimestamp("00003894112233445566.000")
    header = FrameHeader(sequence=41, captured_at=token)

    result = ResultEnvelope[FaceDetections].for_frame(
        header,
        "face",
        FaceDetections(),
    )

    assert result.sequence == 41
    assert result.captured_at == token
    assert json.loads(result.to_wire())["captured_at"] == "00003894112233445566.000"


def test_a_result_names_the_capability_that_produced_it() -> None:
    """Routing a result is a lookup by name, not a match on its shape."""
    result = ResultEnvelope[GestureDetections].for_frame(
        FrameHeader(sequence=1, captured_at=CaptureTimestamp("1")),
        "gesture",
        GestureDetections(gestures=(GestureDetection(label="wave", confidence=0.8),)),
    )

    assert result.capability == "gesture"
    assert result.payload.gestures[0].label == "wave"


def test_an_empty_result_answers_a_frame_like_any_other() -> None:
    """No detections for frame N is a successful result for frame N."""
    result = ResultEnvelope[FaceDetections].for_frame(
        FrameHeader(sequence=9, captured_at=CaptureTimestamp("1")),
        "face",
        FaceDetections(),
    )

    assert result.payload.faces == ()
    assert ResultEnvelope[FaceDetections].from_wire(result.to_wire()) == result


def test_a_result_envelope_validates_the_payload_it_was_parameterised_with() -> None:
    """The envelope is generic, so the payload is checked against its capability."""
    face = ResultEnvelope[FaceDetections](
        sequence=2,
        captured_at=CaptureTimestamp("1"),
        capability="face",
        payload=FaceDetections(
            faces=(
                FaceDetection(centre=NormalisedPoint(x=0.0, y=0.0), confidence=1.0),
            ),
        ),
    )

    with pytest.raises(ValidationError):
        ResultEnvelope[GestureDetections].from_wire(face.to_wire())


def test_an_error_can_name_the_frame_it_concerns_or_no_frame_at_all() -> None:
    """Not every failure belongs to a frame; authentication precedes them."""
    per_frame = SessionError(
        code=ErrorCode.CAPABILITY_FAILED,
        detail="the face model refused the frame",
        sequence=12,
    )
    session_wide = SessionError(code=ErrorCode.UNAUTHENTICATED)

    assert per_frame.sequence == 12
    assert session_wide.sequence is None
    assert session_wide.detail == ""


def test_an_error_code_travels_as_its_string_value() -> None:
    """Another implementation reads a name, not an ordinal."""
    raw = SessionError(code=ErrorCode.MALFORMED_MESSAGE).to_wire()

    assert json.loads(raw)["code"] == "malformed_message"


def test_an_unknown_error_code_is_refused() -> None:
    """The set of codes is closed, unlike the set of capability names."""
    with pytest.raises(ValidationError):
        SessionError.from_wire(b'{"code":"teapot","detail":"","sequence":null}')


def test_a_close_names_why_the_session_ended() -> None:
    """The last message is a reason, not just a disconnection."""
    close = SessionClose(reason=CloseReason.GOING_AWAY, detail="restarting")

    assert json.loads(close.to_wire()) == {
        "reason": "going_away",
        "detail": "restarting",
    }


def test_a_detail_string_is_bounded() -> None:
    """An explanation is an explanation, not an unbounded payload."""
    with pytest.raises(ValidationError):
        SessionClose(reason=CloseReason.PROTOCOL_ERROR, detail="x" * 501)
