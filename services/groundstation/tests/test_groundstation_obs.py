"""Observability: what the boot log says, what a metric carries, what a span is.

Two tests matter most, and they are both about something not being there. The
boot log and the configuration endpoint are two surfaces reporting the same
thing, and the way that goes wrong is that one of them is updated and the other
is not — so they are checked against the same credential, in the same test. And
a camera frame must reach none of these surfaces at all, so a real frame is
driven through a real pipeline and then looked for in every one of them.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`. Nothing here touches a socket, a clock or a file.
"""

from __future__ import annotations

import httpx
import pytest
import structlog
from groundstation_support import (
    CREDENTIAL,
    EchoCapability,
    StaticRegistry,
    agreed,
    build_observability,
    captured_logs,
    jpeg_bytes,
    make_header,
    make_settings,
)

from reachy_groundstation.api.app import create_app
from reachy_groundstation.config import REDACTED_SET, Settings, resolved_configuration
from reachy_groundstation.feed import FeedRegistry
from reachy_groundstation.obs import (
    STAGE_DECODE,
    build_metrics,
    configure_logging,
    frame_context,
    frame_exemplar,
    get_logger,
    log_resolved_configuration,
    render_metrics,
    session_context,
    set_capability_gauges,
)
from reachy_groundstation.obs import (
    build_observability as build_service_observability,
)
from reachy_groundstation.pipeline.queue import QueuedFrame
from reachy_groundstation.pipeline.runner import FramePipeline

OTHER_CREDENTIAL = "a-different-example-credential"


def test_a_session_binding_reaches_every_line_beneath_it() -> None:
    """Threading the identifier through every call would eventually miss one."""
    with captured_logs() as logs, session_context("abcd1234"):
        get_logger(__name__).info("something.happened")
    assert logs[0]["session"] == "abcd1234"


#:= docs/specs/groundstation/index.md#req-028-work-is-attributable-end-to-end
#:% Every log line and metric emitted while handling a frame MUST carry the session
#:% identifier and the frame's sequence number.
def test_a_frame_binding_adds_the_sequence_number() -> None:
    """Searching by sequence number finds every stage that touched the frame."""
    with captured_logs() as logs, session_context("abcd1234"), frame_context(19):
        get_logger(__name__).info("stage.finished")
    assert (logs[0]["session"], logs[0]["sequence"]) == ("abcd1234", 19)


def test_a_binding_does_not_outlive_its_context() -> None:
    """One session's identifier must not leak into the next session's lines."""
    with captured_logs() as logs:
        with session_context("first"):
            get_logger(__name__).info("inside")
        get_logger(__name__).info("outside")
    assert logs[0]["session"] == "first"
    assert "session" not in logs[1]


def test_the_exemplar_carries_the_session_and_the_sequence_number() -> None:
    """As an exemplar rather than a label, so the series count stays bounded."""
    assert frame_exemplar("abcd", 5) == {"session": "abcd", "sequence": "5"}


def test_metrics_are_built_on_their_own_registry() -> None:
    """Two bundles do not collide, which is what makes a test's metrics its own."""
    first = build_metrics()
    second = build_metrics()
    first.frames_received_total.inc()
    assert first.registry.get_sample_value("groundstation_frames_received_total") == 1
    assert second.registry.get_sample_value("groundstation_frames_received_total") == 0


def test_a_stage_timing_is_labelled_by_its_stage() -> None:
    """Per-stage means per-stage: decode is its own series."""
    metrics = build_metrics()
    metrics.stage_seconds.labels(stage=STAGE_DECODE).observe(0.002)
    assert (
        metrics.registry.get_sample_value(
            "groundstation_stage_seconds_count",
            {"stage": STAGE_DECODE},
        )
        == 1
    )


def test_capability_gauges_report_health() -> None:
    """A capability that is not ready reads as zero, not as missing."""
    metrics = build_metrics()
    set_capability_gauges(metrics, {"echo": True, "broken": False})
    assert (
        metrics.registry.get_sample_value(
            "groundstation_capability_up",
            {"capability": "broken"},
        )
        == 0
    )


def test_the_plain_text_exposition_is_served_by_default() -> None:
    """A scraper that asks for nothing in particular still gets an answer."""
    body, content_type = render_metrics(build_metrics(), None)
    assert "text/plain" in content_type
    assert b"groundstation_sessions_active" in body


def test_openmetrics_is_served_when_it_is_asked_for() -> None:
    """Exemplars only travel in the newer format, so the choice is not cosmetic."""
    metrics = build_metrics()
    metrics.stage_seconds.labels(stage=STAGE_DECODE).observe(
        0.002,
        exemplar=frame_exemplar("abcd", 3),
    )
    body, content_type = render_metrics(
        metrics,
        "application/openmetrics-text; version=1.0.0",
    )
    assert "application/openmetrics-text" in content_type
    assert b'session="abcd"' in body


