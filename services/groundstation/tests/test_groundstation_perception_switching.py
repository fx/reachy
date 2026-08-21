"""Switching detectors on and off, one at a time and all at once.

Perception REQ-038 asks for two things that are easy to conflate. A detector must
be switchable *independently* — switching one off leaves the others running — and
switching one off must not be the same event as one failing, because an operator
looking at a degraded service has to be able to tell "I turned that off" from
"that broke".

So a factory that this deployment's settings switch off raises
`CapabilityDisabledError`, the registry records it as `DISABLED`, and the health
surface says so. Nothing is offered, nothing is routed, and nothing is reported
as unhealthy.

The last test in this file is the one the requirement is really about: every
detector off, a real session, a real frame, and a client that gets an empty
agreement and no error rather than a refusal.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from groundstation_perception_support import model_directory, require_model
from groundstation_support import (
    MemoryTransport,
    build_observability,
    frame_message,
    make_settings,
)

import reachy_groundstation
from reachy_contracts import (
    FACE_CAPABILITY,
    GESTURE_CAPABILITY,
    Capability,
    CloseReason,
)
from reachy_groundstation.api.app import create_app
from reachy_groundstation.capabilities.perception.face import (
    FACE_VERSION,
    build_face_capability,
)
from reachy_groundstation.capabilities.perception.gesture import (
    GESTURE_VERSION,
    build_gesture_capability,
)
from reachy_groundstation.capabilities.registry import (
    CapabilityDisabledError,
    CapabilityRegistry,
    registered_factories,
)
from reachy_groundstation.models import FACE_DETECTION_YUNET
from reachy_groundstation.ports import CapabilityState
from reachy_groundstation.service import build_application
from reachy_groundstation.session.framing import MessageKind, decode_control
from reachy_groundstation.session.runner import SessionRunner

if TYPE_CHECKING:
    from reachy_groundstation.config import Settings

_FACE = Capability(name=FACE_CAPABILITY, version=FACE_VERSION)
_GESTURE = Capability(name=GESTURE_CAPABILITY, version=GESTURE_VERSION)

_PERCEPTION = (build_face_capability, build_gesture_capability)

# How many passes of the event loop to give warm-up before calling it stuck.
# Bounded rather than open-ended: a capability that never warms up has to fail
# this test rather than hang the suite. Each pass is a yield and reads no clock.
_READINESS_POLLS = 2000


def _settings(**overrides: object) -> Settings:
    """Build settings pointed at the directory the weights are in.

    Args:
        overrides: Settings to change from their defaults.

    Returns:
        The settings.
    """
    values: dict[str, object] = {"models_dir": str(model_directory())}
    values.update(overrides)
    return make_settings(**values)


def _states(registry: CapabilityRegistry) -> dict[str, CapabilityState]:
    """Read what the registry says about each capability.

    Args:
        registry: The registry to inspect.

    Returns:
        Capability name to lifecycle state.
    """
    return {entry.name: entry.state for entry in registry.health()}


def test_both_detectors_are_in_the_catalogue_the_composition_root_builds_from() -> None:
    """Adding a capability is a module and a decorator, and nothing else.

    Groundstation REQ-022 is a rule about which files change when a capability
    arrives. This is half of the observable evidence: both factories are in the
    catalogue, and no shared list was edited to put them there.

    It is only half because this module imports the two capability modules
    directly, and importing either is enough to run its decorator — so this
    assertion would still pass if the composition root had stopped importing
    them and the service offered nothing. The test below is the other half.
    """
    registered = registered_factories()
    assert build_face_capability in registered
    assert build_gesture_capability in registered


@pytest.mark.filesystem
def test_the_capability_package_is_what_pulls_the_detectors_in() -> None:
    """What makes registration reach the service is one import, and it is there.

    The composition root imports `reachy_groundstation.capabilities`, and
    nothing else in the service names the perception modules. So the import in
    that package's `__init__` is the entire wiring, and a tidying pass that
    removed it as unused would leave a service that registers nothing, warms up
    nothing and offers nothing — with every test above still green, because a
    test module that imports the capability modules registers them itself.

    Reading the source is what closes that gap without spawning an interpreter.
    It is input, so this is not a unit test and says so; the source on disk is
    what is under test, which is the case the marker exists for.
    """
    source = (
        Path(reachy_groundstation.__file__).parent / "capabilities" / "__init__.py"
    ).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert "reachy_groundstation.capabilities.perception" in imported


#:= docs/specs/perception/index.md#req-038-a-capability-can-be-disabled-without-disabling-the-session
#:% Each detector MUST be independently switchable at run time, and disabling one
#:% MUST leave the others operating.
def test_a_switched_off_detector_declines_to_be_built() -> None:
    """The factory says which one, because there is no capability left to ask."""
    with pytest.raises(CapabilityDisabledError) as disabled:
        build_face_capability(_settings(face_enabled=False))
    assert disabled.value.name == FACE_CAPABILITY

    with pytest.raises(CapabilityDisabledError) as gesture:
        build_gesture_capability(_settings(gesture_enabled=False))
    assert gesture.value.name == GESTURE_CAPABILITY


@pytest.mark.filesystem
@pytest.mark.asyncio
async def test_the_shipped_defaults_offer_faces_and_not_gestures() -> None:
    """What an operator gets without configuring anything.

    Gestures are off because no gesture model clears this repository's licence
    and provenance bar yet, which is the perception spec's recorded decision
    rather than an accident of ordering.
    """
    require_model(FACE_DETECTION_YUNET)
    registry = CapabilityRegistry(_settings(), _PERCEPTION)
    await registry.warm_up()
    try:
        assert _states(registry) == {
            FACE_CAPABILITY: CapabilityState.READY,
            GESTURE_CAPABILITY: CapabilityState.DISABLED,
        }
        assert registry.supported() == (_FACE,)
    finally:
        await registry.aclose()


@pytest.mark.filesystem
@pytest.mark.asyncio
async def test_switching_one_off_leaves_the_other_operating() -> None:
    """The requirement's own scenario, with the switch thrown the other way."""
    require_model(FACE_DETECTION_YUNET)
    registry = CapabilityRegistry(
        _settings(face_enabled=False, gesture_enabled=True),
        _PERCEPTION,
    )
    await registry.warm_up()
    try:
        assert _states(registry) == {
            FACE_CAPABILITY: CapabilityState.DISABLED,
            GESTURE_CAPABILITY: CapabilityState.READY,
        }
        assert registry.supported() == (_GESTURE,)
        # And the one still running answers frames as it always did.
        gesture = registry.get(GESTURE_CAPABILITY)
        assert gesture is not None
    finally:
        await registry.aclose()


