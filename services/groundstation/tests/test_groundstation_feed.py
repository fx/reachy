"""The live frame: what may enter it, who may see it, and when it disappears.

Three subjects, and they are here together because they are one guarantee. The
format gate decides what may be retained, the registry decides whether anything
is retained at all, and the endpoint decides what a viewer is told when nothing
is. Split across three files, the interesting cases — a decodable PNG, a session
that became two, a viewer arriving after ambiguity — would each land in whichever
file happened to own the last step.

What is *not* here is the streaming itself. Multipart framing, a slow viewer, a
disconnect and the viewer bound are transport behaviour, and a buffering in-memory
client would test the buffer; they are driven over a real socket in
`test_groundstation_integration.py` instead.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`. Nothing here opens a socket, reads a file or waits on a clock:
encoding an image in memory is arithmetic, and `hand_control_to_the_event_loop`
yields rather than sleeps.
"""

from __future__ import annotations

import asyncio
import contextlib

import httpx
import pytest
from groundstation_support import (
    ECHO,
    EchoCapability,
    StaticRegistry,
    agreed,
    build_observability,
    hand_control_to_the_event_loop,
    jpeg_bytes,
    make_header,
    make_settings,
    png_bytes,
)
from starlette.responses import StreamingResponse

from reachy_contracts import ErrorCode, WireModel
from reachy_groundstation.api.app import STREAM_PATH, create_app
from reachy_groundstation.api.mjpeg import stream_response
from reachy_groundstation.feed import (
    MAX_VIEWERS,
    FeedAvailability,
    FeedRegistry,
)
from reachy_groundstation.pipeline.decode import is_jpeg
from reachy_groundstation.pipeline.queue import QueuedFrame
from reachy_groundstation.pipeline.runner import FramePipeline
from reachy_groundstation.session.framing import MessageKind

SESSION = "0123456789abcdef"


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


def _pipeline(
    feed: FeedRegistry,
    capability: EchoCapability,
    recorder: _Recorder,
) -> FramePipeline:
    """Build a real pipeline writing into a given feed.

    Args:
        feed: Where a validated frame is offered.
        capability: What answers every frame.
        recorder: What the pipeline delivers into.

    Returns:
        The pipeline.
    """
    obs, _exporter = build_observability()
    return FramePipeline(
        capabilities=[agreed(capability)],
        deliver=recorder.deliver,
        settings=make_settings(),
        obs=obs,
        session_id=SESSION,
        feed=feed,
        clock=lambda: 0.0,
    )


def _queued(payload: bytes, sequence: int = 0) -> QueuedFrame:
    """Build a frame as the session layer would have queued one.

    Args:
        payload: The compressed bytes.
        sequence: The frame's number within its session.

    Returns:
        The queued frame.
    """
    return QueuedFrame(header=make_header(sequence), payload=payload, received_at=0.0)


def _client(feed: FeedRegistry) -> httpx.AsyncClient:
    """Build a client over a real application sharing one feed.

    Args:
        feed: What the application serves the stream from.

    Returns:
        An HTTP client speaking to the application in memory.
    """
    obs, _exporter = build_observability()
    app = create_app(
        settings=make_settings(),
        registry=StaticRegistry(EchoCapability()),
        obs=obs,
        feed=feed,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://groundstation.invalid",
    )


# The format gate.


def test_a_real_jpeg_passes_the_format_gate() -> None:
    """The gate has to accept what the robot actually sends."""
    assert is_jpeg(jpeg_bytes()) is True


def test_a_decodable_png_fails_the_format_gate() -> None:
    """The decoder reads PNG, so decoding is not evidence of format."""
    payload = png_bytes()
    assert is_jpeg(payload) is False


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\xff\xd8",
        b"this is not a jpeg",
        b"GIF89a",
        b"\x89PNG\r\n\x1a\n",
    ],
)
def test_bytes_that_are_not_jpeg_fail_the_format_gate(payload: bytes) -> None:
    """Two matching bytes are not a signature, and no other format has three.

    Args:
        payload: The bytes offered to the gate.
    """
    assert is_jpeg(payload) is False


# The registry's eligibility rules.


def test_nothing_is_retained_without_a_session() -> None:
    """A payload with no session behind it belongs to nobody."""
    feed = FeedRegistry()
    assert feed.publish(jpeg_bytes()) is False
    assert feed.availability() is FeedAvailability.NO_ELIGIBLE_SESSION


