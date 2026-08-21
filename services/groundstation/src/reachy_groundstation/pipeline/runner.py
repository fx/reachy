"""The per-session pipeline: take a frame, decode it once, answer it.

One frame, one decode, every agreed capability. Results are assembled against the
frame's sequence number and carry the frame's capture token copied through
untouched, which is what lets the robot compute a result's age against the clock
that produced the token.

A capability that fails or overruns costs its own answer and nothing else: the
error is reported against the frame it concerns and the remaining capabilities
still answer. A capability that finds nothing has produced a successful result,
so it is delivered and no error counter moves.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING

from opentelemetry.trace import Status, StatusCode

from reachy_contracts import ErrorCode, ResultEnvelope, SessionError
from reachy_groundstation.obs import (
    STAGE_DECODE,
    STAGE_EMIT,
    STAGE_QUEUE,
    frame_context,
    frame_exemplar,
    get_logger,
)
from reachy_groundstation.pipeline.decode import DecodeError, decode_jpeg
from reachy_groundstation.pipeline.queue import QueueClosedError
from reachy_groundstation.ports import DecodedFrame
from reachy_groundstation.session.framing import MessageKind

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Sequence

    from reachy_contracts import WireModel
    from reachy_groundstation.config import Settings
    from reachy_groundstation.obs import Observability
    from reachy_groundstation.pipeline.queue import FrameQueue, QueuedFrame
    from reachy_groundstation.ports import AgreedCapability

__all__ = ["Deliver", "FramePipeline"]

# What the pipeline hands a finished message to. The session layer owns the
# transport; the pipeline owns what to say and never how to send it.
type Deliver = Callable[[MessageKind, "WireModel"], Awaitable[None]]

_logger = get_logger(__name__)


class FramePipeline:
    """Processes the frames of one session, in the order they arrived."""

    def __init__(
        self,
        *,
        capabilities: Sequence[AgreedCapability],
        deliver: Deliver,
        settings: Settings,
        obs: Observability,
        session_id: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create a pipeline for one session.

        Args:
            capabilities: The agreed capabilities, in the order they were
                agreed, each already carrying the name it was agreed under.
                Every one of them answers every frame.
            deliver: What to hand a finished message to.
            settings: The settings in effect.
            obs: Where timings, spans and log lines go.
            session_id: The session these frames belong to.
            clock: A monotonic source, injected so timing is testable without
                sleeping. Purely local — it is never compared with a frame's
                capture token.
        """
        self._capabilities = tuple(capabilities)
        self._deliver = deliver
        self._settings = settings
        self._obs = obs
        self._session_id = session_id
        self._clock = clock

    async def run(self, queue: FrameQueue) -> None:
        """Process frames until the queue closes.

        Args:
            queue: The session's queue.
        """
        while True:
            try:
                frame = await queue.get()
            except QueueClosedError:
                return
            await self.process(frame)

    #:= docs/specs/robot-link/index.md#req-013-an-empty-result-is-a-valid-result
    #:% A result message carrying no detections MUST be treated as a successful result
    #:% for that frame.
    async def process(self, frame: QueuedFrame) -> None:
        """Decode one frame and answer it with every agreed capability.

        Args:
            frame: The frame taken off the queue.
        """
        with frame_context(frame.header.sequence), self._frame_span(frame):
            self._obs.metrics.stage_seconds.labels(stage=STAGE_QUEUE).observe(
                max(self._clock() - frame.received_at, 0.0),
                exemplar=self._exemplar(frame.header.sequence),
            )
            decoded = await self._decode(frame)
            if decoded is None:
                return
            for agreed in self._capabilities:
                await self._answer(decoded, agreed)

    @contextmanager
    def _frame_span(self, frame: QueuedFrame) -> Iterator[None]:
        """Open the span every stage of this frame nests under.

        Args:
            frame: The frame being handled.

        Yields:
            Nothing; the span is the point.
        """
        with self._obs.tracer.start_as_current_span(
            "frame",
            attributes={
                "reachy.session_id": self._session_id,
                "reachy.sequence": frame.header.sequence,
                "reachy.payload_bytes": len(frame.payload),
            },
        ):
            yield

    def _exemplar(self, sequence: int) -> dict[str, str]:
        """Identify the frame a timing belongs to.

        Args:
            sequence: The frame's number within this session.

        Returns:
            The exemplar labels.
        """
        return frame_exemplar(self._session_id, sequence)

    async def _decode(self, frame: QueuedFrame) -> DecodedFrame | None:
        """Decode a frame once, for every capability to share.

        Args:
            frame: The frame taken off the queue.

        Returns:
            The decoded frame, or `None` when the payload was not decodable —
            in which case the client has already been told.
        """
        started = self._clock()
        with self._obs.tracer.start_as_current_span("decode") as span:
            try:
                image = decode_jpeg(frame.payload)
            except DecodeError as error:
                span.set_status(Status(StatusCode.ERROR, str(error)))
                await self._report(
                    ErrorCode.MALFORMED_MESSAGE,
                    str(error),
                    frame.header.sequence,
                )
                return None
            finally:
                self._obs.metrics.stage_seconds.labels(stage=STAGE_DECODE).observe(
                    self._clock() - started,
                    exemplar=self._exemplar(frame.header.sequence),
                )
        return DecodedFrame(header=frame.header, image=image)

    #:= docs/specs/robot-link/index.md#req-016-results-return-the-capture-timestamp-unaltered
    #:% Every result MUST carry the capture timestamp of the frame it derives from,
    #:% byte-for-byte as the capturing side supplied it, so that the capturing side can
    #:% compute the result's age against the same clock that produced it.
    async def _answer(
        self,
        decoded: DecodedFrame,
        agreed: AgreedCapability,
    ) -> None:
        """Run one capability over the decoded frame and deliver its answer.

        The envelope is built by `ResultEnvelope.for_frame`, which moves the
        frame's sequence number and its capture token across without this code
        reading either. The token is the robot's, and nothing here has a clock it
        could be compared against.

        Args:
            decoded: The frame, shared with every other agreed capability.
            agreed: The capability to run, under its negotiated name.
        """
        name = agreed.name
        started = self._clock()
        with self._obs.tracer.start_as_current_span(
            "capability",
            attributes={"reachy.capability": name},
        ) as span:
            try:
                payload: WireModel = await asyncio.wait_for(
                    agreed.capability.process(decoded),
                    timeout=self._settings.capability_timeout_seconds,
                )
            except Exception as error:
                # A capability is arbitrary code running inside this process,
                # and `wait_for` adds a `TimeoutError` to whatever it raises of
                # its own. Either way the cost is that capability's answer to
                # this frame and nothing more: the remaining capabilities still
                # answer and the session continues. `CancelledError` derives
                # from `BaseException`, so shutting the session down still
                # unwinds through here rather than being reported as a failure.
                span.set_status(Status(StatusCode.ERROR, repr(error)))
                _logger.warning(
                    "capability.failed",
                    capability=name,
                    error=repr(error),
                )
                await self._report(
                    ErrorCode.CAPABILITY_FAILED,
                    f"{name}: {error!r}"[:500],
                    decoded.sequence,
                )
                return
            finally:
                self._obs.metrics.capability_seconds.labels(capability=name).observe(
                    self._clock() - started,
                    exemplar=self._exemplar(decoded.sequence),
                )

        emitted = self._clock()
        await self._deliver(
            MessageKind.RESULT,
            ResultEnvelope.for_frame(decoded.header, name, payload),
        )
        self._obs.metrics.stage_seconds.labels(stage=STAGE_EMIT).observe(
            self._clock() - emitted,
            exemplar=self._exemplar(decoded.sequence),
        )
        self._obs.metrics.results_emitted_total.labels(capability=name).inc(
            exemplar=self._exemplar(decoded.sequence),
        )

    async def _report(self, code: ErrorCode, detail: str, sequence: int) -> None:
        """Tell the client that one frame went wrong, without ending anything.

        Args:
            code: What kind of failure this is.
            detail: A human-readable explanation, never a credential.
            sequence: The frame this concerns.
        """
        self._obs.metrics.errors_total.labels(code=code.value).inc(
            exemplar=self._exemplar(sequence),
        )
        await self._deliver(
            MessageKind.ERROR,
            SessionError(code=code, detail=detail, sequence=sequence),
        )
