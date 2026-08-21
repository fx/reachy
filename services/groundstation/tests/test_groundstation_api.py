"""The operator surface: liveness, readiness, capability health, metrics, config.

These drive the real application over `httpx.ASGITransport`, which speaks HTTP to
the ASGI app in memory. The routes, the responses and the status codes are the
real ones; no socket is opened, so these stay unit tests.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from groundstation_support import (
    CREDENTIAL,
    ECHO,
    EchoCapability,
    ExplodingCapability,
    StaticRegistry,
    build_observability,
    hand_control_to_the_event_loop,
    make_settings,
)

from reachy_contracts import Capability
from reachy_groundstation.api.app import create_app
from reachy_groundstation.capabilities.registry import CapabilityRegistry
from reachy_groundstation.config import REDACTED_SET, Settings
from reachy_groundstation.ports import CapabilityPort, CapabilityRegistryPort

BROKEN = Capability(name="broken", version=1)


def _client(registry: CapabilityRegistryPort, **overrides: object) -> httpx.AsyncClient:
    """Build a client over the real application.

    Args:
        registry: What the application is composed around.
        overrides: Settings to change from their defaults.

    Returns:
        An HTTP client speaking to the application in memory.
    """
    obs, _exporter = build_observability()
    app = create_app(
        settings=make_settings(**overrides),
        registry=registry,
        obs=obs,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://groundstation.invalid",
    )


def _broken(settings: Settings) -> CapabilityPort:
    """Build a capability whose warm-up fails.

    Args:
        settings: The settings in effect, unused.

    Returns:
        The capability.
    """
    del settings
    return ExplodingCapability(BROKEN, on_warm_up=True)


def _echo(settings: Settings) -> CapabilityPort:
    """Build the echo capability.

    Args:
        settings: The settings in effect, unused.

    Returns:
        The capability.
    """
    del settings
    return EchoCapability()


@pytest.mark.asyncio
async def test_liveness_says_only_that_the_process_is_alive() -> None:
    """Liveness answers a different question from readiness."""
    async with _client(StaticRegistry(EchoCapability())) as client:
        response = await client.get("/livez")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


#:= docs/specs/groundstation/index.md#req-026-readiness-is-distinct-from-liveness
#:% The service MUST report itself ready only once every capability it will offer
#:% has completed its warm-up.
@pytest.mark.asyncio
async def test_readiness_refuses_traffic_until_warm_up_completes() -> None:
    """An orchestrator polling this holds the first session back."""
    registry = CapabilityRegistry(make_settings(), [_echo])
    async with _client(registry) as client:
        warming = await client.get("/readyz")
        await registry.warm_up()
        warmed = await client.get("/readyz")

    assert warming.status_code == 503
    assert warming.json()["ready"] is False
    assert warmed.status_code == 200
    assert warmed.json()["ready"] is True


@pytest.mark.asyncio
async def test_liveness_is_true_while_readiness_is_false() -> None:
    """The two endpoints disagree during warm-up, which is the whole point."""
    registry = CapabilityRegistry(make_settings(), [_echo])
    async with _client(registry) as client:
        assert (await client.get("/livez")).status_code == 200
        assert (await client.get("/readyz")).status_code == 503


#:= docs/specs/groundstation/index.md#req-025-a-failed-capability-does-not-take-down-the-service
#:% When a capability fails to initialise, the service MUST continue serving the
#:% capabilities that initialised successfully.
@pytest.mark.asyncio
async def test_a_degraded_service_reports_which_capability_failed() -> None:
    """Being one capability short is legible as a failure, not as a smaller set."""
    registry = CapabilityRegistry(make_settings(), [_broken, _echo])
    await registry.warm_up()
    async with _client(registry) as client:
        response = await client.get("/capabilities")

    body = response.json()
    assert body["ready"] is True
    assert body["offered"] == ["echo"]
    states = {entry["name"]: entry["state"] for entry in body["capabilities"]}
    assert states == {"broken": "unhealthy", "echo": "ready"}


@pytest.mark.asyncio
async def test_a_degraded_service_still_becomes_ready() -> None:
    """One model that will not load does not hold the service down forever."""
    registry = CapabilityRegistry(make_settings(), [_broken, _echo])
    await registry.warm_up()
    async with _client(registry) as client:
        assert (await client.get("/readyz")).status_code == 200


@pytest.mark.asyncio
async def test_capability_health_reaches_the_metrics_without_a_second_request() -> None:
    """A scraper reads `/metrics` and nothing else.

    A gauge that only appears once somebody has visited a different endpoint is
    a gauge nobody can alert on, so `/metrics` refreshes it itself.
    """
    registry = CapabilityRegistry(make_settings(), [_broken, _echo])
    await registry.warm_up()
    async with _client(registry) as client:
        metrics = await client.get("/metrics")

    assert 'groundstation_capability_up{capability="echo"} 1.0' in metrics.text
    assert 'groundstation_capability_up{capability="broken"} 0.0' in metrics.text


#:= docs/specs/groundstation/index.md#req-029-per-stage-timings-are-measured-and-exposed
#:% The service MUST record the duration of each pipeline stage separately and
#:% expose those durations as metrics.
@pytest.mark.asyncio
async def test_the_metrics_endpoint_exposes_the_stage_histograms() -> None:
    """Each stage is its own series, so a regression names its own stage."""
    async with _client(StaticRegistry(EchoCapability())) as client:
        body = (await client.get("/metrics")).text
    assert "groundstation_stage_seconds" in body
    assert "groundstation_capability_seconds" in body


@pytest.mark.asyncio
async def test_the_metrics_endpoint_serves_openmetrics_when_asked() -> None:
    """Exemplars travel in OpenMetrics and are dropped by the older format."""
    async with _client(StaticRegistry(EchoCapability())) as client:
        response = await client.get(
            "/metrics",
            headers={"accept": "application/openmetrics-text; version=1.0.0"},
        )
    assert "application/openmetrics-text" in response.headers["content-type"]
    assert response.text.endswith("# EOF\n")


#:= docs/specs/groundstation/index.md#req-030-the-effective-configuration-is-retrievable-at-run-time
#:% The service MUST expose its fully resolved configuration over its own interface
#:% while running, with every value marked secret replaced by a redacted
#:% placeholder.
@pytest.mark.asyncio
async def test_the_configuration_endpoint_answers_what_is_in_effect() -> None:
    """An operator checks a setting without restarting or reading a log."""
    async with _client(StaticRegistry(), port=9443) as client:
        body = (await client.get("/config")).json()
    assert body["port"] == 9443
    assert body["queue_bound"] == 2


@pytest.mark.asyncio
async def test_the_configuration_endpoint_includes_the_defaults() -> None:
    """Everything in effect is present, including what nobody set."""
    async with _client(StaticRegistry()) as client:
        body = (await client.get("/config")).json()
    assert set(body) == set(Settings.model_fields)


@pytest.mark.asyncio
async def test_the_configuration_endpoint_never_returns_the_credential() -> None:
    """The endpoint is reachable by anything that can reach the service."""
    async with _client(StaticRegistry()) as client:
        response = await client.get("/config")
    assert response.json()["credential"] == REDACTED_SET
    assert CREDENTIAL not in response.text


@pytest.mark.asyncio
async def test_warm_up_runs_in_the_background_at_startup() -> None:
    """Readiness has to be answerable while warm-up is still happening.

    Awaiting warm-up at startup would make the readiness endpoint unreachable
    until it finished, which reports "ready" to an orchestrator that never got a
    "not ready" — so the gate here holds warm-up open while a real request is
    served, and the answer is 503.
    """
    gate = asyncio.Event()

    class _Gated(EchoCapability):
        async def warm_up(self) -> None:
            await gate.wait()

    registry = CapabilityRegistry(make_settings(), [lambda _: _Gated()])
    obs, _exporter = build_observability()
    app = create_app(
        settings=make_settings(),
        registry=registry,
        obs=obs,
        warm_up=registry.warm_up,
    )
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://groundstation.invalid",
        ) as client,
        app.router.lifespan_context(app),
    ):
        assert (await client.get("/readyz")).status_code == 503
        gate.set()
        await hand_control_to_the_event_loop()
        assert registry.ready is True
        assert (await client.get("/readyz")).status_code == 200
    assert registry.ready is True


@pytest.mark.asyncio
async def test_the_session_endpoint_is_registered_at_the_specified_path() -> None:
    """The robot link topology names /v1/session, and this is that path."""
    obs, _exporter = build_observability()
    app = create_app(
        settings=make_settings(),
        registry=StaticRegistry(EchoCapability()),
        obs=obs,
    )
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/v1/session" in paths
    assert ECHO.name == "echo"


@pytest.mark.asyncio
async def test_the_lifespan_closes_the_capabilities_it_started() -> None:
    """A service that only ever exits by being killed would leak what it holds.

    Nothing in this build holds anything yet, but the first capability with a
    model in it will, and by then the wiring has to already be there.
    """
    closed: list[str] = []

    class _Closing(EchoCapability):
        async def aclose(self) -> None:
            closed.append(self.descriptor.name)

    registry = CapabilityRegistry(make_settings(), [lambda _: _Closing()])
    obs, _exporter = build_observability()
    app = create_app(
        settings=make_settings(),
        registry=registry,
        obs=obs,
        warm_up=registry.warm_up,
        shutdown=registry.aclose,
    )
    async with app.router.lifespan_context(app):
        await hand_control_to_the_event_loop()
        assert closed == []
    assert closed == ["echo"]


@pytest.mark.asyncio
async def test_an_application_with_no_lifecycle_hooks_still_starts() -> None:
    """Both hooks are optional: a test composing an app by hand passes neither."""
    obs, _exporter = build_observability()
    app = create_app(
        settings=make_settings(),
        registry=StaticRegistry(EchoCapability()),
        obs=obs,
    )
    async with app.router.lifespan_context(app):
        assert app.routes
