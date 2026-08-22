"""Turning a playable file into the float samples the daemon's push path takes.

Change 0016 moved playback off `play_sound(path)` and onto
`start_playing` / `push_audio_sample` / `stop_playing`, because a path is a
thing you hand over and samples are a thing you can multiply — and multiplying
them is the only remaining place the robot's loudness can come from. This module
is the first half of that move: bytes on disk become one float32 array at the
rate the daemon says it wants.

**Three formats, and they are not an arbitrary three.** Home Assistant's
text-to-speech proxy serves **MP3**; this wheel's own cues are **FLAC** and
**WAV**. `av` decodes all three and resamples, which is why it is the decoder
here and why change 0016 declares it as a dependency of this member rather than
leaving it ambient in the robot's environment.

**Mono, deliberately.** The daemon's `push_audio_sample` fans a mono array out
to however many channels the output device has, by copying it — so pushing mono
costs nothing and loses nothing. Asking the resampler for stereo instead would
cost 3 dB: it rematrixes a mono source by halving it into two channels, measured
here as a shipped cue's peak falling from 0.699 to 0.495. Three decibels is not
a rounding error in a change whose entire purpose is loudness.

Nothing here reads a clock or touches the daemon. It is called from the thread
that already resolves a sound, which is off the event loop, because decoding a
long file is not something the ESPHome protocol should wait behind.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import av
import numpy as np
import numpy.typing as npt

from reachy_mini_ha_satellite.adapters.output_gain import Samples

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ["DEFAULT_OUTPUT_RATE", "DecodeError", "decode_file"]

# What to decode to when the daemon cannot say. It reports a negative rate when
# it has no audio device at all, and 48 kHz is what its own playback pipeline
# pins its caps to — so this is the daemon's own number rather than a guess,
# used only on the path where the daemon is not answering.
DEFAULT_OUTPUT_RATE: Final = 48000

# The packed float32 the resampler is asked for. Packed rather than planar, so
# one frame arrives as a single interleaved row and a mono decode is already the
# one-dimensional array the daemon's push path wants.
_PACKED_FLOAT: Final = "flt"
_MONO: Final = "mono"


class DecodeError(RuntimeError):
    """A file could not be decoded into samples.

    Raised rather than answered with silence: a caller that pushed an empty
    array would report the sound as having played, and the operator would be
    left with a robot that says nothing and logs nothing.
    """


def decode_file(path: str, *, rate: int) -> Samples:
    """Decode one file to mono float32 at `rate`.

    Args:
        path: The local file to decode.
        rate: The sample rate to resample to, which is what the daemon reports
            its output running at. A rate at or below zero — which is what the
            daemon answers when it has no audio device — falls back to
            `DEFAULT_OUTPUT_RATE`.

    Returns:
        One flat array of float32 samples in [-1, 1].

    Raises:
        DecodeError: If the file cannot be opened, holds no audio stream, or
            decodes to nothing.
    """
    target = rate if rate > 0 else DEFAULT_OUTPUT_RATE
    try:
        blocks = list(_decoded_blocks(path, target))
    except av.FFmpegError as error:
        message = f"could not decode {path!r}: {error}"
        raise DecodeError(message) from error
    if not blocks:
        message = f"{path!r} decoded to no audio at all"
        raise DecodeError(message)
    # Each block is one interleaved row, so the join is along it and the result
    # is flattened to the one dimension the daemon's push path expects.
    return np.concatenate(blocks, axis=1).reshape(-1).astype(np.float32, copy=False)


def _decoded_blocks(path: str, rate: int) -> Iterator[npt.NDArray[Any]]:
    """Decode and resample one file, a block at a time.

    Args:
        path: The local file.
        rate: The rate to resample to.

    Yields:
        Each resampled block, shaped one row by however many samples. Typed
        loosely because `to_ndarray` answers with whichever dtype the frame's
        format implies, and the format is pinned to packed float32 four lines
        below rather than in a type — `decode_file` is where that becomes a
        promise.

    Raises:
        DecodeError: If the file holds no audio stream.
    """
    with av.open(path) as container:
        if not container.streams.audio:
            message = f"{path!r} holds no audio stream"
            raise DecodeError(message)
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format=_PACKED_FLOAT, layout=_MONO, rate=rate)
        for frame in container.decode(stream):
            for resampled in resampler.resample(frame):
                yield resampled.to_ndarray()
        # Flushing is not optional: the resampler holds a tail whose length
        # depends on the rate conversion, and dropping it truncates every sound
        # by a few milliseconds — inaudible on a chime, and the last syllable of
        # a short answer.
        for resampled in resampler.resample(None):
            yield resampled.to_ndarray()
