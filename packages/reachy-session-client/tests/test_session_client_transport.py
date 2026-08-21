"""The WebSocket adapter, and what it turns a broken connection into.

This is the only module in the package that knows what a WebSocket is, and its
whole job is to turn one library's failures into the one error the session
retries on. That mapping is what is tested here, against a connection object
that fails on demand — the real library is driven end to end by the integration
tests, where a connection is dropped for real.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import pytest
from websockets.exceptions import WebSocketException

from reachy_session_client import ConnectionFailedError, WebSocketTransport


class BrokenConnection:
    """A `websockets` client connection that has gone away.

    Attributes:
        closed: Whether the connection was closed.
    """

    def __init__(self, error: BaseException) -> None:
        """Prepare the failure every call will raise.

        Args:
            error: What the library would raise.
        """
        self._error = error
        self.closed = False

    async def send(self, message: str | bytes) -> None:
        """Fail to send.

        Args:
            message: What was to be sent.

        Raises:
            BaseException: The prepared failure.
        """
        del message
        raise self._error

    async def recv(self) -> str | bytes:
        """Fail to receive.

        Returns:
            Nothing; this always raises.

        Raises:
            BaseException: The prepared failure.
        """
        raise self._error

    async def close(self) -> None:
        """Record that the connection was closed."""
        self.closed = True


class BinaryConnection:
    """A connection that sends binary, which travels the other way."""

    async def send(self, message: str | bytes) -> None:
        """Accept anything.

        Args:
            message: What was sent.
        """

    async def recv(self) -> str | bytes:
        """Send binary at the client.

        Returns:
            Bytes, which this protocol never sends downwards.
        """
        return b"\xff\xd8 a frame, travelling the wrong way"

    async def close(self) -> None:
        """Close, which this connection does nothing about."""


def transport_over(connection: object) -> WebSocketTransport:
    """Wrap a stand-in connection in the adapter under test.

    Args:
        connection: The object to adapt. The adapter calls three methods on it,
            and these fakes are those three methods.

    Returns:
        The adapter.
    """
    return WebSocketTransport(connection)  # type: ignore[arg-type]  # the adapter's contract with `websockets` is `send`, `recv` and `close`; a stand-in providing those is what it is being driven with


@pytest.mark.parametrize(
    "error",
    [WebSocketException("the connection went away"), OSError("network is down")],
)
@pytest.mark.asyncio
async def test_a_failure_to_send_text_is_the_error_the_session_retries_on(
    error: BaseException,
) -> None:
    """One error for every reason, because every reason has the same answer.

    Args:
        error: What the library raised.
    """
    transport = transport_over(BrokenConnection(error))

    with pytest.raises(ConnectionFailedError):
        await transport.send_text("anything")


@pytest.mark.asyncio
async def test_a_failure_to_send_a_frame_is_the_same_error() -> None:
    """A frame and a control message fail the same way and are retried alike."""
    transport = transport_over(BrokenConnection(WebSocketException("gone")))

    with pytest.raises(ConnectionFailedError):
        await transport.send_bytes(b"a frame")


@pytest.mark.asyncio
async def test_a_failure_to_receive_is_the_same_error() -> None:
    """Which is what the results loop reconnects on."""
    transport = transport_over(BrokenConnection(OSError("network is down")))

    with pytest.raises(ConnectionFailedError):
        await transport.receive()


@pytest.mark.asyncio
async def test_binary_from_the_groundstation_ends_the_connection() -> None:
    """Frames travel one way; a connection sending them back is not usable."""
    transport = transport_over(BinaryConnection())

    with pytest.raises(ConnectionFailedError, match="travels the other way"):
        await transport.receive()


@pytest.mark.asyncio
async def test_closing_reaches_the_connection() -> None:
    """The adapter holds nothing of its own to release."""
    connection = BrokenConnection(WebSocketException("gone"))
    transport = transport_over(connection)

    await transport.close()

    assert connection.closed