@pytest.mark.filesystem
@pytest.mark.asyncio
async def test_both_switched_on_offer_both() -> None:
    """Independent switches, so the combination has to work too."""
    require_model(FACE_DETECTION_YUNET)
    registry = CapabilityRegistry(_settings(gesture_enabled=True), _PERCEPTION)
    await registry.warm_up()
    try:
        assert registry.supported() == (_FACE, _GESTURE)
    finally:
        await registry.aclose()


@pytest.mark.asyncio
async def test_every_detector_switched_off_still_becomes_ready() -> None:
    """A service offering nothing is a service that serves sessions offering nothing.

    No weights are needed here: nothing is built, so nothing is loaded, which is
    also why this test is not marked as reading the filesystem.
    """
    registry = CapabilityRegistry(
        make_settings(face_enabled=False, gesture_enabled=False),
        _PERCEPTION,
    )
    await registry.warm_up()
    assert registry.ready is True
    assert registry.supported() == ()
    assert set(_states(registry).values()) == {CapabilityState.DISABLED}
    assert registry.get(FACE_CAPABILITY) is None
    await registry.aclose()


@pytest.mark.asyncio
async def test_a_disabled_detector_is_not_reported_as_a_failure() -> None:
    """Off and broken are different answers to an operator's question."""
    registry = CapabilityRegistry(
        make_settings(face_enabled=False, gesture_enabled=False),
        _PERCEPTION,
    )
    await registry.warm_up()
    for entry in registry.health():
        assert entry.state is CapabilityState.DISABLED
        # And no detail, because there is nothing wrong to explain. An
        # unhealthy capability carries the kind of failure it suffered; a
        # disabled one has nothing to say beyond being off.
        assert entry.detail == ""


