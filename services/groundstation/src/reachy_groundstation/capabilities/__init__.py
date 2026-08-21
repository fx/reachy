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

**No capability ships here yet, and that is the design.** Perception is change
0005. What ships in this change is the seam, exercised end to end by a test
capability that returns a fixed answer — which is what makes the registry,
routing and pipeline verifiable with no model anywhere.
"""

from __future__ import annotations

from reachy_groundstation.capabilities.base import CapabilityBase
from reachy_groundstation.capabilities.registry import (
    CapabilityFactory,
    CapabilityRegistry,
    register,
    registered_factories,
)

__all__ = [
    "CapabilityBase",
    "CapabilityFactory",
    "CapabilityRegistry",
    "register",
    "registered_factories",
]
