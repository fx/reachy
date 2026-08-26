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

    from starlette.applications import Starlette

    from reachy_groundstation.capabilities.registry import CapabilityFactory
    from reachy_groundstation.config import Settings
    from reachy_groundstation.obs import Observability

__all__ = ["build_application", "main"]

_logger = get_logger(__name__)


def build_application(
    settings: Settings,
    obs: Observability,
    factories: Sequence[CapabilityFactory] | None = None,
) -> tuple[Starlette, CapabilityRegistry]:
    """Wire the capabilities into an application.

    Args:
        settings: The settings in effect.
        obs: Where timings, spans and log lines go.
        factories: What to build. Defaults to everything registered.

    Returns:
        The application, and the registry it was built around. Closing the
        registry is wired into the application's own lifespan, so the caller
        keeps it only in order to inspect it.
    """
    registry = CapabilityRegistry(settings, factories)
    # One for the whole process, which is what makes "exactly one authenticated
    # session" a question the operator feed can answer: a registry per session
    # or per request would count to one every time and show whichever robot
    # happened to be asking.
    app = create_app(
        settings=settings,
        registry=registry,
        obs=obs,
        feed=FeedRegistry(),
        warm_up=registry.warm_up,
        shutdown=registry.aclose,
    )
    return app, registry


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
    app, _registry = build_application(settings, obs)

    _logger.info("service.starting", host=settings.host, port=settings.port)
    uvicorn.run(
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
    )
    return 0
