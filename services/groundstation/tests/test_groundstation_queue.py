"""The bounded queue, filled until it drops.

A test that sets a bound and never reaches it proves nothing about what happens
when it is reached, so every test here fills the queue. Nothing sleeps: the
consumer is resumed by awaiting the queue, not by waiting for a clock.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import asyncio

import pytest
from groundstation_support import hand_control_to_the_event_loop, make_header

from reachy_groundstation.pipeline.queue import (
    FrameQueue,
    QueueClosedError,
    QueuedFrame,
)


def _frame(sequence: int) -> QueuedFrame:
    """Build a queued frame with a distinguishable sequence number.

    Args:
        sequence: The frame's number.

    Returns:
        The queued frame.
    """
    return QueuedFrame(
        header=make_header(sequence),
        payload=b"jpeg",
        received_at=float(sequence),
    )


def test_a_bound_below_one_is_refused() -> None:
    """A queue that can hold nothing is a configuration mistake."""
    with pytest.raises(ValueError, match="would hold nothing"):
        FrameQueue(0)


def test_a_put_below_the_bound_drops_nothing() -> None:
    """Backpressure is what happens at the bound, not before it."""
    queue = FrameQueue(3)
    assert queue.put(_frame(0)) == 0
    assert len(queue) == 1


#:= docs/specs/robot-link/index.md#req-015-overload-drops-frames-rather-than-queueing-them
#:% When frames arrive faster than they can be processed, the oldest unprocessed
#:% frame MUST be discarded in preference to growing the queue or blocking the
#:% producer.
def test_the_oldest_frame_is_the_one_discarded() -> None:
    """Filling a bound-two queue with three frames loses the first."""
    queue = FrameQueue(2)
    assert queue.put(_frame(1)) == 0
    assert queue.put(_frame(2)) == 0
    assert queue.put(_frame(3)) == 1
    assert len(queue) == 2


def test_the_queue_never_grows_past_its_bound() -> None:
    """Ten frames into a bound of two leaves two, not ten."""
    queue = FrameQueue(2)
    dropped = sum(queue.put(_frame(sequence)) for sequence in range(10))
    assert len(queue) == 2
    assert dropped == 8


@pytest.mark.asyncio
async def test_the_most_recent_frame_survives_the_overload() -> None:
    """Dropping oldest is only useful if the newest is still processed."""
    queue = FrameQueue(2)
    for sequence in range(5):
        queue.put(_frame(sequence))
    first = await queue.get()
    second = await queue.get()
    assert (first.header.sequence, second.header.sequence) == (3, 4)


@pytest.mark.asyncio
async def test_a_waiting_consumer_is_woken_by_a_put() -> None:
    """The consumer parks on the queue rather than polling it."""
    queue = FrameQueue(2)
    getter = asyncio.ensure_future(queue.get())
    await hand_control_to_the_event_loop(1)
    queue.put(_frame(11))
    assert (await getter).header.sequence == 11


@pytest.mark.asyncio
async def test_closing_an_empty_queue_releases_the_consumer() -> None:
    """A session that ends must not leave its pipeline parked forever."""
    queue = FrameQueue(2)
    getter = asyncio.ensure_future(queue.get())
    await hand_control_to_the_event_loop(1)
    queue.close()
    with pytest.raises(QueueClosedError):
        await getter


@pytest.mark.asyncio
async def test_closing_a_full_queue_still_yields_what_it_holds() -> None:
    """Frames already accepted are answered before the pipeline stops."""
    queue = FrameQueue(2)
    queue.put(_frame(1))
    queue.close()
    assert (await queue.get()).header.sequence == 1
    with pytest.raises(QueueClosedError):
        await queue.get()


def test_the_bound_is_readable() -> None:
    """The queue reports what it was configured with."""
    assert FrameQueue(7).bound == 7
