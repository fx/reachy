"""The capability registry: registration, warm-up, and surviving a failure.

The registry is the change's central guarantee — that the service is not coupled
to whatever the first capability turns out to look like. There is no production
capability yet, so it is proved with two unrelated ones from the test support
module, which is exactly the arrangement the change document asks for.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`. Nothing here opens a socket or reads a file. The one test that
configures a warm-up timeout does wait on a clock, bounded at ten
milliseconds, because a timeout elapsing is the behaviour it is about.
"""

from __future__ import annotations

import asyncio

import pytest
from groundstation_support import (
    ECHO,
    TALLY,
    EchoCapability,
    ExplodingCapability,
    TallyCapability,
    make_settings,
)

from reachy_contracts import Capability
from reachy_groundstation.capabilities import registry as registry_module
from reachy_groundstation.capabilities.registry import (
    CapabilityRegistry,
    register,
    registered_factories,
)
from reachy_groundstation.config import Settings
from reachy_groundstation.ports import CapabilityPort, CapabilityState

BROKEN = Capability(name="broken", version=1)


def _echo(settings: Settings) -> CapabilityPort:
    """Build the echo capability.

    Args:
        settings: The settings in effect, unused.

    Returns:
        The capability.
    """
    del settings
    return EchoCapability()


def _tally(settings: Settings) -> CapabilityPort:
    """Build the tally capability.

    Args:
        settings: The settings in effect, unused.

    Returns:
        The capability.
    """
    del settings
    return TallyCapability()


def _broken_warm_up(settings: Settings) -> CapabilityPort:
    """Build a capability whose warm-up fails.

    Args:
        settings: The settings in effect, unused.

    Returns:
        The capability.
    """
    del settings
    return ExplodingCapability(BROKEN, on_warm_up=True)


def _broken_constructor(settings: Settings) -> CapabilityPort:
    """Fail before a capability exists at all.

    Args:
        settings: The settings in effect, unused.

    Returns:
        Never.

    Raises:
        RuntimeError: Always.
    """
    del settings
    message = "this capability cannot be constructed"
    raise RuntimeError(message)


@pytest.mark.asyncio
async def test_a_registry_offers_nothing_before_warm_up() -> None:
    """A capability that has not warmed up is not one to negotiate for."""
    registry = CapabilityRegistry(make_settings(), [_echo])
    assert registry.ready is False
    assert registry.supported() == ()


#:= docs/specs/groundstation/index.md#req-026-readiness-is-distinct-from-liveness
#:% The service MUST report itself ready only once every capability it will offer
#:% has completed its warm-up.
@pytest.mark.asyncio
async def test_readiness_arrives_with_warm_up() -> None:
    """Ready means warmed up, which is what an orchestrator waits for."""
    registry = CapabilityRegistry(make_settings(), [_echo])
    await registry.warm_up()
    assert registry.ready is True
    assert registry.supported() == (ECHO,)


@pytest.mark.asyncio
async def test_warm_up_reaches_the_capability() -> None:
    """A capability that loads a model is given the chance to do it early."""
    capability = EchoCapability()
    registry = CapabilityRegistry(make_settings(), [lambda _: capability])
    await registry.warm_up()
    assert capability.warmed == 1


@pytest.mark.asyncio
async def test_two_unrelated_capabilities_are_both_offered() -> None:
    """The registry is not shaped around whichever capability came first."""
    registry = CapabilityRegistry(make_settings(), [_echo, _tally])
    await registry.warm_up()
    assert registry.supported() == (ECHO, TALLY)


@pytest.mark.asyncio
async def test_a_capability_is_routed_to_by_name() -> None:
    """Routing is by the name negotiation agreed on, and by nothing else."""
    registry = CapabilityRegistry(make_settings(), [_echo, _tally])
    await registry.warm_up()
    routed = registry.get("tally")
    assert routed is not None
    assert routed.descriptor == TALLY


@pytest.mark.asyncio
async def test_an_unknown_name_routes_nowhere() -> None:
    """A name this build cannot serve is absent, not an error."""
    registry = CapabilityRegistry(make_settings(), [_echo])
    await registry.warm_up()
    assert registry.get("gesture") is None


#:= docs/specs/groundstation/index.md#req-025-a-failed-capability-does-not-take-down-the-service
#:% When a capability fails to initialise, the service MUST continue serving the
#:% capabilities that initialised successfully.
@pytest.mark.asyncio
async def test_a_failed_warm_up_leaves_the_rest_serving() -> None:
    """One corrupt model is one capability short, not a dead service."""
    registry = CapabilityRegistry(make_settings(), [_broken_warm_up, _echo])
    await registry.warm_up()
    assert registry.ready is True
    assert registry.supported() == (ECHO,)
    assert registry.get("broken") is None


