"""End to end over a real server and a real WebSocket.

Every test here opens a socket, and every test here says so with
`@pytest.mark.enable_socket`. The reason is the same in all of them: this change
is about session behaviour, and a mocked transport would test the mock. A real
uvicorn server is started in-process on the loopback interface with an ephemeral
port, and a real `websockets` client drives it — the same handshake, the same
framing, the same close codes a robot will meet.

Backpressure and reconnection are induced rather than configured. A capability
that parks on an event holds the pipeline while frames pile up behind it, and the
reconnection test drops the connection mid-session and negotiates again against a
service whose capability set changed in between.

The operator feed is here for the same reason. Its subject is multipart framing,
a viewer bound, a disconnect and a stream that has to end — none of which an
in-memory client that buffers a whole response can show. So the feed's viewers
are real HTTP clients reading a real socket while a real robot session feeds it,
and its eligibility rules are unit-tested in `test_groundstation_feed.py`.

Shutting the server down is part of what is under test rather than a fixture
detail, which is why every test here stops it the way a container does — the
composition root's own server class, a real `SIGTERM`, and no graceful-shutdown
timeout to bound the wait. A harness that differed from the composed service on
any of those three would be unable to see a stream that stops it from stopping.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from typing import TYPE_CHECKING

import httpx
import pytest
import uvicorn
import websockets.exceptions
from groundstation_support import (
    CREDENTIAL,
    ECHO,
    TALLY,
    BlockingCapability,
    EchoCapability,
    StaticRegistry,
    TallyCapability,
    build_observability,
    frame_message,
    jpeg_bytes,
    make_settings,
    offer_message,
)
from websockets.asyncio.client import connect

from reachy_contracts import SessionAgreement, SessionClose
from reachy_groundstation.api.app import SESSION_PATH, STREAM_PATH, create_app
from reachy_groundstation.api.mjpeg import BOUNDARY
from reachy_groundstation.feed import MAX_VIEWERS, FeedRegistry
from reachy_groundstation.service import FeedClosingServer
from reachy_groundstation.session.framing import MessageKind, decode_control
from reachy_groundstation.session.transport import CLOSE_POLICY_VIOLATION

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from starlette.applications import Starlette
    from websockets.asyncio.client import ClientConnection

    from reachy_groundstation.obs import Observability
    from reachy_groundstation.ports import CapabilityRegistryPort

STAMP = "17352.884"

# How long a client waits for a message that should already be on its way. Long
# enough that a loaded runner does not flake, short enough that a genuine hang
# fails the suite rather than stalling it.
_TIMEOUT = 10.0


@contextlib.contextmanager
def _shutdown_signal_absorbed() -> Iterator[None]:
    """Keep the signal these tests stop the server with from ending the run.

    Uvicorn restores the handlers it replaced and then re-raises whatever signal
    it caught, so that a process which asked to stop still stops. Here the
    handler that re-raise reaches is pytest's, and the suite would end at the
    first server it shut down. This is the one part of the path these tests
    replace: the delivery, uvicorn's own handler and everything its shutdown
    does are real.

    Yields:
        Nothing; the absorbing is the point.
    """
    previous = signal.signal(signal.SIGTERM, lambda *_: None)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


class _Harness:
    """A running server and the pieces a test wants to look at.

    Attributes:
        port: The ephemeral port the server bound.
        url: Where to open a session.
        obs: The reporting bundle the service is writing to.
        feed: The live frame the application serves `/stream.mjpg` from, so a
            test can read the viewer count back rather than inferring it from
            what the endpoint answered.
        serving: The task running the server, so a test about stopping can wait
            for it to stop rather than assume it did.
    """

    def __init__(
        self,
        port: int,
        obs: Observability,
        feed: FeedRegistry,
        serving: asyncio.Task[None],
    ) -> None:
        """Record where the server ended up.

        Args:
            port: The ephemeral port it bound.
            obs: The reporting bundle it writes to.
            feed: The live frame it serves.
            serving: The task running it.
        """
        self.port = port
        self.url = f"ws://127.0.0.1:{port}{SESSION_PATH}"
        self.obs = obs
        self.feed = feed
        self.serving = serving

    def sample(self, name: str) -> float:
        """Read one metric back.

        Args:
            name: The full metric name.

        Returns:
            Its current value, or zero when nothing has been recorded.
        """
        value = self.obs.metrics.registry.get_sample_value(name)
        return 0.0 if value is None else value


@contextlib.asynccontextmanager
async def _serving(
    registry: CapabilityRegistryPort,
    **overrides: object,
) -> AsyncIterator[_Harness]:
    """Run the real application on a real socket for the duration of a test.

    The server is the composition root's own `FeedClosingServer`, configured the
    way `service.main` configures it and stopped the way a container stops it —
    including leaving `timeout_graceful_shutdown` unset. A harness that bounded
    the graceful shutdown where production does not would be compensating for
    the one defect this file is best placed to catch: an open stream that makes
    the drain wait for the very close it is waiting to be told about.

    Args:
        registry: What the application is composed around.
        overrides: Settings to change from their defaults.

    Yields:
        Where the server is listening and what it is reporting to.
    """
    obs, _exporter = build_observability()
    feed = FeedRegistry()
    app: Starlette = create_app(
        settings=make_settings(**overrides),
        registry=registry,
        obs=obs,
        feed=feed,
    )
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_config=None,
        ws="websockets-sansio",
        # Mirrors how `reachy_groundstation.service` serves the application, so
        # what these tests drive is what a robot will meet.
        ws_max_size=make_settings(**overrides).max_message_bytes,
        lifespan="on",
    )
    server = FeedClosingServer(config, feed=feed)
    # Installed before the server is, because uvicorn records the handlers it
    # displaces as it starts serving and restores those on the way out.
    with _shutdown_signal_absorbed():
        task = asyncio.create_task(server.serve(), name="uvicorn")
        try:
            # Bounded rather than open-ended: a server that failed to start
            # would otherwise hang the suite instead of failing it.
            deadline = asyncio.get_running_loop().time() + _TIMEOUT
            while not server.started:
                if task.done():
                    await task
                    message = "the server stopped before it started"
                    raise AssertionError(message)
                if asyncio.get_running_loop().time() > deadline:
                    message = "the server did not start"
                    raise AssertionError(message)
                await asyncio.sleep(0.005)
            port = server.servers[0].sockets[0].getsockname()[1]
            yield _Harness(port, obs, feed, task)
        finally:
            # `handle_exit` and not `should_exit`, because `handle_exit` is what
            # a signal reaches and it is where the feed is closed. Setting the
            # flag would skip that and hang here on any test that left a viewer
            # open — which is exactly what the composed service used to do.
            server.handle_exit(signal.SIGTERM, None)
            await asyncio.wait_for(task, timeout=_TIMEOUT)


async def _receive(connection: ClientConnection) -> tuple[MessageKind, bytes]:
    """Read the next control message off a connection.

    Args:
        connection: The open WebSocket.

    Returns:
        The kind and canonical bytes of the message.
    """
    raw = await asyncio.wait_for(connection.recv(), timeout=_TIMEOUT)
    return decode_control(raw if isinstance(raw, str) else raw.decode("utf-8"))


#:= docs/specs/robot-link/index.md#req-010-the-robot-is-a-client-only
#:% The robot MUST open the session outbound to the groundstation, and the
#:% groundstation MUST NOT require any inbound listener on the robot.
@pytest.mark.enable_socket  # a real server and a real client; see the module docstring
@pytest.mark.asyncio
async def test_a_client_opens_a_session_and_gets_results_back() -> None:
    """One outbound connection carries the offer, the frames and the results."""
    async with (
        _serving(StaticRegistry(EchoCapability(), TallyCapability())) as harness,
        connect(harness.url) as connection,
    ):
        await connection.send(offer_message(ECHO, TALLY))
        kind, payload = await _receive(connection)
        assert kind is MessageKind.AGREEMENT
        assert SessionAgreement.from_wire(payload).capabilities == (ECHO, TALLY)

        await connection.send(frame_message(0, stamp=STAMP))
        first = json.loads((await _receive(connection))[1])
        second = json.loads((await _receive(connection))[1])

    assert [first["capability"], second["capability"]] == ["echo", "tally"]
    assert first["sequence"] == 0
    assert first["captured_at"] == STAMP


#:= docs/specs/robot-link/index.md#req-011-one-session-carries-every-exchange
#:% All frames, results, and control messages for a running app MUST travel over a
#:% single session, established once and reused for the lifetime of that session.
@pytest.mark.enable_socket  # a real server and a real client; see the module docstring
@pytest.mark.asyncio
async def test_many_frames_travel_over_the_one_connection() -> None:
    """No second connection is opened for the traffic that follows."""
    async with _serving(StaticRegistry(EchoCapability()), queue_bound=8) as harness:
        async with connect(harness.url) as connection:
            await connection.send(offer_message(ECHO))
            await _receive(connection)
            for sequence in range(5):
                await connection.send(frame_message(sequence))
                answered = json.loads((await _receive(connection))[1])
                assert answered["sequence"] == sequence
        assert harness.sample("groundstation_sessions_active") == 0


#:= docs/specs/robot-link/index.md#req-019-sessions-are-authenticated
#:% The groundstation MUST reject a session whose client does not present a valid
#:% credential.
@pytest.mark.enable_socket  # a real server and a real client; see the module docstring
@pytest.mark.asyncio
async def test_an_unauthenticated_client_is_closed_without_negotiating() -> None:
    """The refusal is a real close code on a real connection."""
    async with _serving(StaticRegistry(EchoCapability())) as harness:
        connection = await connect(harness.url)
        await connection.send(offer_message(ECHO, credential="the-wrong-one"))
        kind, payload = await _receive(connection)
        assert kind is MessageKind.CLOSE
        assert SessionClose.from_wire(payload).reason.value == "unauthenticated"
        with pytest.raises(websockets.exceptions.ConnectionClosed) as raised:
            await asyncio.wait_for(connection.recv(), timeout=_TIMEOUT)
        await connection.wait_closed()
        assert raised.value.rcvd is not None
        assert raised.value.rcvd.code == CLOSE_POLICY_VIOLATION


#:= docs/specs/robot-link/index.md#req-015-overload-drops-frames-rather-than-queueing-them
#:% When frames arrive faster than they can be processed, the oldest unprocessed
#:% frame MUST be discarded in preference to growing the queue or blocking the
#:% producer.
@pytest.mark.enable_socket  # a real server and a real client; see the module docstring
@pytest.mark.asyncio
async def test_overload_drops_the_oldest_and_still_answers_the_newest() -> None:
    """The queue is filled for real: the pipeline is held while frames arrive."""
    blocking = BlockingCapability()
    async with (
        _serving(StaticRegistry(blocking), queue_bound=1) as harness,
        connect(harness.url) as connection,
    ):
        await connection.send(offer_message(ECHO))
        await _receive(connection)

        for sequence in range(6):
            await connection.send(frame_message(sequence))

        # Wait for the service to have taken delivery of all six before letting
        # the pipeline move, so the overload is real rather than a race that
        # sometimes happens. Bounded, so that a service which never takes
        # delivery fails this test rather than hanging the suite.
        deadline = asyncio.get_running_loop().time() + _TIMEOUT
        while harness.sample("groundstation_frames_received_total") < 6:
            if asyncio.get_running_loop().time() > deadline:
                message = "the service did not take delivery of six frames"
                raise AssertionError(message)
            await asyncio.sleep(0.005)

        # With a bound of one, at most two frames can have survived: the one the
        # pipeline had already taken and the one still queued. Whether it took
        # the first frame before the second arrived is a scheduling detail, so
        # the assertion is the guarantee rather than the timing.
        dropped = harness.sample("groundstation_frames_dropped_total")
        assert dropped >= 4

        blocking.release.set()
        answered = [
            json.loads((await _receive(connection))[1])["sequence"]
            for _ in range(6 - int(dropped))
        ]

    # The newest frame is the one that survived, which is the point of dropping
    # the oldest rather than the newest.
    assert answered[-1] == 5
    assert blocking.processed == answered


#:= docs/specs/robot-link/index.md#req-012-capabilities-are-negotiated-at-session-start
#:% Both sides MUST exchange the set of capabilities they support, each with a
#:% version, before any capability-specific message is sent.
@pytest.mark.enable_socket  # a real server and a real client; see the module docstring
@pytest.mark.asyncio
async def test_a_reconnection_negotiates_against_the_current_capability_set() -> None:
    """Negotiation is per session; a service that changed is the normal case."""
    registry = StaticRegistry(EchoCapability(), TallyCapability())
    async with _serving(registry) as harness:
        connection = await connect(harness.url)
        await connection.send(offer_message(ECHO, TALLY))
        _, payload = await _receive(connection)
        assert SessionAgreement.from_wire(payload).capabilities == (ECHO, TALLY)

        # The connection is torn down without a close handshake, which is what a
        # network stall or a restart looks like from the other end.
        connection.transport.abort()
        await connection.wait_closed()

        # The service loses a capability while nothing is connected.
        registry.capabilities = registry.capabilities[:1]

        async with connect(harness.url) as second:
            await second.send(offer_message(ECHO, TALLY))
            _, again = await _receive(second)
            assert SessionAgreement.from_wire(again).capabilities == (ECHO,)


@pytest.mark.enable_socket  # a real server and a real client; see the module docstring
@pytest.mark.asyncio
async def test_a_session_dropped_mid_frame_leaves_the_service_serving() -> None:
    """A client that vanishes costs its own session and nothing else."""
    async with _serving(StaticRegistry(EchoCapability())) as harness:
        first = await connect(harness.url)
        await first.send(offer_message(ECHO))
        await _receive(first)
        await first.send(frame_message(0))
        await first.close()
        await first.wait_closed()

        async with connect(harness.url) as second:
            await second.send(offer_message(ECHO))
            assert (await _receive(second))[0] is MessageKind.AGREEMENT
            await second.send(frame_message(0))
            assert json.loads((await _receive(second))[1])["capability"] == "echo"


@pytest.mark.enable_socket  # a real server and a real client; see the module docstring
@pytest.mark.asyncio
async def test_the_operator_endpoints_are_served_beside_the_session() -> None:
    """One process, one port: the session endpoint and the health surface."""
    async with _serving(StaticRegistry(EchoCapability())) as harness:
        async with connect(harness.url) as connection:
            await connection.send(offer_message(ECHO))
            await _receive(connection)
        reader, writer = await asyncio.open_connection("127.0.0.1", harness.port)
        writer.write(b"GET /livez HTTP/1.0\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), timeout=_TIMEOUT)
        writer.close()
        await writer.wait_closed()
    assert b"200 OK" in response
    assert b'"alive"' in response


@pytest.mark.enable_socket  # a real server and a real client; see the module docstring
@pytest.mark.asyncio
async def test_a_credential_never_reaches_the_configuration_endpoint() -> None:
    """The same redaction the boot log applies, applied at run time."""
    async with _serving(StaticRegistry(EchoCapability())) as harness:
        reader, writer = await asyncio.open_connection("127.0.0.1", harness.port)
        writer.write(b"GET /config HTTP/1.0\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), timeout=_TIMEOUT)
        writer.close()
        await writer.wait_closed()
    assert CREDENTIAL.encode() not in response
    assert b'"credential":"<set>"' in response


class _Viewer:
    """One MJPEG client, reading parts off a real response as they arrive.

    A part is read out of a buffer rather than out of whatever the transport
    happened to deliver, because a chunk boundary is not a part boundary: the
    server writes one part per frame and the network is free to split or join
    them anywhere.

    Attributes:
        response: The open streaming response.
    """

    def __init__(self, response: httpx.Response) -> None:
        """Start reading a response nothing has consumed yet.

        Args:
            response: The open streaming response.
        """
        self.response = response
        self._chunks = response.aiter_bytes()
        self._buffer = bytearray()

    async def _fill(self) -> None:
        """Take one more chunk off the wire.

        Raises:
            StopAsyncIteration: When the server has finished the response.
        """
        async with asyncio.timeout(_TIMEOUT):
            self._buffer += await anext(self._chunks)

    async def part(self) -> tuple[bytes, dict[str, str], bytes]:
        """Read one whole part.

        Returns:
            The boundary line, the part's headers, and its body — the body taken
            by the declared length rather than by searching for the next
            boundary, which is what a length is for.
        """
        while b"\r\n\r\n" not in self._buffer:
            await self._fill()
        head, _, rest = bytes(self._buffer).partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        headers = {
            name.decode("ascii").strip().lower(): value.decode("ascii").strip()
            for name, _, value in (line.partition(b":") for line in lines[1:])
        }
        length = int(headers["content-length"])
        self._buffer = bytearray(rest)
        # The two bytes past the body are the CRLF separating this part from the
        # next boundary.
        while len(self._buffer) < length + 2:
            await self._fill()
        body = bytes(self._buffer[:length])
        del self._buffer[: length + 2]
        return lines[0], headers, body

    async def ended(self) -> bool:
        """Wait for the server to finish the response.

        Returns:
            True once the stream is over, having consumed anything still on the
            way. A stream that never ends fails the test on `_fill`'s timeout
            rather than stalling the suite.
        """
        try:
            while True:
                await self._fill()
        except StopAsyncIteration:
            return True


@contextlib.asynccontextmanager
async def _viewing(port: int) -> AsyncIterator[_Viewer]:
    """Open one viewer on the feed and disconnect it on the way out.

    Args:
        port: Where the server is listening.

    Yields:
        The viewer.
    """
    async with (
        httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=_TIMEOUT) as web,
        web.stream("GET", STREAM_PATH) as response,
    ):
        yield _Viewer(response)


@contextlib.asynccontextmanager
async def _robot(harness: _Harness, frames: int = 1) -> AsyncIterator[ClientConnection]:
    """Open one authenticated session and drive frames through it.

    Each frame is awaited to its result before the next is sent, which is what
    makes "the feed has this frame" true at a point a test can name: the pipeline
    offers a payload to the feed while decoding it, and the result is delivered
    after that.

    Args:
        harness: The running server.
        frames: How many frames to send, each a different shade so the one the
            feed retained is identifiable.

    Yields:
        The still-open connection.
    """
    async with connect(harness.url) as connection:
        await connection.send(offer_message(ECHO))
        await _receive(connection)
        for sequence in range(frames):
            await connection.send(
                frame_message(sequence, payload=jpeg_bytes(fill=10 * sequence)),
            )
            await _receive(connection)
        yield connection


async def _stream_status(port: int) -> int:
    """Ask for the feed and report only what it answered.

    The body is deliberately not read: a stream that was granted would never
    finish, and what this asks is which of the four answers came back.

    Args:
        port: Where the server is listening.

    Returns:
        The status code.
    """
    async with (
        httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=_TIMEOUT) as web,
        web.stream("GET", STREAM_PATH) as response,
    ):
        return response.status_code


@pytest.mark.enable_socket  # a real server and a real client; see the module docstring
@pytest.mark.asyncio
async def test_the_feed_sends_the_frame_the_robot_sent_as_a_multipart_part() -> None:
    """The operator's client reads the robot's own bytes, framed and unaltered."""
    payload = jpeg_bytes(fill=0)
    async with (
        _serving(StaticRegistry(EchoCapability())) as harness,
        _robot(harness),
        _viewing(harness.port) as viewer,
    ):
        assert viewer.response.status_code == 200
        content_type = viewer.response.headers["content-type"]
        cache = viewer.response.headers["cache-control"]
        boundary, headers, body = await viewer.part()

    assert content_type == f"multipart/x-mixed-replace; boundary={BOUNDARY}"
    assert cache == "no-store"
    assert boundary == f"--{BOUNDARY}".encode("ascii")
    assert headers["content-type"] == "image/jpeg"
    assert headers["content-length"] == str(len(payload))
    assert body == payload


@pytest.mark.enable_socket  # a real server and a real client; see the module docstring
@pytest.mark.asyncio
async def test_a_viewer_arriving_late_gets_the_newest_frame_and_no_backlog() -> None:
    """Five frames arrived and one is retained, so the first part is the fifth."""
    async with (
        _serving(StaticRegistry(EchoCapability()), queue_bound=8) as harness,
        _robot(harness, frames=5),
        _viewing(harness.port) as viewer,
    ):
        _boundary, _headers, body = await viewer.part()

    assert body == jpeg_bytes(fill=40)


@pytest.mark.enable_socket  # a real server and a real client; see the module docstring
@pytest.mark.asyncio
async def test_the_viewer_bound_refuses_a_further_viewer_and_frees_on_disconnect() -> (
    None
):
    """Four at once; the fifth is told the service is busy, not that it is broken."""
    async with _serving(StaticRegistry(EchoCapability())) as harness, _robot(harness):
        async with contextlib.AsyncExitStack() as viewers:
            # Kept in a list, and that is load-bearing rather than tidy: a
            # viewer nothing refers to is collected, its response iterator is
            # finalised, and the connection closes — which would end the very
            # streams this test is counting.
            open_viewers = [
                await viewers.enter_async_context(_viewing(harness.port))
                for _ in range(MAX_VIEWERS)
            ]
            for viewer in open_viewers:
                await viewer.part()
            assert (await _stream_status(harness.port), harness.feed.viewers) == (
                429,
                MAX_VIEWERS,
            )

        # Every viewer disconnected on the way out of the stack. A slot released
        # only on a clean end would leave this at 429 for ever, so the poll is
        # the assertion and its bound is what makes a leak a failure.
        deadline = asyncio.get_running_loop().time() + _TIMEOUT
        while await _stream_status(harness.port) != 200:
            if asyncio.get_running_loop().time() > deadline:
                message = "a disconnected viewer never gave its slot back"
                raise AssertionError(message)
            await asyncio.sleep(0.005)


@pytest.mark.enable_socket  # a real server and a real client; see the module docstring
@pytest.mark.asyncio
async def test_the_feed_ends_when_the_robot_session_closes() -> None:
    """The viewer finishes rather than waiting on a robot that has gone."""
    async with (
        _serving(StaticRegistry(EchoCapability())) as harness,
        _robot(harness) as connection,
        _viewing(harness.port) as viewer,
    ):
        await viewer.part()
        await connection.close()
        await connection.wait_closed()
        assert await viewer.ended() is True


@pytest.mark.enable_socket  # a real server and a real client; see the module docstring
@pytest.mark.asyncio
async def test_a_second_robot_ends_the_feed_and_refuses_the_next_viewer() -> None:
    """Ambiguity is refused rather than resolved by connection order."""
    async with (
        _serving(StaticRegistry(EchoCapability())) as harness,
        _robot(harness),
        _viewing(harness.port) as viewer,
    ):
        await viewer.part()
        async with connect(harness.url) as second:
            await second.send(offer_message(ECHO))
            await _receive(second)
            assert await viewer.ended() is True
            assert await _stream_status(harness.port) == 409


@pytest.mark.enable_socket  # a real server and a real client; see the module docstring
@pytest.mark.asyncio
async def test_the_feed_refuses_a_viewer_when_no_robot_is_connected() -> None:
    """No stream is held open waiting for a robot that may never arrive."""
    async with _serving(StaticRegistry(EchoCapability())) as harness:
        assert await _stream_status(harness.port) == 503


@pytest.mark.enable_socket  # a real server and a real client; see the module docstring
@pytest.mark.asyncio
async def test_an_unauthenticated_client_never_makes_the_feed_ambiguous() -> None:
    """A wrong credential is refused before anything counts it as a session."""
    async with _serving(StaticRegistry(EchoCapability())) as harness, _robot(harness):
        refused = await connect(harness.url)
        await refused.send(offer_message(ECHO, credential="the-wrong-one"))
        await refused.wait_closed()
        assert await _stream_status(harness.port) == 200


@pytest.mark.enable_socket  # a real server and a real client; see the module docstring
@pytest.mark.asyncio
async def test_a_shutdown_signal_ends_an_attached_viewer_and_stops_the_server() -> None:
    """A stream is the one response that would otherwise never end by itself.

    Every part of this is the composed service's: the server class, the way it
    is configured, the signal a container sends, and uvicorn's own shutdown
    sequence with its graceful timeout left at the unbounded default. Driving
    the lifespan hook directly instead would prove nothing, because the whole
    defect is that uvicorn does not reach that hook until the viewers it is
    waiting for have gone.

    The session stays open and the frame stays retained across the signal, on
    purpose: the viewer has to end because the process is stopping rather than
    because the feed stopped being eligible, which is the only reading under
    which this is evidence. And the frame is published straight into the feed
    rather than through a robot, so the single connection the drain has to wait
    for is the viewer's.
    """
    async with _serving(StaticRegistry(EchoCapability())) as harness:
        with harness.feed.authenticated_session():
            harness.feed.publish(jpeg_bytes())
            async with _viewing(harness.port) as viewer:
                await viewer.part()
                assert harness.feed.viewers == 1

                os.kill(os.getpid(), signal.SIGTERM)

                assert await viewer.ended() is True
                # Shielded, so a server that has not stopped fails this test
                # rather than being cancelled into looking as if it had.
                await asyncio.wait_for(
                    asyncio.shield(harness.serving),
                    timeout=_TIMEOUT,
                )
            assert harness.feed.viewers == 0
