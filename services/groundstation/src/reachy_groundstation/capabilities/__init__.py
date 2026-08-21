"""The capabilities this service offers, and the registry that holds them.

A capability is an interface plus a registration. The interface is
`reachy_groundstation.ports.CapabilityPort`; the registration is `register`
below. Adding one means writing a module in this package, decorating its factory,
and importing that module from here — and touching nothing under `api/`,
`session/` or `pipeline/`, which is groundstation REQ-022 stated as a rule about
files.

That rule is enforced rather than documented: `just lint-capability-boundary`
fails the build if anything in those three packages imports this one. The
dependency runs the other way. They hold a `CapabilityRegistryPort` handed to
them by the composition root in `reachy_groundstation.service`, which is the only
module outside this package that names it.

**Perception is what ships here.** `perception/` holds face detection and gesture
recognition, and importing it is what registers them — which is why it is
imported below rather than merely available. A capability that is switched off by
configuration declines to be built, and the registry records that as its own
state rather than as a failure.
"""

from __future__ import annotations

from reachy_groundstation.capabilities import perception
from reachy_groundstation.capabilities.base import CapabilityBase
from reachy_groundstation.capabilities.registry import (
    CapabilityDisabledError,
    CapabilityFactory,
    CapabilityRegistry,
    register,
    registered_factories,
)

__all__ = [
    "CapabilityBase",
    "CapabilityDisabledError",
    "CapabilityFactory",
    "CapabilityRegistry",
    "perception",
    "register",
    "registered_factories",
]
