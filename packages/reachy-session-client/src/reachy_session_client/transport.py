"""What the session needs of the connection underneath it, from the client end.

The protocol is narrow on purpose, and it is the mirror of the groundstation's:
a client sends text and binary and receives text, because frames travel one way.
Nothing else about WebSockets reaches the session, which is what lets the
session's own behaviour — negotiation, sequencing, supersession, reconnection —
be exercised without a server, while the integration tests drive the real
transport against the real service.

The seam is a factory rather than a connection. Reconnection needs to open a
*new* connection, so the thing the session holds has to be something it can call
again, and a test induces a reconnection by handing it a factory that fails for
a while and then does not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Protocol

from websockets.asyncio.client import connect as _connect
from websockets.exceptions import WebSocketException

from reachy_session_client.errors import ConnectionFailedError
from reachy_session_client.urls import redact_url

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from websockets.asyncio.client import ClientConnection

__all__ = [
    "ClientTransport",
    "TransportFactory",
    "WebSocketTransport",
    "open_websocket",
]

# How long the opening handshake may take before the attempt is abandoned and
# retried. The link is a WLAN measured at 100-170 ms idle with 700 ms spikes, so
# this is generous by two orders of magnitude: it is here to stop a black-holed
# connection from parking the client forever, not to police latency.
_OPEN_TIMEOUT_SECONDS: Final = 10.0


class ClientTransport(Protocol):
    """One connection, from the session's point of view."""

    async def send_text(self, text: str) -> None:
        """Send one control message.

        Args:
            text: The already-framed message.

        Raises:
            ConnectionFailedError: If the connection ended.
        """
        ...

    async def send_bytes(self, data: bytes) -> None:
        """Send one frame.

        Args:
            data: The already-framed binary message.

        Raises:
            ConnectionFailedError: If the connection ended.
        """
        ...

    async def receive(self) -> str:
        """Wait for the next control message from the groundstation.

        Returns:
            The text of the message.

        Raises:
            ConnectionFailedError: If the connection ended.
        """
        ...

    async def close(self) -> None:
        """End the connection, tolerating one that has already ended."""
        ...


# What the session calls to get a connection, once at the start and again on
# every reconnection.
type TransportFactory = Callable[[str], Awaitable[ClientTransport]]


class WebSocketTransport:
    """Adapts a `websockets` client connection to `ClientTransport`.

    This is the only module in the package that knows what a WebSocket is.
    """

    def __init__(self, connection: ClientConnection) -> None:
        """Wrap an open connection.

        Args:
            connection: The connection, already established.
        """
        self._connection = connection

    async def send_text(self, text: str) -> None:
        """Send one control message.

        Args:
            text: The already-framed message.

        Raises:
            ConnectionFailedError: If the connection ended.
        """
        try:
            await self._connection.send(text)
        except (WebSocketException, OSError) as error:
            raise ConnectionFailedError(str(error)) from error

    async def send_bytes(self, data: bytes) -> None:
        """Send one frame.

        Args:
            data: The already-framed binary message.

        Raises:
            ConnectionFailedError: If the connection ended.
        """
        try:
            await self._connection.send(data)
        except (WebSocketException, OSError) as error:
            raise ConnectionFailedError(str(error)) from error

    async def receive(self) -> str:
        """Wait for the next control message.

        Returns:
            The text of the message.

        Raises:
            ConnectionFailedError: If the connection ended, or if the
                groundstation sent binary — which is not a message this
                protocol has in that direction, so the connection is not one
                this client can go on using.
        """
        try:
            message = await self._connection.recv()
        except (WebSocketException, OSError) as error:
            raise ConnectionFailedError(str(error)) from error
        if isinstance(message, bytes):
            detail = "the groundstation sent binary, which travels the other way"
            raise ConnectionFailedError(detail)
        return message

    async def close(self) -> None:
        """End the connection, tolerating one that has already ended."""
        await self._connection.close()


#:= docs/specs/robot-link/index.md#req-010-the-robot-is-a-client-only
#:% The robot MUST open the session outbound to the groundstation, and the
#:% groundstation MUST NOT require any inbound listener on the robot.
async def open_websocket(url: str) -> ClientTransport:
    """Open a session connection outbound to the groundstation.

    Outbound and nothing else: this package opens connections and never accepts
    one, so a robot running it needs no listening port and no inbound rule.

    Args:
        url: Where the groundstation serves its session endpoint.

    Returns:
        The connection, ready to carry a session.

    Raises:
        ConnectionFailedError: If the connection could not be opened. Every
            reason is reported the same way because every reason has the same
            answer — the caller retries with a growing delay.
    """
    try:
        connection = await _connect(
            url,
            open_timeout=_OPEN_TIMEOUT_SECONDS,
        )
    except (WebSocketException, OSError, TimeoutError) as error:
        # `WebSocketException` covers the handshake failures — a refused
        # upgrade, a status the server answered with — and `OSError` covers the
        # connection never being made. `str` is empty on some of them, so the
        # type name stands in rather than a message that says nothing.
        detail = str(error) or type(error).__name__
        # The address is rendered rather than quoted. This function is public
        # API and nothing here has validated the URL it was handed, so a
        # credential in the user information or the query would otherwise land
        # in an error message -- which is precisely what reachyctl REQ-059
        # forbids, and the error path is where it would happen.
        message = f"could not open a session at {redact_url(url)}: {detail}"
        raise ConnectionFailedError(message) from error
    return WebSocketTransport(connection)
