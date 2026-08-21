"""Taking a timing: warm up untimed, then measure, then summarise.

One function, and the two decisions in it are worth stating.

**The warm-up passes are untimed and are not in the distribution.** The first
pass through anything here allocates arenas, plans kernels and fills caches, and
including it would put a number in the sample set that describes the first
frame after a restart rather than the steady state the baseline is about. The
cold cost is worth measuring; it is measured on its own, by the benchmarks that
care about it, rather than smuggled into every median.

**The clock is `time.perf_counter` and it is an argument.** The default is the
highest-resolution monotonic clock the interpreter offers, which is what a
sub-millisecond stage needs; passing a fake is what lets this module's own tests
assert on the arithmetic without spending the time they would otherwise measure.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from reachy_bench.stats import Distribution

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

__all__ = ["measure", "measure_async"]


def measure(
    work: Callable[[], object],
    *,
    iterations: int,
    warmup: int = 0,
    clock: Callable[[], float] = time.perf_counter,
) -> Distribution:
    """Run something repeatedly and summarise how long it took.

    Args:
        work: What to time. Its return value is discarded, which is deliberate:
            a benchmark that accumulated results would be measuring the
            accumulation too.
        iterations: How many timed passes to take.
        warmup: How many untimed passes to take first.
        clock: A monotonic source, in seconds.

    Returns:
        The distribution of the timed passes, in milliseconds.

    Raises:
        ValueError: If fewer than one timed pass was asked for, or if the
            warm-up count is negative. A measurement of nothing is not a fast
            measurement.
    """
    if iterations < 1:
        message = f"a timing takes at least one pass, not {iterations}"
        raise ValueError(message)
    if warmup < 0:
        message = f"a warm-up count is not negative: {warmup}"
        raise ValueError(message)
    for _ in range(warmup):
        work()
    samples: list[float] = []
    for _ in range(iterations):
        started = clock()
        work()
        samples.append(clock() - started)
    return Distribution.of_seconds(samples)


async def measure_async(
    work: Callable[[], Awaitable[object]],
    *,
    iterations: int,
    warmup: int = 0,
    clock: Callable[[], float] = time.perf_counter,
) -> Distribution:
    """Run an awaitable repeatedly and summarise how long it took.

    The synchronous twin above, for the stages that are coroutines: a capability
    answering a frame, and the pipeline answering one end to end. Timing them
    inside one event loop rather than starting one per pass is the difference
    between measuring the stage and measuring `asyncio.run`.

    Args:
        work: What to time, as a nullary factory producing the awaitable. A
            factory rather than the awaitable itself, because a coroutine can
            only be awaited once and this awaits many.
        iterations: How many timed passes to take.
        warmup: How many untimed passes to take first.
        clock: A monotonic source, in seconds.

    Returns:
        The distribution of the timed passes, in milliseconds.

    Raises:
        ValueError: If fewer than one timed pass was asked for, or if the
            warm-up count is negative.
    """
    if iterations < 1:
        message = f"a timing takes at least one pass, not {iterations}"
        raise ValueError(message)
    if warmup < 0:
        message = f"a warm-up count is not negative: {warmup}"
        raise ValueError(message)
    for _ in range(warmup):
        await work()
    samples: list[float] = []
    for _ in range(iterations):
        started = clock()
        await work()
        samples.append(clock() - started)
    return Distribution.of_seconds(samples)
