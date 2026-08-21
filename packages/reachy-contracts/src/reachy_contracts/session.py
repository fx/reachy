"""The session envelope: negotiation, framing, results, errors and close.

These are the messages that make a session a session, in the order a session
uses them. A client presents a credential and offers what it can speak; the
groundstation answers with the set both sides agreed on; frames go up carrying a
sequence number and a capture token; results come back keyed to that same
sequence number and carrying that same token; either side may report an error or
close.

What is deliberately absent is as important as what is here.

Nothing in this module names a capability. `Capability` is a name and a version,
both values, and `ResultEnvelope` is generic in its payload — so a capability is
added by declaring a payload type and putting it in `CAPABILITY_PAYLOADS`, and
no type here changes. An enumeration of the known capabilities would make every
new one a change to the negotiation types, which is what groundstation REQ-022
forbids.

Framing is absent too. A frame's header is declared here because the header is
contract — both sides read the same sequence number and copy the same capture
token — while how a header and its opaque JPEG bytes are packed into a datagram
belongs to the transport that carries them.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Annotated, Self

from pydantic import (
    Field,
    SecretStr,
    StringConstraints,
    field_serializer,
    model_validator,
)

from reachy_contracts.values import (
    CapabilityName,
    CaptureTimestamp,
    WireModel,
)

__all__ = [
    "Capability",
    "CapabilityVersion",
    "CloseReason",
    "ErrorCode",
    "FrameHeader",
    "ResultEnvelope",
    "SequenceNumber",
    "SessionAgreement",
    "SessionClose",
    "SessionError",
    "SessionOffer",
    "negotiate",
]

# Frames are numbered from zero for the lifetime of one session. A session that
# reconnects negotiates again and starts over, so the number is only ever
# meaningful against the session it was sent on.
SequenceNumber = Annotated[int, Field(ge=0)]

# Capability versions are whole numbers rather than dotted strings because the
# only comparison the protocol performs on one is equality. See `negotiate`.
CapabilityVersion = Annotated[int, Field(ge=1)]

_Detail = Annotated[str, StringConstraints(max_length=500)]


class Capability(WireModel):
    """A capability one side can speak, at one version.

    Attributes:
        name: The capability's identifier, a value rather than an enumerator.
        version: Which revision of that capability is being spoken.
    """

    name: CapabilityName
    version: CapabilityVersion


def _reject_duplicate_names(capabilities: Sequence[Capability]) -> None:
    """Refuse a capability set that names the same capability twice.

    One version of a capability per session keeps agreement unambiguous: with
    two versions of one name on offer there is no stated rule for which the
    agreed set should hold, and inventing one now would be inventing it before
    a second version of anything exists.

    Args:
        capabilities: The set to check.

    Raises:
        ValueError: If any name appears more than once.
    """
    names = [capability.name for capability in capabilities]
    if len(set(names)) != len(names):
        message = f"a capability is named more than once: {sorted(names)}"
        raise ValueError(message)


#:= docs/specs/robot-link/index.md#req-012-capabilities-are-negotiated-at-session-start
#:% Both sides MUST exchange the set of capabilities they support, each with a
#:% version, before any capability-specific message is sent.
class SessionOffer(WireModel):
    """The client's opening message: its credential and what it can speak.

    The credential is held as a secret so that logging a message, or dropping
    one into a traceback, cannot print it; the wire serialisation puts the real
    value back, because presenting it is the entire point of the message.

    Attributes:
        credential: The shared secret the groundstation authenticates against.
        capabilities: What this client can speak, possibly nothing.
    """

    credential: SecretStr
    capabilities: tuple[Capability, ...] = ()

    @field_serializer("credential", when_used="json")
    def _reveal_credential(self, credential: SecretStr) -> str:
        """Put the real credential on the wire.

        Args:
            credential: The held secret.

        Returns:
            The secret's value, which is what the groundstation checks.
        """
        return credential.get_secret_value()

    @model_validator(mode="after")
    def _names_are_unique(self) -> Self:
        """Reject an offer naming one capability twice.

        Returns:
            The validated offer.
        """
        _reject_duplicate_names(self.capabilities)
        return self


class SessionAgreement(WireModel):
    """The groundstation's answer: the set both sides can speak.

    Attributes:
        capabilities: The agreed set, possibly empty.
    """

    capabilities: tuple[Capability, ...] = ()

    @model_validator(mode="after")
    def _names_are_unique(self) -> Self:
        """Reject an agreement naming one capability twice.

        Returns:
            The validated agreement.
        """
        _reject_duplicate_names(self.capabilities)
        return self


def negotiate(
    offer: SessionOffer,
    supported: Sequence[Capability],
) -> SessionAgreement:
    """Reduce an offer to the capabilities the other side also speaks.

    A capability survives only when the name *and* the version match exactly.
    An exact match is what makes an upgrade a coordinated act rather than a
    guess about what an unfamiliar version does, and a compatibility range would
    be a rule written before there is a second version to test it against.

    A capability that does not survive is simply absent from the result: the
    session continues with whatever else was agreed, because a version the other
    side does not offer is a normal outcome of two components upgrading at
    different times, not an error.

    Args:
        offer: What the client said it can speak.
        supported: What this side can speak.

    Returns:
        The agreed set, in the order the offer listed it.
    """
    available = frozenset(supported)
    agreed = tuple(
        capability for capability in offer.capabilities if capability in available
    )
    return SessionAgreement(capabilities=agreed)


#:= docs/specs/robot-link/index.md#req-014-results-are-keyed-to-the-frame-that-produced-them
#:% Every frame MUST carry a monotonically increasing sequence number, and every
#:% result MUST identify the sequence number of the frame it derives from.
class FrameHeader(WireModel):
    """What travels with a frame's opaque payload bytes.

    The frame itself is already JPEG-compressed by the capture hardware and the
    protocol never re-encodes it, so the header is the whole of what the link
    reads about a frame.

    Attributes:
        sequence: This frame's number within the session, increasing.
        captured_at: The capturing side's opaque token for when it was taken.
    """

    sequence: SequenceNumber
    captured_at: CaptureTimestamp


class ResultEnvelope[PayloadT: WireModel](WireModel):
    """One capability's answer to one frame.

    Generic in its payload rather than a union over the known capabilities: a
    consumer parameterises the envelope with the payload type it looked up in
    `CAPABILITY_PAYLOADS`, so a capability is added without this type changing.

    Attributes:
        sequence: The frame this answers.
        captured_at: That frame's capture token, copied through unaltered.
        capability: Which capability produced the payload.
        payload: The capability's own message for this frame.
    """

    sequence: SequenceNumber
    captured_at: CaptureTimestamp
    capability: CapabilityName
    payload: PayloadT

    @classmethod
    def for_frame(
        cls,
        header: FrameHeader,
        capability: CapabilityName,
        payload: PayloadT,
    ) -> Self:
        """Answer a frame, carrying its sequence number and token across.

        This is the only construction the groundstation needs, and it exists so
        that copying the capture token is what happens by default. The token is
        moved from the header to the result and is not read on the way: the
        groundstation has no clock it could be compared against and needs none.

        Args:
            header: The header of the frame being answered.
            capability: Which capability produced the payload.
            payload: The capability's message for this frame.

        Returns:
            The result envelope for that frame.
        """
        return cls(
            sequence=header.sequence,
            captured_at=header.captured_at,
            capability=capability,
            payload=payload,
        )


class ErrorCode(StrEnum):
    """Why a message or a session was refused.

    Attributes:
        UNAUTHENTICATED: No valid credential was presented.
        MALFORMED_MESSAGE: A message did not parse as its declared type.
        CAPABILITY_FAILED: An agreed capability could not process a frame.
        INTERNAL: Anything the receiving side cannot attribute further.
    """

    UNAUTHENTICATED = "unauthenticated"
    MALFORMED_MESSAGE = "malformed_message"
    CAPABILITY_FAILED = "capability_failed"
    INTERNAL = "internal"


class SessionError(WireModel):
    """A failure report, which does not by itself end the session.

    Attributes:
        code: What kind of failure this is.
        detail: A human-readable explanation, never a credential.
        sequence: The frame this concerns, when it concerns one.
    """

    code: ErrorCode
    detail: _Detail = ""
    sequence: SequenceNumber | None = None


class CloseReason(StrEnum):
    """Why a session is ending.

    Attributes:
        GOING_AWAY: An orderly shutdown by either side.
        UNAUTHENTICATED: The credential was missing or wrong.
        PROTOCOL_ERROR: The other side sent something unusable.
    """

    GOING_AWAY = "going_away"
    UNAUTHENTICATED = "unauthenticated"
    PROTOCOL_ERROR = "protocol_error"


class SessionClose(WireModel):
    """The last message on a session, naming why it ended.

    Attributes:
        reason: Why the session is ending.
        detail: A human-readable explanation, never a credential.
    """

    reason: CloseReason
    detail: _Detail = ""