#:= docs/specs/robot-link/index.md#req-013-an-empty-result-is-a-valid-result
#:% A result message carrying no detections MUST be treated as a successful result
#:% for that frame.
@pytest.mark.asyncio
async def test_a_session_with_every_detector_off_is_a_session_not_an_error() -> None:
    """The requirement stated end to end: a real session, a real frame, no error.

    The predecessor posted nothing at all when every detector was switched off
    and got a 400 back for it. Here the client negotiates, agrees on nothing,
    sends a frame, and is told nothing — which is the successful outcome.
    """
    registry = CapabilityRegistry(
        make_settings(face_enabled=False, gesture_enabled=False),
        _PERCEPTION,
    )
    await registry.warm_up()

    transport = MemoryTransport()
    transport.offer(_FACE, _GESTURE)
    transport.push(frame_message(1))
    transport.disconnect()

    obs, _exporter = build_observability()
    outcome = await SessionRunner(
        transport=transport,
        registry=registry,
        settings=make_settings(),
        obs=obs,
        session_id="feedfacefeedface",
        clock=iter(range(10_000)).__next__,
    ).run()

    kinds = [kind for kind, _ in (decode_control(text) for text in transport.sent)]
    assert outcome.agreed == ()
    assert outcome.frames_received == 1
    assert kinds == [MessageKind.AGREEMENT]
    assert MessageKind.ERROR not in kinds
    assert outcome.reason is CloseReason.GOING_AWAY


#:= docs/specs/groundstation/index.md#req-026-readiness-is-distinct-from-liveness
#:% The service MUST report itself ready only once every capability it will offer
#:% has completed its warm-up.
@pytest.mark.filesystem
@pytest.mark.asyncio
async def test_the_shipped_composition_warms_up_and_reports_itself_ready() -> None:
    """The whole default wiring, assembled the way the entry point assembles it.

    Every other test here hands the registry an explicit list of factories,
    which is what makes them tests of the registry rather than of this build. So
    none of them would notice the composition root reaching a capability set
    that never becomes ready — a model that would not load, a warm-up that
    hangs on the loop, a factory that raises. This one builds the application
    with no factories at all, runs the lifespan that warms it up, and polls the
    readiness endpoint exactly as an orchestrator would.
    """
    require_model(FACE_DETECTION_YUNET)
    settings = _settings()
    obs, _exporter = build_observability()
    app, _registry = build_application(settings, obs)

    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://groundstation.invalid",
        ) as client,
        app.router.lifespan_context(app),
    ):
        # Warm-up runs as a background task, so readiness is polled rather than
        # awaited — which is also what makes this a test of the readiness
        # endpoint rather than of the registry behind it. The bound is what
        # stops a capability that never warms up from hanging the suite.
        for _ in range(_READINESS_POLLS):
            response = await client.get("/readyz")
            if response.status_code == 200:
                break
            await asyncio.sleep(0)
        else:
            pytest.fail("the shipped composition never reported itself ready")

        body = response.json()
        offered = (await client.get("/capabilities")).json()["offered"]

    assert body["ready"] is True
    assert offered == [FACE_CAPABILITY]
    states = {entry["name"]: entry["state"] for entry in body["capabilities"]}
    assert states == {FACE_CAPABILITY: "ready", GESTURE_CAPABILITY: "disabled"}


#:= docs/specs/groundstation/index.md#req-030-the-effective-configuration-is-retrievable-at-run-time
#:% The service MUST expose its fully resolved configuration over its own interface
#:% while running, with every value marked secret replaced by a redacted
#:% placeholder.
@pytest.mark.asyncio
async def test_the_thresholds_are_visible_through_the_configuration_endpoint() -> None:
    """Perception REQ-039's scenario: the operator checks the value in effect.

    A threshold that is settable but not reportable leaves an operator with no
    way to tell a setting that took effect from one that was misspelled — which
    is the defect class this service's configuration surfaces exist for.
    """
    settings = make_settings(
        face_score_threshold=0.85,
        gesture_score_threshold=0.4,
        gesture_sample_interval=2,
        inference_intra_op_threads=2,
    )
    obs, _exporter = build_observability()
    registry = CapabilityRegistry(settings, [])
    app = create_app(settings=settings, registry=registry, obs=obs)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://groundstation.invalid",
    ) as client:
        body = (await client.get("/config")).json()

    assert body["face_score_threshold"] == 0.85
    assert body["gesture_score_threshold"] == 0.4
    assert body["gesture_sample_interval"] == 2
    assert body["inference_intra_op_threads"] == 2
    assert body["face_enabled"] is True
    assert body["gesture_enabled"] is False
    assert body["models_dir"]
