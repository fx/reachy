"""The pipeline: one decode, every capability, one result each.

The guarantees checked here are the ones that would be expensive to discover
later — that the frame is decoded once and the same array is shared, that an
empty answer is a successful answer, and that the capture token is copied through
without being read.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`. Nothing here opens a socket, reads a file or waits on a clock, and
the pipeline's own clock is injected as a counter.
"""

from __future__ import annotations

import asyncio

import pytest
from groundstation_support import (
    ECHO,
    TALLY,
    EchoCapability,
    ExplodingCapability,
    TallyCapability,
    agreed,
    build_observability,
    captured_logs,
    jpeg_bytes,
    make_header,
    make_settings,
    recorded_spans,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from reachy_contracts import (
    FACE_CAPABILITY,
    Capability,
    ErrorCode,
    FaceDetections,
    GestureDetections,
    ResultEnvelope,
    SessionError,
    WireModel,
)
from reachy_groundstation.capabilities.base import CapabilityBase
from reachy_groundstation.obs import (
    STAGE_DECODE,
    STAGE_EMIT,
    STAGE_QUEUE,
    Observability,
    session_context,
)
from reachy_groundstation.pipeline.queue import FrameQueue, QueuedFrame
from reachy_groundstation.pipeline.runner import FramePipeline
from reachy_groundstation.ports import CapabilityPort, DecodedFrame
from reachy_groundstation.session.framing import MessageKind
from reachy_groundstation.session.transport import TransportClosedError

# A timeout that has already expired by the time the event loop looks at it.
# The assertion in each of these tests is about what happens when a timeout
# fires, so the timeout is made to fire on the next pass of the loop rather than
# in ten milliseconds — the suite waits on no clock, and the outcome cannot turn
# on how loaded the runner is. Zero is not available: a zero timeout would be a
# usable configuration value, and refusing one is the point of the constraint.
_ALREADY_ELAPSED = 1e-6

SESSION = "0123456789abcdef"
STAMP = "17352.884"


class _Recorder:
    """Collects what the pipeline decided to send."""

    def __init__(self) -> None:
        """Start with nothing recorded."""
        self.messages: list[tuple[MessageKind, WireModel]] = []

    async def deliver(self, kind: MessageKind, message: WireModel) -> None:
        """Record one message.

        Args:
            kind: Which contract type it is.
            message: The message itself.
        """
        self.messages.append((kind, message))

    def results(self) -> list[ResultEnvelope[WireModel]]:
        """Pick out the results.

        Returns:
            Every result envelope, in the order it was delivered.
        """
        return [
            message
            for kind, message in self.messages
            if kind is MessageKind.RESULT and isinstance(message, ResultEnvelope)
        ]

    def errors(self) -> list[SessionError]:
        """Pick out the errors.

        Returns:
            Every error, in the order it was delivered.
        """
        return [
            message
            for kind, message in self.messages
            if kind is MessageKind.ERROR and isinstance(message, SessionError)
        ]


def _queued(sequence: int = 0, payload: bytes | None = None) -> QueuedFrame:
    """Build a frame as it would come off the queue.

    Args:
        sequence: The frame's number.
        payload: Its compressed bytes, defaulting to a small JPEG.

    Returns:
        The queued frame.
    """
    return QueuedFrame(
        header=make_header(sequence, STAMP),
        payload=jpeg_bytes() if payload is None else payload,
        received_at=0.0,
    )


def _pipeline(
    *capabilities: CapabilityPort,
    recorder: _Recorder,
    **overrides: object,
) -> tuple[FramePipeline, Observability, InMemorySpanExporter]:
    """Build a pipeline over the given capabilities.

    The clock counts rather than ticks: every stage timing is a difference
    between two successive integers, so the durations are deterministic and no
    test waits for a real one.

    Args:
        capabilities: What the session agreed on.
        recorder: Where delivered messages go.
        overrides: Settings to change from their defaults.

    Returns:
        The pipeline, the reporting bundle it writes to, and where its spans
        land.
    """
    obs, exporter = build_observability()
    pipeline = FramePipeline(
        capabilities=[agreed(capability) for capability in capabilities],
        deliver=recorder.deliver,
        settings=make_settings(**overrides),
        obs=obs,
        session_id=SESSION,
        clock=iter(range(1000)).__next__,
    )
    return pipeline, obs, exporter


@pytest.mark.asyncio
async def test_every_agreed_capability_answers_the_frame() -> None:
    """Routing fans one frame out to everything the session agreed on."""
    recorder = _Recorder()
    pipeline, _obs, _spans = _pipeline(
        EchoCapability(), TallyCapability(), recorder=recorder
    )
    await pipeline.process(_queued(3))
    assert [result.capability for result in recorder.results()] == ["echo", "tally"]


@pytest.mark.asyncio
async def test_a_frame_is_decoded_once_and_the_array_is_shared() -> None:
    """Decode is 2 ms against a 39 ms pass; it scales with capability count."""
    echo, tally = EchoCapability(), TallyCapability()
    recorder = _Recorder()
    pipeline, _obs, _spans = _pipeline(echo, tally, recorder=recorder)
    await pipeline.process(_queued(1))
    assert len(echo.seen) == 1
    assert echo.seen[0] is tally.seen[0]


@pytest.mark.asyncio
async def test_a_capability_receives_the_decoded_frame_not_the_bytes() -> None:
    """The compressed payload stops at the decoder."""
    echo = EchoCapability()
    pipeline, _obs, _spans = _pipeline(echo, recorder=_Recorder())
    await pipeline.process(_queued(1))
    seen = echo.seen[0]
    assert isinstance(seen, DecodedFrame)
    assert (seen.height, seen.width) == (24, 32)


#:= docs/specs/robot-link/index.md#req-014-results-are-keyed-to-the-frame-that-produced-them
#:% Every frame MUST carry a monotonically increasing sequence number, and every
#:% result MUST identify the sequence number of the frame it derives from.
@pytest.mark.asyncio
async def test_a_result_names_the_frame_it_answers() -> None:
    """A consumer applying results in order needs the key to do it with."""
    recorder = _Recorder()
    pipeline, _obs, _spans = _pipeline(EchoCapability(), recorder=recorder)
    await pipeline.process(_queued(41))
    assert [result.sequence for result in recorder.results()] == [41]


#:= docs/specs/robot-link/index.md#req-016-results-return-the-capture-timestamp-unaltered
#:% Every result MUST carry the capture timestamp of the frame it derives from,
#:% byte-for-byte as the capturing side supplied it, so that the capturing side can
#:% compute the result's age against the same clock that produced it.
@pytest.mark.asyncio
async def test_the_capture_token_is_copied_through_byte_for_byte() -> None:
    """The stamp belongs to the robot's clock and is never read here."""
    recorder = _Recorder()
    pipeline, _obs, _spans = _pipeline(
        EchoCapability(), TallyCapability(), recorder=recorder
    )
    await pipeline.process(_queued(9))
    assert {result.captured_at.root for result in recorder.results()} == {STAMP}


@pytest.mark.asyncio
async def test_a_capture_token_this_service_cannot_interpret_still_returns() -> None:
    """An opaque stamp is opaque: nothing here parses it, so nothing rejects it."""
    recorder = _Recorder()
    pipeline, _obs, _spans = _pipeline(EchoCapability(), recorder=recorder)
    frame = QueuedFrame(
        header=make_header(2, "not-a-number-at-all"),
        payload=jpeg_bytes(),
        received_at=0.0,
    )
    await pipeline.process(frame)
    assert recorder.results()[0].captured_at.root == "not-a-number-at-all"


#:= docs/specs/robot-link/index.md#req-013-an-empty-result-is-a-valid-result
#:% A result message carrying no detections MUST be treated as a successful result
#:% for that frame.
@pytest.mark.asyncio
async def test_an_empty_result_is_delivered_as_a_result() -> None:
    """No gesture in the frame is an answer, not a failure to answer."""
    recorder = _Recorder()
    pipeline, _obs, _spans = _pipeline(
        TallyCapability(empty=True),
        recorder=recorder,
    )
    await pipeline.process(_queued(5))
    (result,) = recorder.results()
    assert isinstance(result.payload, GestureDetections)
    assert result.payload.gestures == ()
    assert recorder.errors() == []


@pytest.mark.asyncio
async def test_an_empty_result_advances_no_error_counter() -> None:
    """The predecessor posted nothing and got a 400 back; this is the fix."""
    recorder = _Recorder()
    pipeline, obs, _spans = _pipeline(TallyCapability(empty=True), recorder=recorder)
    await pipeline.process(_queued(5))
    samples = {
        sample.labels["code"]: sample.value
        for metric in obs.metrics.registry.collect()
        for sample in metric.samples
        if metric.name == "groundstation_errors"
    }
    assert samples == {}


@pytest.mark.asyncio
async def test_a_capability_that_raises_costs_only_its_own_answer() -> None:
    """One failing capability does not silence the ones beside it."""
    recorder = _Recorder()
    broken = ExplodingCapability(Capability(name="broken", version=1), on_process=True)
    pipeline, _obs, _spans = _pipeline(broken, EchoCapability(), recorder=recorder)
    await pipeline.process(_queued(6))
    assert [result.capability for result in recorder.results()] == ["echo"]
    (error,) = recorder.errors()
    assert error.code is ErrorCode.CAPABILITY_FAILED
    assert error.sequence == 6
    # Which capability failed and what kind of failure it was, and nothing the
    # exception itself said: that text is the capability's, and it stays in the
    # log rather than crossing the link.
    assert error.detail == "broken: RuntimeError"
    assert "cannot answer this frame" not in error.detail


@pytest.mark.asyncio
async def test_a_capability_that_overruns_is_abandoned() -> None:
    """A capability with no timeout would hold the session's whole pipeline."""

    class _Stuck(EchoCapability):
        async def process(self, frame: DecodedFrame) -> WireModel:
            del frame
            await asyncio.Event().wait()
            raise AssertionError

    recorder = _Recorder()
    pipeline, _obs, _spans = _pipeline(
        _Stuck(),
        recorder=recorder,
        capability_timeout_seconds=_ALREADY_ELAPSED,
    )
    await pipeline.process(_queued(7))
    assert recorder.results() == []
    assert recorder.errors()[0].code is ErrorCode.CAPABILITY_FAILED


@pytest.mark.asyncio
async def test_an_undecodable_frame_is_reported_and_no_capability_runs() -> None:
    """A payload that is not an image never reaches a capability."""
    echo = EchoCapability()
    recorder = _Recorder()
    pipeline, _obs, _spans = _pipeline(echo, recorder=recorder)
    await pipeline.process(_queued(8, payload=b"this is not a jpeg"))
    assert echo.seen == []
    (error,) = recorder.errors()
    assert error.code is ErrorCode.MALFORMED_MESSAGE
    assert error.sequence == 8


#:= docs/specs/groundstation/index.md#req-029-per-stage-timings-are-measured-and-exposed
#:% The service MUST record the duration of each pipeline stage separately and
#:% expose those durations as metrics.
@pytest.mark.asyncio
async def test_each_stage_is_timed_separately() -> None:
    """Decode, each capability and emission are distinguishable afterwards."""
    recorder = _Recorder()
    pipeline, obs, _spans = _pipeline(
        EchoCapability(),
        TallyCapability(),
        recorder=recorder,
    )
    await pipeline.process(_queued(2))
    stages = {
        sample.labels["stage"]
        for metric in obs.metrics.registry.collect()
        for sample in metric.samples
        if sample.name.endswith("_count") and "stage" in sample.labels
    }
    capabilities = {
        sample.labels["capability"]
        for metric in obs.metrics.registry.collect()
        for sample in metric.samples
        if sample.name.endswith("_count") and "capability" in sample.labels
    }
    assert stages == {STAGE_QUEUE, STAGE_DECODE, STAGE_EMIT}
    assert capabilities == {"echo", "tally"}


#:= docs/specs/groundstation/index.md#req-028-work-is-attributable-end-to-end
#:% Every log line and metric emitted while handling a frame MUST carry the session
#:% identifier and the frame's sequence number.
@pytest.mark.asyncio
async def test_a_timing_carries_the_session_and_the_sequence_number() -> None:
    """As an exemplar, not as a label: a label per frame is unbounded series."""
    recorder = _Recorder()
    pipeline, obs, _spans = _pipeline(EchoCapability(), recorder=recorder)
    await pipeline.process(_queued(77))
    exemplars = [
        sample.exemplar
        for metric in obs.metrics.registry.collect()
        for sample in metric.samples
        if sample.exemplar is not None
    ]
    assert exemplars
    assert all(
        exemplar.labels == {"session": SESSION, "sequence": "77"}
        for exemplar in exemplars
    )


@pytest.mark.asyncio
async def test_the_pipeline_opens_a_span_for_every_stage() -> None:
    """Tracing spans across the stages, so a slow frame is legible as one."""
    recorder = _Recorder()
    pipeline, _obs, exporter = _pipeline(
        EchoCapability(),
        TallyCapability(),
        recorder=recorder,
    )
    await pipeline.process(_queued(4))
    assert recorded_spans(exporter) == (
        "decode",
        "capability",
        "capability",
        "frame",
    )


@pytest.mark.asyncio
async def test_the_frame_span_names_the_session_and_the_sequence_number() -> None:
    """The span is the other half of REQ-028's attributability."""
    recorder = _Recorder()
    pipeline, _obs, exporter = _pipeline(EchoCapability(), recorder=recorder)
    await pipeline.process(_queued(12))
    frame_span = next(
        span for span in exporter.get_finished_spans() if span.name == "frame"
    )
    assert frame_span.attributes is not None
    assert frame_span.attributes["reachy.session_id"] == SESSION
    assert frame_span.attributes["reachy.sequence"] == 12


@pytest.mark.asyncio
async def test_the_pipeline_drains_its_queue_and_stops_when_it_closes() -> None:
    """A session that ends leaves no task parked on a queue nobody feeds."""
    echo = EchoCapability()
    recorder = _Recorder()
    pipeline, _obs, _spans = _pipeline(echo, recorder=recorder)
    queue = FrameQueue(4)
    queue.put(_queued(1))
    queue.put(_queued(2))
    queue.close()
    await pipeline.run(queue)
    assert [result.sequence for result in recorder.results()] == [1, 2]


@pytest.mark.asyncio
async def test_a_result_carries_the_capability_that_produced_it() -> None:
    """A consumer routes a result by name, so the name has to be on it."""
    recorder = _Recorder()
    pipeline, _obs, _spans = _pipeline(EchoCapability(), recorder=recorder)
    await pipeline.process(_queued(0))
    (result,) = recorder.results()
    assert result.capability == ECHO.name
    assert isinstance(result.payload, FaceDetections)
    assert TALLY.name != result.capability


#:= docs/specs/groundstation/index.md#req-028-work-is-attributable-end-to-end
#:% Every log line and metric emitted while handling a frame MUST carry the session
#:% identifier and the frame's sequence number.
@pytest.mark.asyncio
async def test_a_pipeline_that_stops_says_which_frame_it_was_handling() -> None:
    """The line naming the failure is emitted where the frame is still known.

    A client that vanishes mid-answer stops the pipeline from inside
    `deliver`, and the exception reaches the session layer with the frame
    context already unwound. Recording it there would produce a line with a
    session and no sequence number.
    """

    class _Vanishing(_Recorder):
        async def deliver(self, kind: MessageKind, message: WireModel) -> None:
            del kind, message
            message_text = "client disconnected mid-answer"
            raise TransportClosedError(message_text)

    recorder = _Vanishing()
    pipeline, _obs, _spans = _pipeline(EchoCapability(), recorder=recorder)
    queue = FrameQueue(2)
    queue.put(_queued(23))
    queue.close()

    # Under a session binding, the way the pipeline task always runs: the
    # session identifier is bound by the session and the sequence number by the
    # pipeline, and a line carries both only if both are still in scope.
    with (
        captured_logs() as logs,
        session_context(SESSION),
        pytest.raises(TransportClosedError),
    ):
        await pipeline.run(queue)

    (line,) = [entry for entry in logs if entry["event"] == "pipeline.stopped"]
    assert line["sequence"] == 23
    assert line["session"] == SESSION
    assert line["error"] == "TransportClosedError"


#:= docs/specs/groundstation/index.md#req-025-a-failed-capability-does-not-take-down-the-service
#:% When a capability fails to initialise, the service MUST continue serving the
#:% capabilities that initialised successfully.
@pytest.mark.asyncio
async def test_a_capability_answering_with_the_wrong_payload_costs_only_itself() -> (
    None
):
    """Assembling the envelope is where a mismatched payload is caught.

    A registered capability name carries a declared payload type, and a
    capability that answers with a different one fails when the envelope is
    built. That has to be contained the same way a capability raising is: if it
    escaped, the session's whole pipeline would stop while frames kept
    arriving, and the client would be told nothing.
    """

    class _Confused(CapabilityBase):
        async def process(self, frame: DecodedFrame) -> WireModel:
            del frame
            return GestureDetections()

    recorder = _Recorder()
    pipeline, _obs, _spans = _pipeline(
        _Confused(Capability(name=FACE_CAPABILITY, version=1)),
        EchoCapability(),
        recorder=recorder,
    )
    await pipeline.process(_queued(31))

    assert [result.capability for result in recorder.results()] == ["echo"]
    (error,) = recorder.errors()
    assert error.code is ErrorCode.CAPABILITY_FAILED
    assert error.detail.startswith("face: ")
    assert error.sequence == 31
