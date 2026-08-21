"""Shared helpers for the groundstation tests.

Everything here is a fake of something the service talks to, or a builder for
something it is configured with. Nothing here fakes the service's own behaviour:
the session layer, the pipeline and the registry are always the real ones, and
the integration tests drive the real transport as well.

The capabilities are the interesting part, and they are deliberately not the real
ones. The registry's central guarantee is that it is not coupled to whatever the
first capability turned out to look like, so it is proved by two unrelated
made-up capabilities routing through it rather than by the perception ones, which
would prove only that the code works with itself. The perception capabilities
have their own tests and their own helpers in
`groundstation_perception_support`. These return payload types from
`reachy_contracts` rather than declaring their own, because a test is not a place
to declare a wire type either.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Final

import cv2
import numpy as np
import structlog
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from reachy_contracts import (
    Capability,
    CaptureTimestamp,
    FaceDetection,
    FaceDetections,
    FrameHeader,
    GestureDetection,
    GestureDetections,
    NormalisedPoint,
    SessionOffer,
    WireModel,
)
from reachy_groundstation.capabilities.base import CapabilityBase
from reachy_groundstation.config import Settings
from reachy_groundstation.obs import Observability, build_metrics
from reachy_groundstation.ports import (
    AgreedCapability,
    CapabilityHealth,
    CapabilityPort,
    CapabilityState,
    DecodedFrame,
)
from reachy_groundstation.session.framing import (
    MessageKind,
    encode_control,
    encode_frame,
)
from reachy_groundstation.session.transport import TransportClosedError

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, MutableMapping, Sequence

    from opentelemetry.sdk.trace import ReadableSpan

__all__ = [
    "CREDENTIAL",
    "ECHO",
    "TALLY",
    "BlockingCapability",
    "EchoCapability",
    "ExplodingCapability",
    "MemoryTransport",
    "StaticRegistry",
    "TallyCapability",
    "agreed",
    "build_observability",
    "captured_logs",
    "frame_message",
    "hand_control_to_the_event_loop",
    "jpeg_bytes",
    "make_header",
    "make_settings",
    "offer_message",
    "recorded_spans",
]

# A placeholder credential. Not anybody's, and never a real one — see the root
# AGENTS.md on what may enter a tracked file in a public repository.
CREDENTIAL: Final = "example-credential"

ECHO: Final = Capability(name="echo", version=1)
TALLY: Final = Capability(name="tally", version=1)


def agreed(capability: CapabilityPort) -> AgreedCapability:
    """Pair a capability with the name a session would agree to route it by.

    Args:
        capability: What answers frames.

    Returns:
        The pairing the pipeline is given.
    """
    return AgreedCapability(name=capability.descriptor.name, capability=capability)


def make_settings(**overrides: object) -> Settings:
    """Build settings for a test, with the credential already filled in.

    Args:
        overrides: Settings to change from their defaults.

    Returns:
        The settings.
    """
    values: dict[str, object] = {"credential": CREDENTIAL}
    values.update(overrides)
    return Settings.model_validate(values)


def build_observability() -> tuple[Observability, InMemorySpanExporter]:
    """Build a reporting bundle whose spans and metrics stay inside one test.

    Returns:
        The bundle, and the exporter its spans land in.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return (
        Observability(
            metrics=build_metrics(),
            tracer=provider.get_tracer("test"),
            provider=provider,
        ),
        exporter,
    )


@contextmanager
def captured_logs() -> Iterator[list[dict[str, Any]]]:
    """Collect log lines with the bound context still on them.

    `structlog.testing.capture_logs` replaces the whole processor chain, which
    drops `merge_contextvars` along with it — so a test using it cannot see the
    session identifier and sequence number the service binds. This keeps that
    one processor and captures what comes out of it.

    Yields:
        The list the captured lines accumulate in.
    """
    entries: list[dict[str, Any]] = []

    def _capture(
        logger: Any,  # noqa: ANN401  # structlog types the wrapped logger as `Any` — it is whatever the configured factory produced — so narrowing it would stop this being a processor
        method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> Mapping[str, Any]:
        """Record one line and hand it back unchanged.

        Args:
            logger: The logger it came from, unused.
            method_name: The severity it was logged at.
            event_dict: The line.

        Returns:
            The line.
        """
        del logger
        entries.append({**event_dict, "log_level": method_name})
        return event_dict

    previous = structlog.get_config()
    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars, _capture],
        wrapper_class=structlog.make_filtering_bound_logger(0),
        logger_factory=structlog.ReturnLoggerFactory(),
        cache_logger_on_first_use=False,
    )
    try:
        yield entries
    finally:
        structlog.configure(**previous)


