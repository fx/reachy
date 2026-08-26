"""`/stream.mjpg`: the retained frame, framed the way every MJPEG client expects.

`multipart/x-mixed-replace` is the oldest streaming convention on the web and the
one Home Assistant's built-in MJPEG IP Camera integration speaks, which is why
this service serves it rather than something newer: the integration's required
input is a stream URL, and this is that URL. Each part declares `image/jpeg` and
the payload's exact length, so a client can read a part without scanning for the
next boundary.

**The endpoint is deliberately unauthenticated**, and that is a deployment
decision recorded in the spec rather than an omission: the groundstation's health,
metrics and configuration surfaces are unauthenticated too, and network reach is
the trust boundary. Camera frames are more sensitive than any of those, so the
setup runbooks say so in as many words. What bounds the exposure here is
everything else about the response: no store, one global frame and no history,
four viewers at once, and a slot released the moment the response ends however it
ends.

Three refusals, three status codes, and they are distinguishable on purpose:

| Situation | Status | Body |
|---|---|---|
| No authenticated session, or none has supplied a validated JPEG yet | 503 | `no_eligible_session` |
| More than one authenticated session | 409 | `ambiguous_sessions` |
| Every viewer slot is taken | 429 | `viewer_capacity_reached` |

503 is the ordinary "the upstream this depends on is not there"; 409 is a
conflict with the current state of the resource, which is exactly what two robots
are — the feed refuses rather than picking one by connection order or frame
recency; and 429 is the standard answer to a concurrency bound, which keeps
"nothing to show" and "no room to show it in" from arriving as the same code. The
bodies name the situation and nothing else: no session identifier, no address, no
count.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from starlette.responses import JSONResponse, Response, StreamingResponse

from reachy_groundstation.feed import FeedAvailability

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from starlette.types import Receive, Scope, Send

    from reachy_groundstation.feed import FeedRegistry

__all__ = ["BOUNDARY", "STREAM_PATH", "stream_response"]

# The path an operator gives Home Assistant, and the one the runbooks quote.
STREAM_PATH: Final = "/stream.mjpg"

# A fixed token, and fixed is what makes it safe: it is not derived from
# anything, so no payload, address or identifier can reach a header through it,
# and it cannot occur inside JPEG data because every part declares its own
# length and a reader never has to search for it.
BOUNDARY: Final = "reachyframe"

_MEDIA_TYPE: Final = f"multipart/x-mixed-replace; boundary={BOUNDARY}"

# Frames are not for keeping, so nothing between here and the viewer may keep
# them. `no-store` is the directive that says so to a cache, a proxy and a
# browser alike, and it goes on every answer this module gives — a refusal
# included, because a cached 503 is a camera that stays dark after the robot
# comes back. Read-only, and shared rather than rebuilt, so the three call sites
# cannot end up naming the header three slightly different ways.
_NO_STORE: Final[Mapping[str, str]] = MappingProxyType({"Cache-Control": "no-store"})

_PART_PREFIX: Final = f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n".encode("ascii")

_CAPACITY_REACHED: Final = "viewer_capacity_reached"

_REFUSALS: Final = {
    FeedAvailability.NO_ELIGIBLE_SESSION: 503,
    FeedAvailability.AMBIGUOUS_SESSIONS: 409,
}


def _part(payload: bytes) -> bytes:
    """Frame one payload as a multipart part.

    Args:
        payload: The original compressed frame, sent on unchanged.

    Returns:
        The bytes to write for this part, headers and all.
    """
    return b"".join(
        (
            _PART_PREFIX,
            b"Content-Length: ",
            str(len(payload)).encode("ascii"),
            b"\r\n\r\n",
            payload,
            b"\r\n",
        ),
    )


def _refusal(reason: str, status_code: int) -> Response:
    """Answer a request that is not going to be given a stream.

    Args:
        reason: The stable identifier-free name of the situation.
        status_code: What to answer with.

    Returns:
        The response.
    """
    return JSONResponse(
        {"feed": reason},
        status_code=status_code,
        headers=_NO_STORE,
    )


async def _parts(feed: FeedRegistry) -> AsyncIterator[bytes]:
    """Yield one part per retained frame, newest first, until the viewer is done.

    Nothing is buffered between rounds: each one asks for whatever is current and
    newer than the last part sent, so a viewer that spent a long time on its last
    part comes back to the frame the robot has now rather than to the queue it
    would otherwise have accumulated.

    Args:
        feed: What holds the live frame.

    Yields:
        The bytes of each part.
    """
    sent = 0
    while True:
        frame = await feed.next_frame(after=sent)
        if frame is None:
            return
        sent = frame.revision
        yield _part(frame.payload)


class _ViewerStream(StreamingResponse):
    """A stream that gives its viewer slot back whenever the response ends.

    The release is in `__call__` rather than in the body generator because a
    generator suspended at a `yield` is not running, so a cancellation that
    arrives while the server is writing a part reaches this frame and not that
    one. Wrapping the whole response is what makes "released on disconnect, on
    cancellation and on ordinary completion" one statement instead of three.
    """

    def __init__(self, feed: FeedRegistry) -> None:
        """Build the response over a reservation the caller already took.

        Args:
            feed: What holds the live frame and owes this response one slot.
        """
        self._feed = feed
        super().__init__(
            _parts(feed),
            media_type=_MEDIA_TYPE,
            headers=_NO_STORE,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the stream and release the slot afterwards.

        Args:
            scope: The ASGI connection scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.
        """
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._feed.release_viewer()


def stream_response(feed: FeedRegistry, method: str) -> Response:
    """Answer one request for the feed.

    Eligibility is decided first and capacity second, so an operator with no
    robot connected is told that rather than being told the service is busy.
    Both decisions and the reservation happen without awaiting, so two requests
    arriving together cannot both take the last slot.

    Args:
        feed: What holds the live frame.
        method: The request method. Starlette answers `HEAD` with whatever
            serves `GET`, and a `HEAD` that started a stream would hold a viewer
            slot for the life of a connection whose body nothing ever reads —
            on an unauthenticated endpoint, four of those are the whole bound.

    Returns:
        The stream, or the refusal that says why there is not one.
    """
    availability = feed.availability()
    if availability is not FeedAvailability.AVAILABLE:
        return _refusal(availability.value, _REFUSALS[availability])
    if method == "HEAD":
        # What a `GET` would have started, and no further. Capacity is not
        # consulted because nothing is reserved: this answer is over by the time
        # it is sent.
        return Response(
            status_code=200,
            media_type=_MEDIA_TYPE,
            headers=_NO_STORE,
        )
    if not feed.reserve_viewer():
        return _refusal(_CAPACITY_REACHED, 429)
    return _ViewerStream(feed)
