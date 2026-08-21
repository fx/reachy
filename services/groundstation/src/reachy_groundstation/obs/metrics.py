"""The metrics registry, the per-stage timers and the drop counter.

Two decisions in here are worth reading before the code.

**A drop is a counter, never a log line.** Frames are dropped precisely when the
service is overloaded, and per-occurrence logging would add its own load at the
moment there is least to spare. `frames_dropped_total` is the whole record.

**A session identifier and a sequence number are exemplars, not labels.**
Groundstation REQ-028 requires every metric emitted while handling a frame to
carry both. As Prometheus labels they would be ruinous — a label value per frame
is unbounded cardinality, and the series would outnumber the frames. An exemplar
attaches exactly that identity to an individual observation without multiplying
series, which is the mechanism the exposition format has for this, so `/metrics`
negotiates OpenMetrics and the timings carry their frame's identity there.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.exposition import choose_encoder

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "STAGE_DECODE",
    "STAGE_EMIT",
    "STAGE_QUEUE",
    "Metrics",
    "build_metrics",
    "frame_exemplar",
    "render_metrics",
    "set_capability_gauges",
]

# The pipeline stages that are not a capability. Each capability is timed under
# its own name in `capability_seconds`, so decode, every capability and result
# emission are separately visible — which is what groundstation REQ-029 asks an
# operator to be able to see without instrumenting further.
STAGE_DECODE: Final = "decode"
STAGE_EMIT: Final = "emit"

# How long a frame waited in its session's queue before anything looked at it.
# Measured against this service's own monotonic clock, never against the frame's
# capture token: the token belongs to the robot's clock and is copied through
# untouched, and comparing it with a clock here would be comparing two machines'
# monotonic epochs.
STAGE_QUEUE: Final = "queue"

# Sub-millisecond through to a second and a half. The face pass this service was
# measured against is 39 ms and decode is 2 ms, so the interesting resolution is
# well below the Prometheus defaults' 5 ms floor.
_LATENCY_BUCKETS: Final = (
    0.001,
    0.002,
    0.005,
    0.010,
    0.025,
    0.050,
    0.100,
    0.250,
    0.500,
    1.000,
    2.500,
)


#:= docs/specs/groundstation/index.md#req-029-per-stage-timings-are-measured-and-exposed
#:% The service MUST record the duration of each pipeline stage separately and
#:% expose those durations as metrics.
@dataclass(frozen=True, slots=True)
class Metrics:
    """Every metric this service exposes, held together rather than globally.

    The collectors live on a registry passed in rather than on the process-wide
    default, so a test builds its own set and reads it back without the
    duplicate-registration errors a global registry produces the second time a
    module is imported.

    Attributes:
        registry: The collector registry `/metrics` renders.
        sessions_total: Sessions by how they ended.
        sessions_active: Sessions currently established.
        frames_received_total: Frames accepted from clients.
        frames_dropped_total: Frames discarded because a session's queue was
            full. The whole record of backpressure.
        results_emitted_total: Results delivered, by capability.
        errors_total: Errors reported to a client, by code.
        stage_seconds: Duration of the pipeline stages that are not a
            capability.
        capability_seconds: Duration of one capability's pass over one frame.
        capability_up: One per capability: 1 when it is ready, 0 when it is not.
    """

    registry: CollectorRegistry
    sessions_total: Counter
    sessions_active: Gauge
    frames_received_total: Counter
    frames_dropped_total: Counter
    results_emitted_total: Counter
    errors_total: Counter
    stage_seconds: Histogram
    capability_seconds: Histogram
    capability_up: Gauge


def build_metrics(registry: CollectorRegistry | None = None) -> Metrics:
    """Create the collectors on a registry of their own.

    Args:
        registry: The registry to register on. A fresh one is created when none
            is given, which is what every caller but a test wants.

    Returns:
        The metrics bundle the rest of the service holds.
    """
    target = CollectorRegistry() if registry is None else registry
    return Metrics(
        registry=target,
        sessions_total=Counter(
            "groundstation_sessions_total",
            "Sessions that reached a terminal state, by how they ended.",
            ("outcome",),
            registry=target,
        ),
        sessions_active=Gauge(
            "groundstation_sessions_active",
            "Sessions currently established.",
            registry=target,
        ),
        frames_received_total=Counter(
            "groundstation_frames_received_total",
            "Frames accepted from a client.",
            registry=target,
        ),
        frames_dropped_total=Counter(
            "groundstation_frames_dropped_total",
            "Frames discarded because the session's queue was at its bound.",
            registry=target,
        ),
        results_emitted_total=Counter(
            "groundstation_results_emitted_total",
            "Results delivered to a client, by capability.",
            ("capability",),
            registry=target,
        ),
        errors_total=Counter(
            "groundstation_errors_total",
            "Errors reported to a client, by code.",
            ("code",),
            registry=target,
        ),
        stage_seconds=Histogram(
            "groundstation_stage_seconds",
            "Duration of a pipeline stage that is not a capability.",
            ("stage",),
            buckets=_LATENCY_BUCKETS,
            registry=target,
        ),
        capability_seconds=Histogram(
            "groundstation_capability_seconds",
            "Duration of one capability's pass over one frame.",
            ("capability",),
            buckets=_LATENCY_BUCKETS,
            registry=target,
        ),
        capability_up=Gauge(
            "groundstation_capability_up",
            "1 when a capability is ready to be offered, 0 when it is not.",
            ("capability",),
            registry=target,
        ),
    )


#:= docs/specs/groundstation/index.md#req-028-work-is-attributable-end-to-end
#:% Every log line and metric emitted while handling a frame MUST carry the session
#:% identifier and the frame's sequence number.
def frame_exemplar(session_id: str, sequence: int) -> dict[str, str]:
    """Identify the frame an observation belongs to.

    Args:
        session_id: The session the frame arrived on.
        sequence: The frame's number within that session.

    Returns:
        The exemplar labels to attach to a timing observation.
    """
    return {"session": session_id, "sequence": str(sequence)}


def render_metrics(metrics: Metrics, accept: str | None) -> tuple[bytes, str]:
    """Render the registry in whichever exposition format the caller asked for.

    Content negotiation is not decoration here: exemplars are carried by the
    OpenMetrics format and dropped by the older text format, so a scraper that
    asks for OpenMetrics is the one that gets a frame's identity alongside its
    timing.

    Args:
        metrics: The bundle to render.
        accept: The request's `Accept` header, if it sent one.

    Returns:
        The rendered body and the content type to return it under.
    """
    encoder, content_type = choose_encoder(accept or "")
    return encoder(metrics.registry), content_type


def set_capability_gauges(
    metrics: Metrics,
    states: Mapping[str, bool],
) -> None:
    """Publish which capabilities are ready.

    Args:
        metrics: The bundle to write to.
        states: Capability name to whether it is ready.
    """
    for name, ready in states.items():
        metrics.capability_up.labels(capability=name).set(1 if ready else 0)