def test_a_tracer_is_built_against_the_configured_service_name() -> None:
    """A span has to say which process produced it."""
    obs = build_service_observability(make_settings(service_name="groundstation-test"))
    resource = obs.provider.resource
    assert resource.attributes["service.name"] == "groundstation-test"
    with obs.tracer.start_as_current_span("probe") as span:
        assert span.is_recording()


def test_configure_logging_installs_the_requested_level() -> None:
    """A level nobody set is not a level in effect."""
    configure_logging(make_settings(log_level="error"))
    logger = structlog.get_logger("probe")
    assert logger.is_enabled_for(40) is True
    assert logger.is_enabled_for(20) is False


def test_configure_logging_accepts_the_console_renderer() -> None:
    """The other format is a real option, not a value the parser merely allows."""
    configure_logging(make_settings(log_format="console"))
    assert isinstance(
        structlog.get_config()["processors"][-1],
        structlog.dev.ConsoleRenderer,
    )


#:= docs/specs/architecture/index.md#req-009-configuration-is-validated-and-self-reporting
#:% Every component that reads configuration from its environment MUST fail to start
#:% when it encounters a variable matching its own prefix that it does not
#:% recognise, and MUST emit its fully resolved configuration at startup with every
#:% value marked secret replaced by a redacted placeholder.
def test_the_boot_log_reports_every_setting_in_effect() -> None:
    """An operator reading the log can tell what is in effect, not what was set."""
    with captured_logs() as logs:
        log_resolved_configuration(make_settings(port=9443))
    (line,) = logs
    assert line["event"] == "configuration.resolved"
    assert line["port"] == 9443
    assert set(Settings.model_fields) <= set(line)


@pytest.mark.asyncio
async def test_a_credential_is_redacted_on_both_self_reporting_surfaces() -> None:
    """One renderer, two surfaces: a secret cannot be missed on one of them.

    Redacting at the log site and again at the endpoint is how the second one
    gets forgotten when a setting is added. This is the test that would notice.
    """
    settings = make_settings(credential=OTHER_CREDENTIAL)
    obs, _exporter = build_observability()

    with captured_logs() as logs:
        log_resolved_configuration(settings)

    app = create_app(settings=settings, registry=StaticRegistry(), obs=obs)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://groundstation.invalid",
    ) as client:
        response = await client.get("/config")

    assert logs[0]["credential"] == REDACTED_SET
    assert OTHER_CREDENTIAL not in str(logs[0])
    assert response.json()["credential"] == REDACTED_SET
    assert OTHER_CREDENTIAL not in response.text
    assert CREDENTIAL not in response.text


@pytest.mark.asyncio
async def test_no_camera_frame_reaches_a_log_a_metric_or_a_span() -> None:
    """The feed retains a frame; nothing that records what happened may.

    Everything an operator can read afterwards is checked at once, because the
    way this goes wrong is that a payload is added to one surface — an error
    detail, a span attribute, an exemplar — while the other two stay clean and
    the test that only looked at logs stays green.

    The malformed frame is here for the same reason: the report that a payload
    would not decode is the message most likely to quote it back.
    """
    payload = jpeg_bytes()
    malformed = b"\xff\xd8\xff\xe0 truncated"
    feed = FeedRegistry()
    obs, exporter = build_observability()
    delivered: list[object] = []

    async def _deliver(kind: object, message: object) -> None:
        delivered.append((kind, message))

    pipeline = FramePipeline(
        capabilities=[agreed(EchoCapability())],
        deliver=_deliver,
        settings=make_settings(),
        obs=obs,
        session_id="0123456789abcdef",
        feed=feed,
        clock=lambda: 0.0,
    )

    with captured_logs() as logs, feed.authenticated_session():
        for sequence, frame in enumerate((payload, malformed)):
            await pipeline.process(
                QueuedFrame(
                    header=make_header(sequence),
                    payload=frame,
                    received_at=0.0,
                ),
            )
        retained = await feed.next_frame(after=0)

    exposition, _content_type = render_metrics(
        obs.metrics,
        "application/openmetrics-text; version=1.0.0",
    )
    spans = repr([span.attributes for span in exporter.get_finished_spans()])
    surfaces = (
        repr(logs).encode("utf-8", "surrogateescape"),
        exposition,
        spans.encode("utf-8", "surrogateescape"),
        repr(delivered).encode("utf-8", "surrogateescape"),
    )

    # The feed did its job, so the absence below is about what was recorded and
    # not about a frame that never arrived.
    assert retained is not None
    assert retained.payload == payload
    for surface in surfaces:
        assert payload not in surface
        assert payload[:16] not in surface
        assert malformed not in surface
        assert b"truncated" not in surface


def test_the_two_surfaces_render_through_the_same_function() -> None:
    """Not an implementation detail: it is what keeps the two from diverging."""
    settings = make_settings()
    with captured_logs() as logs:
        log_resolved_configuration(settings)
    rendered = resolved_configuration(settings)
    assert {key: logs[0][key] for key in rendered} == rendered