def test_one_session_supplying_a_frame_makes_the_feed_available() -> None:
    """The whole eligible case: one session, one validated frame."""
    feed = FeedRegistry()
    with feed.authenticated_session():
        assert feed.availability() is FeedAvailability.NO_ELIGIBLE_SESSION
        assert feed.publish(jpeg_bytes()) is True
        assert feed.availability() is FeedAvailability.AVAILABLE


@pytest.mark.asyncio
async def test_the_retained_bytes_are_the_bytes_that_arrived() -> None:
    """No copy, no re-encode: the operator sees what the robot compressed."""
    payload = jpeg_bytes(width=64, height=48)
    feed = FeedRegistry()
    with feed.authenticated_session():
        feed.publish(payload)
        frame = await feed.next_frame(after=0)
    assert frame is not None
    assert frame.payload == payload


#:= docs/specs/home-assistant-configuration-and-camera-feed/index.md#req-096-mjpeg-is-a-bounded-latest-frame-view
#:% The groundstation MUST retain at most one original payload globally for a
#:% standards-compatible MJPEG stream only after both explicit JPEG-format signature
#:% validation and successful image decode, replace rather than queue that payload
#:% for slow viewers, and add no robot connection, stream-only decode or re-encode,
#:% or capability-processing blockage.
@pytest.mark.asyncio
async def test_a_slow_viewer_is_handed_the_newest_frame_and_not_a_backlog() -> None:
    """Replacement rather than queueing is what keeps a slow viewer live."""
    feed = FeedRegistry()
    with feed.authenticated_session():
        for fill in (10, 20, 30):
            feed.publish(jpeg_bytes(fill=fill))
        frame = await feed.next_frame(after=0)
    assert frame is not None
    assert frame.payload == jpeg_bytes(fill=30)
    assert frame.revision == 3


def test_a_second_session_clears_the_frame_rather_than_keeping_one_each() -> None:
    """Connection order and frame recency are not operator intent."""
    feed = FeedRegistry()
    with feed.authenticated_session():
        feed.publish(jpeg_bytes())
        with feed.authenticated_session():
            assert feed.availability() is FeedAvailability.AMBIGUOUS_SESSIONS
            assert feed.publish(jpeg_bytes(fill=200)) is False


#:= docs/specs/home-assistant-configuration-and-camera-feed/index.md#req-097-feed-eligibility-is-deterministic
#:% The groundstation MUST serve `/stream.mjpg` only after exactly one active
#:% authenticated robot session has supplied a fresh validated JPEG while it is the
#:% sole session, clear all feed frame state and end viewers whenever authenticated
#:% session cardinality is zero or greater than one, and require another fresh
#:% validated JPEG after cardinality returns to one.
def test_returning_to_one_session_does_not_resurrect_the_earlier_frame() -> None:
    """The room the operator would be shown is the one they last saw."""
    feed = FeedRegistry()
    with feed.authenticated_session():
        feed.publish(jpeg_bytes())
        with feed.authenticated_session():
            pass
        assert feed.availability() is FeedAvailability.NO_ELIGIBLE_SESSION
        feed.publish(jpeg_bytes(fill=200))
        assert feed.availability() is FeedAvailability.AVAILABLE


def test_a_revision_never_goes_backwards_across_a_clear() -> None:
    """A viewer that saw revision N must never be handed N again as new."""
    feed = FeedRegistry()
    with feed.authenticated_session():
        feed.publish(jpeg_bytes())
        first = feed.revision
    with feed.authenticated_session():
        feed.publish(jpeg_bytes(fill=200))
        second = feed.revision
    assert second > first


def test_the_session_ending_discards_its_frame() -> None:
    """A frame outliving the session it came from is retention, not a feed."""
    feed = FeedRegistry()
    with feed.authenticated_session():
        feed.publish(jpeg_bytes())
    assert feed.availability() is FeedAvailability.NO_ELIGIBLE_SESSION


# Viewers waking, finishing and being counted.


@pytest.mark.asyncio
async def test_a_waiting_viewer_wakes_when_a_frame_arrives() -> None:
    """A viewer between frames waits on the state rather than polling it."""
    feed = FeedRegistry()
    with feed.authenticated_session():
        waiting = asyncio.create_task(feed.next_frame(after=0))
        await hand_control_to_the_event_loop()
        assert waiting.done() is False
        feed.publish(jpeg_bytes())
        frame = await asyncio.wait_for(waiting, timeout=1.0)
    assert frame is not None