async def hand_control_to_the_event_loop(turns: int = 100) -> None:
    """Let every other task run, without waiting on a clock.

    `asyncio.sleep(0)` yields to the event loop and resumes on its next pass: it
    reads no clock, schedules no timer and adds no wall time, so it is not the
    sleeping the no-input-or-output rule forbids. It is how a test drives
    another task to its next await point deterministically — the alternative is
    a real delay, which is both slower and less certain.

    The number of turns is bounded so that a task which never reaches the state
    a test is waiting for fails that test rather than hanging the suite.

    Args:
        turns: How many times to yield.
    """
    for _ in range(turns):
        await asyncio.sleep(0)


def recorded_spans(exporter: InMemorySpanExporter) -> tuple[str, ...]:
    """Name the spans a test produced, in the order they finished.

    Args:
        exporter: Where the spans landed.

    Returns:
        The span names.
    """
    spans: Sequence[ReadableSpan] = exporter.get_finished_spans()
    return tuple(span.name for span in spans)


def jpeg_bytes(width: int = 32, height: int = 24, fill: int = 128) -> bytes:
    """Encode a solid-colour JPEG.

    Encoding happens in memory: this is arithmetic, not input or output, so a
    unit test may do it.

    Args:
        width: The image's width in pixels.
        height: The image's height in pixels.
        fill: The grey level to fill it with.

    Returns:
        The encoded bytes.
    """
    image = np.full((height, width, 3), fill, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    if not ok:  # pragma: no cover - cv2 does not fail on a solid array
        message = "cv2 declined to encode a solid array"
        raise RuntimeError(message)
    return bytes(encoded.tobytes())


def make_header(sequence: int, stamp: str = "12345.678") -> FrameHeader:
    """Build a frame header carrying an opaque capture token.

    Args:
        sequence: The frame's number within its session.
        stamp: The capture token, which the groundstation must copy through
            byte for byte and never interpret.

    Returns:
        The header.
    """
    return FrameHeader(sequence=sequence, captured_at=CaptureTimestamp(stamp))


def frame_message(
    sequence: int,
    payload: bytes | None = None,
    stamp: str = "12345.678",
) -> bytes:
    """Build a complete frame message as a client would send one.

    Args:
        sequence: The frame's number within its session.
        payload: The compressed frame. A small JPEG is encoded when none is
            given.
        stamp: The capture token.

    Returns:
        The binary message.
    """
    return encode_frame(
        make_header(sequence, stamp),
        jpeg_bytes() if payload is None else payload,
    )


def offer_message(
    *capabilities: Capability,
    credential: str = CREDENTIAL,
) -> str:
    """Build the control message a client opens a session with.

    Args:
        capabilities: What the client claims it can speak.
        credential: What it presents. Overridden to test the rejection path.

    Returns:
        The text message.
    """
    return encode_control(
        MessageKind.OFFER,
        SessionOffer.model_validate(
            {"credential": credential, "capabilities": capabilities},
        ),
    )


class EchoCapability(CapabilityBase):
    """Answers every frame with one face at a position derived from the frame.

    It records the decoded frames it was handed, which is how the "decode once
    and share it" guarantee is checked: two capabilities in one session are given
    the same object, not two decodes of the same bytes.
    """

    def __init__(self, descriptor: Capability = ECHO) -> None:
        """Create the capability.

        Args:
            descriptor: What it negotiates as.
        """
        super().__init__(descriptor)
        self.seen: list[DecodedFrame] = []
        self.warmed = 0

    async def warm_up(self) -> None:
        """Count that warm-up happened."""
        self.warmed += 1

    async def process(self, frame: DecodedFrame) -> WireModel:
        """Answer one frame.

        Args:
            frame: The decoded frame.

        Returns:
            One face, positioned so the answer is traceable to its frame.
        """
        self.seen.append(frame)
        return FaceDetections(
            faces=(
                FaceDetection(
                    centre=NormalisedPoint(x=0.25, y=-0.25),
                    confidence=0.5,
                ),
            ),
        )


class TallyCapability(CapabilityBase):
    """Answers with a gesture, or with nothing at all when asked to.

    The empty answer is not a degenerate case to be tolerated: robot link REQ-013
    makes it an ordinary successful result, and this is what exercises it.
    """

    def __init__(
        self,
        descriptor: Capability = TALLY,
        *,
        empty: bool = False,
    ) -> None:
        """Create the capability.

        Args:
            descriptor: What it negotiates as.
            empty: Whether to answer with no detections.
        """
        super().__init__(descriptor)
        self.empty = empty
        self.seen: list[DecodedFrame] = []

    async def process(self, frame: DecodedFrame) -> WireModel:
        """Answer one frame.

        Args:
            frame: The decoded frame.

        Returns:
            One gesture, or none when this capability was built empty.
        """
        self.seen.append(frame)
        if self.empty:
            return GestureDetections()
        return GestureDetections(
            gestures=(GestureDetection(label="wave", confidence=0.75),),
        )


class ExplodingCapability(CapabilityBase):
    """Fails wherever it is told to, so the failure paths are real failures."""

    def __init__(
        self,
        descriptor: Capability,
        *,
        on_warm_up: bool = False,
        on_process: bool = False,
    ) -> None:
        """Create the capability.

        Args:
            descriptor: What it negotiates as.
            on_warm_up: Whether warming up raises.
            on_process: Whether processing a frame raises.
        """
        super().__init__(descriptor)
        self._on_warm_up = on_warm_up
        self._on_process = on_process

    async def warm_up(self) -> None:
        """Fail to warm up, when built to.

        Raises:
            RuntimeError: When built to fail here.
        """
        if self._on_warm_up:
            message = "this capability cannot load its model"
            raise RuntimeError(message)

    async def process(self, frame: DecodedFrame) -> WireModel:
        """Fail to answer, when built to.

        Args:
            frame: The decoded frame, unused.

        Returns:
            An empty face payload when it was not built to fail.

        Raises:
            RuntimeError: When built to fail here.
        """
        del frame
        if self._on_process:
            message = "this capability cannot answer this frame"
            raise RuntimeError(message)
        return FaceDetections()


class BlockingCapability(CapabilityBase):
    """Holds every frame until released, which is how overload is induced.

    A queue bound that is never reached proves nothing about what happens when it
    is. Parking the pipeline here lets frames pile up behind it for real.
    """

    def __init__(self, descriptor: Capability = ECHO) -> None:
        """Create the capability, initially blocking.

        Args:
            descriptor: What it negotiates as.
        """
        super().__init__(descriptor)
        self.release = asyncio.Event()
        self.entered = asyncio.Event()
        self.processed: list[int] = []

    async def process(self, frame: DecodedFrame) -> WireModel:
        """Wait to be released, then answer.

        Args:
            frame: The decoded frame.

        Returns:
            An empty face payload.
        """
        self.entered.set()
        await self.release.wait()
        self.processed.append(frame.sequence)
        return FaceDetections()


class StaticRegistry:
    """A registry a test composes by hand, satisfying `CapabilityRegistryPort`.

    The session layer takes the port rather than the real registry, so a test can
    change what is on offer between two connections — which is what proves that
    negotiation is performed per session and never cached across reconnections.
    """

    def __init__(self, *capabilities: CapabilityPort, ready: bool = True) -> None:
        """Create a registry offering exactly these capabilities.

        Args:
            capabilities: What to offer, in order.
            ready: What to report for readiness.
        """
        self.capabilities = list(capabilities)
        self._ready = ready

    @property
    def ready(self) -> bool:
        """Whether this registry claims to be ready.

        Returns:
            What the test asked for.
        """
        return self._ready

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


class _Closed:
    """The sentinel that ends a `MemoryTransport`'s inbound stream."""


class MemoryTransport:
    """A `SessionTransport` backed by a queue, for the tests that need no server.

    The integration tests use the real WebSocket. This exists for the unit tests
    of the session's own logic, where a server would be input and output for
    nothing.
    """

    def __init__(self, fail_send_after: int | None = None) -> None:
        """Create an empty transport.

        Args:
            fail_send_after: How many messages may be sent before sending
                reports the connection gone. `None` means never, which is what
                every test but the one about a client vanishing mid-answer
                wants.
        """
        self.inbound: asyncio.Queue[str | bytes | _Closed] = asyncio.Queue()
        self.sent: list[str] = []
        self.closed: tuple[int, str] | None = None
        self._fail_send_after = fail_send_after

    def offer(self, *capabilities: Capability, credential: str = CREDENTIAL) -> None:
        """Queue an opening offer from the client.

        Args:
            capabilities: What the client claims it can speak.
            credential: What it presents.
        """
        self.inbound.put_nowait(
            offer_message(*capabilities, credential=credential),
        )

    def push(self, message: str | bytes) -> None:
        """Queue a message from the client.

        Args:
            message: The text or bytes to deliver next.
        """
        self.inbound.put_nowait(message)

    def disconnect(self) -> None:
        """Make the next receive report that the client went away."""
        self.inbound.put_nowait(_Closed())

    async def receive(self) -> str | bytes:
        """Take the next queued message.

        Returns:
            The message.

        Raises:
            TransportClosedError: When the queued sentinel is reached.
        """
        message = await self.inbound.get()
        if isinstance(message, _Closed):
            raise TransportClosedError("client disconnected")
        return message

    async def send(self, text: str) -> None:
        """Record an outgoing message, or report the connection gone.

        Args:
            text: The already-framed message.

        Raises:
            TransportClosedError: Once as many messages have been sent as this
                transport was built to allow.
        """
        if (
            self._fail_send_after is not None
            and len(self.sent) >= self._fail_send_after
        ):
            raise TransportClosedError("client disconnected mid-answer")
        self.sent.append(text)

    async def close(self, code: int, reason: str) -> None:
        """Record that the session was closed.

        Args:
            code: The RFC 6455 close code.
            reason: The explanation sent with it.
        """
        self.closed = (code, reason)
