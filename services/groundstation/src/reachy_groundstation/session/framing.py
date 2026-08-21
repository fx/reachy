"""How a contract message and a frame's opaque bytes are packed into a message.

The contracts package declares what travels and deliberately stops short of how
it is packed: framing belongs to the transport that carries it. This module is
that framing, and it is built from `json` and `struct` rather than from a model
of its own — a second pydantic model here would be a second wire type outside the
package that owns them, which is exactly what the repository's TID253 ban is
there to prevent.

Two message shapes travel on a session.

**Control** messages are text. A session carries several message types in the
same direction — an agreement, then results, errors and a close — so the
receiver needs to know which type it is holding before it can parse it, and
guessing from which fields happen to be present is a discrimination rule that
breaks the first time two types overlap. The envelope is one JSON object with a
`kind` and a `message`, and the message is the contract type's own canonical
bytes, embedded verbatim: this module never re-serialises a message, so the
bytes the golden fixtures pin are the bytes that go out.

**Frames** are binary. A four-byte big-endian header length, the frame header's
canonical JSON, then the JPEG bytes exactly as the capture hardware produced
them. The payload is never copied through a text encoding, because it is already
compressed and re-encoding it is the one thing the protocol is careful not to do.
"""

from __future__ import annotations

import json
import struct
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from reachy_contracts import FrameHeader

if TYPE_CHECKING:
    from reachy_contracts import WireModel

__all__ = [
    "FramingError",
    "MessageKind",
    "decode_control",
    "decode_frame",
    "encode_control",
    "encode_frame",
]

# The frame header's length prefix: four bytes, big-endian, unsigned.
_LENGTH_PREFIX: Final = struct.Struct("!I")

# A header is a sequence number and a short opaque token. Anything larger is not
# a header this protocol produced, and refusing it early keeps a malformed
# length from being read as a promise about how much memory to reserve.
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

    The inner object is re-serialised compactly rather than handed over as a
    dictionary, so the caller parses it with the same `from_wire` the sender
    serialised it with and gets the same strictness — a value that JSON-mode
    validation would refuse does not become acceptable by having passed through
    a dict on the way.

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

    return kind, json.dumps(envelope[_MESSAGE], separators=(",", ":")).encode("utf-8")


def encode_frame(header: FrameHeader, payload: bytes) -> bytes:
    """Pack a frame header and its compressed payload into one binary message.

    Args:
        header: The frame's sequence number and capture token.
        payload: The frame's bytes, already compressed by the capture hardware.

    Returns:
        The binary message to send.

    Raises:
        FramingError: If the header does not fit the length this framing allows.
    """
    encoded = header.to_wire()
    if len(encoded) > _MAX_HEADER_BYTES:
        detail = f"frame header is {len(encoded)} bytes, over {_MAX_HEADER_BYTES}"
        raise FramingError(detail)
    return _LENGTH_PREFIX.pack(len(encoded)) + encoded + payload


def decode_frame(data: bytes) -> tuple[FrameHeader, bytes]:
    """Unpack a binary message into a frame header and its payload.

    Args:
        data: The binary message as it arrived.

    Returns:
        The header and the compressed payload, which is returned unaltered.

    Raises:
        FramingError: If the message is not a frame of this framing, or if its
            header does not parse as a `FrameHeader`.
    """
    if len(data) < _LENGTH_PREFIX.size:
        detail = f"frame message is {len(data)} bytes, too short for a header length"
        raise FramingError(detail)

    (header_length,) = _LENGTH_PREFIX.unpack_from(data)
    if header_length == 0 or header_length > _MAX_HEADER_BYTES:
        detail = f"frame header length {header_length} is out of range"
        raise FramingError(detail)

    end = _LENGTH_PREFIX.size + header_length
    if len(data) < end:
        detail = (
            f"frame message declares a {header_length}-byte header it does not hold"
        )
        raise FramingError(detail)

    try:
        header = FrameHeader.from_wire(data[_LENGTH_PREFIX.size : end])
    except ValueError as error:
        detail = f"frame header does not parse: {error}"
        raise FramingError(detail) from error

    payload = data[end:]
    if not payload:
        detail = "frame message carries no payload"
        raise FramingError(detail)
    return header, payload
