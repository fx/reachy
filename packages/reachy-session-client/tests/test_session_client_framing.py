"""The client's framing, and the proof that it is the groundstation's framing.

Two ends of one transport have to pack messages identically or nothing works,
and this repository currently holds two copies of that packing: the
groundstation's, which landed with the service, and this package's, which landed
with the client. The last test in this module is what keeps them one packing
rather than two — it asserts that both encoders produce identical bytes and that
each side decodes what the other produced, so a change made to one and not the
other fails here rather than on a robot.

That test imports `reachy_groundstation`. It is the one place in this package
that does, it is a test rather than shipped code, and it is deliberate: a
cross-implementation check that used its own idea of the other side would be
checking itself.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import pytest
from session_client_support import CREDENTIAL, FACE, GESTURE

from reachy_contracts import (
    CaptureTimestamp,
    FrameHeader,
    SessionAgreement,
    SessionOffer,
)
from reachy_groundstation.session import framing as server
from reachy_session_client import (
    FramingError,
    MessageKind,
    decode_control,
    encode_control,
    encode_frame,
)

HEADER = FrameHeader(sequence=7, captured_at=CaptureTimestamp("17352.884"))
PAYLOAD = b"\xff\xd8\xff\xe0 not really a JPEG, but opaque bytes all the same"


def test_a_control_message_round_trips_through_its_own_framing() -> None:
    """The kind survives, and so do the message's canonical bytes."""
    agreement = SessionAgreement(capabilities=(FACE,))

    kind, payload = decode_control(encode_control(MessageKind.AGREEMENT, agreement))

    assert kind is MessageKind.AGREEMENT
    assert payload == agreement.to_wire()


def test_the_carried_message_is_embedded_rather_than_re_serialised() -> None:
    """The golden corpus pins the contract's bytes, so framing must not touch them."""
    offer = SessionOffer.model_validate(
        {"credential": CREDENTIAL, "capabilities": (FACE, GESTURE)},
    )

    text = encode_control(MessageKind.OFFER, offer)

    assert offer.to_wire().decode("utf-8") in text


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("{", "not JSON"),
        ('"a string"', "must be an object"),
        ('{"kind":"offer"}', "must be an object"),
        ('{"kind":"offer","message":{},"extra":1}', "must be an object"),
        ('{"kind":"telepathy","message":{}}', "unknown control message kind"),
    ],
)
def test_a_message_that_is_not_of_this_framing_is_refused(
    text: str,
    reason: str,
) -> None:
    """Each rejection names what was wrong with the shape.

    Args:
        text: The message as it would have arrived.
        reason: What the failure should say.
    """
    with pytest.raises(FramingError, match=reason):
        decode_control(text)


def test_a_frame_carries_its_header_length_then_its_header_then_its_bytes() -> None:
    """The payload is never copied through a text encoding."""
    message = encode_frame(HEADER, PAYLOAD)

    assert message.endswith(PAYLOAD)
    assert HEADER.to_wire() in message


def test_a_frame_message_with_no_frame_in_it_is_refused_before_it_is_sent() -> None:
    """The other end refuses one, so this end does not spend a round trip on it."""
    with pytest.raises(FramingError, match="must carry a payload"):
        encode_frame(HEADER, b"")


def test_an_oversize_header_is_refused() -> None:
    """A header this large is not one this protocol produced.

    The sequence number is the only unbounded field a header has — the contract
    constrains it to be non-negative and nothing more — so a preposterous one is
    how the real bound is reached rather than by lowering the bound for the
    test, which would prove only that the test can subtract.
    """
    header = FrameHeader(
        sequence=10**5000,
        captured_at=CaptureTimestamp("17352.884"),
    )

    with pytest.raises(FramingError, match="over 4096"):
        encode_frame(header, PAYLOAD)


#:= docs/specs/reachyctl/index.md#req-057-the-probe-exercises-the-real-session-protocol
#:% The probe command MUST establish a session using the same protocol
#:% implementation the robot application uses.
@pytest.mark.parametrize(
    "kind",
    [MessageKind.OFFER, MessageKind.AGREEMENT, MessageKind.CLOSE],
)
def test_the_client_frames_a_message_exactly_as_the_groundstation_does(
    kind: MessageKind,
) -> None:
    """One packing, in two files until one of them can import the other.

    Args:
        kind: Which control message kind to compare.
    """
    agreement = SessionAgreement(capabilities=(FACE, GESTURE))

    mine = encode_control(kind, agreement)
    theirs = server.encode_control(server.MessageKind(kind.value), agreement)

    assert mine == theirs


def test_each_side_decodes_what_the_other_encoded() -> None:
    """Byte equality on the encoders is not enough: the decoders must agree too."""
    agreement = SessionAgreement(capabilities=(FACE,))

    from_server = decode_control(
        server.encode_control(server.MessageKind.AGREEMENT, agreement),
    )
    from_client = server.decode_control(
        encode_control(MessageKind.AGREEMENT, agreement),
    )

    assert from_client[0].value == from_server[0].value == "agreement"
    assert from_client[1] == from_server[1] == agreement.to_wire()


def test_the_groundstation_unpacks_a_frame_this_client_packed() -> None:
    """The header and the compressed payload arrive as they were sent."""
    header, payload = server.decode_frame(encode_frame(HEADER, PAYLOAD))

    assert header == HEADER
    assert payload == PAYLOAD


def test_both_sides_name_the_same_message_kinds() -> None:
    """A kind added on one side and not the other is a message nobody can read."""
    assert {kind.value for kind in MessageKind} == {
        kind.value for kind in server.MessageKind
    }
