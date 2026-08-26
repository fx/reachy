"""The composition root: the one module that knows about all the others.

This is where the capabilities, the registry, the observability bundle and the
application meet. It is deliberately the only module outside
`reachy_groundstation.capabilities` that imports it — the api, session and
pipeline packages are forbidden from doing so, and `just lint-capability-boundary`
fails the build if one of them tries.

`main` is the whole of startup, in the order it happens: read the environment or
refuse to start, install logging and tracing, say out loud what the resolved
configuration is, build the capabilities, and serve. Nothing reads the
environment behind that first step.
"""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

import uvicorn

from reachy_groundstation.api.app import create_app
from reachy_groundstation.capabilities.registry import CapabilityRegistry
from reachy_groundstation.config import (
    ConfigurationError,
    load_settings,
)
from reachy_groundstation.feed import FeedRegistry
from reachy_groundstation.obs import (
    build_observability,
    configure_logging,
    get_logger,
    log_resolved_configuration,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import FrameType

    from starlette.applications import Starlette

    from reachy_groundstation.capabilities.registry import CapabilityFactory
    from reachy_groundstation.config import Settings
    from reachy_groundstation.obs import Observability

__all__ = ["FeedClosingServer", "build_application", "build_server", "main"]

_logger = get_logger(__name__)


class FeedClosingServer(uvicorn.Server):
    """A server that finishes the feed's viewers before it drains connections.

    Shutdown has a fixed order and the feed sits on the wrong side of it. When a
    signal arrives uvicorn stops listening, asks every open connection to finish,
    waits for the ones that have not, and only then sends the lifespan shutdown
    event on which the application closes its feed. A viewer parked in
    `next_frame` is one of the connections being waited for, and the close it is
    waiting to be told about is the one thing that would end it — so with the
    default unlimited graceful timeout that wait is unbounded, and the process
    stops whenever the last viewer happens to go away rather than when it was
    asked to.

    `handle_exit` runs before all of that: uvicorn's signal handler calls it, and
    the drain begins only once the serving loop notices `should_exit`. Closing
    the feed there means every viewer's response ends of its own accord during
    the drain and gives its slot back exactly as a disconnect does, so the drain
    has nothing left to wait for.

    A viewer is only ever parked while exactly one session is authenticated, so
    the drain would also free it *eventually* — by closing that session's socket
    and letting the count fall to zero. That is the dependency being removed
    rather than the reason there is nothing to fix: it makes stopping wait on how
    fast one session's teardown propagates through the runner, and a session that
    is slow to unwind, or wedged, is a shutdown that does not happen at all.

    A bounded `timeout_graceful_shutdown` is the other way to stop the wait and a
    worse one: it bounds every connection rather than the ones holding a stream
    open, it turns an orderly end into a cancelled task, and it spends the whole
    timeout on every shutdown that happens to have a viewer attached instead of
    finishing at once.

    It is a class rather than a signal handler installed around `uvicorn.run`
    because uvicorn replaces the handlers for its own signals while it serves.
    A handler registered outside would be the one it displaced.
    """

    def __init__(self, config: uvicorn.Config, feed: FeedRegistry) -> None:
        """Serve one application and remember the feed its viewers read from.

        Args:
            config: What to serve, and how.
            feed: The live frame `/stream.mjpg` is served from. It has to be the
                one the application was composed with, or the viewers this
                closes are not the viewers that are waiting.
        """
        super().__init__(config)
        self._feed = feed

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        """Finish every viewer, then let uvicorn start stopping.

        The close is handed to the event loop rather than performed here, and
        that is the difference between removing the wait and moving it. This
        runs in a signal handler, which Python may execute between any two
        bytecodes of the main thread — including the handful in `next_frame`
        between its closed check and its read of the event it is about to wait
        on. Closing in that window would swap the event out from under a viewer
        that had already decided to wait, and park it on one nothing will ever
        set, which is the deadlock this class exists to prevent. Scheduling it
        means the close runs between whole steps of the loop, where
        `next_frame`'s own reasoning about not having awaited holds.

        Nothing is lost by the deferral: uvicorn only notices `should_exit` on
        the next pass of its serving loop, so the callback runs first, and the
        drain begins after both.

        Args:
            sig: The signal that arrived.
            frame: The interrupted stack frame, which is uvicorn's to interpret.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Before serving or after it: no loop is running, so no viewer is
            # parked on the feed and there is nothing for the deferral to
            # protect — and nothing that would run a scheduled callback either.
            self._feed.close()
        else:
            # `call_soon_threadsafe` and not `call_soon`: the latter is not safe
            # to call from a signal handler, and this is one even though it is
            # already on the loop's thread.
            loop.call_soon_threadsafe(self._feed.close)
        # Closing is idempotent, which is what makes a second signal harmless:
        # it reaches a feed that is already closed, or one with a close already
        # scheduled, and asks uvicorn to force the exit. Neither step is
        # troubled by the other having happened.
        super().handle_exit(sig, frame)


def build_application(
    settings: Settings,
    obs: Observability,
    factories: Sequence[CapabilityFactory] | None = None,
    feed: FeedRegistry | None = None,
) -> tuple[Starlette, CapabilityRegistry]:
    """Wire the capabilities into an application.

    Args:
        settings: The settings in effect.
        obs: Where timings, spans and log lines go.
        factories: What to build. Defaults to everything registered.
        feed: The live frame to serve `/stream.mjpg` from. One for the whole
            process, which is what makes "exactly one authenticated session" a
            question the operator feed can answer — a feed per session or per
            request would count to one every time and show whichever robot
            happened to be asking. It is a parameter so that `main` can hand the
            same one to the server, which has to be able to finish its viewers
            before uvicorn waits for them; a caller with no such need leaves it
            off and gets one of its own.

    Returns:
        The application, and the registry it was built around. Closing the
        registry is wired into the application's own lifespan, so the caller
        keeps it only in order to inspect it.
    """
    registry = CapabilityRegistry(settings, factories)
    app = create_app(
        settings=settings,
        registry=registry,
        obs=obs,
        feed=FeedRegistry() if feed is None else feed,
        warm_up=registry.warm_up,
        shutdown=registry.aclose,
    )
    return app, registry


def build_server(
    settings: Settings,
    obs: Observability,
    factories: Sequence[CapabilityFactory] | None = None,
    feed: FeedRegistry | None = None,
) -> FeedClosingServer:
    """Compose the application and the server that serves it around one feed.

    A function rather than three statements inside `main` because the one thing
    worth checking about it is not visible from outside otherwise: that the feed
    `/stream.mjpg` reads and the feed the server closes on the way out are the
    same object. Two of them would look exactly like one until a shutdown left
    the real viewers parked.

    Args:
        settings: The settings in effect.
        obs: Where timings, spans and log lines go.
        factories: What capabilities to build. Defaults to everything
            registered.
        feed: The live frame to compose both halves around. One is built when
            none is given.

    Returns:
        The server, not yet running.
    """
    live_feed = FeedRegistry() if feed is None else feed
    app, _registry = build_application(settings, obs, factories, feed=live_feed)
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_config=None,
        # The sans-io implementation, named explicitly: uvicorn's older
        # `websockets` integration is deprecated and warns on import, and
        # "auto" would let the choice drift with the dependency.
        ws="websockets-sansio",
        # The same bound the session checks, enforced a layer lower so an
        # oversize message is refused as it arrives rather than after the whole
        # of it has been assembled in memory. The session's own check is what
        # holds for a transport that does not offer one.
        ws_max_size=settings.max_message_bytes,
        # `timeout_graceful_shutdown` is deliberately left unset. Bounding the
        # drain is not what stops a stream from holding it up — closing the feed
        # is, and `FeedClosingServer` does that before the drain starts.
    )
    return FeedClosingServer(config, feed=live_feed)


#:= docs/specs/architecture/index.md#req-009-configuration-is-validated-and-self-reporting
#:% Every component that reads configuration from its environment MUST fail to start
#:% when it encounters a variable matching its own prefix that it does not
#:% recognise, and MUST emit its fully resolved configuration at startup with every
#:% value marked secret replaced by a redacted placeholder.
def main(argv: Sequence[str] | None = None) -> int:
    """Start the service, or explain why it will not start.

    Args:
        argv: Command-line arguments. None are accepted: everything this service
            reads it reads from the environment, so that a deployment is
            described in one place.

    Returns:
        The process exit status.
    """
    if argv:
        sys.stderr.write(
            "reachy-groundstation takes no arguments; "
            "it is configured through REACHY_GROUNDSTATION_* variables.\n",
        )
        return 2

    try:
        settings = load_settings()
    except ConfigurationError as error:
        sys.stderr.write(f"{error}\n")
        return 78  # EX_CONFIG, which is what an init system should see.

    configure_logging(settings)
    log_resolved_configuration(settings)

    obs = build_observability(settings)
    server = build_server(settings, obs)

    _logger.info("service.starting", host=settings.host, port=settings.port)
    server.run()
    return 0
