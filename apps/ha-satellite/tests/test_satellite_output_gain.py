"""The boost, the knee and the limiter, over arrays rather than through audio.

Every test here is a unit test: no daemon, no file, no speaker. That is the
whole reason change 0016 put the curve in a module of its own — "does this
sound harsh?" cannot be asserted, but "is it continuous at the knee, monotonic,
and inside [-1, 1]?" can, and those are the properties the harshness came from
violating.

**The shape is pinned rather than the samples.** A test that asserted a
particular output value would fail the next time somebody tuned the knee and
would say nothing about whether the tuning was right. These say what must hold
for any tuning: the curve does not jump, does not fold back on itself, and does
not leave full scale — and a cue mastered hot is not handed the gain a quiet
voice needs.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import math
from typing import Final

import numpy as np
import pytest

from reachy_mini_ha_satellite.adapters.output_gain import (
    DEFAULT_BOOST_PERCENT,
    KNEE,
    MAX_BOOST_PERCENT,
    MIN_BOOST_PERCENT,
    LevelMeter,
    Levels,
    amplify,
    dbfs,
    effective_gain,
    headroom_gain,
    soft_limit,
)

# A boost well past the knee, so the limiter is doing something in every test
# that uses it. The default, as it happens.
_BOOST: Final = 5.0

# How finely the curve is sampled when a test is asserting on its shape. Enough
# that a discontinuity anywhere in [-2, 2] is caught, and small enough that the
# test is instant.
_STEPS: Final = 4001


def _sweep(limit: float = 2.0) -> np.ndarray:
    """Build a ramp through the whole interesting range of input levels.

    Args:
        limit: How far past full scale to go, so the clip is exercised too.

    Returns:
        Evenly spaced samples from `-limit` to `limit`.
    """
    return np.linspace(-limit, limit, _STEPS, dtype=np.float32)


class TestTheCurvesShape:
    """What must be true of the limiter however it is tuned."""

    def test_it_never_leaves_full_scale(self) -> None:
        """R4's hard promise, and the one a speaker enforces painfully."""
        out = soft_limit(_sweep(), _BOOST)

        assert float(np.abs(out).max()) <= 1.0

    def test_it_never_leaves_full_scale_even_at_the_largest_boost(self) -> None:
        """The bound is the range's, not a property of the default."""
        out = soft_limit(_sweep(limit=10.0), MAX_BOOST_PERCENT / 100.0)

        assert float(np.abs(out).max()) <= 1.0

    def test_it_is_monotonic(self) -> None:
        """A curve that folded back would make a louder input come out quieter."""
        out = soft_limit(_sweep(), _BOOST)

        assert np.all(np.diff(out) >= -1e-7)

    def test_it_is_continuous(self) -> None:
        """No step anywhere, and the knee is where a naive limiter puts one."""
        sweep = _sweep()
        out = soft_limit(sweep, _BOOST)
        # The largest jump the input itself justifies, with room for the
        # steepest part of the curve — which is below the knee, where the gain
        # is exactly the boost.
        biggest = float(np.abs(np.diff(sweep)).max()) * _BOOST * 1.5

        assert float(np.abs(np.diff(out)).max()) <= biggest

    def test_it_is_exactly_linear_below_the_knee(self) -> None:
        """R4: the ordinary case is amplification and nothing else.

        A limiter that started working at zero would be compressing every
        syllable of ordinary speech rather than catching the peaks.
        """
        quiet = np.linspace(0.0, KNEE / _BOOST * 0.99, 100, dtype=np.float32)

        out = soft_limit(quiet, _BOOST)

        assert out == pytest.approx(quiet * _BOOST, abs=1e-6)

    def test_it_is_symmetric(self) -> None:
        """A limiter that treated the two halves differently would add a bias."""
        sweep = _sweep()

        out = soft_limit(sweep, _BOOST)

        assert out == pytest.approx(-soft_limit(-sweep, _BOOST), abs=1e-7)

    def test_a_gain_below_unity_attenuates(self) -> None:
        """The predecessor returned the input untouched here, and could.

        Its `soft_limit` was only ever handed boosts. Ours also carries Home
        Assistant's volume and the ducking factor, so ignoring a gain below one
        would make the volume control inert — the very defect change 0016 is
        about.
        """
        source = _sweep(limit=1.0)

        out = soft_limit(source, 0.25)

        assert out == pytest.approx(source * 0.25, abs=1e-6)

    def test_a_source_already_past_full_scale_is_still_bounded(self) -> None:
        """The final clip, which in ordinary use catches nothing."""
        source = np.array([-4.0, 4.0], dtype=np.float32)

        out = soft_limit(source, 1.0)

        assert list(out) == [-1.0, 1.0]

    def test_it_does_not_touch_what_it_was_given(self) -> None:
        """The decoded source is kept and played again by `resume`."""
        source = _sweep(limit=1.0)
        before = source.copy()

        soft_limit(source, _BOOST)

        assert source == pytest.approx(before)


