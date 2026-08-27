r"""Drive a real session against a running groundstation and report what happened.

This is the half of `just image-verify` that exercises the artifact rather than
inspecting it. A Dockerfile that builds successfully and produces a service that
cannot start is a passing build and a broken release, so what CI checks is that
the built image, started, warms its models up, reports itself ready and answers a
frame with a detection — the same sequence a robot performs.

**The session is driven by `reachy_session_client`, not by anything written
here.** Reachyctl REQ-057 makes that package the one client implementation of the
protocol, and a second one written to test an image is exactly what it forbids:
it would pass its own expectations and prove nothing about what a robot meets.
So this module contributes a readiness poll, a file read and an exit status, and
the negotiation, the framing and the result envelope are the shared client's.

It is designed to run *inside a container on the same Docker network as the
service*, which is what lets the service be verified while attached to a network
with no route off the host — see the `image-verify` recipe, which mounts this
script and the client's source into a sibling container. Nothing here assumes
otherwise: it is given a base URL and it uses it.

Run it as a script:

    python scripts/verify_groundstation_image.py \\
        --base-url http://127.0.0.1:8080 --credential …

The default frame is a committed perception fixture with one face in it, so a
successful run is evidence that the model baked into the image loaded and ran,
not merely that a WebSocket opened.

The operator feed is verified in the same run and from inside the same session,
because that is the only state in which it can be: `/stream.mjpg` serves a frame
only while exactly one authenticated session has supplied one. So after the
result comes back — the frame is offered to the feed as it is decoded, which is
before the result is sent — one multipart part is read off the endpoint and
compared with the fixture's own bytes. Reading it takes no second session and no
second client: it is an ordinary HTTP request, made with the standard library
over the same isolated network.

All three things this does with `--base-url` read its scheme the same way. The
session endpoint becomes `ws://` or `wss://`, readiness goes through urllib, and
the feed connection is plaintext or TLS on the port that scheme implies. An
`https://` base URL is a supported invocation, and one that half of the script
understood would fail in a way that reads as the service being broken.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from reachy_contracts import FACE_CAPABILITY, Capability
from reachy_groundstation.api.app import SESSION_PATH, STREAM_PATH
from reachy_session_client import Credential, FrameResult, SessionClient

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "VerificationError",
    "drive_session",
    "frame_bytes",
    "main",
    "read_feed_part",
    "readiness",
    "session_url",
    "wait_until_ready",
]

# The version of the face capability this client speaks. It is the capability's
# own `FACE_VERSION`, restated as a literal rather than imported, because
# negotiation is where a client and a service discover they disagree: importing
# the service's constant would make the two agree by construction and verify
# nothing. A bump to the capability that this does not follow shows up as an
# empty agreed set, which is exactly the failure worth seeing.
_FACE_VERSION: Final = 1

# How long to wait for a result that should already be on its way. Generous
# because the ARM image is verified under emulation, where one detection pass
# costs seconds rather than tens of milliseconds.
_RESULT_TIMEOUT_SECONDS: Final = 120.0
_POLL_INTERVAL_SECONDS: Final = 1.0

# The frame driven through the service when none is named. One face, drawn
# rather than photographed — see the NOTICE beside it.
_DEFAULT_FRAME: Final = (
    Path(__file__).resolve().parent.parent
    / "services"
    / "groundstation"
    / "tests"
    / "fixtures"
    / "perception"
    / "face_single.jpg"
)


class VerificationError(RuntimeError):
    """The running image did not do what a deployed groundstation must do."""


def session_url(base_url: str) -> str:
    """Derive the session endpoint from the service's base URL.

    Args:
        base_url: Where the service is listening, as an HTTP URL.

    Returns:
        The WebSocket URL of the session endpoint.

    Raises:
        VerificationError: If `base_url` is not an HTTP or HTTPS URL. The scheme
            decides the WebSocket scheme, so guessing at one would produce a
            connection failure that reads as the service being broken.
    """
    trimmed = base_url.rstrip("/")
    for http, websocket in (("https://", "wss://"), ("http://", "ws://")):
        if trimmed.startswith(http):
            return f"{websocket}{trimmed[len(http) :]}{SESSION_PATH}"
    message = f"--base-url must be an http or https URL, got {base_url!r}"
    raise VerificationError(message)


def frame_bytes(path: Path) -> bytes:
    """Read the frame to send, exactly as capture hardware would have produced it.

    The bytes are handed to the client compressed and are never re-encoded on
    the way, which is the one thing the protocol is careful not to do.

    Args:
        path: The JPEG to send.

    Returns:
        Its contents.

    Raises:
        VerificationError: If the file is not there.
    """
    if not path.is_file():
        message = f"no frame to send: {path} is not a file"
        raise VerificationError(message)
    return path.read_bytes()


def readiness(base_url: str, timeout: float) -> tuple[bool, object]:
    """Ask the service once whether it is ready to be sent work.

    Args:
        base_url: Where the service is listening.
        timeout: How long to wait for the answer.

    Returns:
        Whether it reported itself ready, and the body it reported it in. A
        service that is starting answers 503 with a body saying which capability
        is still warming, so the body is worth carrying back either way.
    """
    request = urllib.request.Request(  # noqa: S310  # the scheme is checked by `session_url` before this runs and the URL is an argument to a verification script, not input to the service
        f"{base_url.rstrip('/')}/readyz",
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310  # same request object, already scheme-checked
            raw = response.read()
    except urllib.error.HTTPError as error:
        return False, _decoded(error.read())
    except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
        return False, repr(error)
    body = _decoded(raw)
    return bool(isinstance(body, dict) and body.get("ready")), body


def _decoded(raw: bytes) -> object:
    """Decode a readiness body, falling back to the text that was sent.

    A wrong port, a proxy error page or an empty body would otherwise end the
    run in a `JSONDecodeError` traceback rather than in a message naming what
    answered.

    Args:
        raw: The bytes of the answer.

    Returns:
        The parsed document, or the text when it is not JSON.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.decode("utf-8", "replace")


