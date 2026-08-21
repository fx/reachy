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

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING

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
    make_settings,
    offer_message,
)
from websockets.asyncio.client import connect

from reachy_contracts import SessionAgreement, SessionClose
from reachy_groundstation.api.app import SESSION_PATH, create_app
from reachy_groundstation.session.framing import MessageKind, decode_control
from reachy_groundstation.session.transport import CLOSE_POLICY_VIOLATION

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.applications import Starlette
    from websockets.asyncio.client import ClientConnection

    from reachy_groundstation.obs import Observability
    from reachy_groundstation.ports import CapabilityRegistryPort

STAMP = "17352.884"

# How long a client waits for a message that should already be on its way. Long
# enough that a loaded runner does not flake, short enough that a genuine hang
# fails the suite rather than stalling it.
_TIMEOUT = 10.0


class _Harness:
    """A running server and the pieces a test wants to look at.

    Attributes:
        port: The ephemeral port the server bound.
        url: Where to open a session.
        obs: The reporting bundle the service is writing to.
    """

    def __init__(self, port: int, obs: Observability) -> None:
        """Record where the server ended up.

        Args:
            port: The ephemeral port it bound.
            obs: The reporting bundle it writes to.
        """
        self.port = port
        self.url = f"ws://127.0.0.1:{port}{SESSION_PATH}"
        self.obs = obs

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

    Args:
        registry: What the application is composed around.
        overrides: Settings to change from their defaults.

    Yields:
        Where the server is listening and what it is reporting to.
    """
    obs, _exporter = build_observability()
    app: Starlette = create_app(
        settings=make_settings(**overrides),
        registry=registry,
        obs=obs,
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
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(), name="uvicorn")
    try:
        # Bounded rather than open-ended: a server that failed to start would
        # otherwise hang the suite instead of failing it.
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
        yield _Harness(port, obs)
    finally:
        server.should_exit = True
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
