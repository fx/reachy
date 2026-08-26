"""The HTTP and WebSocket surface: sessions, health, metrics, configuration, video.

Six endpoints, and the distinctions between them are the point.

`/livez` answers whether the process is alive. `/readyz` answers whether it is
ready to be sent work, which is a different question with a different answer
during warm-up — an orchestrator that conflated them would send the first session
into a service whose first inference is slow. `/capabilities` reports every
capability including the ones that failed, so a degraded service says so rather
than looking merely smaller than expected.

`/config` returns the fully resolved configuration with every secret reported as
set rather than by value. It renders through `resolved_configuration`, which is
also what the boot log emits, because a redaction applied in two places is a
redaction that will be forgotten in one of them.

`/stream.mjpg` shows the frame the sole authenticated session most recently sent,
and `api/mjpeg.py` holds it — the framing, the three refusals and the viewer
bound are enough of a subject to be read on their own.

The registry arrives from the composition root and this module never learns what
is in it. Nothing here imports `reachy_groundstation.capabilities`, and
`just lint-capability-boundary` fails the build if that changes.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute

from reachy_groundstation.api.mjpeg import STREAM_PATH, stream_response
from reachy_groundstation.api.websocket import WebSocketTransport
from reachy_groundstation.config import resolved_configuration
from reachy_groundstation.feed import FeedRegistry
from reachy_groundstation.obs import (
    get_logger,
    render_metrics,
    set_capability_gauges,
)
from reachy_groundstation.ports import CapabilityState
from reachy_groundstation.session.runner import SessionRunner, new_session_id

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine

    from starlette.requests import Request
    from starlette.websockets import WebSocket

    from reachy_groundstation.config import Settings
    from reachy_groundstation.obs import Observability
    from reachy_groundstation.ports import CapabilityHealth, CapabilityRegistryPort

__all__ = ["SESSION_PATH", "STREAM_PATH", "create_app"]

# The path the robot link spec's topology diagram names.
SESSION_PATH = "/v1/session"

_logger = get_logger(__name__)


def _health_payload(health: CapabilityHealth) -> dict[str, Any]:
    """Render one capability for the health surface.

    Args:
        health: What the registry reports about it.

    Returns:
        The JSON-serialisable form.
    """
    return {
        "name": health.name,
        "version": health.version,
        "state": health.state.value,
        "detail": health.detail,
    }


#:= docs/specs/groundstation/index.md#req-030-the-effective-configuration-is-retrievable-at-run-time
#:% The service MUST expose its fully resolved configuration over its own interface
#:% while running, with every value marked secret replaced by a redacted
#:% placeholder.
def create_app(
    *,
    settings: Settings,
    registry: CapabilityRegistryPort,
    obs: Observability,
    feed: FeedRegistry | None = None,
    warm_up: Callable[[], Coroutine[Any, Any, None]] | None = None,
    shutdown: Callable[[], Coroutine[Any, Any, None]] | None = None,
) -> Starlette:
    """Build the ASGI application.

    Args:
        settings: The settings in effect.
        registry: What sessions negotiate against and route into.
        obs: Where timings, spans and log lines go.
        feed: What counts authenticated sessions and holds the one live frame
            `/stream.mjpg` serves. The composition root builds it and hands the
            same one to the sessions this application starts; an application
            composed by hand gets one of its own, so one test's frames cannot
            reach another's.
        warm_up: What to run in the background at startup. Running it as a task
            rather than awaiting it is what lets `/readyz` answer "not yet"
            while it is happening.
        shutdown: What to await when the server stops. This is where a
            capability gets to release whatever it holds; a service that only
            ever exits by being killed would leak it.

    Returns:
        The application, ready to be served.
    """
    live_feed = FeedRegistry() if feed is None else feed

    def _publish_capability_health() -> tuple[CapabilityHealth, ...]:
        """Read the registry's health and publish it as gauges.

        Returns:
            What the registry reports, so a caller that also needs to render it
            does not ask twice.
        """
        health = registry.health()
        set_capability_gauges(
            obs.metrics,
            {entry.name: entry.state is CapabilityState.READY for entry in health},
        )
        return health

    async def livez(request: Request) -> Response:
        """Report that the process is alive.

        Args:
            request: The incoming request, unused.

        Returns:
            Always 200. This says nothing about readiness.
        """
        del request
        return JSONResponse({"status": "alive"})

    #:= docs/specs/groundstation/index.md#req-026-readiness-is-distinct-from-liveness
    #:% The service MUST report itself ready only once every capability it will offer
    #:% has completed its warm-up.
    async def readyz(request: Request) -> Response:
        """Report whether the service is ready to be sent work.

        Args:
            request: The incoming request, unused.

        Returns:
            200 once every capability has finished warming up, 503 before that.
        """
        del request
        ready = registry.ready
        return JSONResponse(
            {
                "ready": ready,
                "capabilities": [
                    _health_payload(health) for health in registry.health()
                ],
            },
            status_code=200 if ready else 503,
        )

    #:= docs/specs/groundstation/index.md#req-025-a-failed-capability-does-not-take-down-the-service
    #:% When a capability fails to initialise, the service MUST continue serving the
    #:% capabilities that initialised successfully.
    async def capabilities(request: Request) -> Response:
        """Report every capability, healthy or not.

        Args:
            request: The incoming request, unused.

        Returns:
            The full list, so a degraded service is legible as degraded rather
            than as one that happens to offer less.
        """
        del request
        health = _publish_capability_health()
        return JSONResponse(
            {
                "ready": registry.ready,
                "offered": [named.name for named in registry.supported()],
                "capabilities": [_health_payload(entry) for entry in health],
            },
        )

    #:= docs/specs/groundstation/index.md#req-029-per-stage-timings-are-measured-and-exposed
    #:% The service MUST record the duration of each pipeline stage separately and
    #:% expose those durations as metrics.
    async def metrics(request: Request) -> Response:
        """Render the metrics registry.

        Args:
            request: The incoming request; its `Accept` header decides whether
                the answer carries exemplars.

        Returns:
            The exposition.
        """
        # The gauges are refreshed here rather than only where the health
        # endpoint happens to have been polled: a scraper reads `/metrics` and
        # nothing else, and a series that appears only after somebody visits a
        # different endpoint is a series nobody can alert on.
        _publish_capability_health()
        body, content_type = render_metrics(
            obs.metrics,
            request.headers.get("accept"),
        )
        return Response(body, media_type=content_type)

    async def configuration(request: Request) -> Response:
        """Return the fully resolved configuration.

        Args:
            request: The incoming request, unused.

        Returns:
            Every setting in effect, with each secret shown as set or unset and
            never by value — the endpoint is reachable by anything that can
            reach the service.
        """
        del request
        return JSONResponse(resolved_configuration(settings))

    async def stream(request: Request) -> Response:
        """Serve the sole authenticated session's newest frame, or say why not.

        Args:
            request: The incoming request. Only its method is read: the
                endpoint takes no parameter, because there is nothing to select
                among and a query naming a session would be the selection the
                spec refuses to make.

        Returns:
            The multipart stream, or one of the three refusals `api/mjpeg.py`
            describes.
        """
        return stream_response(live_feed, request.method)

    #:= docs/specs/robot-link/index.md#req-010-the-robot-is-a-client-only
    #:% The robot MUST open the session outbound to the groundstation, and the
    #:% groundstation MUST NOT require any inbound listener on the robot.
    async def session(websocket: WebSocket) -> None:
        """Carry one session for its whole life.

        Args:
            websocket: The incoming connection.
        """
        await websocket.accept()
        session_id = new_session_id()
        runner = SessionRunner(
            transport=WebSocketTransport(websocket),
            registry=registry,
            settings=settings,
            obs=obs,
            session_id=session_id,
            feed=live_feed,
        )
        outcome = await runner.run()
        _logger.info(
            "session.finished",
            session=outcome.session_id,
            reason=outcome.reason.value,
            frames=outcome.frames_received,
            dropped=outcome.frames_dropped,
        )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        """Start warm-up in the background, and close what was built on the way out.

        Args:
            app: The application, unused.

        Yields:
            Nothing; the application runs inside this.
        """
        del app
        task = (
            None if warm_up is None else asyncio.create_task(warm_up(), name="warm-up")
        )
        try:
            yield
        finally:
            # Before the capabilities, and synchronously: closing the feed
            # discards the retained frame and finishes every viewer, so nothing
            # is left waiting on a value the process is about to stop producing.
            live_feed.close()
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            if shutdown is not None:
                await shutdown()

    return Starlette(
        routes=[
            Route("/livez", livez, methods=["GET"]),
            Route("/readyz", readyz, methods=["GET"]),
            Route("/capabilities", capabilities, methods=["GET"]),
            Route("/metrics", metrics, methods=["GET"]),
            Route("/config", configuration, methods=["GET"]),
            Route(STREAM_PATH, stream, methods=["GET"]),
            WebSocketRoute(SESSION_PATH, session),
        ],
        lifespan=lifespan,
    )
