"""The seam between the session and the work a session carries.

Everything the transport, the session layer and the pipeline know about a
capability is declared here, and nothing here names a capability. That is the
whole of what groundstation REQ-022 asks for in mechanical terms: routing is by
name against a registry, so a capability is added by implementing
`CapabilityPort` and registering it, and no file under `api/`, `session/` or
`pipeline/` changes.

The module sits beside those packages rather than inside `capabilities/`
deliberately. `just lint-capability-boundary` forbids the three of them from
importing `reachy_groundstation.capabilities` at all — a rule that is only
enforceable if the types they legitimately need live somewhere else. A port
declared here and satisfied structurally over there is what inverts that
dependency.

`DecodedFrame` is the other half of the seam. A frame is decoded once per frame
and the same array is handed to every agreed capability, so the decoded form is
a shared value type rather than something the pipeline keeps to itself. It never
crosses the wire, which is why it is a dataclass over a numpy array and not a
`reachy_contracts` model.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import numpy as np
import numpy.typing as npt

if TYPE_CHECKING:
    from reachy_contracts import Capability, CapabilityName, FrameHeader, WireModel

__all__ = [
    "AgreedCapability",
    "CapabilityHealth",
    "CapabilityPort",
    "CapabilityRegistryPort",
    "CapabilityState",
    "DecodedFrame",
    "ImageArray",
]

# `numpy.typing` is imported at run time rather than under `TYPE_CHECKING`: the
# alias below is lazy, so anything that evaluates it — `typing.get_type_hints`
# over `DecodedFrame`, for one — would otherwise raise `NameError` on a name that
# only existed for the type checker.
#
# The decoded frame's pixels: an 8-bit array as OpenCV produces it, which is
# height by width by three colour channels. The alias exists so that a
# capability's signature says what it receives without repeating numpy's
# spelling of it, and so that a future change of decoder has one place to state
# what it hands over.
type ImageArray = npt.NDArray[np.uint8]


# `eq=False` because two decoded frames are never usefully compared: numpy's
# `==` on the image would produce an array, and a dataclass `__eq__` would then
# raise on its truthiness. Frames are identified by their sequence number.
@dataclass(frozen=True, slots=True, eq=False)
class DecodedFrame:
    """One frame, decoded once, shared by every capability agreed for a session.

    Attributes:
        header: The frame's sequence number and its opaque capture token. Both
            are copied onto every result; neither is interpreted here.
        image: The decoded pixels.
    """

    header: FrameHeader
    image: ImageArray

    @property
    def sequence(self) -> int:
        """The frame's number within its session.

        Returns:
            The sequence number every result for this frame answers.
        """
        return self.header.sequence

    @property
    def height(self) -> int:
        """The decoded frame's height in pixels.

        Returns:
            The number of pixel rows.
        """
        return int(self.image.shape[0])

    @property
    def width(self) -> int:
        """The decoded frame's width in pixels.

        Returns:
            The number of pixel columns.
        """
        return int(self.image.shape[1])


@dataclass(frozen=True, slots=True)
class AgreedCapability:
    """One capability, under the name a session agreed to route to it by.

    The name travels beside the capability rather than being read back off it.
    `descriptor` is a property on third-party code: the registry reads it once,
    inside the guard that contains a capability's failures, and everything after
    that uses the value it read. Asking again on every frame would put that
    read outside the containment groundstation REQ-025 requires — a property
    that starts raising mid-session would take down the session's pipeline
    rather than costing one capability its answer.

    Attributes:
        name: What negotiation agreed to call it.
        capability: What answers frames under that name.
    """

    name: str
    capability: CapabilityPort


class CapabilityState(StrEnum):
    """Where a capability is in its lifecycle.

    `DISABLED` and `UNHEALTHY` are both "offered to nobody" and are deliberately
    not the same answer. A disabled capability is one this deployment switched
    off; an unhealthy one is one that tried and failed. Reporting the first as
    the second would send an operator looking for a fault that is a setting, and
    reporting the second as the first would hide a real one.

    Attributes:
        WARMING: Built, but its warm-up has not finished yet.
        READY: Warmed up and offered during negotiation.
        DISABLED: Switched off by configuration; never built, never offered.
        UNHEALTHY: It failed to build or to warm up, and is offered to nobody.
    """

    WARMING = "warming"
    READY = "ready"
    DISABLED = "disabled"
    UNHEALTHY = "unhealthy"


@dataclass(frozen=True, slots=True)
class CapabilityHealth:
    """What the health surface reports about one capability.

    Attributes:
        name: The capability's name, as negotiation uses it.
        version: The revision this build implements, or `None` when the
            capability failed before it could say.
        state: Where it is in its lifecycle.
        detail: Why it is unhealthy, when it is. Empty otherwise.
    """

    name: str
    version: int | None
    state: CapabilityState
    detail: str = ""


#:= docs/specs/groundstation/index.md#req-022-capabilities-register-without-transport-changes
#:% Adding a capability MUST NOT require modification to the session layer, the
#:% transport, or any other capability.
@runtime_checkable
class CapabilityPort(Protocol):
    """What the pipeline requires of anything it routes a frame to.

    A capability is an interface plus a registration, and this is the interface.
    It says nothing about models, files or threads: those are the concern of the
    capability that needs them, which is what keeps the pipeline able to grow a
    second and a third capability without growing knowledge of any of them.
    """

    @property
    def descriptor(self) -> Capability:
        """The name and version this capability negotiates under.

        Returns:
            The capability's wire identity.
        """
        ...

    async def warm_up(self) -> None:
        """Do whatever must happen before the first frame is cheap.

        Readiness is reported only once this has returned for every capability
        the service will offer, so an implementation that loads a model does it
        here rather than on the first frame.
        """
        ...

    async def process(self, frame: DecodedFrame) -> WireModel:
        """Answer one frame.

        Args:
            frame: The decoded frame, shared with every other agreed capability.
                Implementations MUST treat the image as read-only.

        Returns:
            This capability's payload for the frame. A payload carrying no
            detections is an ordinary successful answer, not a failure.
        """
        ...

    async def aclose(self) -> None:
        """Release whatever the capability holds."""
        ...


class CapabilityRegistryPort(Protocol):
    """What the session layer requires of the thing it negotiates against.

    The session layer holds one of these and never learns where its members came
    from. Negotiation is performed against `supported()` once per session and is
    never cached across reconnections, because a groundstation that restarted
    with a different capability set is an ordinary case rather than an edge one.
    """

    @property
    def ready(self) -> bool:
        """Whether every capability the service will offer has warmed up.

        Returns:
            True once warm-up has finished, however it finished.
        """
        ...

    def supported(self) -> tuple[Capability, ...]:
        """The capabilities this service can currently speak.

        Returns:
            The ready capabilities, in a stable order. One that failed to
            initialise is absent, which is how the service keeps serving the
            rest.
        """
        ...

    def get(self, name: CapabilityName) -> CapabilityPort | None:
        """Look a capability up by the name negotiation agreed on.

        Args:
            name: The capability's name.

        Returns:
            The capability, or `None` when this build cannot route that name.
        """
        ...

    def health(self) -> tuple[CapabilityHealth, ...]:
        """Report every capability, including the ones that failed.

        Returns:
            One entry per capability the service tried to build.
        """
        ...
