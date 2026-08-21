"""What the session layer needs of the connection underneath it.

The protocol is narrow on purpose. A session receives text and binary from the
client and sends only text back — control messages and results — because frames
travel one way. Nothing else about WebSockets reaches the session layer, which is
what lets the session's own behaviour be exercised without a server, while the
integration tests drive the real transport end to end rather than a stand-in for
it.
"""

from __future__ import annotations

from typing import Final, Protocol

__all__ = [
    "CLOSE_GOING_AWAY",
    "CLOSE_NORMAL",
    "CLOSE_POLICY_VIOLATION",
    "CLOSE_PROTOCOL_ERROR",
    "SessionTransport",
    "TransportClosedError",
]

# RFC 6455 close codes. Policy violation is what an unauthenticated client gets:
# the connection was well-formed and the credential was not acceptable.
CLOSE_NORMAL: Final = 1000
CLOSE_GOING_AWAY: Final = 1001
CLOSE_PROTOCOL_ERROR: Final = 1002
CLOSE_POLICY_VIOLATION: Final = 1008


class TransportClosedError(Exception):
    """The other side went away, whether politely or not."""


class SessionTransport(Protocol):
    """One connection, from the session layer's point of view."""

    async def receive(self) -> str | bytes:
        """Wait for the next message from the client.

        Returns:
            The text of a control message, or the bytes of a frame.

        Raises:
            TransportClosedError: If the connection ended.
        """
        ...

    async def send(self, text: str) -> None:
        """Send one control message or result.

        Args:
            text: The already-framed message.

        Raises:
            TransportClosedError: If the connection ended.
        """
        ...

    async def close(self, code: int, reason: str) -> None:
        """End the connection.

        Args:
            code: The RFC 6455 close code.
            reason: A short explanation, never a credential.
        """
        ...