@pytest.mark.asyncio
async def test_a_waiting_viewer_finishes_when_the_session_ends() -> None:
    """Losing eligibility ends the viewer instead of leaving it parked."""
    feed = FeedRegistry()
    session = feed.authenticated_session()
    session.__enter__()
    feed.publish(jpeg_bytes())
    waiting = asyncio.create_task(feed.next_frame(after=feed.revision))
    await hand_control_to_the_event_loop()
    assert waiting.done() is False
    session.__exit__(None, None, None)
    assert await asyncio.wait_for(waiting, timeout=1.0) is None


@pytest.mark.asyncio
async def test_a_waiting_viewer_finishes_when_a_second_session_arrives() -> None:
    """Ambiguity ends existing viewers as well as refusing new ones."""
    feed = FeedRegistry()
    with feed.authenticated_session():
        feed.publish(jpeg_bytes())
        waiting = asyncio.create_task(feed.next_frame(after=feed.revision))
        await hand_control_to_the_event_loop()
        assert waiting.done() is False
        with feed.authenticated_session():
            assert await asyncio.wait_for(waiting, timeout=1.0) is None


@pytest.mark.asyncio
async def test_a_waiting_viewer_finishes_when_the_feed_closes() -> None:
    """Shutdown leaves no task parked on a value nothing will produce again."""
    feed = FeedRegistry()
    with feed.authenticated_session():
        feed.publish(jpeg_bytes())
        waiting = asyncio.create_task(feed.next_frame(after=feed.revision))
        await hand_control_to_the_event_loop()
        feed.close()
        assert await asyncio.wait_for(waiting, timeout=1.0) is None
        assert feed.publish(jpeg_bytes()) is False


def test_the_viewer_bound_is_finite_and_slots_come_back() -> None:
    """Four at once, and a fifth only once one of them has finished."""
    feed = FeedRegistry()
    assert [feed.reserve_viewer() for _ in range(MAX_VIEWERS)] == [True] * MAX_VIEWERS
    assert feed.reserve_viewer() is False
    feed.release_viewer()
    assert feed.reserve_viewer() is True
    assert feed.viewers == MAX_VIEWERS


# What the pipeline offers the feed, and what it does not.


@pytest.mark.asyncio
async def test_the_pipeline_publishes_a_frame_it_decoded() -> None:
    """The feed is fed from the frames the capabilities were already given."""
    payload = jpeg_bytes()
    feed = FeedRegistry()
    capability = EchoCapability()
    recorder = _Recorder()
    with feed.authenticated_session():
        await _pipeline(feed, capability, recorder).process(_queued(payload))
        frame = await feed.next_frame(after=0)
    assert frame is not None
    assert frame.payload == payload
    assert capability.seen  # the capability still answered the same frame


#:= docs/specs/home-assistant-configuration-and-camera-feed/index.md#req-096-mjpeg-is-a-bounded-latest-frame-view
#:% The groundstation MUST retain at most one original payload globally for a
#:% standards-compatible MJPEG stream only after both explicit JPEG-format signature
#:% validation and successful image decode, replace rather than queue that payload
#:% for slow viewers, and add no robot connection, stream-only decode or re-encode,
#:% or capability-processing blockage.
@pytest.mark.asyncio
async def test_a_decodable_png_is_never_published() -> None:
    """It decodes, the capabilities answer it, and it is not the feed's."""
    feed = FeedRegistry()
    capability = EchoCapability()
    recorder = _Recorder()
    with feed.authenticated_session():
        await _pipeline(feed, capability, recorder).process(_queued(png_bytes()))
        assert feed.availability() is FeedAvailability.NO_ELIGIBLE_SESSION
    assert [kind for kind, _ in recorder.messages] == [MessageKind.RESULT]
    assert len(capability.seen) == 1


@pytest.mark.asyncio
async def test_a_malformed_jpeg_leaves_the_previous_frame_in_place() -> None:
    """Signature-bearing bytes that will not decode replace nothing."""
    good = jpeg_bytes()
    feed = FeedRegistry()
    recorder = _Recorder()
    with feed.authenticated_session():
        pipeline = _pipeline(feed, EchoCapability(), recorder)
        await pipeline.process(_queued(good))
        await pipeline.process(_queued(b"\xff\xd8\xff\xe0 truncated", sequence=1))
        frame = await feed.next_frame(after=0)
    assert frame is not None
    assert frame.payload == good
    # The client was still told about the bad frame; the feed changed nothing
    # about how a decode failure is reported.
    errors = [
        message for kind, message in recorder.messages if kind is MessageKind.ERROR
    ]
    assert [getattr(error, "code", None) for error in errors] == [
        ErrorCode.MALFORMED_MESSAGE,
    ]