def wait_until_ready(base_url: str, deadline_seconds: float) -> object:
    """Poll readiness until the service reports itself ready, or give up.

    Groundstation REQ-026 makes readiness mean warm-up finished, so this is the
    point at which the model baked into the image is known to have loaded.

    Args:
        base_url: Where the service is listening.
        deadline_seconds: How long to keep asking.

    Returns:
        The body of the answer that reported ready.

    Raises:
        VerificationError: If the deadline passes first. The message carries the
            last answer, which names the capability that did not warm up.
    """
    deadline = time.monotonic() + deadline_seconds
    last: object = "nothing answered yet"
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        ready, last = readiness(base_url, timeout=min(remaining, 5.0))
        if ready:
            return last
        time.sleep(_POLL_INTERVAL_SECONDS)
    message = (
        f"the service did not report ready within {deadline_seconds:.0f}s; "
        f"last answer: {last!r}"
    )
    raise VerificationError(message)


# What a scheme means when a URL names no port, and the whole set of schemes this
# script speaks. `session_url` maps the same two onto `ws://` and `wss://` and
# readiness reaches them through urllib, so a third code path understanding only
# one of them is what makes `--base-url https://…` half-work.
_DEFAULT_PORTS: Final[Mapping[str, int]] = MappingProxyType({"http": 80, "https": 443})


@dataclass(frozen=True, slots=True)
class _FeedEndpoint:
    """Where to open the feed connection, and whether to wrap it in TLS.

    Attributes:
        host: What to connect to.
        port: Which port, defaulted from the scheme when the URL named none.
        netloc: What to send as `Host`, exactly as the caller wrote it.
        tls: The context to negotiate with, or `None` for a plaintext
            connection. It is `create_default_context`, so an `https://` base
            URL is verified rather than merely encrypted — a verifier that
            accepted any certificate would report a misdirected connection as a
            working one.
    """

    host: str
    port: int
    netloc: str
    tls: ssl.SSLContext | None


