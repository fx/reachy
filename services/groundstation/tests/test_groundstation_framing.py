"""Framing: the envelope around a contract message, and around a frame.

The contracts package owns what travels; this module owns how it is packed. The
tests that matter here are the ones that show the packing does not touch what it
carries — a golden fixture goes into a control message and comes back out byte
for byte, and a frame's compressed payload survives a round trip unchanged.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import json

import pytest

from reachy_contracts import (
    Capability,
    CaptureTimestamp,
    FaceDetections,
    FrameHeader,
    ResultEnvelope,
    SessionAgreement,
    SessionOffer,
    fixture_bytes,
    load_fixture,
)
from reachy_groundstation.session.framing import (
    FramingError,
    MessageKind,
    decode_control,
    decode_frame,
    encode_control,
    encode_frame,
)

STAMP = "17352.884"


def test_a_control_message_names_the_type_it_carries() -> None:
    """A receiver knows what it is holding before it parses it."""
    text = encode_control(MessageKind.AGREEMENT, SessionAgreement())
    assert json.loads(text)["kind"] == "agreement"


def test_a_control_message_embeds_the_canonical_bytes_unaltered() -> None:
    """Framing does not re-serialise; the contract's own bytes travel."""
    agreement = SessionAgreement(capabilities=(Capability(name="face", version=1),))
    text = encode_control(MessageKind.AGREEMENT, agreement)
    assert agreement.to_wire().decode("utf-8") in text


def test_a_control_message_round_trips() -> None:
    """What is packed is what comes out."""
    offer = SessionOffer.model_validate({"credential": "example-credential"})
    kind, payload = decode_control(encode_control(MessageKind.OFFER, offer))
    assert kind is MessageKind.OFFER
    assert SessionOffer.from_wire(payload) == offer


@pytest.mark.filesystem
def test_every_golden_fixture_survives_the_control_envelope() -> None:
    """The corpus pins the wire format; this framing must not disturb it.

    Reading committed bytes is input, so this is a contract test rather than a
    unit test and says so with the marker.
    """
    for name, kind, model in (
        ("session-offer.json", MessageKind.OFFER, SessionOffer),
        ("session-agreement.json", MessageKind.AGREEMENT, SessionAgreement),
    ):
        message = load_fixture(name, model)
        kind_out, payload = decode_control(encode_control(kind, message))
        assert kind_out is kind
        assert model.from_wire(payload) == message


@pytest.mark.filesystem
def test_a_golden_result_survives_the_control_envelope() -> None:
    """A result is the message the pipeline emits most, so it is pinned too."""
    envelope = ResultEnvelope[FaceDetections].from_wire(
        fixture_bytes("face-result.json"),
    )
    _, payload = decode_control(encode_control(MessageKind.RESULT, envelope))
    assert ResultEnvelope[FaceDetections].from_wire(payload) == envelope


@pytest.mark.filesystem
def test_an_empty_result_survives_the_control_envelope() -> None:
    """A frame that contained no face is an ordinary message on the wire."""
    envelope = ResultEnvelope[FaceDetections].from_wire(
        fixture_bytes("empty-face-result.json"),
    )
    _, payload = decode_control(encode_control(MessageKind.RESULT, envelope))
    assert ResultEnvelope[FaceDetections].from_wire(payload).payload.faces == ()


@pytest.mark.parametrize(
    "text",
    [
        "not json at all",
        '{"kind":"agreement"}',
        '{"message":{}}',
        '{"kind":"agreement","message":{},"extra":1}',
        '["kind","message"]',
    ],
)
def test_a_malformed_control_message_is_refused(text: str) -> None:
    """The envelope is a shape, and anything else is not one."""
    with pytest.raises(FramingError):
        decode_control(text)


def test_an_unknown_control_kind_is_refused() -> None:
    """A kind this build cannot route is a framing failure, not a guess."""
    with pytest.raises(FramingError, match="unknown control message kind"):
        decode_control('{"kind":"telemetry","message":{}}')


def test_a_frame_round_trips_with_its_payload_untouched() -> None:
    """The payload is already compressed; nothing here re-encodes it."""
    header = FrameHeader(sequence=7, captured_at=CaptureTimestamp(STAMP))
    payload = b"\xff\xd8\xff\xe0 not really a jpeg \x00\x01\x02"
    decoded_header, decoded_payload = decode_frame(encode_frame(header, payload))
    assert decoded_header == header
    assert decoded_payload == payload


def test_a_frames_capture_token_survives_the_framing() -> None:
    """The stamp is opaque, and framing is one more place not to touch it."""
    header = FrameHeader(sequence=1, captured_at=CaptureTimestamp(STAMP))
    decoded, _ = decode_frame(encode_frame(header, b"payload"))
    assert decoded.captured_at.root == STAMP


@pytest.mark.filesystem
def test_the_golden_frame_header_frames_and_unframes() -> None:
    """The header on the wire is the one the corpus pins."""
    header = load_fixture("frame-header.json", FrameHeader)
    decoded, payload = decode_frame(encode_frame(header, b"jpeg"))
    assert decoded == header
    assert payload == b"jpeg"


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x00\x01",
        b"\x00\x00\x00\x00",
        b"\x00\x00\x10\x01" + b"x" * 16,
        b"\x00\x00\x00\x20" + b"x" * 4,
    ],
)
def test_a_malformed_frame_message_is_refused(data: bytes) -> None:
    """A length prefix is a claim, and an unchecked claim is an allocation."""
    with pytest.raises(FramingError):
        decode_frame(data)


def test_a_frame_header_that_does_not_parse_is_refused() -> None:
    """The header is a contract type; bytes that are not one do not pass."""
    body = b'{"sequence":"seven"}'
    message = len(body).to_bytes(4, "big") + body + b"jpeg"
    with pytest.raises(FramingError, match="does not parse"):
        decode_frame(message)


def test_a_frame_with_no_payload_is_refused() -> None:
    """A frame message without a frame in it is not a frame message."""
    header = FrameHeader(sequence=0, captured_at=CaptureTimestamp(STAMP))
    with pytest.raises(FramingError, match="no payload"):
        decode_frame(encode_frame(header, b""))


def test_a_header_too_large_to_frame_is_refused() -> None:
    """A sequence number is a whole number with no upper bound in Python.

    The contract constrains the capture token's length and the sequence
    number's sign, but not its magnitude, so an absurd one serialises to an
    absurd header. The framing refuses it rather than putting a length on the
    wire that the length prefix cannot express.
    """
    header = FrameHeader(
        sequence=10**5000,
        captured_at=CaptureTimestamp(STAMP),
    )
    with pytest.raises(FramingError, match="over"):
        encode_frame(header, b"jpeg")
