"""Taking a timing: the warm-up, the sample count, and what is not in the sample.

Two functions, and what has to hold of both is that the untimed passes really
are untimed. A warm-up folded into the distribution would put the first pass
after a restart into every median, which is the opposite of what the recorded
baseline is about.

The clock is a fake that advances by a fixed amount per call, so these assert on
the arithmetic without spending the time they would otherwise measure. Nothing
here sleeps.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import pytest

from reachy_bench.timing import measure, measure_async


class Clock:
    """A monotonic source that advances a fixed amount every time it is read.

    Attributes:
        reads: How many times it has been asked the time.
    """

    def __init__(self, step: float = 0.001) -> None:
        """Start the clock at zero.

        Args:
            step: How far it advances per read, in seconds.
        """
        self.reads = 0
        self._step = step
        self._now = 0.0

    def __call__(self) -> float:
        """Read the clock.

        Returns:
            The time, having advanced it.
        """
        self.reads += 1
        now = self._now
        self._now += self._step
        return now


def test_every_timed_pass_is_measured_and_the_warm_up_passes_are_not() -> None:
    """A warm-up in the distribution would describe a cold start, every time."""
    calls: list[str] = []

    distribution = measure(
        lambda: calls.append("pass"),
        iterations=3,
        warmup=2,
        clock=Clock(),
    )

    assert len(calls) == 5
    assert distribution.samples == 3


def test_a_timing_is_the_span_between_two_reads_of_the_clock() -> None:
    """One step of the fake clock, which is one millisecond."""
    distribution = measure(lambda: None, iterations=4, clock=Clock(step=0.001))

    assert distribution.median_ms == pytest.approx(1.0)
    assert distribution.min_ms == pytest.approx(1.0)
    assert distribution.max_ms == pytest.approx(1.0)


def test_a_timing_of_no_passes_is_refused() -> None:
    """A measurement of nothing is not a fast measurement."""
    with pytest.raises(ValueError, match="at least one pass"):
        measure(lambda: None, iterations=0)


def test_a_negative_warm_up_count_is_refused() -> None:
    """It would silently mean no warm-up, which is a different measurement."""
    with pytest.raises(ValueError, match="not negative"):
        measure(lambda: None, iterations=1, warmup=-1)


@pytest.mark.asyncio
async def test_an_awaitable_is_timed_the_same_way() -> None:
    """The stages that are coroutines get the same treatment as the rest."""
    calls: list[str] = []

    async def _work() -> None:
        """Stand in for a capability answering a frame."""
        calls.append("pass")

    distribution = await measure_async(
        _work,
        iterations=3,
        warmup=1,
        clock=Clock(),
    )

    assert len(calls) == 4
    assert distribution.samples == 3
    assert distribution.median_ms == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_an_asynchronous_timing_of_no_passes_is_refused() -> None:
    """The same rule as its synchronous twin."""

    async def _work() -> None:
        """Never called."""
        message = "a refused timing must not run its work"
        raise AssertionError(message)

    with pytest.raises(ValueError, match="at least one pass"):
        await measure_async(_work, iterations=0)


@pytest.mark.asyncio
async def test_a_negative_asynchronous_warm_up_count_is_refused() -> None:
    """The same rule as its synchronous twin."""

    async def _work() -> None:
        """Never called."""
        message = "a refused timing must not run its work"
        raise AssertionError(message)

    with pytest.raises(ValueError, match="not negative"):
        await measure_async(_work, iterations=1, warmup=-1)