def _feed_endpoint(base_url: str) -> _FeedEndpoint:
    """Work out how to reach the feed from the service's base URL.

    Args:
        base_url: Where the service is listening.

    Returns:
        The connection to open.

    Raises:
        VerificationError: If the URL names no scheme this script speaks, or no
            host. Both are the caller's mistake, and connecting anyway would
            report one as the service being broken.
    """
    split = urllib.parse.urlsplit(base_url.rstrip("/"))
    if split.scheme not in _DEFAULT_PORTS:
        message = f"--base-url must be an http or https URL, got {base_url!r}"
        raise VerificationError(message)
    if split.hostname is None:
        message = f"--base-url names no host: {base_url!r}"
        raise VerificationError(message)
    return _FeedEndpoint(
        host=split.hostname,
        port=split.port or _DEFAULT_PORTS[split.scheme],
        netloc=split.netloc,
        tls=ssl.create_default_context() if split.scheme == "https" else None,
    )


def _parse_status(line: bytes) -> int:
    """Read the status code out of an HTTP status line.

    Args:
        line: The first line of the response, without its line ending.

    Returns:
        The status code.

    Raises:
        VerificationError: If the line is not an HTTP status line. Something
            other than the service answering is worth a message that says so.
    """
    fields = line.split(maxsplit=2)
    if len(fields) < 2 or not fields[0].startswith(b"HTTP/"):
        message = f"the feed answered with {line!r}, which is not an HTTP response"
        raise VerificationError(message)
    try:
        return int(fields[1])
    except ValueError as error:
        message = f"the feed answered with {line!r}, which names no status code"
        raise VerificationError(message) from error


def _parse_headers(block: bytes) -> dict[str, str]:
    """Read a header block into a mapping keyed by lower-cased name.

    Args:
        block: The header lines, without the blank line that ends them and
            without the status or boundary line above them.

    Returns:
        Header name to value.
    """
    headers: dict[str, str] = {}
    for line in block.split(b"\r\n"):
        name, separator, value = line.partition(b":")
        if separator:
            headers[name.decode("latin-1").strip().lower()] = value.decode(
                "latin-1",
            ).strip()
    return headers


async def _read_until(
    reader: asyncio.StreamReader,
    buffer: bytearray,
    marker: bytes,
) -> bytes:
    """Read until a marker has arrived, and take everything before it.

    Args:
        reader: Where the bytes come from.
        buffer: What has already been read and not yet consumed.
        marker: The separator to stop at.

    Returns:
        The bytes before the marker, which are removed from `buffer` along with
        the marker itself.

    Raises:
        VerificationError: If the connection ended before the marker arrived.
    """
    while marker not in buffer:
        chunk = await reader.read(65536)
        if not chunk:
            message = f"the feed closed before sending {marker!r}"
            raise VerificationError(message)
        buffer += chunk
    head, _, rest = bytes(buffer).partition(marker)
    buffer[:] = rest
    return head