class TestTheHeadroomCap:
    """R5: a cue is not given the gain that a voice needs."""

    def test_a_quiet_source_may_be_amplified_a_long_way(self) -> None:
        """Text-to-speech arrives around -15 dBFS and has room to spare."""
        quiet = np.array([0.1, -0.1], dtype=np.float32)

        assert headroom_gain(quiet) == pytest.approx(10.0)

    def test_a_source_at_full_scale_may_not_be_amplified_at_all(self) -> None:
        """`timer_finished.flac` peaks at 0.0 dBFS."""
        hot = np.array([1.0, -1.0], dtype=np.float32)

        assert headroom_gain(hot) == pytest.approx(1.0)

    def test_a_source_past_full_scale_is_not_turned_down_by_the_cap(self) -> None:
        """This is a cap on amplification, not a second volume control."""
        clipping = np.array([2.0, -2.0], dtype=np.float32)

        assert headroom_gain(clipping) == pytest.approx(1.0)

    def test_silence_has_no_cap_to_apply(self) -> None:
        """Silence has no peak, and dividing by it would not be a number."""
        assert headroom_gain(np.zeros(16, dtype=np.float32)) == pytest.approx(1.0)

    def test_an_empty_source_is_not_a_division(self) -> None:
        """An empty source is refused upstream; the arithmetic holds regardless."""
        assert headroom_gain(np.zeros(0, dtype=np.float32)) == pytest.approx(1.0)


class TestTheEffectiveGain:
    """How the boost, the volume, the ducking and the cap combine."""

    def test_a_quiet_source_gets_the_whole_boost(self) -> None:
        """R1. This is where the loudness comes from."""
        gain = effective_gain(volume=100.0, boost_percent=500.0, headroom=10.0)

        assert gain == pytest.approx(5.0)

    def test_a_hot_source_gets_none_of_it(self) -> None:
        """R5, and the reason a chime is not a blare."""
        gain = effective_gain(volume=100.0, boost_percent=500.0, headroom=1.0)

        assert gain == pytest.approx(1.0)

    def test_the_volume_scales_a_hot_source_too(self) -> None:
        """The correction to the change document's formula, and why it matters.

        Capping the *result* rather than the boost would leave the slider inert
        from 100% down to 29% for a source with 1.43x of headroom — which is
        the wake chime this wheel ships. Ducking would fail the same way, on
        music, which is exactly when ducking matters.
        """
        gain = effective_gain(volume=50.0, boost_percent=500.0, headroom=1.0)

        assert gain == pytest.approx(0.5)

    def test_ducking_multiplies_on_top(self) -> None:
        """The vendored layer ducks so a wake word can be heard over music."""
        gain = effective_gain(
            volume=100.0,
            boost_percent=500.0,
            duck=0.25,
            headroom=10.0,
        )

        assert gain == pytest.approx(1.25)

    def test_silence_is_a_level_rather_than_an_absence(self) -> None:
        """A duck factor of zero is a request, and zero is falsy."""
        gain = effective_gain(volume=100.0, boost_percent=500.0, duck=0.0)

        assert gain == pytest.approx(0.0)

    def test_it_never_inverts_the_waveform(self) -> None:
        """Which a negative multiplier would do rather than quieten anything."""
        gain = effective_gain(volume=-50.0, boost_percent=500.0)

        assert gain == pytest.approx(0.0)

    def test_it_never_exceeds_the_headroom(self) -> None:
        """Even for a volume past the 0-100 the media-player entity offers."""
        gain = effective_gain(volume=400.0, boost_percent=500.0, headroom=2.0)

        assert gain == pytest.approx(2.0)


