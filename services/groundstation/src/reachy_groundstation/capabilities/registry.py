"""What is registered, what warmed up, and what is offered as a result.

Registration is a decorator on a factory rather than a table of names, so a
capability declares itself in its own module and no shared list has to be edited
in step with it. `registered_factories` is what the composition root builds from;
a test passes its own factories instead, which is how the registry's behaviour is
exercised without any capability existing.

Two guarantees live here.

**A capability that fails does not take the service down.** Building one is
wrapped, warming one up is wrapped and bounded by a timeout, and either failure
records the capability as unhealthy and leaves everything else serving. That is
groundstation REQ-025, and it is the reason the registry builds every capability
before warming any of them: a constructor that raised would otherwise stop the
ones after it from being attempted at all.

**Readiness means warm-up finished.** `ready` is false until `warm_up` has
returned for every capability, however each of them turned out, which is what
lets the readiness endpoint hold an orchestrator off until the first inference
would not be slow.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from reachy_groundstation.obs import get_logger
from reachy_groundstation.ports import (
    CapabilityHealth,
    CapabilityPort,
    CapabilityState,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from reachy_contracts import Capability, CapabilityName
    from reachy_groundstation.config import Settings

__all__ = [
    "CapabilityFactory",
    "CapabilityRegistry",
    "register",
    "registered_factories",
]

_logger = get_logger(__name__)

# What a capability module registers: something that builds the capability from
# the settings in effect. A factory rather than an instance, because a capability
# that holds a model should not construct one at import time.
type CapabilityFactory = Callable[[Settings], CapabilityPort]

_FACTORIES: list[CapabilityFactory] = []


#:= docs/specs/groundstation/index.md#req-022-capabilities-register-without-transport-changes
#:% Adding a capability MUST NOT require modification to the session layer, the
#:% transport, or any other capability.
def register(factory: CapabilityFactory) -> CapabilityFactory:
    """Declare a capability the service offers.

    Args:
        factory: Builds the capability from the settings in effect.

    Returns:
        The factory, unchanged, so this reads as a decorator.
    """
    _FACTORIES.append(factory)
    return factory


def registered_factories() -> tuple[CapabilityFactory, ...]:
    """List everything registered so far.

    Returns:
        The factories, in registration order.
    """
    return tuple(_FACTORIES)


class _Entry:
    """One capability and what became of it.

    Attributes:
        name: What it is known as, which is its own name when it got far enough
            to have one and the factory's name otherwise.
        capability: The built capability, or `None` when building failed.
        state: Where it is in its lifecycle.
        detail: Why it is unhealthy, when it is.
    """

    __slots__ = ("capability", "detail", "name", "state")

    def __init__(
        self,
        name: str,
        capability: CapabilityPort | None,
        state: CapabilityState,
        detail: str = "",
    ) -> None:
        """Record one capability's fate.

        Args:
            name: What it is known as.
            capability: The built capability, or `None`.
            state: Where it is in its lifecycle.
            detail: Why it is unhealthy, when it is.
        """
        self.name = name
        self.capability = capability
        self.state = state
        self.detail = detail

    def health(self) -> CapabilityHealth:
        """Render this entry for the health surface.

        Returns:
            What the health endpoint reports about this capability.
        """
        return CapabilityHealth(
            name=self.name,
            version=(
                self.capability.descriptor.version
                if self.capability is not None
                else None
            ),
            state=self.state,
            detail=self.detail,
        )


#:= docs/specs/groundstation/index.md#req-025-a-failed-capability-does-not-take-down-the-service
#:% When a capability fails to initialise, the service MUST continue serving the
#:% capabilities that initialised successfully.
class CapabilityRegistry:
    """Holds the capabilities, their health, and what may be offered."""

    def __init__(
        self,
        settings: Settings,
        factories: Sequence[CapabilityFactory] | None = None,
    ) -> None:
        """Build every registered capability, surviving the ones that fail.

        Args:
            settings: The settings in effect, handed to each factory.
            factories: What to build. Defaults to everything registered, which
                is what the composition root wants; a test passes its own.
        """
        self._settings = settings
        self._entries: list[_Entry] = []
        self._ready = False
        for factory in registered_factories() if factories is None else factories:
            self._entries.append(self._build(factory))

    def _build(self, factory: CapabilityFactory) -> _Entry:
        """Build one capability, recording a failure rather than raising it.

        Args:
            factory: What to build.

        Returns:
            The entry for it, warming or unhealthy.
        """
        label = getattr(factory, "__name__", repr(factory))
        try:
            capability = factory(self._settings)
        except Exception as error:
            _logger.error("capability.build_failed", factory=label, error=repr(error))
            return _Entry(label, None, CapabilityState.UNHEALTHY, repr(error)[:500])
        return _Entry(capability.descriptor.name, capability, CapabilityState.WARMING)

    #:= docs/specs/groundstation/index.md#req-026-readiness-is-distinct-from-liveness
    #:% The service MUST report itself ready only once every capability it will offer
    #:% has completed its warm-up.
    async def warm_up(self) -> None:
        """Warm every capability up, then declare the service ready.

        A capability that fails or overruns is recorded as unhealthy and is
        offered to nobody; the rest are unaffected.
        """
        for entry in self._entries:
            if entry.capability is None:
                continue
            try:
                await asyncio.wait_for(
                    entry.capability.warm_up(),
                    timeout=self._settings.warm_up_timeout_seconds,
                )
            except Exception as error:
                _logger.error(
                    "capability.warm_up_failed",
                    capability=entry.name,
                    error=repr(error),
                )
                entry.state = CapabilityState.UNHEALTHY
                entry.detail = repr(error)[:500]
            else:
                entry.state = CapabilityState.READY
                _logger.info("capability.ready", capability=entry.name)
        self._ready = True

    @property
    def ready(self) -> bool:
        """Whether warm-up has finished for every capability.

        Returns:
            True once `warm_up` has returned.
        """
        return self._ready

    def supported(self) -> tuple[Capability, ...]:
        """The capabilities that may be offered during negotiation.

        Returns:
            The descriptors of the ready capabilities, in registration order.
        """
        return tuple(
            entry.capability.descriptor
            for entry in self._entries
            if entry.capability is not None and entry.state is CapabilityState.READY
        )

    def get(self, name: CapabilityName) -> CapabilityPort | None:
        """Look a ready capability up by name.

        Args:
            name: The name negotiation agreed on.

        Returns:
            The capability, or `None` when nothing ready answers to that name.
        """
        for entry in self._entries:
            if (
                entry.capability is not None
                and entry.state is CapabilityState.READY
                and entry.capability.descriptor.name == name
            ):
                return entry.capability
        return None

    def health(self) -> tuple[CapabilityHealth, ...]:
        """Report every capability, including the ones that failed.

        Returns:
            One entry per capability the service tried to build.
        """
        return tuple(entry.health() for entry in self._entries)

    async def aclose(self) -> None:
        """Close every capability that was built, ignoring what they raise."""
        for entry in self._entries:
            if entry.capability is None:
                continue
            try:
                await entry.capability.aclose()
            except Exception as error:
                _logger.warning(
                    "capability.close_failed",
                    capability=entry.name,
                    error=repr(error),
                )
