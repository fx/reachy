"""The bounded per-session queue that drops the oldest frame under pressure.

Robot link REQ-015 says what this has to do: when frames arrive faster than they
can be processed, the oldest unprocessed frame is discarded rather than the queue
growing or the producer blocking. `asyncio.Queue` does neither of those things —
it blocks the producer — so the queue is a deque with an event, which is the
smallest thing that has the required behaviour.

Which frame is "oldest" is decided by arrival order at this end of the link. The
frame's capture token is not consulted: it belongs to the robot's clock, this
service has no clock to compare it against, and inventing a comparison would be
inventing exactly the cross-machine time base the protocol avoids.

A drop increments a counter and writes no log line. Drops happen when the service
is already saturated, and per-occurrence logging would add load at the worst
possible moment.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reachy_contracts import FrameHeader

__all__ = ["FrameQueue", "QueueClosedError", "QueuedFrame"]


@dataclass(frozen=True, slots=True)
class QueuedFrame:
    """One frame waiting to be processed.

    Attributes:
        header: The frame's sequence number and its opaque capture token.
        payload: The compressed frame, untouched since the capture hardware
            produced it.
        received_at: When this end of the link took delivery, on this service's
            own monotonic clock. Purely local, and the only clock reading
            involved in handling a frame.
    """

    header: FrameHeader
    payload: bytes
    received_at: float


class QueueClosedError(Exception):
    """The queue was closed and holds nothing more."""


#:= docs/specs/robot-link/index.md#req-015-overload-drops-frames-rather-than-queueing-them
#:% When frames arrive faster than they can be processed, the oldest unprocessed
#:% frame MUST be discarded in preference to growing the queue or blocking the
#:% producer.
class FrameQueue:
    """A bounded queue with one consumer, which drops oldest when it is full."""

    def __init__(self, bound: int) -> None:
        """Create an empty queue.

        Args:
            bound: The most frames that may wait at once. Must be at least one.

        Raises:
            ValueError: If the bound is not at least one.
        """
        if bound < 1:
            message = f"a queue bound of {bound} would hold nothing"
            raise ValueError(message)
        self._bound = bound
        self._items: deque[QueuedFrame] = deque()
        self._arrived = asyncio.Event()
        self._closed = False

    @property
    def bound(self) -> int:
        """The most frames that may wait at once.

        Returns:
            The bound this queue was created with.
        """
        return self._bound

    def __len__(self) -> int:
        """Count the frames waiting.

        Returns:
            How many frames are queued right now.
        """
        return len(self._items)

    def put(self, frame: QueuedFrame) -> int:
        """Accept a frame, discarding the oldest if there is no room.

        The producer is never blocked and never told to slow down: a robot that
        had to wait for the groundstation would be spending its own scarce cores
        on the wait.

        Args:
            frame: The frame that just arrived.

        Returns:
            How many frames this put discarded — zero or one, and the caller
            counts it.
        """
        dropped = 0
        while len(self._items) >= self._bound:
            self._items.popleft()
            dropped += 1
        self._items.append(frame)
        self._arrived.set()
        return dropped

    async def get(self) -> QueuedFrame:
        """Take the oldest waiting frame, waiting for one if necessary.

        Returns:
            The frame that has been waiting longest.

        Raises:
            QueueClosedError: If the queue was closed and is empty.
        """
        while True:
            if self._items:
                return self._items.popleft()
            if self._closed:
                raise QueueClosedError
            self._arrived.clear()
            await self._arrived.wait()

    def close(self) -> None:
        """Stop accepting waiters, so a consumer parked in `get` can finish."""
        self._closed = True
        self._arrived.set()
