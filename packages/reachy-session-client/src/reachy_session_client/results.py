"""What a client holds about the answers coming back, and how it parses them.

Three things live here, and all three are shaped by one rule from the contracts
package: **which capabilities exist is data**. `CAPABILITY_PAYLOADS` maps a
capability name to the payload type it produces, and a capability is added by
putting a row in it. So the parser derives its result types from that mapping
rather than naming any capability, `count_detections` counts what a payload
holds without knowing what it is, and a capability this build has never heard of
is an ignorable message rather than a parse failure.

That last one matters more than it looks. Refusing to parse an unfamiliar
capability would make an older robot unable to hold a session with a newer
groundstation at all, rather than simply ignoring the traffic it cannot use —
and negotiation already guarantees it asked for none of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from reachy_contracts import CAPABILITY_PAYLOADS, ResultEnvelope, WireModel

if TYPE_CHECKING:
    from collections.abc import Mapping

    from reachy_contracts import CapabilityName, CaptureTimestamp, SequenceNumber

__all__ = [
    "FrameResult",
    "SessionStats",
    "count_detections",
    "result_model_for",
]

# One concrete result type per registered capability, derived from the registry
# rather than enumerated. Enumerating them here would make adding a capability a
# change to this module, which is the coupling `CAPABILITY_PAYLOADS` exists to
# remove — groundstation REQ-022 says the same thing from the service's side.
_RESULT_MODELS: Final[Mapping[CapabilityName, type[ResultEnvelope[WireModel]]]] = (
    MappingProxyType(
        {
            name: ResultEnvelope[payload]  # type: ignore[valid-type]  # the payload type is a value in a registry, not a name in this file: parameterising from data is the whole point, and enumerating the capabilities instead would defeat it
            for name, payload in CAPABILITY_PAYLOADS.items()
        },
    )
)


def result_model_for(capability: str) -> type[ResultEnvelope[WireModel]] | None:
    """Find the result type a named capability answers with.

    Args:
        capability: The name the result declared itself under.

    Returns:
        The concrete `ResultEnvelope` for that capability, or `None` when this
        build does not know the capability — which is a message to ignore, not
        an error to report.
    """
    return _RESULT_MODELS.get(capability)


def count_detections(payload: WireModel) -> int:
    """Count how many things a capability found, without knowing what they are.

    Every capability payload declared so far is a message holding one tuple of
    detections — faces, gestures — and a payload holding none is robot-link
    REQ-013's empty result, which is an ordinary success. Counting the tuples
    rather than reading a named field keeps that true for the next capability
    without this function learning its name.

    Args:
        payload: The capability's message for one frame.

    Returns:
        How many detections it carries, which is legitimately zero.
    """
    return sum(
        len(value)
        for name in type(payload).model_fields
        if isinstance(value := getattr(payload, name), tuple)
    )


@dataclass(frozen=True, slots=True)
class FrameResult:
    """One capability's answer to one frame, as the client received it.

    Attributes:
        envelope: The contract message itself, parsed.
        received_at: When it arrived, on the client's own monotonic clock.
        round_trip_seconds: How long the frame took to go out and come back,
            measured against the clock that stamped it. `None` when the capture
            token is not one this client minted, in which case there is no
            clock to measure it against.
    """

    envelope: ResultEnvelope[WireModel]
    received_at: float
    round_trip_seconds: float | None

    @property
    def sequence(self) -> SequenceNumber:
        """Which frame this answers.

        Returns:
            The frame's sequence number within its session.
        """
        return self.envelope.sequence

    @property
    def capability(self) -> CapabilityName:
        """Which capability produced it.

        Returns:
            The capability's name.
        """
        return self.envelope.capability

    @property
    def captured_at(self) -> CaptureTimestamp:
        """The capture token the frame carried, returned unaltered.

        Returns:
            The token, byte for byte as this client sent it.
        """
        return self.envelope.captured_at

    @property
    def payload(self) -> WireModel:
        """The capability's message for this frame.

        Returns:
            The payload, which is empty when the capability found nothing.
        """
        return self.envelope.payload

    @property
    def detections(self) -> int:
        """How many things the capability found.

        Returns:
            The count, which is legitimately zero.
        """
        return count_detections(self.envelope.payload)


@dataclass(slots=True)
class SessionStats:
    """What a session has done, for a caller that wants to report on it.

    This is the whole of this package's observability. It emits no log lines and
    holds no logger: it is used by a daemon and by a CLI with its own output
    conventions, so what happened is readable and the consumer decides what to
    say about it.

    Attributes:
        connection_attempts: Every attempt to establish a session, successful
            or not, including the first.
        reconnections: Sessions established after the first one dropped.
        frames_submitted: Frames put on the wire.
        frames_dropped: Frames not sent because no session was up. The producer
            is never blocked and nothing is queued behind a dead connection.
        results_applied: Results delivered to the consumer.
        results_superseded: Results discarded because a newer frame's result had
            already been applied.
        results_ignored: Results naming a capability this build cannot parse.
        errors_received: `SessionError` messages the groundstation sent. An
            empty result is not one of these.
    """

    connection_attempts: int = 0
    reconnections: int = 0
    frames_submitted: int = 0
    frames_dropped: int = 0
    results_applied: int = 0
    results_superseded: int = 0
    results_ignored: int = 0
    errors_received: int = 0
