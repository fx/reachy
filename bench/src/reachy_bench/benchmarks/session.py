"""`session`: what the robot link costs — establishing one, and using one.

Three numbers, and the split is the one the robot-link design turns on. A cold
connection to the predecessor cost 378 ms at p50, which is why the link is one
long-lived session rather than a request per frame; a result came back in 54 ms
per request with connection reuse, which is what the session is for. So
establishing and using are measured apart, and re-establishing after a drop is
measured too, because that is what a robot actually pays when a groundstation
restarts.

**No robot and no hardware.** The service runs in this process on the loopback
interface and the session is driven by `reachy_session_client` — the one client
implementation, the same one the robot application uses, which is what makes
this a measurement of the protocol rather than of a benchmark's idea of it
(reachyctl REQ-057 is the rule; this module is a consumer of it, not a second
one).

**The round trip is the client's own figure.** `FrameResult.round_trip_seconds`
is a single-clock subtraction between the monotonic stamp the client minted for
the frame and the moment the result carrying that stamp came back. Nothing here
re-derives it, so what this reports is what a robot would compute about itself.

**The network underneath is loopback, and the result says so.** Every recorded
figure in the spec crossed a 2.4 GHz WLAN at 100-170 ms idle round-trip with
700 ms spikes. That is not a property of this stack and it is not something a
runner can reproduce, so the benchmarks spec's decision is to record the network
rather than control it — and a loopback figure compared against a WLAN one is a
comparison of two networks.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final

from reachy_bench.registry import BenchmarkSpec, Options
from reachy_bench.result import BenchmarkResult, Measurement, Status
from reachy_bench.stats import Distribution
from reachy_bench.timing import measure_async

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from reachy_bench.result import Detail

__all__ = ["SESSION", "build"]

NAME: Final = "session"

# How long to wait for the service to start, and for a frame to be answered,
# before concluding something is wrong. Long enough that a loaded machine does
# not fail the run, short enough that a wedged service fails it rather than
# hanging it.
_TIMEOUT_SECONDS: Final = 120.0

# The credential the in-process service is configured with and the client
# presents. A placeholder that exists for the length of one benchmark run and is
# never anybody's — see the root AGENTS.md on what may enter a tracked file.
_CREDENTIAL: Final = "benchmark-placeholder"

_LOOPBACK_NOTE: Final = (
    "measured over the loopback interface with the service in the same "
    "process, so the transport is real and the network is not: every figure "
    "the spec records crossed a 2.4 GHz WLAN at 100-170 ms idle round-trip "
    "with 700 ms spikes. The two are not comparable as network measurements, "
    "which is why the benchmarks spec records the network rather than "
    "normalising it away"
)

# How the session is measured. An argument so the result assembly around it is
# exercised without opening a socket.
type SessionMeasure = Callable[
    [Options],
    tuple[Mapping[str, Distribution], Mapping[str, Detail]],
]


def _measure_session(  # pragma: no cover
    options: Options,
) -> tuple[Mapping[str, Distribution], Mapping[str, Detail]]:
    """Run the service in this process and drive a session against it.

    Not unit-tested, and excluded from coverage rather than mocked, along with
    the three functions below it: they start a real server, open real sockets
    and run real inference, so a unit test of them would be a unit test of
    uvicorn. What exercises them is `just bench`, which the benchmark workflow
    runs on every pull request.

    Args:
        options: What the run was configured with.

    Returns:
        The distributions by measurement suffix, and the configuration they
        were taken under.
    """
    return asyncio.run(_drive(options))


async def _serve(  # pragma: no cover - see `_measure_session`
    options: Options,
) -> tuple[object, asyncio.Task[None], int]:
    """Start the real application on an ephemeral loopback port.

    Args:
        options: What the run was configured with.

    Returns:
        The server, the task serving it, and the port it bound.

    Raises:
        RuntimeError: If the server did not start, or stopped while starting.
    """
    import uvicorn

    from reachy_groundstation.config import load_settings
    from reachy_groundstation.obs import build_observability
    from reachy_groundstation.service import build_application

    # The port is uvicorn's rather than the settings model's: the model bounds
    # it to a real port number and this asks the kernel for an ephemeral one, so
    # that two benchmark runs on one machine do not collide.
    settings = load_settings(
        {
            "REACHY_GROUNDSTATION_CREDENTIAL": _CREDENTIAL,
            "REACHY_GROUNDSTATION_MODELS_DIR": str(options.models_dir),
            "REACHY_GROUNDSTATION_HOST": "127.0.0.1",
        },
    )
    app, _registry = build_application(settings, build_observability(settings))
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_config=None,
        ws="websockets-sansio",
        ws_max_size=settings.max_message_bytes,
        lifespan="on",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(), name="benchmark-groundstation")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _TIMEOUT_SECONDS
    while not server.started:
        if task.done():
            await task
            message = "the groundstation stopped before it started"
            raise RuntimeError(message)
        if loop.time() > deadline:
            message = "the groundstation did not start within the benchmark's bound"
            raise RuntimeError(message)
        await asyncio.sleep(0.01)
    return server, task, int(server.servers[0].sockets[0].getsockname()[1])


async def _drive(  # pragma: no cover - see `_measure_session`
    options: Options,
) -> tuple[Mapping[str, Distribution], Mapping[str, Detail]]:
    """Establish sessions, exchange frames, and time both.

    Args:
        options: What the run was configured with.

    Returns:
        The distributions and the configuration.

    Raises:
        RuntimeError: If a frame was submitted and never answered, which is a
            broken measurement rather than a slow one.
    """
    from reachy_contracts import FACE_CAPABILITY, Capability
    from reachy_groundstation.api.app import SESSION_PATH
    from reachy_session_client import Credential, SessionClient

    payload = (options.fixtures / options.frame).read_bytes()
    server, task, port = await _serve(options)
    url = f"ws://127.0.0.1:{port}{SESSION_PATH}"
    capabilities = (Capability(name=FACE_CAPABILITY, version=1),)

    def _client() -> SessionClient:
        """Build a client for one session.

        Returns:
            A client that has not connected.
        """
        return SessionClient(
            url=url,
            credential=Credential(_CREDENTIAL),
            capabilities=capabilities,
        )

    # Fewer passes than a purely local timing takes: each one is a TCP
    # connection, a WebSocket handshake and a negotiation, and the figure is
    # stable enough that fifty of them would measure the same thing more slowly.
    establishments = max(1, min(options.iterations, 20))
    try:
        # The service warms its capabilities in the background as it starts, so
        # the first session is refused until it is ready. Waiting here rather
        # than inside the timed passes is the difference between measuring a
        # connection and measuring a warm-up.
        await _await_ready(_client)

        async def _establish() -> None:
            """Open a session and say goodbye to it."""
            client = _client()
            await client.connect()
            await client.aclose()

        connect = await measure_async(
            _establish,
            iterations=establishments,
            warmup=1,
        )

        held = _client()
        await held.connect()
        results = held.results()
        try:

            async def _exchange() -> None:
                """Send one frame and wait for its result.

                Raises:
                    RuntimeError: If the frame was not accepted, which means
                        the session dropped underneath the measurement.
                """
                if await held.submit_frame(payload) is None:
                    message = "the session dropped while a frame was in flight"
                    raise RuntimeError(message)
                await asyncio.wait_for(anext(results), timeout=_TIMEOUT_SECONDS)

            # A plain loop, not `measure_async`. The round trips below are the
            # client's own figures — a single-clock subtraction between the
            # stamp it minted for a frame and the moment the result carrying
            # that stamp came back — so nothing here times anything, and timing
            # the warm-up only to discard the distribution would read as a
            # measurement being thrown away. One pass to get the exchange
            # flowing, plus the configured warm-up.
            for _ in range(options.warmup + 1):
                await _exchange()
            round_trips: list[float] = []
            for _ in range(options.iterations):
                if await held.submit_frame(payload) is None:
                    message = "the session dropped while a frame was in flight"
                    raise RuntimeError(message)
                result = await asyncio.wait_for(
                    anext(results),
                    timeout=_TIMEOUT_SECONDS,
                )
                if result.round_trip_seconds is not None:
                    round_trips.append(result.round_trip_seconds)
            if not round_trips:
                message = "no frame came back with a measurable round trip"
                raise RuntimeError(message)
        finally:
            await results.aclose()
            await held.aclose()

        # Re-establishment on a client that has already held a session, which
        # is what a robot pays when a groundstation restarts. It is the
        # negotiation and the handshake and not the backoff delay: the delay is
        # configuration, and timing it would report the setting rather than the
        # cost.
        reused = _client()
        await reused.connect()

        async def _re_establish() -> None:
            """Drop the session this client holds and open another."""
            await reused.aclose()
            await reused.connect()

        reconnect = await measure_async(
            _re_establish,
            iterations=establishments,
            warmup=1,
        )
        await reused.aclose()
    finally:
        server.should_exit = True  # type: ignore[attr-defined]  # `_serve` returns uvicorn's Server as `object` so this module's signature does not depend on uvicorn's types; the attribute is the documented way to stop it
        await asyncio.wait_for(task, timeout=_TIMEOUT_SECONDS)

    distributions: Mapping[str, Distribution] = {
        "connect": connect,
        "round_trip": Distribution.of_seconds(round_trips),
        "reconnect": reconnect,
    }
    configuration: Mapping[str, Detail] = {
        "transport": "websocket over the loopback interface",
        "capability": FACE_CAPABILITY,
        "frame": options.frame,
        "frame_bytes": len(payload),
        "establishments": establishments,
        "iterations": options.iterations,
        "warmup": options.warmup,
    }
    return distributions, configuration


async def _await_ready(  # pragma: no cover - see `_measure_session`
    build_client: Callable[[], object],
) -> None:
    """Wait until the service will accept a session.

    Args:
        build_client: How to build a client to try with.

    Raises:
        RuntimeError: If it never became ready within the benchmark's bound.
    """
    from reachy_session_client import SessionClient, SessionClientError

    loop = asyncio.get_running_loop()
    deadline = loop.time() + _TIMEOUT_SECONDS
    while loop.time() < deadline:
        client = build_client()
        assert isinstance(client, SessionClient)  # noqa: S101  # narrowing the seam's `object` for the type checker; the factory is this module's own and returns nothing else
        try:
            await client.connect()
        except SessionClientError:
            await asyncio.sleep(0.25)
            continue
        finally:
            await client.aclose()
        return
    message = "the groundstation never became ready within the benchmark's bound"
    raise RuntimeError(message)


def build(
    options: Options,
    *,
    measure_session: SessionMeasure = _measure_session,
) -> BenchmarkResult:
    """Measure establishing a session, using one, and re-establishing one.

    Args:
        options: What the run was configured with.
        measure_session: How to take the measurements.

    Returns:
        The benchmark's result.
    """
    distributions, configuration = measure_session(options)
    measurements = tuple(
        Measurement.timing(f"{NAME}.{suffix}", distribution)
        for suffix, distribution in distributions.items()
    )
    notes = [_LOOPBACK_NOTE]
    if options.network:
        notes.append(f"network as reported by the operator: {options.network}")
    return BenchmarkResult(
        benchmark=NAME,
        status=Status.MEASURED,
        configuration=dict(configuration),
        measurements=measurements,
        notes=tuple(notes),
    )


SESSION: Final = BenchmarkSpec(
    name=NAME,
    summary="Establishing a session, a frame's round trip, and reconnecting.",
    requires_hardware=False,
    run=build,
)
