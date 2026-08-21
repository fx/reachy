"""The distribution statistics, checked against hand-computed answers.

These are the numbers every other part of the suite is built on: the value the
gate compares is a median produced here, and the tolerance the gate uses was
argued from a standard deviation produced here. So they are checked against
values worked out by hand rather than against the same functions computing them
a second way.

No test in this module performs any input or output. Nothing here reads a clock:
`Distribution.of_seconds` is handed numbers.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import pytest

from reachy_bench.stats import HIGH_PERCENTILE, Distribution, finite, percentile


def test_the_percentile_is_a_sample_that_was_actually_observed() -> None:
    """Nearest-rank, so every reported figure is a real observation."""
    samples = [10.0, 20.0, 30.0, 40.0]
    assert percentile(samples, 50.0) == 20.0
    assert percentile(samples, 75.0) == 30.0
    assert percentile(samples, 100.0) == 40.0


def test_the_lowest_percentile_is_the_smallest_sample_not_the_largest() -> None:
    """Rank zero has a ceiling of zero, and index -1 would be the maximum.

    This is the guard in `percentile`, and it is the difference between
    reporting a minimum and reporting a maximum.
    """
    assert percentile([5.0, 1.0, 9.0], 0.0) == 1.0


def test_the_percentile_does_not_depend_on_the_order_it_was_given() -> None:
    """Samples arrive in the order they were taken, not sorted."""
    assert percentile([40.0, 10.0, 30.0, 20.0], 50.0) == 20.0


@pytest.mark.parametrize("rank", [-0.1, 100.1])
def test_a_percentile_outside_zero_to_a_hundred_is_refused(rank: float) -> None:
    """A rank that is not one is a caller mistake, not a measurement.

    Args:
        rank: The rank to refuse.
    """
    with pytest.raises(ValueError, match="between 0 and 100"):
        percentile([1.0], rank)


def test_a_percentile_over_nothing_is_refused() -> None:
    """An empty run is a benchmark that did not happen."""
    with pytest.raises(ValueError, match="no samples"):
        percentile([], 50.0)


def test_a_distribution_reports_a_median_and_a_high_percentile() -> None:
    """REQ-069's requirement, on numbers whose answers are known.

    Twenty samples: nineteen at one millisecond and one at a hundred. The mean
    is 5.95 ms, which is the figure that would hide the slow case; the median is
    1 ms and the 95th percentile is the slow one.
    """
    samples = [0.001] * 19 + [0.100]
    distribution = Distribution.of_seconds(samples)

    assert distribution.samples == 20
    assert distribution.median_ms == pytest.approx(1.0)
    assert distribution.p95_ms == pytest.approx(1.0)
    assert distribution.max_ms == pytest.approx(100.0)
    assert distribution.mean_ms == pytest.approx(5.95)


def test_the_high_percentile_reflects_the_slow_cases() -> None:
    """REQ-069's scenario: an intermittently slow stage is not averaged away.

    One in ten passes is a hundred times slower. At twenty samples the 95th
    percentile is the nineteenth, which is one of the slow ones.
    """
    samples = ([0.001] * 9 + [0.100]) * 2
    distribution = Distribution.of_seconds(samples)

    assert distribution.median_ms == pytest.approx(1.0)
    assert distribution.p95_ms == pytest.approx(100.0)


def test_the_high_percentile_is_the_ninety_fifth() -> None:
    """The constant is what it says, so a reader can check the arithmetic."""
    assert HIGH_PERCENTILE == 95.0


def test_a_single_observation_has_no_spread_rather_than_an_undefined_one() -> None:
    """A cold connection is measured once, and must still be reportable."""
    distribution = Distribution.of_seconds([0.378])

    assert distribution.samples == 1
    assert distribution.median_ms == pytest.approx(378.0)
    assert distribution.stdev_ms == 0.0
    assert distribution.relative_spread == 0.0


def test_the_relative_spread_is_the_deviation_over_the_median() -> None:
    """This is the number the detection tolerance was argued from."""
    distribution = Distribution.of_seconds([0.010, 0.012, 0.014])

    assert distribution.median_ms == pytest.approx(12.0)
    assert distribution.stdev_ms == pytest.approx(2.0)
    assert distribution.relative_spread == pytest.approx(2.0 / 12.0)


def test_a_measurement_too_fast_for_the_clock_reports_no_spread() -> None:
    """A zero median has no fraction, and dividing by it would raise."""
    distribution = Distribution.of_seconds([0.0, 0.0, 0.0])

    assert distribution.median_ms == 0.0
    assert distribution.relative_spread == 0.0


def test_a_distribution_over_nothing_is_refused() -> None:
    """The same rule as a percentile over nothing, at the level above."""
    with pytest.raises(ValueError, match="no samples"):
        Distribution.of_seconds([])


def test_the_document_carries_every_statistic_rounded_to_the_microsecond() -> None:
    """Two result files should differ where the measurement differs."""
    document = Distribution.of_seconds([0.0012345, 0.0023456]).as_document()

    assert set(document) == {
        "samples",
        "min_ms",
        "median_ms",
        "p95_ms",
        "max_ms",
        "mean_ms",
        "stdev_ms",
    }
    assert document["min_ms"] == pytest.approx(1.234)
    assert document["max_ms"] == pytest.approx(2.346)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_figure_is_refused(value: float) -> None:
    """A gate built on one fails open, which is the one direction it must not.

    Args:
        value: The figure to refuse.
    """
    with pytest.raises(ValueError, match="not a figure"):
        finite(value)


def test_a_finite_figure_comes_back_as_a_float() -> None:
    """Including one a JSON document carried as a whole number."""
    assert finite(97451) == 97451.0
    assert finite("1.5") == 1.5


def test_something_that_is_not_a_number_at_all_is_refused() -> None:
    """The caller catches both, because to a document they are one event."""
    with pytest.raises((TypeError, ValueError)):
        finite("plenty")