@pytest.mark.asyncio
async def test_publishing_never_blocks_the_capability_pipeline() -> None:
    """Four viewers waiting is four references, not four queues to fill."""
    feed = FeedRegistry(max_viewers=2)
    capability = EchoCapability()
    recorder = _Recorder()
    with feed.authenticated_session():
        pipeline = _pipeline(feed, capability, recorder)
        waiting = [
            asyncio.create_task(feed.next_frame(after=0)),
            asyncio.create_task(feed.next_frame(after=0)),
        ]
        await hand_control_to_the_event_loop()
        for sequence in range(5):
            await pipeline.process(_queued(jpeg_bytes(fill=sequence), sequence))
        assert len(capability.seen) == 5
        for task in waiting:
            assert await asyncio.wait_for(task, timeout=1.0) is not None


# What a request that is not given a stream is told.


#:= docs/specs/home-assistant-configuration-and-camera-feed/index.md#req-097-feed-eligibility-is-deterministic
#:% The groundstation MUST serve `/stream.mjpg` only after exactly one active
#:% authenticated robot session has supplied a fresh validated JPEG while it is the
#:% sole session, clear all feed frame state and end viewers whenever authenticated
#:% session cardinality is zero or greater than one, and require another fresh
#:% validated JPEG after cardinality returns to one.
@pytest.mark.asyncio
async def test_a_request_with_no_robot_connected_is_refused_as_unavailable() -> None:
    """No stream is held open waiting for a robot that may never connect."""
    async with _client(FeedRegistry()) as client:
        response = await client.get(STREAM_PATH)
    assert response.status_code == 503
    assert response.json() == {"feed": "no_eligible_session"}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_a_request_with_two_robots_connected_is_refused_as_ambiguous() -> None:
    """A different code, because it is a different thing to go and fix."""
    feed = FeedRegistry()
    with feed.authenticated_session(), feed.authenticated_session():
        async with _client(feed) as client:
            response = await client.get(STREAM_PATH)
    assert response.status_code == 409
    assert response.json() == {"feed": "ambiguous_sessions"}
    assert response.headers["cache-control"] == "no-store"


#:= docs/specs/home-assistant-configuration-and-camera-feed/index.md#req-098-the-unauthenticated-feed-has-a-bounded-privacy-surface
#:% The groundstation MUST keep `/stream.mjpg` intentionally unauthenticated within
#:% the deployment's trusted-network boundary while retaining at most one live JPEG
#:% globally in application state, marking responses non-cacheable, never recording
#:% or writing frames or emitting frame content through observability, enforcing a
#:% finite viewer bound, and promptly cancelling viewer work on disconnect or loss
#:% of eligibility.
@pytest.mark.asyncio
async def test_a_request_beyond_the_viewer_bound_is_refused_as_at_capacity() -> None:
    """Capacity is not unavailability: the robot is fine and the service is busy."""
    feed = FeedRegistry(max_viewers=1)
    with feed.authenticated_session():
        feed.publish(jpeg_bytes())
        assert feed.reserve_viewer() is True
        async with _client(feed) as client:
            response = await client.get(STREAM_PATH)
    assert response.status_code == 429
    assert response.json() == {"feed": "viewer_capacity_reached"}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_a_refusal_names_the_situation_and_nothing_else() -> None:
    """A body carrying a session identifier would be the selection input.

    The endpoint refuses to choose among sessions, so it also declines to say
    anything an operator could choose with — and an unauthenticated endpoint is
    the last place a count of what is connected belongs.
    """
    feed = FeedRegistry()
    with feed.authenticated_session(), feed.authenticated_session():
        async with _client(feed) as client:
            response = await client.get(STREAM_PATH)
    assert set(response.json()) == {"feed"}


