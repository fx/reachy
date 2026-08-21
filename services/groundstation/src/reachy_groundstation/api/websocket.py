"""The Starlette WebSocket, presented as the narrow thing a session needs.

This is the only module in the service that knows what a WebSocket is. It exists
so that `session/` depends on a four-method protocol rather than on a web
framework — and so that the integration tests can drive the real server, the real
frames and the real close codes without the session layer having a seam that only
a test uses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.websockets import WebSocketDisconnect, WebSocketState

from reachy_groundstation.session.transport import TransportClosedError

if TYPE_CHECKING:
    from starlette.websockets import WebSocket

__all__ = ["WebSocketTransport"]


class WebSocketTransport:
    """Adapts a Starlette `WebSocket` to `SessionTransport`."""

    def __init__(self, websocket: WebSocket) -> None:
        """Wrap an accepted connection.

        Args:
            websocket: The connection, already accepted.
        """
        self._websocket = websocket

    async def receive(self) -> str | bytes:
        """Wait for the next message from the client.

        Returns:
            The text of a control message, or the bytes of a frame.

        Raises:
            TransportClosedError: If the connection ended, or if the client sent a
                message that is neither text nor binary.
        """
        try:
            message = await self._websocket.receive()
        except WebSocketDisconnect as error:
            raise TransportClosedError(str(error)) from error
        except RuntimeError as error:
            # Starlette raises this when the connection has already gone. It is
            # the same event as a disconnect from this side of the seam.
            raise TransportClosedError(str(error)) from error

        if message["type"] == "websocket.disconnect":
            raise TransportClosedError("client disconnected")
        text = message.get("text")
        if text is not None:
            return str(text)
        data = message.get("bytes")
        if data is not None:
            return bytes(data)
        raise TransportClosedError("connection produced neither text nor bytes")

    async def send(self, text: str) -> None:
        """Send one control message or result.

        Args:
            text: The already-framed message.

        Raises:
            TransportClosedError: If the connection ended.
        """
        try:
            await self._websocket.send_text(text)
        except (WebSocketDisconnect, RuntimeError) as error:
            raise TransportClosedError(str(error)) from error

    async def close(self, code: int, reason: str) -> None:
        """End the connection, tolerating one that has already ended.

        Args:
            code: The RFC 6455 close code.
            reason: A short explanation, never a credential.
        """
        if self._websocket.client_state is WebSocketState.DISCONNECTED:
            return
        try:
            await self._websocket.close(code=code, reason=reason)
        except RuntimeError:
            # The connection went away between the check and the close. There
            # is nothing left to tell anybody.
            return
