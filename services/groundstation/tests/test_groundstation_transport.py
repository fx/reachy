"""The WebSocket adapter: how a framework's failures become one exception.

The session layer sees `TransportClosedError` and nothing else, which is what
lets it be written without a web framework in it. Getting there means mapping
several unrelated Starlette failures onto that one exception, and each of those
is a state a real connection reaches only by losing a race.

So the connection here is a stub. The real one is driven end to end by
`test_groundstation_integration.py`; what is checked here is the mapping, which a
real connection cannot be made to produce on demand.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`. Nothing here touches a socket, a clock or a file.
"""

from __future__ import annotations

from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect, WebSocketState

from reachy_groundstation.api.websocket import WebSocketTransport
from reachy_groundstation.session.transport import (
    CLOSE_NORMAL,
    TransportClosedError,
)


class _Stub:
    """A stand-in for a Starlette `WebSocket` that fails on demand.

    Attributes:
        client_state: What Starlette would report about the connection.
        sent: What was sent through it.
        closed: The code and reason it was closed with.
    """

    def __init__(
        self,
        *,
        message: dict[str, Any] | None = None,
        raises: Exception | None = None,
        state: WebSocketState = WebSocketState.CONNECTED,
    ) -> None:
        """Create a stub.

        Args:
            message: What `receive` should hand back.
            raises: What every operation should raise instead.
            state: What `client_state` should report.
        """
        self._message = message
        self._raises = raises
        self.client_state = state
        self.sent: list[str] = []
        self.closed: tuple[int, str] | None = None

    async def receive(self) -> dict[str, Any]:
        """Hand back the configured message, or fail.

        Returns:
            The message.

        Raises:
            Exception: Whatever this stub was built to raise.
        """
        if self._raises is not None:
            raise self._raises
        return self._message or {}

    async def send_text(self, text: str) -> None:
        """Record an outgoing message, or fail.

        Args:
            text: The message.

        Raises:
            Exception: Whatever this stub was built to raise.
        """
        if self._raises is not None:
            raise self._raises
        self.sent.append(text)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """Record the close, or fail.

        Args:
            code: The close code.
            reason: The explanation.

        Raises:
            Exception: Whatever this stub was built to raise.
        """
        if self._raises is not None:
            raise self._raises
        self.closed = (code, reason)


def _transport(stub: _Stub) -> WebSocketTransport:
    """Wrap a stub as the transport the session layer sees.

    Args:
        stub: The stand-in connection.

    Returns:
        The transport.
    """
    return WebSocketTransport(stub)  # type: ignore[arg-type]  # a stand-in, see the module docstring


@pytest.mark.asyncio
async def test_a_text_message_arrives_as_text() -> None:
    """A control message is a string on this side of the seam."""
    transport = _transport(_Stub(message={"type": "websocket.receive", "text": "hi"}))
    assert await transport.receive() == "hi"


@pytest.mark.asyncio
async def test_a_binary_message_arrives_as_bytes() -> None:
    """A frame is bytes, and is never decoded on the way in."""
    transport = _transport(
        _Stub(message={"type": "websocket.receive", "bytes": b"\x01\x02"}),
    )
    assert await transport.receive() == b"\x01\x02"


@pytest.mark.asyncio
async def test_a_disconnect_message_ends_the_session() -> None:
    """Starlette reports the disconnect as a message, not as an exception."""
    transport = _transport(_Stub(message={"type": "websocket.disconnect"}))
    with pytest.raises(TransportClosedError):
        await transport.receive()


@pytest.mark.asyncio
async def test_a_disconnect_exception_ends_the_session() -> None:
    """And sometimes it reports it as an exception instead."""
    transport = _transport(_Stub(raises=WebSocketDisconnect(1001)))
    with pytest.raises(TransportClosedError):
        await transport.receive()


@pytest.mark.asyncio
async def test_a_runtime_error_on_a_gone_connection_ends_the_session() -> None:
    """A connection already gone raises this, and it means the same thing."""
    transport = _transport(_Stub(raises=RuntimeError("already disconnected")))
    with pytest.raises(TransportClosedError):
        await transport.receive()


@pytest.mark.asyncio
async def test_a_message_that_is_neither_text_nor_bytes_ends_the_session() -> None:
    """There is no third kind of message this protocol has a meaning for."""
    transport = _transport(_Stub(message={"type": "websocket.receive"}))
    with pytest.raises(TransportClosedError):
        await transport.receive()


@pytest.mark.asyncio
async def test_sending_reaches_the_connection() -> None:
    """The ordinary path: a framed message goes out as text."""
    stub = _Stub()
    await _transport(stub).send("a message")
    assert stub.sent == ["a message"]


@pytest.mark.asyncio
async def test_sending_to_a_gone_connection_ends_the_session() -> None:
    """A result for a client that left is not an error worth propagating."""
    transport = _transport(_Stub(raises=RuntimeError("connection is closed")))
    with pytest.raises(TransportClosedError):
        await transport.send("a message")


@pytest.mark.asyncio
async def test_closing_passes_the_code_and_the_reason_through() -> None:
    """The close code is contract: a refused client must see 1008."""
    stub = _Stub()
    await _transport(stub).close(CLOSE_NORMAL, "going away")
    assert stub.closed == (CLOSE_NORMAL, "going away")


@pytest.mark.asyncio
async def test_closing_an_already_closed_connection_does_nothing() -> None:
    """Shutdown runs on a path a disconnect has often already travelled."""
    stub = _Stub(state=WebSocketState.DISCONNECTED)
    await _transport(stub).close(CLOSE_NORMAL, "going away")
    assert stub.closed is None


@pytest.mark.asyncio
async def test_a_connection_that_goes_during_the_close_is_not_an_error() -> None:
    """There is nothing left to tell anybody, so there is nothing to raise."""
    stub = _Stub(raises=RuntimeError("cannot close"))
    await _transport(stub).close(CLOSE_NORMAL, "going away")
    assert stub.closed is None
