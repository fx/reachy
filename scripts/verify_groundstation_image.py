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
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Final

from reachy_contracts import FACE_CAPABILITY, Capability
from reachy_groundstation.api.app import SESSION_PATH
from reachy_session_client import Credential, FrameResult, SessionClient

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "VerificationError",
    "drive_session",
    "frame_bytes",
    "main",
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


async def drive_session(url: str, credential: str, payload: bytes) -> FrameResult:
    """Negotiate a session, send one frame, and read the answer back.

    Every protocol step here belongs to `SessionClient`: the offer it builds,
    the agreement it parses, the framing it applies, and the envelope it
    validates. What is left is the assertions — that the capability was actually
    agreed, and that something came back.

    Args:
        url: The session endpoint.
        credential: What the service authenticates the session against.
        payload: The compressed frame to send.

    Returns:
        The result the face capability produced for that frame.

    Raises:
        VerificationError: If the service agreed to no face capability, dropped
            the frame because no session was up, or sent no result before the
            deadline.
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

        return await _first_result(client)


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
            "session through it, and fail unless it answers with a detection."
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
        result = asyncio.run(drive_session(url, arguments.credential, payload))
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
        if result.detections < arguments.expect_detections:
            message = (
                f"expected at least {arguments.expect_detections} detection(s) "
                f"in {arguments.frame.name}, got {result.detections}"
            )
            raise VerificationError(message)
    except VerificationError as error:
        sys.stderr.write(f"image-verify: {error}\n")
        return 1
    sys.stdout.write("image-verify: the running image answered a real session\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