@pytest.mark.asyncio
async def test_a_failed_warm_up_is_reported_as_unhealthy() -> None:
    """The failure is visible, rather than the capability merely being absent."""
    registry = CapabilityRegistry(make_settings(), [_broken_warm_up, _echo])
    await registry.warm_up()
    states = {entry.name: entry.state for entry in registry.health()}
    assert states == {
        "broken": CapabilityState.UNHEALTHY,
        "echo": CapabilityState.READY,
    }


@pytest.mark.asyncio
async def test_a_failed_warm_up_records_the_kind_of_failure() -> None:
    """The health endpoint is reachable by anything that can reach the service.

    So what it publishes is the kind of failure. A model loader's own message
    names the path it could not open, and that belongs in the log.
    """
    registry = CapabilityRegistry(make_settings(), [_broken_warm_up])
    await registry.warm_up()
    (entry,) = registry.health()
    assert entry.detail == "RuntimeError"
    assert "cannot load its model" not in entry.detail


@pytest.mark.asyncio
async def test_a_constructor_failure_does_not_stop_the_next_capability() -> None:
    """Every capability is built before any is warmed, so all are attempted."""
    registry = CapabilityRegistry(make_settings(), [_broken_constructor, _echo])
    await registry.warm_up()
    assert registry.supported() == (ECHO,)
    unhealthy = [entry for entry in registry.health() if entry.version is None]
    assert len(unhealthy) == 1
    assert unhealthy[0].detail == "RuntimeError"


@pytest.mark.asyncio
async def test_a_warm_up_that_overruns_is_a_failure_not_a_hang() -> None:
    """A capability that never finishes warming must not hold the service."""
    started = asyncio.Event()

    class _Stuck(EchoCapability):
        async def warm_up(self) -> None:
            started.set()
            await asyncio.Event().wait()

    registry = CapabilityRegistry(
        make_settings(warm_up_timeout_seconds=0.01),
        [lambda _: _Stuck()],
    )
    await registry.warm_up()
    assert registry.ready is True
    assert registry.supported() == ()


@pytest.mark.asyncio
async def test_closing_the_registry_reaches_every_capability() -> None:
    """What a capability holds is released when the service stops."""
    closed: list[str] = []

    class _Closing(EchoCapability):
        async def aclose(self) -> None:
            closed.append(self.descriptor.name)

    registry = CapabilityRegistry(make_settings(), [lambda _: _Closing()])
    await registry.warm_up()
    await registry.aclose()
    assert closed == ["echo"]


@pytest.mark.asyncio
async def test_a_capability_that_fails_to_close_does_not_stop_the_others() -> None:
    """Shutdown is best effort; one bad close does not strand the rest."""
    closed: list[str] = []

    class _Rude(EchoCapability):
        async def aclose(self) -> None:
            message = "this capability will not let go"
            raise RuntimeError(message)

    class _Polite(TallyCapability):
        async def aclose(self) -> None:
            closed.append(self.descriptor.name)

    registry = CapabilityRegistry(
        make_settings(),
        [lambda _: _Rude(), lambda _: _Polite()],
    )
    await registry.warm_up()
    await registry.aclose()
    assert closed == ["tally"]


def test_register_adds_to_what_the_composition_root_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registration is the whole of what adding a capability changes.

    Args:
        monkeypatch: Used to give this test its own catalogue. The real one is
            process-wide by design — a capability registers itself when its
            module is imported — so a test that appended to it would leak into
            every other.
    """
    monkeypatch.setattr(registry_module, "_FACTORIES", [])

    @register
    def _registered(settings: Settings) -> CapabilityPort:
        """Build the echo capability.

        Args:
            settings: The settings in effect, unused.

        Returns:
            The capability.
        """
        del settings
        return EchoCapability()

    assert registered_factories() == (_registered,)


@pytest.mark.asyncio
async def test_a_registry_with_no_capabilities_is_still_ready() -> None:
    """This build ships no capability, and it still serves sessions."""
    registry = CapabilityRegistry(make_settings(), [])
    await registry.warm_up()
    assert registry.ready is True
    assert registry.supported() == ()
    assert registry.health() == ()


@pytest.mark.asyncio
async def test_closing_a_registry_skips_a_capability_that_never_existed() -> None:
    """A factory that raised left nothing to close, and that is not a failure."""
    registry = CapabilityRegistry(make_settings(), [_broken_constructor, _echo])
    await registry.warm_up()
    await registry.aclose()
    assert [entry.state for entry in registry.health()] == [
        CapabilityState.UNHEALTHY,
        CapabilityState.READY,
    ]


@pytest.mark.asyncio
async def test_the_default_lifecycle_hooks_leave_the_capability_usable() -> None:
    """A capability holding no model inherits both hooks and overrides neither."""
    capability = TallyCapability()
    await capability.warm_up()
    await capability.aclose()
    assert capability.descriptor == TALLY
