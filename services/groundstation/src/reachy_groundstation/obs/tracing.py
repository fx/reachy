"""Tracing spans across the pipeline stages.

A tracer is passed to the components that create spans rather than fetched from
the global provider at the point of use. That is not ceremony: OpenTelemetry
refuses to replace a provider once one is installed, so a test that wanted to
read spans back from a globally-wired service would either get one process-wide
provider shared between every test or none at all. Handing the tracer in makes a
test's provider local to that test, and makes the production wiring a single
call in the composition root.

What the provider exports is deployment configuration and belongs to the change
that packages the image. `build_tracer_provider` installs no span processor, so
spans are created and recorded and go nowhere until an exporter is attached.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Tracer

from reachy_contracts import __version__

if TYPE_CHECKING:
    from reachy_groundstation.config import Settings

__all__ = ["INSTRUMENTATION_NAME", "build_tracer", "build_tracer_provider"]

INSTRUMENTATION_NAME: Final = "reachy_groundstation"


def build_tracer_provider(settings: Settings) -> TracerProvider:
    """Create a provider identified as this service.

    Args:
        settings: The settings in effect; its service name identifies the
            process in whatever eventually collects the spans.

    Returns:
        A provider with no span processor attached.
    """
    return TracerProvider(
        resource=Resource.create(
            {
                SERVICE_NAME: settings.service_name,
                SERVICE_VERSION: __version__,
            },
        ),
    )


def build_tracer(provider: TracerProvider) -> Tracer:
    """Obtain the tracer this service's spans are created from.

    Args:
        provider: The provider to draw the tracer from.

    Returns:
        The tracer.
    """
    return provider.get_tracer(INSTRUMENTATION_NAME)
