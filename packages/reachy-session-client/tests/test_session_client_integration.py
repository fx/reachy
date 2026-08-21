"""The client against a real groundstation, over a real WebSocket.

Every test here opens a socket, and every test here says so with
`@pytest.mark.enable_socket`. The reason is the same in all of them: what is
under test is the protocol, and a mocked transport would test the mock. A real
uvicorn server runs in-process on the loopback interface with an ephemeral port,
the real `reachy_groundstation` application answers, and the client under test is
the same one the robot application will import — which is what reachyctl REQ-057
asks for.

Reconnection is induced rather than configured. The connection the client is
holding is dropped for real, against a server that never went anywhere, and the
client is watched recovering onto a new one.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Final

import pytest
from session_client_server import (
    CREDENTIAL,
    FACE,
    RecordingConnections,
    StaticRegistry,
    WatchfulFace,
    jpeg_bytes,
    serving,
)

from reachy_contracts import Capability
from reachy_session_client import (
    Backoff,
    Credential,
    SessionClient,
    SessionRefusedError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

    from reachy_session_client import FrameResult

# Real delays, because this test drives a real server — but small ones. A
# non-zero sleep in an integration test is what makes the wait deterministic;
# see REVIEW.md on the distinction from a unit test that sleeps.
FAST: Final = Backoff(initial_seconds=0.01, multiplier=2.0, maximum_seconds=0.05)

TIMEOUT: Final = 10.0

# A capability the groundstation in these tests does not offer.
UNOFFERED: Final = Capability(name="gesture", version=1)


def client_for(
    url: str,
    connections: RecordingConnections,
    *,
    credential: str = CREDENTIAL,
    capabilities: tuple[Capability, ...] = (FACE,),
) -> SessionClient:
    """Build a client pointed at a running groundstation.

    Args:
        url: Where the server is listening.
        connections: The factory that opens and remembers real transports.
        credential: What to present.
        capabilities: What to offer.

    Returns:
        The client, not yet connected.
    """
    return SessionClient(
        url=url,
        credential=Credential(credential),
        capabilities=capabilities,
        open_transport=connections,
        backoff=FAST,
    )


async def next_result(client: SessionClient) -> FrameResult:
    """Submit a frame and wait for the answer to it.

    Args:
        client: The connected client.

    Returns:
        The first result that arrives.
    """
    results = client.results()
    try:
        await client.submit_frame(jpeg_bytes())
        return await asyncio.wait_for(anext(results), timeout=TIMEOUT)
    finally:
        await results.aclose()


@contextlib.asynccontextmanager
async def submitting(
    client: SessionClient,
    interval: float = 0.02,
) -> AsyncIterator[None]:
    """Keep producing frames for the duration of the block, as a camera would.

    A recovering client needs frames to answer, and the frames produced while it
    is still reconnecting are dropped rather than queued — which is the point of
    producing them from a separate task here rather than sending one and hoping
    it lands after the recovery.

    Args:
        client: The client to submit to.
        interval: How long to wait between frames.

    Yields:
        Nothing; the block runs while frames are being produced.
    """

    async def produce() -> None:
        """Submit frames until cancelled."""
        while True:
            await client.submit_frame(jpeg_bytes())
            await asyncio.sleep(interval)

    task = asyncio.create_task(produce(), name="frames")
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


#:= docs/specs/reachyctl/index.md#req-057-the-probe-exercises-the-real-session-protocol
#:% The probe command MUST establish a session using the same protocol
#:% implementation the robot application uses.
@pytest.mark.enable_socket  # a real server and the real client; see the module docstring
@pytest.mark.asyncio
async def test_a_session_carries_a_frame_up_and_a_result_back() -> None:
    """One outbound connection, the real framing, and an answer keyed to the frame."""
    capability = WatchfulFace()
    connections = RecordingConnections()
    async with serving(StaticRegistry(capability)) as server:
        client = client_for(server.url, connections)
        async with client:
            agreed = await client.connect()
            result = await next_result(client)

    assert [named.name for named in agreed.capabilities] == ["face"]
    assert result.sequence == 0
    assert result.capability == "face"
    assert result.detections == 1
    assert result.round_trip_seconds is not None
    assert result.round_trip_seconds >= 0
    assert capability.seen == [0]
    assert connections.count == 1


#:= docs/specs/robot-link/index.md#req-016-results-return-the-capture-timestamp-unaltered
#:% Every result MUST carry the capture timestamp of the frame it derives from,
#:% byte-for-byte as the capturing side supplied it, so that the capturing side can
#:% compute the result's age against the same clock that produced it.
@pytest.mark.enable_socket  # a real server and the real client; see the module docstring
@pytest.mark.asyncio
async def test_the_capture_token_comes_back_byte_for_byte() -> None:
    """The groundstation copied it through and never looked at it."""
    connections = RecordingConnections()
    async with serving(StaticRegistry(WatchfulFace())) as server:
        client = client_for(server.url, connections)
        async with client:
            results = client.results()
            try:
                header = await client.submit_frame(jpeg_bytes())
                result = await asyncio.wait_for(anext(results), timeout=TIMEOUT)
            finally:
                await results.aclose()

    assert header is not None
    assert result.captured_at.root == header.captured_at.root


#:= docs/specs/robot-link/index.md#req-013-an-empty-result-is-a-valid-result
#:% A result message carrying no detections MUST be treated as a successful result
#:% for that frame.
@pytest.mark.enable_socket  # a real server and the real client; see the module docstring
@pytest.mark.asyncio
async def test_a_frame_with_nothing_in_it_comes_back_as_a_success() -> None:
    """No detections over the real wire, and no error counter moves."""
    connections = RecordingConnections()
    async with serving(StaticRegistry(WatchfulFace(empty=True))) as server:
        client = client_for(server.url, connections)
        async with client:
            result = await next_result(client)
            stats = client.stats

    assert result.detections == 0
    assert stats.results_applied == 1
    assert stats.errors_received == 0


#:= docs/specs/robot-link/index.md#req-012-capabilities-are-negotiated-at-session-start
#:% Both sides MUST exchange the set of capabilities they support, each with a
#:% version, before any capability-specific message is sent.
@pytest.mark.enable_socket  # a real server and the real client; see the module docstring
@pytest.mark.asyncio
async def test_a_capability_the_service_does_not_offer_is_absent_and_harmless() -> None:
    """The session continues, and nothing sends results the client cannot read."""
    connections = RecordingConnections()
    async with serving(StaticRegistry(WatchfulFace())) as server:
        client = client_for(
            server.url,
            connections,
            capabilities=(FACE, UNOFFERED),
        )
        async with client:
            result = await next_result(client)

            assert client.agreed("gesture") is None
            assert client.agreed("face") == FACE

    assert result.capability == "face"


#:= docs/specs/reachyctl/index.md#req-059-secrets-are-never-written-to-output
#:% The tool MUST NOT write credentials to its output, its logs, or its error
#:% messages.
@pytest.mark.enable_socket  # a real server and the real client; see the module docstring
@pytest.mark.asyncio
async def test_a_wrong_credential_is_refused_without_naming_it() -> None:
    """A real close code on a real connection, and nothing quoted back."""
    wrong = "the-wrong-one"
    connections = RecordingConnections()
    async with serving(StaticRegistry(WatchfulFace())) as server:
        client = client_for(server.url, connections, credential=wrong)

        with pytest.raises(SessionRefusedError) as raised:
            await client.connect()

    assert raised.value.reason == "unauthenticated"
    assert wrong not in str(raised.value)
    assert not client.connected


#:= docs/specs/robot-link/index.md#req-018-reconnection-is-automatic-and-rate-limited
#:% A client MUST re-establish a dropped session automatically, and MUST increase
#:% the delay between successive failed attempts up to a bound.
@pytest.mark.enable_socket  # a real server and the real client; see the module docstring
@pytest.mark.asyncio
async def test_a_connection_dropped_for_real_is_re_established_for_real() -> None:
    """The session the client held is killed; it opens a new one and carries on."""
    registry = StaticRegistry(WatchfulFace())
    connections = RecordingConnections()
    async with serving(registry) as server:
        client = client_for(server.url, connections)
        async with client:
            before = await next_result(client)

            # The connection is ended under the client, which is what a network
            # stall or a restarted service looks like from this end. The server
            # itself never goes anywhere, so what is being watched is the
            # client's recovery and not the server's.
            await connections.drop_latest()

            results = client.results()
            try:
                async with submitting(client):
                    after = await asyncio.wait_for(anext(results), timeout=TIMEOUT)
            finally:
                await results.aclose()
            stats = client.stats

    assert before.sequence == 0
    # Numbering restarted with the new session, so the first frame answered on
    # it is frame zero however many were produced and dropped on the way.
    assert after.sequence == 0
    assert stats.reconnections == 1
    assert stats.frames_dropped >= 1
    assert connections.count == 2


async def _awaiting_a_result(
    client: SessionClient,
    results: AsyncGenerator[FrameResult, None],
) -> asyncio.Task[FrameResult]:
    """Start waiting for a result, and return once the session is back up.

    Reconnection happens inside the result iteration, so the iteration has to
    be driven for it to happen at all — and it is driven by a task rather than
    polled with a short `wait_for`, because cancelling `anext` on an
    asynchronous generator closes the generator. A poll built that way ends the
    iteration on its first timeout and every later call raises
    `StopAsyncIteration`.

    Args:
        client: The client to wait on.
        results: Its result iteration, which the reconnection lives inside.

    Returns:
        The still-pending wait, for the caller to make its negative assertion
        against and then cancel.

    Raises:
        AssertionError: If it never reconnects, so that the test fails rather
            than the suite hanging.
    """
    pending = asyncio.create_task(anext(results), name="one-result")
    deadline = asyncio.get_running_loop().time() + TIMEOUT
    while client.stats.reconnections < 1:
        if asyncio.get_running_loop().time() > deadline:
            pending.cancel()
            message = "the client did not re-establish its session"
            raise AssertionError(message)
        await asyncio.sleep(0.01)
    return pending


#:= docs/specs/robot-link/index.md#req-012-capabilities-are-negotiated-at-session-start
#:% Both sides MUST exchange the set of capabilities they support, each with a
#:% version, before any capability-specific message is sent.
@pytest.mark.enable_socket  # a real server and the real client; see the module docstring
@pytest.mark.asyncio
async def test_a_reconnection_negotiates_against_what_is_offered_now() -> None:
    """A service whose capability set changed while nothing was connected."""
    registry = StaticRegistry(WatchfulFace())
    connections = RecordingConnections()
    async with serving(registry) as server:
        client = client_for(server.url, connections)
        async with client:
            await next_result(client)
            assert client.agreed("face") is not None

            registry.capabilities = []
            await connections.drop_latest()

            results = client.results()
            try:
                async with submitting(client):
                    # Two different waits, deliberately given two different
                    # bounds. Reconnecting is something that must happen, so it
                    # gets the generous one and a loaded runner cannot fail this
                    # test for a timing reason. Not receiving a result is
                    # something that must NOT happen, so it gets a short one —
                    # a negative assertion is only as strong as it is quick.
                    pending = await _awaiting_a_result(client, results)
                    # `shield`, so the timeout leaves the wait alive: cancelling
                    # it would close the iteration, and the iteration is what
                    # the assertions below read the agreement out of.
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(asyncio.shield(pending), timeout=0.5)
                    pending.cancel()
                    with contextlib.suppress(
                        asyncio.CancelledError,
                        StopAsyncIteration,
                    ):
                        await pending
            finally:
                await results.aclose()

            assert client.agreement is not None
            assert client.agreement.capabilities == ()
            assert client.stats.reconnections == 1
