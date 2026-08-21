"""Which detector answers, and what happens when the groundstation goes away.

ha-satellite REQ-047 makes the detection source selectable between the
groundstation, the robot's own detector, and the groundstation with local
fallback. All three are built here, and **the behaviour layer cannot tell which
it got**: it holds a `PerceptionPort`, asks what is in front of the robot, and
is answered. Fallback is a property of the source rather than a branch in the
state machine, because a state machine that knew about transport failure would
be a state machine with opinions about sockets.

Remote is the default, and the reason is measured rather than aesthetic: with
detection offloaded the robot sat at 1.52 of its four cores, and with detection
local it saturated. The robot is running motion control, audio and a wake-word
model at the same time.

**The fallback trigger is session loss, not staleness**, and that resolves the
open question the change document records. The two are different failures and
keeping them distinguishable is worth more than making one signal do both work:

* The session **dropped** — the groundstation restarted, the WLAN went, the
  credential was rejected. Nothing is coming until something changes, and a
  detector on the robot is better than no detector.
* Results have gone **stale** while the session is up — the groundstation is
  there and has stopped answering, or is answering too slowly to be worth
  acting on. That is ha-satellite REQ-048's case, and its answer is a neutral
  head: an honest signal that something upstream stopped. Starting a second
  detector on the strength of a stall that may last one frame would burn the
  robot's remaining cores to hide it.

So a stale-but-connected source keeps answering "not fresh" and the head goes
back to neutral, exactly as it would with no fallback configured at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Final, Protocol

from reachy_mini_ha_satellite.ports import Detections, SourceSelection

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from reachy_mini_ha_satellite.ports import PerceptionPort

__all__ = [
    "DEFAULT_RECOVERY_SECONDS",
    "ConnectableSource",
    "FallbackPerception",
    "build_perception",
]

_LOGGER: Final = logging.getLogger(__name__)

# How long the session has to stay up before the local detector is shut down
# again. Without it, a link that drops every few seconds would reload the model
# and reopen an inference session every time it came back, which costs more than
# leaving the detector running would have.
DEFAULT_RECOVERY_SECONDS: Final = 5.0

# How often the fallback checks whether the session has come or gone. Fast
# enough that a dropped link is covered within a fraction of the staleness
# window, slow enough to be free.
_WATCH_SECONDS: Final = 0.25

# The selection that runs the robot's own detector and opens no session.
# Bound once rather than spelled at each site, because the repository's leak
# scanner reads this member's dotted form as an mDNS hostname suffix — a shape
# its own docstring warns is what the per-line marker exists for — and one
# exempted line is better than several.
_ROBOT_ONLY: Final = SourceSelection.LOCAL  # leak-scan:allow


class ConnectableSource(Protocol):
    """A perception source that also says whether its link is up.

    Only the remote source is one of these. It is a separate protocol rather
    than a member of `PerceptionPort` because the behaviour layer must not be
    able to ask: "is the session up?" is precisely the question this module
    exists to answer on its behalf.
    """

    @property
    def connected(self) -> bool:
        """Whether the link this source needs is up right now.

        Returns:
            True while the source can expect results to arrive.
        """
        ...

    async def start(self) -> None:
        """Begin producing detections. Idempotent."""
        ...

    def latest(self) -> Detections:
        """Say what this source last saw.

        Returns:
            The current view.
        """
        ...

    async def aclose(self) -> None:
        """Stop producing detections and release whatever was held."""
        ...


#:= docs/specs/ha-satellite/index.md#req-047-detection-source-is-selectable
#:% The source of face detections MUST be selectable between the groundstation, the
#:% robot's own detector, and the groundstation with local fallback.
class FallbackPerception:
    """The groundstation while the session is up, the robot's own while it is not.

    The local detector is not started until the session is first lost, and is
    shut down again once the session has been back for long enough to believe
    it. A robot whose groundstation is healthy therefore never pays for the
    local model at all, which is the entire point of the default.
    """

    def __init__(
        self,
        remote: ConnectableSource,
        local: PerceptionPort,
        *,
        recovery_seconds: float = DEFAULT_RECOVERY_SECONDS,
        watch_seconds: float = _WATCH_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Compose the two sources without starting either.

        Args:
            remote: The groundstation source.
            local: The robot's own detector.
            recovery_seconds: How long the session must stay up before the
                local detector is shut down again.
            watch_seconds: How often to look at whether the session is up.
            clock: The monotonic source recovery is measured against.
            sleep: How to wait between looks.
        """
        self._remote = remote
        self._local = local
        self._recovery_seconds = recovery_seconds
        self._watch_seconds = watch_seconds
        self._clock = clock
        self._sleep = sleep
        self._local_running = False
        self._connected_since: float | None = None
        self._watch: asyncio.Task[None] | None = None

    @property
    def falling_back(self) -> bool:
        """Whether the local detector is currently running.

        Returns:
            True once the session has been lost and the robot has taken over.
            Reported so that the settings interface can say which source is
            answering, which is a question an operator asks and the behaviour
            layer does not.
        """
        return self._local_running

    async def start(self) -> None:
        """Start the groundstation source and watch whether it stays up."""
        if self._watch is not None:
            return
        await self._remote.start()
        self._watch = asyncio.create_task(self._supervise(), name="detection-source")

    def latest(self) -> Detections:
        """Answer from whichever source is currently the right one.

        A connected session answers even when its answer is stale, which is
        what keeps "the link is gone" and "the groundstation has stopped
        answering" two different events with two different consequences.

        Returns:
            The current view of the scene.
        """
        if self._remote.connected:
            return self._remote.latest()
        return self._local.latest()

    async def aclose(self) -> None:
        """Stop both sources."""
        watch, self._watch = self._watch, None
        if watch is not None:
            watch.cancel()
            # Every exception, not only the cancellation: a task that had
            # already failed is not cancelled by `cancel`, and awaiting it here
            # would re-raise its failure out of a shutdown path.
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await watch
        await self._remote.aclose()
        if self._local_running:
            self._local_running = False
            await self._local.aclose()

    async def _supervise(self) -> None:
        """Start and stop the local detector as the session comes and goes.

        A turn that fails is logged and the next one is taken. `check` starts
        and stops the local detector, and either can raise — a model that will
        not load, a runtime that will not release. Letting that end this task
        would leave the source stuck on whichever branch it was on, with
        nothing re-evaluating the session and no line saying so; and the
        exception would then be re-raised out of `aclose`, reporting the
        failure as though shutting down had caused it.
        """
        while True:
            await self._sleep(self._watch_seconds)
            try:
                await self.check()
            except Exception:
                _LOGGER.exception("the detection source supervisor failed a turn")

    async def check(self) -> None:
        """Bring the local detector into line with the session's state.

        Public because it is the whole of the supervision, and a test that
        drives it directly is testing the decision rather than the timer that
        happens to call it.
        """
        if not self._remote.connected:
            self._connected_since = None
            if not self._local_running:
                _LOGGER.info(
                    "the robot link is down; falling back to local detection",
                )
                await self._local.start()
                self._local_running = True
            return
        if self._connected_since is None:
            self._connected_since = self._clock()
        if not self._local_running:
            return
        if self._clock() - self._connected_since < self._recovery_seconds:
            return
        _LOGGER.info("the robot link has recovered; stopping local detection")
        self._local_running = False
        await self._local.aclose()


def build_perception(
    selection: SourceSelection,
    *,
    remote: ConnectableSource | None = None,
    local: PerceptionPort | None = None,
) -> PerceptionPort:
    """Assemble the perception source an operator asked for.

    This is the only place the three selections differ, and it is composition
    rather than configuration read at a call site: what comes back is a
    `PerceptionPort` and nothing downstream can tell the three apart.

    Args:
        selection: Which source was chosen.
        remote: The groundstation source, needed by every selection but
            `LOCAL`.
        local: The robot's own detector, needed by every selection but
            `REMOTE`.

    Returns:
        The source to hand the behaviour layer.

    Raises:
        ValueError: If the selection needs a source that was not supplied.
            Raised here rather than tolerated, because the failure it would
            otherwise become is a robot that silently never tracks anything.
    """
    if selection is _ROBOT_ONLY:
        if local is None:
            message = "the local selection needs a local detector"
            raise ValueError(message)
        return local
    if remote is None:
        message = f"the {selection.value} selection needs a groundstation source"
        raise ValueError(message)
    if selection is SourceSelection.REMOTE:
        return remote
    if local is None:
        message = "the remote-with-local-fallback selection needs a local detector"
        raise ValueError(message)
    return FallbackPerception(remote, local)