class TestTheBoostRange:
    """R3's numbers, which are adopted from the predecessor rather than derived."""

    def test_the_default_sits_inside_the_range(self) -> None:
        """A default outside its own bounds is a setting nobody can resolve."""
        assert MIN_BOOST_PERCENT <= DEFAULT_BOOST_PERCENT <= MAX_BOOST_PERCENT

    def test_the_floor_is_unity(self) -> None:
        """Below it the boost would be an attenuator, which the volume already is."""
        assert pytest.approx(100.0) == MIN_BOOST_PERCENT

    def test_the_ceiling_is_the_measured_one(self) -> None:
        """800% is +18 dB.

        Which the predecessor describes as past the point where the measured
        -15.5 dBFS text-to-speech peaks sit hard in the limiter.
        """
        assert pytest.approx(800.0) == MAX_BOOST_PERCENT
        assert 20.0 * math.log10(MAX_BOOST_PERCENT / 100.0) == pytest.approx(
            18.06,
            abs=0.01,
        )


class TestWhatTheLimiterReports:
    """R6: "distorted" and "too quiet" become different lines in a log."""

    def test_it_reports_both_peaks(self) -> None:
        """The pair is the point: one alone cannot tell them apart."""
        source = np.array([0.1, -0.1], dtype=np.float32)

        _out, levels = amplify(source, 2.0)

        assert levels.peak_in == pytest.approx(dbfs(0.1))
        assert levels.peak_out == pytest.approx(dbfs(0.2))

    def test_nothing_is_limited_below_the_knee(self) -> None:
        """So a zero here means the boost was exactly linear."""
        source = np.full(100, 0.1, dtype=np.float32)

        _out, levels = amplify(source, 2.0)

        assert levels.limited == pytest.approx(0.0)

    def test_everything_is_limited_when_everything_is_over(self) -> None:
        """And a large number means the source is being squashed."""
        source = np.full(100, 0.9, dtype=np.float32)

        _out, levels = amplify(source, 2.0)

        assert levels.limited == pytest.approx(1.0)

    def test_half_limited_is_reported_as_half(self) -> None:
        """The proportion is over samples, so it is a number to act on."""
        source = np.array([0.1] * 50 + [0.9] * 50, dtype=np.float32)

        _out, levels = amplify(source, 2.0)

        assert levels.limited == pytest.approx(0.5)

    def test_silence_is_reported_as_silence_rather_than_as_a_number(self) -> None:
        """A peak of zero has no dBFS, and negative infinity says so."""
        assert dbfs(0.0) == -math.inf

    def test_the_line_names_everything_it_measured(self) -> None:
        """An operator reads this, so it has to be one readable line."""
        levels = Levels(peak_in=-15.0, peak_out=-1.0, limited=0.25, gain=5.0)

        described = levels.describe()

        assert "peak in -15.0dBFS" in described
        assert "out -1.0dBFS" in described
        assert "limited 25.0%" in described
        assert "gain 5.00x" in described


class TestTheMeterAcrossAnUtterance:
    """The gain is read per chunk, so the report is assembled from all of them."""

    def test_the_peaks_are_the_loudest_anywhere_in_it(self) -> None:
        """Not the last chunk's, which is usually a fade to nothing."""
        meter = LevelMeter()

        meter.add(Levels(peak_in=-20.0, peak_out=-6.0, limited=0.0, gain=5.0), 100)
        meter.add(Levels(peak_in=-3.0, peak_out=-1.0, limited=0.0, gain=5.0), 100)
        meter.add(Levels(peak_in=-40.0, peak_out=-30.0, limited=0.0, gain=5.0), 100)

        assert meter.levels().peak_in == pytest.approx(-3.0)
        assert meter.levels().peak_out == pytest.approx(-1.0)

    def test_the_proportion_is_over_every_sample(self) -> None:
        """A weighted mean, so a short trailing chunk does not count as a long one."""
        meter = LevelMeter()

        meter.add(Levels(peak_in=-3.0, peak_out=-1.0, limited=1.0, gain=5.0), 900)
        meter.add(Levels(peak_in=-3.0, peak_out=-1.0, limited=0.0, gain=5.0), 100)

        assert meter.levels().limited == pytest.approx(0.9)

    def test_the_gain_reported_is_the_one_in_force_at_the_end(self) -> None:
        """Which is what an operator who just moved the slider wants to see."""
        meter = LevelMeter()

        meter.add(Levels(peak_in=-3.0, peak_out=-1.0, limited=0.0, gain=5.0), 100)
        meter.add(Levels(peak_in=-3.0, peak_out=-7.0, limited=0.0, gain=1.0), 100)

        assert meter.levels().gain == pytest.approx(1.0)

    def test_an_utterance_that_pushed_nothing_is_not_a_division(self) -> None:
        """A sound superseded before its first chunk still logs a line."""
        assert LevelMeter().levels().limited == pytest.approx(0.0)
