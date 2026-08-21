"""Drive a real session against a running groundstation and report what happened.

This is the half of `just image-verify` that exercises the artifact rather than
inspecting it. A Dockerfile that builds successfully and produces a service that
cannot start is a passing build and a broken release, so what CI checks is that
the built image, started, warms its models up, reports itself ready and answers a
frame with a detection — the same sequence a robot performs.

**It does not reimplement the session protocol.** The offer, the framing and the
result envelope come from `reachy_contracts` and
`reachy_groundstation.session.framing`, which are the same modules the service
parses with. A hand-rolled client here would drift from the protocol and the
drift would look like a passing verification.

It is designed to run *inside a container on the same Docker network as the
service*, which is what lets the service be verified while attached to a network
with no route off the host — see the `image-verify` recipe. Nothing here assumes
otherwise: it is given a base URL and it uses it.

Run it as a script:

    python scripts/verify_groundstation_image.py --base-url http://127.0.0.1:8080

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

from websockets.asyncio.client import connect

from reachy_contracts import (
    FACE_CAPABILITY,
    Capability,
    CaptureTimestamp,
    FaceDetections,
    FrameHeader,
    ResultEnvelope,
    SessionAgreement,
    SessionOffer,
)
from reachy_groundstation.api.app import SESSION_PATH
from reachy_groundstation.session.framing import (
    MessageKind,
    decode_control,
    encode_control,
    encode_frame,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from websockets.asyncio.client import ClientConnection

__all__ = [
    "VerificationError",
    "capture_stamp",
    "drive_session",
    "frame_for",
    "main",
    "offer_for",
    "readiness",
    "session_url",
    "wait_until_ready",
]

# The version of the face capability this client speaks. It is the capability's
# own `FACE_VERSION`, restated as a literal rather than imported, because
# negotiation is where a client and a service discover they disagree: importing
# the service's constant would make the two agree by construction and verify
# nothing. A bump to the capability that this does not follow shows up here as
# an empty agreed set, which is exactly the failure worth seeing.
_FACE_VERSION: Final = 1

# What a client with no clock of its own puts in a frame header. The
# groundstation copies it onto the result byte for byte and never reads it, so
# any token does; this one is recognisable in a log.
_CAPTURE_LABEL: Final = "image-verify"

# How long to wait for something that should already be on its way. Warm-up runs
# one real inference before the service reports ready, so the readiness deadline
# is separately generous.
_MESSAGE_TIMEOUT_SECONDS: Final = 30.0
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


def capture_stamp(sequence: int) -> CaptureTimestamp:
    """Mint the opaque capture token for one frame.

    Args:
        sequence: The frame's number within the session.

    Returns:
        A token this client can recognise in a result and in a log line.
    """
    return CaptureTimestamp(f"{_CAPTURE_LABEL}-{sequence}")


def offer_for(credential: str) -> str:
    """Build the control message that opens a session.

    Args:
        credential: What the service authenticates the session against.

    Returns:
        The encoded offer.
    """
    return encode_control(
        MessageKind.OFFER,
        SessionOffer.model_validate(
            {
                "credential": credential,
                "capabilities": (
                    Capability(name=FACE_CAPABILITY, version=_FACE_VERSION),
                ),
            },
        ),
    )


def frame_for(path: Path, sequence: int = 0) -> bytes:
    """Build the binary frame message carrying an image.

    Args:
        path: The JPEG to send, exactly as the capture hardware would have
            produced it. It is never re-encoded.
        sequence: The frame's number within the session.

    Returns:
        The encoded frame message.

    Raises:
        VerificationError: If the file is not there.
    """
    if not path.is_file():
        message = f"no frame to send: {path} is not a file"
        raise VerificationError(message)
    return encode_frame(
        FrameHeader(sequence=sequence, captured_at=capture_stamp(sequence)),
        path.read_bytes(),
    )


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
            body = json.loads(response.read())
    except urllib.error.HTTPError as error:
        return False, json.loads(error.read())
    except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
        return False, repr(error)
    return bool(isinstance(body, dict) and body.get("ready")), body


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


async def drive_session(
    url: str,
    credential: str,
    frame: bytes,
) -> ResultEnvelope[FaceDetections]:
    """Negotiate a session, send one frame, and read the answer back.

    Args:
        url: The session endpoint.
        credential: What the service authenticates the session against.
        frame: The encoded frame message to send.

    Returns:
        The result the face capability produced for that frame.

    Raises:
        VerificationError: If the service closed the session, refused the
            credential, agreed to no capabilities, or answered with something
            other than a face result.
    """
    async with connect(url, max_size=None) as connection:
        await connection.send(offer_for(credential))
        kind, payload = await _receive(connection)
        if kind is not MessageKind.AGREEMENT:
            message = f"expected an agreement, got a {kind.value}: {payload!r}"
            raise VerificationError(message)
        agreement = SessionAgreement.from_wire(payload)
        if FACE_CAPABILITY not in [
            capability.name for capability in agreement.capabilities
        ]:
            message = (
                f"the service agreed to {[c.name for c in agreement.capabilities]}, "
                f"which does not include {FACE_CAPABILITY!r}; the model in the "
                f"image did not load"
            )
            raise VerificationError(message)

        await connection.send(frame)
        kind, payload = await _receive(connection)
        if kind is not MessageKind.RESULT:
            message = f"expected a result, got a {kind.value}: {payload!r}"
            raise VerificationError(message)
        return ResultEnvelope[FaceDetections].from_wire(payload)


async def _receive(connection: ClientConnection) -> tuple[MessageKind, bytes]:
    """Read the next control message off an open session.

    Args:
        connection: The open WebSocket.

    Returns:
        The kind and canonical bytes of the message.

    Raises:
        VerificationError: If the service sent a binary message, which nothing
            in this direction ever is.
    """
    raw = await asyncio.wait_for(connection.recv(), timeout=_MESSAGE_TIMEOUT_SECONDS)
    if isinstance(raw, bytes):
        message = f"the service sent {len(raw)} binary bytes where text was due"
        raise VerificationError(message)
    return decode_control(raw)


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
        "--expect-faces",
        type=int,
        default=1,
        help="how many faces the answer must carry (default: %(default)s)",
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

        frame = frame_for(arguments.frame)
        result = asyncio.run(drive_session(url, arguments.credential, frame))
        faces = result.payload.faces
        sys.stdout.write(
            f"image-verify: {arguments.frame.name} "
            f"({len(frame)} bytes on the wire) answered by "
            f"{result.capability!r} at sequence {result.sequence} "
            f"with {len(faces)} face(s): "
            f"{[(f.centre.x, f.centre.y, f.confidence) for f in faces]}\n",
        )
        if result.captured_at != capture_stamp(result.sequence):
            message = (
                f"the capture token came back as {result.captured_at.root!r}, "
                f"not the {capture_stamp(result.sequence).root!r} that was sent"
            )
            raise VerificationError(message)
        if len(faces) < arguments.expect_faces:
            message = (
                f"expected at least {arguments.expect_faces} face(s) in "
                f"{arguments.frame.name}, got {len(faces)}"
            )
            raise VerificationError(message)
    except VerificationError as error:
        sys.stderr.write(f"image-verify: {error}\n")
        return 1
    sys.stdout.write("image-verify: the running image answered a real session\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
