"""A starting point for a capability, and no obligation to use it.

`CapabilityPort` in `reachy_groundstation.ports` is the whole contract, and it is
a `Protocol`: anything with the right shape is a capability, whether or not it
inherits from anything here. This base class exists because most capabilities
want the same two defaults — a warm-up that does nothing and a close that does
nothing — and writing them out again in each one is how they drift.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reachy_contracts import Capability, WireModel
    from reachy_groundstation.ports import DecodedFrame

__all__ = ["CapabilityBase"]


class CapabilityBase(ABC):
    """A capability that answers frames, with the lifecycle hooks defaulted."""

    def __init__(self, descriptor: Capability) -> None:
        """Record what this capability negotiates as.

        Args:
            descriptor: The name and version this capability speaks under.
        """
        self._descriptor = descriptor

    @property
    def descriptor(self) -> Capability:
        """The name and version this capability negotiates under.

        Returns:
            The capability's wire identity.
        """
        return self._descriptor

    async def warm_up(self) -> None:
        """Do whatever must happen before the first frame is cheap.

        The default does nothing, which is right for a capability that holds no
        model. One that does overrides this rather than loading lazily, so that
        readiness means what groundstation REQ-026 says it means.
        """
        return

    @abstractmethod
    async def process(self, frame: DecodedFrame) -> WireModel:
        """Answer one frame.

        Args:
            frame: The decoded frame, shared with every other agreed
                capability, and to be treated as read-only.

        Returns:
            This capability's payload for the frame, which may carry no
            detections.
        """

    async def aclose(self) -> None:
        """Release whatever the capability holds.

        The default does nothing.
        """
        return
