"""How a contract message and a frame's opaque bytes are packed into a message.

This is the client half of the framing the groundstation's
`reachy_groundstation.session.framing` implements, and it is deliberately built
the same way: from `json` and `struct` rather than from a model of its own,
because a pydantic model here would be a wire type declared outside the package
that owns them, which is what the repository's TID253 ban prevents.

Two message shapes travel on a session. **Control** messages are text: one JSON
object with a `kind` and a `message`, where the message is the contract type's
own canonical bytes embedded verbatim, so this module never re-serialises a
message and the bytes the golden fixtures pin are the bytes that go out.
**Frames** are binary: a four-byte big-endian header length, the frame header's
canonical JSON, then the JPEG bytes exactly as the capture hardware produced
them.

The client is a narrower half than the server. It sends control messages and
frames and receives control messages only, because frames travel one way, so
there is no `decode_frame` here and there is no place one could be called from.

⚠️ **This mirrors the groundstation's framing, and the mirror is checked.** The
two ends of one transport must pack messages identically, and the groundstation's
copy predates this package. `test_session_client_framing.py` asserts that both
encoders produce identical bytes for the same message and that each side decodes
the other's, so a change to one that is not made to the other fails a test rather
than a robot. Folding them into a single module is a change of its own: it means
editing the groundstation, which change 0007 does not.
"""

from __future__ import annotations

import json
import struct
from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from reachy_contracts import FrameHeader, WireModel

__all__ = [
    "FramingError",
    "MessageKind",
    "decode_control",
    "encode_control",
    "encode_frame",
]

# The frame header's length prefix: four bytes, big-endian, unsigned.
_LENGTH_PREFIX: Final = struct.Struct("!I")

# A header is a sequence number and a short opaque token. Anything larger is not
# a header this protocol produced.
_MAX_HEADER_BYTES: Final = 4096

_KIND: Final = "kind"
_MESSAGE: Final = "message"


class MessageKind(StrEnum):
    """Which contract type a control message carries.

    Attributes:
        OFFER: The client's opening message, a `SessionOffer`.
        AGREEMENT: The groundstation's answer, a `SessionAgreement`.
        RESULT: One capability's answer to one frame, a `ResultEnvelope`.
        ERROR: A failure report, a `SessionError`.
        CLOSE: The last message on a session, a `SessionClose`.
    """

    OFFER = "offer"
    AGREEMENT = "agreement"
    RESULT = "result"
    ERROR = "error"
    CLOSE = "close"


class FramingError(ValueError):
    """A message did not have the shape this framing describes."""


def encode_control(kind: MessageKind, message: WireModel) -> str:
    """Pack a contract message into a control message.

    The message's canonical bytes are embedded as they are. Round-tripping them
    through `json.dumps` would substitute this module's formatting for the
    serialisation the contracts package defines, and the golden fixtures pin the
    latter.

    Args:
        kind: Which contract type `message` is.
        message: The message to carry.

    Returns:
        The text to send.
    """
    inner = message.to_wire().decode("utf-8")
    return f'{{"{_KIND}":"{kind.value}","{_MESSAGE}":{inner}}}'


def decode_control(text: str) -> tuple[MessageKind, bytes]:
    """Unpack a control message into its kind and the bytes it carries.

    The inner object is re-serialised rather than handed over as a dictionary,
    so the caller parses it with the same `from_wire` the sender serialised it
    with and gets the same strictness — a value that JSON-mode validation would
    refuse does not become acceptable by having passed through a dict on the
    way.

    The re-serialisation reproduces the sender's bytes rather than merely an
    equivalent document: `separators` removes the whitespace `json` would insert
    and `ensure_ascii=False` leaves non-ASCII characters as the UTF-8 the sender
    wrote, while JSON object order is insertion order both ways.

    Args:
        text: The control message as it arrived.

    Returns:
        The kind, and the canonical bytes of the message it carries.

    Raises:
        FramingError: If the text is not a control message of this framing.
    """
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError as error:
        message = f"control message is not JSON: {error}"
        raise FramingError(message) from error

    if not isinstance(envelope, dict) or envelope.keys() != {_KIND, _MESSAGE}:
        detail = f"control message must be an object with {_KIND!r} and {_MESSAGE!r}"
        raise FramingError(detail)

    try:
        kind = MessageKind(envelope[_KIND])
    except ValueError as error:
        detail = f"unknown control message kind: {envelope[_KIND]!r}"
        raise FramingError(detail) from error

    return kind, json.dumps(
        envelope[_MESSAGE],
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def encode_frame(header: FrameHeader, payload: bytes) -> bytes:
    """Pack a frame header and its compressed payload into one binary message.

    Args:
        header: The frame's sequence number and capture token.
        payload: The frame's bytes, already compressed by the capture hardware.

    Returns:
        The binary message to send.

    Raises:
        FramingError: If the header does not fit the length this framing allows,
            or if the payload is empty — a frame message carrying no frame is
            one the other end refuses, so it fails here rather than on the wire.
    """
    encoded = header.to_wire()
    if len(encoded) > _MAX_HEADER_BYTES:
        detail = f"frame header is {len(encoded)} bytes, over {_MAX_HEADER_BYTES}"
        raise FramingError(detail)
    if not payload:
        detail = "a frame message must carry a payload"
        raise FramingError(detail)
    return _LENGTH_PREFIX.pack(len(encoded)) + encoded + payload
