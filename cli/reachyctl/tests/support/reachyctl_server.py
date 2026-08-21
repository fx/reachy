"""A real groundstation, in-process, for the probe's integration test.

The groundstation is the real `reachy_groundstation` application served by a
real uvicorn on the loopback interface with an ephemeral port. Nothing about the
protocol is mocked on either side, which is the whole reason change 0007 exists
before the robot application does: reachyctl REQ-057 is a statement about the
protocol, and only real traffic is evidence for it.

The capability is this suite's own. Perception — the first production capability
— is change 0005, and a member's `tests/support/` is private to that member, so
reaching into another one's would tie two suites together for reasons neither
owns. It answers with a payload type from `reachy_contracts`, because a test is
not a place to declare a wire type either.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from pathlib import Path
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

if TYPE_CHECKING:
    from collections.abc import Iterator

    from reachy_contracts import WireModel
    from reachy_groundstation.ports import CapabilityPort, DecodedFrame

__all__ = [
    "CREDENTIAL",
    "FACE",
    "TIMEOUT",
    "CountingFace",
    "SlowFace",
    "StaticRegistry",
    "serving",
    "write_frames",
]

# A placeholder credential. Not anybody's, and never a real one — see the root
# AGENTS.md on what may enter a tracked file in a public repository.
CREDENTIAL: Final = "example-credential"

FACE: Final = Capability(name=FACE_CAPABILITY, version=1)

# How long a test waits for something that should already be on its way. Long
# enough that a loaded runner does not flake, short enough that a genuine hang
# fails the suite rather than stalling it.
TIMEOUT: Final = 20.0


class CountingFace(CapabilityBase):
    """Answers each frame with as many faces as its sequence number, capped.

    Varying the answer per frame is what lets the probe's report be checked
    against the frames it sent rather than against a constant — a report that
    said "one face" for every frame would look identical whether or not the
    sequence numbers were being carried through.
    """

    def __init__(self, *, faces_at_most: int = 3) -> None:
        """Create the capability.

        Args:
            faces_at_most: The largest number of faces to report.
        """
        super().__init__(FACE)
        self._faces_at_most = faces_at_most
        self.seen: list[int] = []

    async def process(self, frame: DecodedFrame) -> WireModel:
        """Answer one frame.

        Args:
            frame: The decoded frame.

        Returns:
            As many faces as the frame's sequence number, so that frame zero is
            robot-link REQ-013's empty result and the rest are not.
        """
        self.seen.append(frame.sequence)
        count = min(frame.sequence, self._faces_at_most)
        return FaceDetections(
            faces=tuple(
                FaceDetection(
                    centre=NormalisedPoint(x=0.25, y=-0.25),
                    confidence=0.5,
                )
                for _ in range(count)
            ),
        )


class SlowFace(CapabilityBase):
    """Answers, but later than a probe with a short staleness window waits.

    Slow rather than silent, deliberately. A capability that never returned
    would be abandoned by the groundstation's own capability timeout seconds
    later, and the test would spend those seconds waiting for a shutdown; this
    one finishes on its own well inside the run.
    """

    def __init__(self, delay: float = 0.5) -> None:
        """Create the capability.

        Args:
            delay: How long to hold each frame before answering it.
        """
        super().__init__(FACE)
        self._delay = delay

    async def process(self, frame: DecodedFrame) -> WireModel:
        """Answer one frame, eventually.

        Args:
            frame: The decoded frame, unused.

        Returns:
            An empty face payload, long after anybody was still waiting.
        """
        del frame
        await asyncio.sleep(self._delay)
        return FaceDetections()


class StaticRegistry:
    """A registry a test composes by hand, satisfying `CapabilityRegistryPort`."""

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
            Always true; readiness has its own suite in the groundstation.
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


def _silence_the_service_log() -> None:
    """Point structlog at a logger that builds lines and then discards them.

    The groundstation logs on paths this test exercises, and writing those lines
    to standard output would bury the test report under a service's boot
    chatter. Nothing here reads what was logged.
    """
    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars],
        wrapper_class=structlog.make_filtering_bound_logger(0),
        logger_factory=structlog.ReturnLoggerFactory(),
        cache_logger_on_first_use=False,
    )


@contextlib.contextmanager
def serving(registry: StaticRegistry) -> Iterator[str]:
    """Run the real application on a real socket for the duration of a test.

    The server runs in a thread with an event loop of its own, and the test
    stays synchronous. That is not a convenience: `reachyctl` is a command-line
    tool, so it owns its event loop and calls `asyncio.run` — and a test that
    held a loop of its own could not invoke the real command inside it. What is
    exercised here is therefore the command an operator types, argument parsing
    and exit status included, against a groundstation in another thread.

    Args:
        registry: What the application is composed around.

    Yields:
        The session URL to point `probe` at.

    Raises:
        AssertionError: If the server does not start, so that a test fails
            rather than the suite hanging.
    """
    _silence_the_service_log()
    settings = Settings.model_validate({"credential": CREDENTIAL, "queue_bound": 8})
    config = uvicorn.Config(
        create_app(
            settings=settings,
            registry=registry,
            obs=build_observability(settings),
        ),
        host="127.0.0.1",
        port=0,
        log_config=None,
        # Mirrors how `reachy_groundstation.service` serves the application, so
        # what this test drives is what a robot will meet.
        ws="websockets-sansio",
        ws_max_size=settings.max_message_bytes,
        lifespan="on",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="groundstation", daemon=True)
    thread.start()
    try:
        # Polling a real server with a real delay, bounded so that a server
        # which never starts fails this test rather than hanging the suite.
        deadline = time.monotonic() + TIMEOUT
        while not server.started:
            if not thread.is_alive():
                message = "the groundstation stopped before it started"
                raise AssertionError(message)
            if time.monotonic() > deadline:
                message = "the groundstation did not start"
                raise AssertionError(message)
            time.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}{SESSION_PATH}"
    finally:
        server.should_exit = True
        thread.join(timeout=TIMEOUT)


def write_frames(directory: Path, count: int) -> None:
    """Write a numbered sequence of small JPEGs for `probe --frames` to read.

    Real files, because reading a directory of recordings is the input `probe`
    takes and an integration test that faked it would be testing the fake.

    Args:
        directory: Where to write them.
        count: How many to write.

    Raises:
        RuntimeError: If the encoder declines a solid array, which it does not.
    """
    image = np.full((24, 32, 3), 128, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    if not ok:  # pragma: no cover - cv2 does not fail on a solid array
        message = "cv2 declined to encode a solid array"
        raise RuntimeError(message)
    payload = bytes(encoded.tobytes())
    for index in range(count):
        (directory / f"frame-{index:03d}.jpg").write_bytes(payload)