async def read_feed_part(base_url: str) -> tuple[Mapping[str, str], bytes]:
    """Read exactly one multipart part off the operator feed.

    The request is HTTP/1.0 on purpose: the response is an endless stream, and
    HTTP/1.0 framing means the parts arrive as they were written rather than
    inside a chunked encoding this would then have to unwrap. What is being
    checked is the multipart framing, not a transfer encoding.

    The scheme decides the transport, the same way it decides the WebSocket
    scheme in `session_url`: an `https://` base URL is reached over TLS and on
    443 unless the URL names a port. This connection is opened by hand rather
    than through urllib because the response never ends, so `https://` has to be
    honoured here too — a plaintext socket on port 80 would fail against exactly
    the deployment the other two code paths support.

    Args:
        base_url: Where the service is listening.

    Returns:
        The part's headers and its body, the body taken by the length the part
        declared rather than by searching for the next boundary.

    Raises:
        VerificationError: If the base URL names no scheme this script speaks or
            no host, or the endpoint refused, answered as something other than a
            multipart stream, or sent a part this cannot read.
    """
    endpoint = _feed_endpoint(base_url)
    reader, writer = await asyncio.open_connection(
        endpoint.host,
        endpoint.port,
        ssl=endpoint.tls,
    )
    buffer = bytearray()
    try:
        writer.write(
            f"GET {STREAM_PATH} HTTP/1.0\r\nHost: {endpoint.netloc}\r\n\r\n".encode(
                "latin-1",
            ),
        )
        await writer.drain()

        head = await _read_until(reader, buffer, b"\r\n\r\n")
        status_line, _, header_block = head.partition(b"\r\n")
        status = _parse_status(status_line)
        response_headers = _parse_headers(header_block)
        if status != 200:
            message = (
                f"the feed answered {status} rather than serving a stream; "
                f"the session that should have made it eligible was still open"
            )
            raise VerificationError(message)
        media_type = response_headers.get("content-type", "")
        if not media_type.startswith("multipart/x-mixed-replace"):
            message = f"the feed answered as {media_type!r}, not as a multipart stream"
            raise VerificationError(message)

        # The first boundary line, then the part's own headers.
        await _read_until(reader, buffer, b"\r\n")
        part_headers = _parse_headers(
            await _read_until(reader, buffer, b"\r\n\r\n"),
        )
        try:
            length = int(part_headers["content-length"])
        except (KeyError, ValueError) as error:
            message = f"the part declared no usable length: {part_headers}"
            raise VerificationError(message) from error

        while len(buffer) < length:
            chunk = await reader.read(65536)
            if not chunk:
                message = (
                    f"the part declared {length} bytes and the feed closed "
                    f"after {len(buffer)}"
                )
                raise VerificationError(message)
            buffer += chunk
        return part_headers, bytes(buffer[:length])
    finally:
        writer.close()
        with suppress(ConnectionError, OSError):
            await writer.wait_closed()


async def drive_session(
    url: str,
    credential: str,
    payload: bytes,
    base_url: str,
) -> tuple[FrameResult, int]:
    """Negotiate a session, send one frame, read the answer and the feed back.

    Every protocol step here belongs to `SessionClient`: the offer it builds,
    the agreement it parses, the framing it applies, and the envelope it
    validates. What is left is the assertions — that the capability was actually
    agreed, that something came back, and that the operator feed served the very
    bytes that were sent.

    The feed is read before the session is left, because leaving it is what makes
    the feed ineligible: it holds a frame for exactly one authenticated session
    and discards it when that session ends.

    Args:
        url: The session endpoint.
        credential: What the service authenticates the session against.
        payload: The compressed frame to send.
        base_url: Where the service is listening, for the feed request.

    Returns:
        The result the face capability produced for that frame, and how many
        bytes the feed served for it.

    Raises:
        VerificationError: If the service agreed to no face capability, dropped
            the frame because no session was up, sent no result before the
            deadline, or served a feed part that is not the frame that was sent.
    """
    client = SessionClient(
        url=url,
        credential=Credential(credential),
        capabilities=(Capability(name=FACE_CAPABILITY, version=_FACE_VERSION),),
        # Long enough that the emulated ARM run's result is still worth acting
        # on when it arrives. `latest` is not read here, but a result the client
        # considers stale is a result this would have to reason about.
        staleness_seconds=_RESULT_TIMEOUT_SECONDS,
    )
    async with client:
        agreement = await client.connect()
        if client.agreed(FACE_CAPABILITY) is None:
            message = (
                f"the service agreed to {[c.name for c in agreement.capabilities]}, "
                f"which does not include {FACE_CAPABILITY!r}; the model in the "
                f"image did not load"
            )
            raise VerificationError(message)

        if await client.submit_frame(payload) is None:
            message = "the frame was dropped: no session was up to send it on"
            raise VerificationError(message)

        result = await _first_result(client)
        headers, body = await asyncio.wait_for(
            read_feed_part(base_url),
            timeout=_RESULT_TIMEOUT_SECONDS,
        )

    if headers.get("content-type") != "image/jpeg":
        message = f"the part was labelled {headers.get('content-type')!r}, not JPEG"
        raise VerificationError(message)
    if body != payload:
        message = (
            f"the feed served {len(body)} bytes that are not the {len(payload)} "
            f"the frame was sent as; the original payload was not what was kept"
        )
        raise VerificationError(message)
    return result, len(body)


