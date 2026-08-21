"""Structured logging, metrics and tracing, bundled so they travel together.

Every component that reports anything takes an `Observability` rather than
reaching for a module-level registry or the global tracer provider. That keeps
the wiring in one place, and it keeps a test's metrics and spans local to that
test instead of accumulating on a process-wide default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from reachy_groundstation.obs.logging import (
    configure_logging,
    frame_context,
    get_logger,
    log_resolved_configuration,
    session_context,
)
from reachy_groundstation.obs.metrics import (
    STAGE_DECODE,
    STAGE_EMIT,
    STAGE_QUEUE,
    Metrics,
    build_metrics,
    frame_exemplar,
    render_metrics,
    set_capability_gauges,
)
from reachy_groundstation.obs.tracing import build_tracer, build_tracer_provider

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.trace import Tracer

    from reachy_groundstation.config import Settings

__all__ = [
    "STAGE_DECODE",
    "STAGE_EMIT",
    "STAGE_QUEUE",
    "Metrics",
    "Observability",
    "build_metrics",
    "build_observability",
    "build_tracer",
    "build_tracer_provider",
    "configure_logging",
    "frame_context",
    "frame_exemplar",
    "get_logger",
    "log_resolved_configuration",
    "render_metrics",
    "session_context",
    "set_capability_gauges",
]


@dataclass(frozen=True, slots=True)
class Observability:
    """The reporting surfaces, handed to whatever reports.

    Attributes:
        metrics: The collectors and the registry `/metrics` renders.
        tracer: What pipeline stages open spans on.
        provider: The tracer's provider, kept so the service can shut it down.
    """

    metrics: Metrics
    tracer: Tracer
    provider: TracerProvider


def build_observability(settings: Settings) -> Observability:
    """Create the reporting surfaces for one process.

    Args:
        settings: The settings in effect.

    Returns:
        The bundle every reporting component is given.
    """
    provider = build_tracer_provider(settings)
    return Observability(
        metrics=build_metrics(),
        tracer=build_tracer(provider),
        provider=provider,
    )
