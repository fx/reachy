"""Making the robot audible: a boost, a soft knee, and a limiter.

The robot's speaker is one USB device with one playback control, and change
0016 measured that control already sitting at `0.00dB` — its maximum. The
daemon's own coarse volume was found at 62 of 100 and raising it to 100 was
still reported as too quiet to hold a conversation with. So there is no gain
left to ask the hardware for, and the loudness has to come from multiplying the
samples before they are pushed. That is the whole of what this module is for.

**The curve and its constants are prior work, not a derivation.** They come from
`output_gain.py` in the application this one replaces, which ran on this robot
and through this speaker, and they were tuned by ear against it. Nothing in this
repository's suite can hear, so re-deriving them here would have been
substituting taste for the only evidence there was — and change 0016 then played
announcements through the real speaker at these values and found them audible
across a room, which is the second piece of evidence and points the same way.
Two of that module's findings are carried with them, in its own words:

* Plain `np.clip` on a boosted signal "squares off every peak and sounds harsh",
  which is why a knee and a `tanh` sit in front of the clip rather than the clip
  standing alone. The limiter is a requirement of change 0016 (R4) rather than a
  refinement that might come later, because hard clipping was tried first.
* The boost is makeup gain for Home Assistant's text-to-speech, "which arrives
  around -15 dBFS". The cues this wheel ships are mastered far hotter —
  `timer_finished.flac` peaks at 0.0 dBFS and the wake chime at -3.1 dBFS — so
  handing them the voice's multiplier drives them deep into the limiter and
  "turns a chime into a blare". `headroom_gain` is what stops that (R5).

**One deliberate departure from that source.** Its `soft_limit` returns the
input untouched when the gain is at or below unity, because the only gains it
was ever passed were boosts. Here the effective gain also carries Home
Assistant's volume control and the ducking factor, either of which puts it below
one legitimately — so a gain below unity multiplies here rather than being
ignored. Ignoring it would make change 0016's R2 false: the volume slider would
round-trip correctly and still change nothing, which is the defect being fixed.
The curve above the knee is untouched.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

import numpy as np
import numpy.typing as npt

__all__ = [
    "DEFAULT_BOOST_PERCENT",
    "KNEE",
    "MAX_BOOST_PERCENT",
    "MIN_BOOST_PERCENT",
    "LevelMeter",
    "Levels",
    "Samples",
    "amplify",
    "dbfs",
    "effective_gain",
    "headroom_gain",
    "soft_limit",
]

# `numpy.typing` is imported at run time rather than under `TYPE_CHECKING`, for
# the reason `daemon.py` records at its own aliases: a PEP 695 alias is lazy, so
# anything that resolves an annotation would otherwise raise `NameError` on a
# name that existed only for the type checker.

# Decoded audio: float32 samples in [-1, 1]. Mono, one dimension, which is what
# `decode.py` produces and what the daemon's push path takes.
type Samples = npt.NDArray[np.float32]

# Where the limiter starts working. Below it the boost is exactly linear, so
# ordinary speech at ordinary levels is amplified and nothing else; above it the
# excess is compressed into the remaining headroom.
KNEE: Final = 0.6

# The boost, in percent, and its bounds. Percent rather than decibels because
# that is the unit the control this replaces was expressed in, and an operator
# moving from one to the other should not have to convert. 800% is +18 dB, and
# the predecessor describes it as past the point where the measured -15.5 dBFS
# text-to-speech peaks sit hard in the limiter.
MIN_BOOST_PERCENT: Final = 100.0
MAX_BOOST_PERCENT: Final = 800.0
DEFAULT_BOOST_PERCENT: Final = 500.0

# What a peak of exactly zero is reported as. A silent buffer has no dBFS —
# the logarithm is undefined at zero — and reporting negative infinity is both
# true and readable in a log line.
_SILENT: Final = -math.inf


@dataclass(frozen=True, slots=True)
class Levels:
    """What the limiter did to one utterance.

    Change 0016's R6. "It sounds distorted" and "it is still too quiet" are
    different faults with different fixes, and without this they are two guesses
    about the same log line.

    Attributes:
        peak_in: The loudest sample before the gain, in dBFS.
        peak_out: The loudest sample after the gain and the limiter, in dBFS.
        limited: The proportion of samples that went past the knee, from 0.0 to
            1.0. Zero means the limiter was inert and the boost was exactly
            linear; a large number means the source is being squashed and the
            boost is set higher than this material wants.
        gain: The gain actually applied, after the headroom cap.
    """

    peak_in: float
    peak_out: float
    limited: float
    gain: float

    def describe(self) -> str:
        """Render this as the line R6 asks for.

        Returns:
            A single line naming the peak going in, the peak coming out, the
            proportion limited and the gain that produced them.
        """
        return (
            f"peak in {self.peak_in:.1f}dBFS "
            f"out {self.peak_out:.1f}dBFS "
            f"limited {self.limited * 100.0:.1f}% "
            f"gain {self.gain:.2f}x"
        )


def dbfs(peak: float) -> float:
    """Turn a linear peak into decibels relative to full scale.

    Args:
        peak: A magnitude, where 1.0 is full scale.

    Returns:
        The level in dBFS, or negative infinity for silence.
    """
    if peak <= 0.0:
        return _SILENT
    return 20.0 * math.log10(peak)


def headroom_gain(pcm: Samples) -> float:
    """Say the largest gain that keeps this source at or below full scale.

    This is R5, and it is why a chime and a sentence are not given the same
    multiplier. The boost is sized for text-to-speech arriving around -15 dBFS;
    a cue mastered at 0.0 dBFS has no headroom at all and is left alone.

    Args:
        pcm: The decoded source.

    Returns:
        The cap, never below 1.0 — a source already past full scale is limited
        rather than attenuated here, which keeps this a cap on amplification
        rather than a second, hidden volume control.
    """
    if pcm.size == 0:
        return 1.0
    peak = float(np.abs(pcm).max())
    if peak <= 0.0:
        # Silence has unbounded headroom, and multiplying it changes nothing.
        return 1.0
    return max(1.0, 1.0 / peak)


def effective_gain(
    *,
    volume: float,
    boost_percent: float,
    duck: float = 1.0,
    headroom: float = math.inf,
) -> float:
    """Work out what one chunk should actually be multiplied by.

    Change 0016's design, in one place so that the adapter does not assemble it
    inline — **with the cap applied to the boost rather than to the result**,
    which is a correction to the formula that document sketches. It has:

        requested = (volume / 100.0) * boost * duck_factor
        effective = min(requested, headroom_of(source))

    Capping last makes R5 true and R2 false. The wake chime this wheel ships
    peaks at -3.1 dBFS, so its headroom is 1.43x; at the default boost the
    request is 5.0x, and the cap swallows it — and goes on swallowing it until
    the volume slider is below 29%, so three quarters of the slider's travel
    changes nothing audible. Ducking fails the same way, and worse: the vendored
    layer ducks music so that a wake word can be heard over it, and music is
    exactly the hot material whose cap would discard the duck.

    Capping the boost instead keeps both. The boost is makeup gain for quiet
    text-to-speech, so it is what must not drive a cue into the limiter (R5);
    the volume and the duck are the operator's and are applied afterwards, so
    they always do something (R2). A source is still never amplified past full
    scale, because the boost cannot exceed the headroom and neither of the other
    two ever exceeds one.

    The cap is passed in rather than measured here, and that separation is the
    other half of the point. Volume and ducking change while a sound is playing,
    so they are read per chunk; the cap is a property of the material, measured
    once over the whole source by `headroom_gain`.

    Args:
        volume: What Home Assistant set, in percent from 0 to 100.
        boost_percent: The configured boost, in percent.
        duck: The ducking factor, 1.0 when not ducked.
        headroom: The largest gain this source may have, from `headroom_gain`.
            Unbounded by default, for a caller that only wants the request.

    Returns:
        The gain to apply. Never negative — a negative multiplier would invert
        the waveform rather than quieten it — and never past the headroom.
    """
    boost = min(boost_percent / 100.0, headroom)
    requested = (volume / 100.0) * boost * duck
    # The outer cap binds only for a volume above 100%, which the media-player
    # entity does not offer and a caller could still pass. Without it "never
    # past full scale from gain alone" would be true of the path that was
    # thought about rather than of the function.
    return min(max(requested, 0.0), headroom)


def soft_limit(pcm: Samples, gain: float) -> Samples:
    """Apply `gain` and keep the result inside [-1, 1] without hard clipping.

    Below `KNEE` the boosted signal is untouched, so the ordinary case is exact
    amplification. Above it the excess over the knee is passed through `tanh`
    and scaled back into the remaining headroom: the curve is continuous at the
    knee, monotonic, and asymptotic at full scale, so a peak is rounded rather
    than squared off.

    Args:
        pcm: The decoded source.
        gain: What to multiply it by.

    Returns:
        A new array, always — the input is never handed back and never mutated,
        so a caller may keep the decoded source and play it again at a different
        volume, which is what `resume` does.
    """
    boosted = np.asarray(pcm * np.float32(gain), dtype=np.float32)
    if gain > 1.0:
        magnitude = np.abs(boosted)
        over = magnitude > KNEE
        if over.any():
            headroom = 1.0 - KNEE
            excess = (magnitude[over] - KNEE) / headroom
            limited = KNEE + headroom * np.tanh(excess)
            boosted[over] = np.sign(boosted[over]) * limited
    # Unconditional, including below unity. Nothing above the knee can reach
    # here past full scale, so in ordinary use this catches nothing; it is what
    # makes "never leaves [-1, 1]" true of a source that arrived past it rather
    # than true only of the path that was thought about.
    return np.clip(boosted, -1.0, 1.0, out=boosted)


class LevelMeter:
    """Accumulates what the limiter did across one utterance.

    An utterance is amplified a chunk at a time — that is what lets Home
    Assistant's volume slider and the ducking factor take effect part way
    through one, rather than at the next — so the line R6 asks for has to be
    assembled from every chunk rather than measured on one array. The peaks are
    the loudest anywhere in the utterance; the proportion limited is over all of
    its samples, not the mean of the per-chunk proportions, which would weight a
    short trailing chunk as heavily as a long one.
    """

    def __init__(self) -> None:
        """Start with nothing having been played."""
        self._peak_in = _SILENT
        self._peak_out = _SILENT
        self._limited = 0.0
        self._samples = 0
        self._gain = 0.0

    def add(self, levels: Levels, samples: int) -> None:
        """Fold one chunk's measurements in.

        Args:
            levels: What amplifying that chunk did.
            samples: How many samples it held.
        """
        self._peak_in = max(self._peak_in, levels.peak_in)
        self._peak_out = max(self._peak_out, levels.peak_out)
        self._limited += levels.limited * samples
        self._samples += samples
        self._gain = levels.gain

    def levels(self) -> Levels:
        """Report the utterance as a whole.

        Returns:
            The peaks either side of the gain, the proportion of the whole
            utterance that was limited, and the gain last applied — which is
            the one in force at the end, after any volume change or ducking.
        """
        limited = self._limited / self._samples if self._samples else 0.0
        return Levels(
            peak_in=self._peak_in,
            peak_out=self._peak_out,
            limited=limited,
            gain=self._gain,
        )


def amplify(pcm: Samples, gain: float) -> tuple[Samples, Levels]:
    """Apply the gain and say what doing so did.

    Args:
        pcm: The decoded source.
        gain: What to multiply it by, already capped by `effective_gain`.

    Returns:
        The amplified samples and the levels R6 reports.
    """
    peak_in = float(np.abs(pcm).max()) if pcm.size else 0.0
    amplified = soft_limit(pcm, gain)
    peak_out = float(np.abs(amplified).max()) if amplified.size else 0.0
    if pcm.size and gain > 1.0:
        limited = float(np.count_nonzero(np.abs(pcm * np.float32(gain)) > KNEE))
        limited /= float(pcm.size)
    else:
        limited = 0.0
    return amplified, Levels(
        peak_in=dbfs(peak_in),
        peak_out=dbfs(peak_out),
        limited=limited,
        gain=gain,
    )