async def _first_result(client: SessionClient) -> FrameResult:
    """Take the first result off a session and stop the client reconnecting.

    Iteration is what keeps the link up, so leaving the generator open would
    leave the client reconnecting on this function's behalf after it has what it
    came for. Closing it is part of using it.

    Args:
        client: The connected session.

    Returns:
        The first result the service sent.

    Raises:
        VerificationError: If none arrived before the deadline.
    """
    results = client.results()
    try:
        return await asyncio.wait_for(
            anext(results),
            timeout=_RESULT_TIMEOUT_SECONDS,
        )
    except StopAsyncIteration as end:
        message = "the session ended before the frame was answered"
        raise VerificationError(message) from end
    except TimeoutError as expired:
        message = (
            f"no result arrived within {_RESULT_TIMEOUT_SECONDS:.0f}s of the "
            f"frame being sent"
        )
        raise VerificationError(message) from expired
    finally:
        await results.aclose()


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    """Read the command line.

    Args:
        argv: The arguments, or `None` to read the real ones.

    Returns:
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(
        prog="verify_groundstation_image.py",
        description=(
            "Wait for a running groundstation to report ready, drive one real "
            "session through it, and fail unless it answers with a detection "
            "and serves the same frame back on its operator feed."
        ),
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8080",
        help="where the service is listening (default: %(default)s)",
    )
    parser.add_argument(
        "--credential",
        required=True,
        help="the shared secret the session is authenticated with",
    )
    parser.add_argument(
        "--frame",
        type=Path,
        default=_DEFAULT_FRAME,
        help="the JPEG to send (default: the one-face perception fixture)",
    )
    parser.add_argument(
        "--ready-timeout",
        type=float,
        default=180.0,
        help="how long to wait for readiness, in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--expect-detections",
        type=int,
        default=1,
        help="how many detections the answer must carry (default: %(default)s)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Verify a running groundstation image.

    Args:
        argv: Command-line arguments, or `None` to read the real ones.

    Returns:
        The process exit status: 0 when the service warmed up and answered, 1
        otherwise.
    """
    arguments = _parse_arguments(argv)
    try:
        url = session_url(arguments.base_url)
        health = wait_until_ready(arguments.base_url, arguments.ready_timeout)
        sys.stdout.write(f"image-verify: ready: {json.dumps(health)}\n")

        payload = frame_bytes(arguments.frame)
        result, served = asyncio.run(
            drive_session(
                url,
                arguments.credential,
                payload,
                arguments.base_url,
            ),
        )
        round_trip = (
            "unmeasured"
            if result.round_trip_seconds is None
            else f"{result.round_trip_seconds * 1000:.0f} ms"
        )
        sys.stdout.write(
            f"image-verify: {arguments.frame.name} ({len(payload)} bytes) "
            f"answered by {result.capability!r} at sequence {result.sequence} "
            f"with {result.detections} detection(s) in {round_trip}: "
            f"{result.payload.to_wire().decode('utf-8')}\n",
        )
        sys.stdout.write(
            f"image-verify: {STREAM_PATH} served the same {served} bytes as "
            f"one image/jpeg part\n",
        )
        if result.detections < arguments.expect_detections:
            message = (
                f"expected at least {arguments.expect_detections} detection(s) "
                f"in {arguments.frame.name}, got {result.detections}"
            )
            raise VerificationError(message)
    except VerificationError as error:
        sys.stderr.write(f"image-verify: {error}\n")
        return 1
    sys.stdout.write(
        "image-verify: the running image answered a real session and served its "
        "frame back\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
