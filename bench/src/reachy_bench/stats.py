"""Distribution statistics: a median, a high percentile, and never a mean alone.

Benchmarks REQ-069 exists because of a specific failure. A stage that is usually
fast and occasionally very slow has a mean that looks fine, and the slow cases —
which are the ones a person in the room notices — are averaged away. So every
timing this suite reports carries a median and a 95th percentile, and the mean
travels beside them rather than instead of them.

**The percentile is nearest-rank, not interpolated.** Every value reported is
therefore a sample that was actually observed, which matters when a reviewer is
comparing two runs and wants to know whether a figure is a measurement or an
arithmetic artefact of two neighbouring ones. It also behaves predictably at the
small sample counts a benchmark run produces: with twenty samples, p95 is the
nineteenth, every time, rather than depending on which interpolation convention
the reader assumes.

Nothing in this module reads a clock or touches a file. It is handed numbers.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["HIGH_PERCENTILE", "Distribution", "finite", "percentile"]

# The high percentile every timing reports beside its median. Ninety-five rather
# than ninety-nine: a benchmark run is tens of samples, and the ninety-ninth
# percentile of thirty samples is the slowest one, which is a maximum wearing a
# percentile's name.
HIGH_PERCENTILE: Final = 95.0

# Seconds to milliseconds. Timings are taken in seconds, because that is what
# `time.perf_counter` deals in, and reported in milliseconds, because that is
# what the recorded baseline is written in.
_MILLISECONDS: Final = 1000.0


def finite(value: object) -> float:
    """Read a number that a comparison can actually compare.

    `float()` accepts `nan` and `inf`, and neither is a figure a gate can be
    built on: `nan` compares false against every bound, so a measurement
    carrying one is reported as within tolerance, and an infinite tolerance
    permits every regression there is. Both fail *open*, which is the one
    direction a gate must never fail in.

    Args:
        value: What the document carried.

    Returns:
        The number.

    Raises:
        ValueError: If it is not a finite number. The caller turns that into a
            message naming the entry.
    """
    number = float(value)  # type: ignore[arg-type]  # the caller catches TypeError: this is where a document's arbitrary JSON is turned into a number, and "not a number at all" and "not a finite one" are the same event to it
    if not math.isfinite(number):
        message = f"{number} is not a figure a comparison can compare"
        raise ValueError(message)
    return number


def percentile(samples: Sequence[float], rank: float) -> float:
    """Take the nearest-rank percentile of some samples.

    Args:
        samples: The observations. Need not be sorted.
        rank: The percentile to take, between 0 and 100 inclusive.

    Returns:
        The sample at that rank. Always one of the values passed in.

    Raises:
        ValueError: If there are no samples, or the rank is outside 0 to 100.
            Both are a caller mistake rather than a measurement outcome: an
            empty run is a benchmark that did not happen, and reporting a
            statistic over nothing is how a broken benchmark reads as a fast one.
    """
    if not samples:
        message = "a percentile over no samples is not a measurement"
        raise ValueError(message)
    if not 0.0 <= rank <= 100.0:
        message = f"a percentile is between 0 and 100, not {rank}"
        raise ValueError(message)
    ordered = sorted(samples)
    # Nearest-rank: the smallest observation at or above the requested fraction
    # of the sample count. The clamp covers rank 0, where the ceiling is zero
    # and the index would otherwise be -1 — which in Python is the *largest*
    # sample, so the guard is the difference between a minimum and a maximum.
    index = math.ceil(rank / 100.0 * len(ordered)) - 1
    return ordered[max(0, min(index, len(ordered) - 1))]


#:= docs/specs/benchmarks/index.md#req-069-latency-is-reported-as-a-distribution
#:% Timing measurements MUST report at least a median and a high percentile rather
#:% than a mean alone.
@dataclass(frozen=True, slots=True, kw_only=True)
class Distribution:
    """What a set of timings looked like, in milliseconds.

    Attributes:
        samples: How many observations it was built from.
        min_ms: The fastest observation.
        median_ms: The middle one. This is the figure the gate compares.
        p95_ms: The high percentile REQ-069 requires, so an intermittently slow
            stage is visible rather than averaged away.
        max_ms: The slowest observation.
        mean_ms: The arithmetic mean. Reported beside the two above and never
            instead of them.
        stdev_ms: The sample standard deviation, or 0.0 from a single
            observation. This is what the detection tolerance was chosen
            against — see the baseline document.
    """

    samples: int
    min_ms: float
    median_ms: float
    p95_ms: float
    max_ms: float
    mean_ms: float
    stdev_ms: float

    @classmethod
    def of_seconds(cls, samples: Sequence[float]) -> Distribution:
        """Summarise timings taken in seconds.

        Args:
            samples: One observation per iteration, in seconds.

        Returns:
            The distribution, in milliseconds.

        Raises:
            ValueError: If there are no samples.
        """
        if not samples:
            message = "a distribution over no samples is not a measurement"
            raise ValueError(message)
        values = [sample * _MILLISECONDS for sample in samples]
        return cls(
            samples=len(values),
            min_ms=min(values),
            median_ms=statistics.median(values),
            p95_ms=percentile(values, HIGH_PERCENTILE),
            max_ms=max(values),
            mean_ms=statistics.fmean(values),
            # `stdev` needs two observations; one sample has no spread rather
            # than an undefined one, and raising here would make a single-shot
            # measurement — a cold connection, of which there is exactly one —
            # impossible to report at all.
            stdev_ms=statistics.stdev(values) if len(values) > 1 else 0.0,
        )

    @property
    def relative_spread(self) -> float:
        """The standard deviation as a fraction of the median.

        This is the number the detection tolerance is argued from: a gate
        tighter than the run-to-run variation fails honest changes, and the only
        way to know what that variation is, is to have measured it.

        Returns:
            The coefficient of variation against the median, or 0.0 when the
            median is zero — which is a measurement too fast for the clock
            rather than a distribution with no spread, and is reported as such
            by the sample count beside it.
        """
        if self.median_ms == 0.0:
            return 0.0
        return self.stdev_ms / self.median_ms

    def as_document(self) -> dict[str, float | int]:
        """Render the distribution for the result document.

        Returns:
            A JSON-serialisable mapping, with every figure rounded to the
            microsecond. Rounding is deliberate: an unrounded float carries
            seventeen digits of a measurement that is repeatable to three, and
            the extra fourteen make two result documents differ in ways nobody
            can read.
        """
        return {
            "samples": self.samples,
            "min_ms": round(self.min_ms, 3),
            "median_ms": round(self.median_ms, 3),
            "p95_ms": round(self.p95_ms, 3),
            "max_ms": round(self.max_ms, 3),
            "mean_ms": round(self.mean_ms, 3),
            "stdev_ms": round(self.stdev_ms, 3),
        }