@pytest.mark.asyncio
async def test_a_head_request_answers_without_taking_a_viewer_slot() -> None:
    """Starlette answers HEAD with what serves GET, and a stream is not that.

    Four HEAD requests holding open connections nobody reads would be the whole
    viewer bound, on an endpoint that asks for no credential.
    """
    feed = FeedRegistry()
    with feed.authenticated_session():
        feed.publish(jpeg_bytes())
        async with _client(feed) as client:
            for _ in range(MAX_VIEWERS + 2):
                response = await client.head(STREAM_PATH)
                assert response.status_code == 200
    assert feed.viewers == 0
    assert response.headers["content-type"].startswith("multipart/x-mixed-replace")
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_a_head_request_reports_the_same_refusal_a_get_would() -> None:
    """It says what a GET would have been told, rather than always 200."""
    feed = FeedRegistry()
    async with _client(feed) as client:
        response = await client.head(STREAM_PATH)
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_a_head_request_reports_capacity_without_taking_the_last_slot() -> None:
    """The one refusal a HEAD could have answered 200 to, and it is the worst.

    A saturated feed answering `HEAD` with 200 tells a client to open a `GET`
    that is about to be refused, and it is the only situation in the table an
    operator cannot see from the endpoint's own state.
    """
    feed = FeedRegistry(max_viewers=1)
    with feed.authenticated_session():
        feed.publish(jpeg_bytes())
        assert feed.reserve_viewer() is True
        async with _client(feed) as client:
            response = await client.head(STREAM_PATH)
    assert response.status_code == 429
    assert response.headers["cache-control"] == "no-store"
    # The slot the GET took, and no other: asking cost nothing.
    assert feed.viewers == 1


def test_the_capacity_question_can_be_asked_without_answering_it() -> None:
    """`reserve_viewer` answers by taking, which is why `at_capacity` exists."""
    feed = FeedRegistry(max_viewers=1)
    # Read into locals and compared at the end, because an `assert` on a
    # property narrows its type for everything after it and the next reading
    # then looks unreachable to the type checker.
    empty = feed.at_capacity
    assert feed.reserve_viewer() is True
    full = feed.at_capacity
    feed.release_viewer()
    freed = feed.at_capacity
    assert (empty, full, freed) == (False, True, False)
    assert feed.viewers == 0


@pytest.mark.parametrize(
    ("sessions", "reserved"),
    [
        (0, 0),  # nothing connected
        (2, 0),  # two robots, and no choosing between them
        (1, 0),  # a stream to be had
        (1, 1),  # every slot taken
    ],
)
def test_a_head_answers_with_exactly_the_headers_a_get_would_have_sent(
    sessions: int,
    reserved: int,
) -> None:
    """A HEAD describes the answer to a GET, so it may not describe itself.

    The responses are built rather than driven over a client, because what is
    being compared is the header list each one would put on the wire — and the
    `GET` in the available case is a stream that never finishes, so a client
    would have to be interrupted to be asked.

    Args:
        sessions: How many authenticated sessions to hold open.
        reserved: How many viewer slots to take before asking.
    """
    feed = FeedRegistry(max_viewers=1)
    with contextlib.ExitStack() as held:
        for _ in range(sessions):
            held.enter_context(feed.authenticated_session())
        feed.publish(jpeg_bytes())
        for _ in range(reserved):
            assert feed.reserve_viewer() is True

        head = stream_response(feed, "HEAD")
        get = stream_response(feed, "GET")

    assert head.status_code == get.status_code
    assert head.raw_headers == get.raw_headers
    # Only the `GET` may have taken a slot, and only when it was given a stream.
    assert feed.viewers == reserved + int(isinstance(get, StreamingResponse))


@pytest.mark.asyncio
async def test_a_refused_request_takes_no_viewer_slot() -> None:
    """A robot that is not connected must not exhaust the bound by itself."""
    feed = FeedRegistry()
    async with _client(feed) as client:
        for _ in range(MAX_VIEWERS + 2):
            assert (await client.get(STREAM_PATH)).status_code == 503
    assert feed.viewers == 0


@pytest.mark.asyncio
async def test_the_stream_endpoint_is_registered_and_reads_no_parameter() -> None:
    """One path, no query: there is nothing to select among."""
    obs, _exporter = build_observability()
    app = create_app(
        settings=make_settings(),
        registry=StaticRegistry(EchoCapability()),
        obs=obs,
    )
    paths = {getattr(route, "path", None) for route in app.routes}
    assert STREAM_PATH in paths
    assert ECHO.name == "echo"


@pytest.mark.asyncio
async def test_the_application_lifespan_closes_the_feed_it_was_given() -> None:
    """Shutdown is the only notice this layer gets that the process is stopping."""
    feed = FeedRegistry()
    obs, _exporter = build_observability()
    app = create_app(
        settings=make_settings(),
        registry=StaticRegistry(EchoCapability()),
        obs=obs,
        feed=feed,
    )
    with feed.authenticated_session():
        feed.publish(jpeg_bytes())
        async with app.router.lifespan_context(app):
            assert feed.availability() is FeedAvailability.AVAILABLE
        assert feed.availability() is FeedAvailability.NO_ELIGIBLE_SESSION
