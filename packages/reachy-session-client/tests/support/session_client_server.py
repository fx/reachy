"""A real groundstation, in-process, for the tests that need one.

This is what makes the session client's integration tests integration tests: a
real uvicorn server on the loopback interface with an ephemeral port, the real
`reachy_groundstation` application composed around a registry the test builds,
and the real session layer answering real frames. Nothing about the protocol is
mocked on either side.

The capabilities here are the test's own rather than the groundstation's,
deliberately. Reaching into another member's `tests/support/` would couple two
suites that are meant to be independent, and perception — the first production
capability — is change 0005. They answer with payload types from
`reachy_contracts`, because a test is not a place to declare a wire type either.

The connection factory is the other half. `SessionClient` takes the factory that
opens its transport, so a test can hold on to what was opened and drop it, which
is how a reconnection is induced over the real transport against a server that
never went anywhere.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Final

import cv2
import numpy as np
import structlog
import uvicorn

from reachy_contracts import (
    FACE_CAPABILITY,
    Capability,
    FaceDetection,
    FaceDetections,
    NormalisedPoint,
)
from reachy_groundstation.api.app import SESSION_PATH, create_app
from reachy_groundstation.capabilities.base import CapabilityBase
from reachy_groundstation.config import Settings
from reachy_groundstation.obs import build_observability
from reachy_groundstation.ports import CapabilityHealth, CapabilityState
from reachy_session_client import open_websocket

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from reachy_contracts import WireModel
    from reachy_groundstation.ports import CapabilityPort, DecodedFrame
    from reachy_session_client import ClientTransport

__all__ = [
    "CREDENTIAL",
    "FACE",
    "RecordingConnections",
    "Serving",
    "StaticRegistry",
    "WatchfulFace",
    "jpeg_bytes",
    "serving",
]

# A placeholder credential. Not anybody's, and never a real one — see the root
# AGENTS.md on what may enter a tracked file in a public repository.
CREDENTIAL: Final = "example-credential"

FACE: Final = Capability(name=FACE_CAPABILITY, version=1)

# How long a test waits for something that should already be on its way. Long
# enough that a loaded runner does not flake, short enough that a genuine hang
# fails the suite rather than stalling it.
TIMEOUT: Final = 10.0


class WatchfulFace(CapabilityBase):
    """Answers every frame with a face, or with nothing when asked to.

    The empty answer is not a degenerate case to be tolerated: robot-link
    REQ-013 makes it an ordinary successful result, and this is what exercises
    it end to end.
    """

    def __init__(self, *, empty: bool = False) -> None:
        """Create the capability.

        Args:
            empty: Whether to answer with no detections.
        """
        super().__init__(FACE)
        self.empty = empty
        self.seen: list[int] = []

    async def process(self, frame: DecodedFrame) -> WireModel:
        """Answer one frame.

        Args:
            frame: The decoded frame.

        Returns:
            One face, or none when this capability was built empty.
        """
        self.seen.append(frame.sequence)
        if self.empty:
            return FaceDetections()
        return FaceDetections(
            faces=(
                FaceDetection(
                    centre=NormalisedPoint(x=0.25, y=-0.25),
                    confidence=0.5,
                ),
            ),
        )


class StaticRegistry:
    """A registry a test composes by hand, satisfying `CapabilityRegistryPort`.

    What is on offer can be changed between two connections, which is what
    proves that a reconnection negotiates again rather than resuming.
    """

    def __init__(self, *capabilities: CapabilityPort) -> None:
        """Create a registry offering exactly these capabilities.

        Args:
            capabilities: What to offer, in order.
        """
        self.capabilities = list(capabilities)

    @property
    def ready(self) -> bool:
        """Whether this registry claims to be ready.

        Returns:
            Always true; readiness is the groundstation's own test's subject.
        """
        return True

    def supported(self) -> tuple[Capability, ...]:
        """What may be offered during negotiation.

        Returns:
            The descriptors, in order.
        """
        return tuple(capability.descriptor for capability in self.capabilities)

    def get(self, name: str) -> CapabilityPort | None:
        """Look a capability up by name.

        Args:
            name: The name negotiation agreed on.

        Returns:
            The capability, or None.
        """
        for capability in self.capabilities:
            if capability.descriptor.name == name:
                return capability
        return None

    def health(self) -> tuple[CapabilityHealth, ...]:
        """Report every capability as ready.

        Returns:
            One entry per capability.
        """
        return tuple(
            CapabilityHealth(
                name=capability.descriptor.name,
                version=capability.descriptor.version,
                state=CapabilityState.READY,
            )
            for capability in self.capabilities
        )


class Serving:
    """A running groundstation and where to reach it.

    Attributes:
        url: Where to open a session.
    """

    def __init__(self, port: int) -> None:
        """Record where the server ended up.

        Args:
            port: The ephemeral port it bound.
        """
        self.url = f"ws://127.0.0.1:{port}{SESSION_PATH}"


@contextlib.asynccontextmanager
async def serving(registry: StaticRegistry) -> AsyncIterator[Serving]:
    """Run the real application on a real socket for the duration of a test.

    Args:
        registry: What the application is composed around.

    Yields:
        Where the server is listening.

    Raises:
        AssertionError: If the server does not start, so that a test fails
            rather than the suite hanging.
    """
    _silence_the_service_log()
    settings = Settings.model_validate({"credential": CREDENTIAL, "queue_bound": 8})
    app = create_app(
        settings=settings,
        registry=registry,
        obs=build_observability(settings),
    )
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_config=None,
        # Mirrors how `reachy_groundstation.service` serves the application, so
        # what these tests drive is what a robot will meet.
        ws="websockets-sansio",
        ws_max_size=settings.max_message_bytes,
        lifespan="on",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(), name="uvicorn")
    try:
        deadline = asyncio.get_running_loop().time() + TIMEOUT
        while not server.started:
            if task.done():
                await task
                message = "the server stopped before it started"
                raise AssertionError(message)
            if asyncio.get_running_loop().time() > deadline:
                message = "the server did not start"
                raise AssertionError(message)
            await asyncio.sleep(0.005)
        yield Serving(server.servers[0].sockets[0].getsockname()[1])
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=TIMEOUT)


def _silence_the_service_log() -> None:
    """Point structlog at a logger that builds lines and then discards them.

    The groundstation logs on paths every one of these tests exercises, and
    writing those lines to standard output would bury the test report under a
    service's boot chatter. Nothing here reads what was logged: the client's
    own behaviour is what is under test, and the service's logging has its own
    suite.
    """
    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars],
        wrapper_class=structlog.make_filtering_bound_logger(0),
        logger_factory=structlog.ReturnLoggerFactory(),
        cache_logger_on_first_use=False,
    )


class RecordingConnections:
    """Opens real connections and keeps them, so a test can drop one.

    The client takes the factory that opens its transport, so this is the
    supported seam rather than a hole poked in the client: the connections are
    the real ones, and dropping the latest is what a network stall looks like
    from the other end.
    """

    def __init__(self) -> None:
        """Start with nothing opened."""
        self.opened: list[ClientTransport] = []

    @property
    def count(self) -> int:
        """How many connections have been opened.

        Returns:
            The number opened so far.
        """
        return len(self.opened)

    async def __call__(self, url: str) -> ClientTransport:
        """Open a connection and remember it.

        Args:
            url: Where the client is connecting.

        Returns:
            The real transport.
        """
        transport = await open_websocket(url)
        self.opened.append(transport)
        return transport

    async def drop_latest(self) -> None:
        """End the connection the client is currently holding."""
        await self.opened[-1].close()


def jpeg_bytes(width: int = 32, height: int = 24, fill: int = 128) -> bytes:
    """Encode a solid-colour JPEG, which is what the groundstation decodes.

    Encoding happens in memory: this is arithmetic, not input or output.

    Args:
        width: The image's width in pixels.
        height: The image's height in pixels.
        fill: The grey level to fill it with.

    Returns:
        The encoded bytes.

    Raises:
        RuntimeError: If the encoder declines, which it does not for a solid
            array — the branch exists so a silent empty frame cannot happen.
    """
    image = np.full((height, width, 3), fill, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    if not ok:  # pragma: no cover - cv2 does not fail on a solid array
        message = "cv2 declined to encode a solid array"
        raise RuntimeError(message)
    return bytes(encoded.tobytes())
