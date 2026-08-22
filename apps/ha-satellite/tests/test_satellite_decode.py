"""Decoding, against real files rather than against a fake decoder.

**Every test here is a contract test and says so with
`@pytest.mark.filesystem`.** Decoding is file input, so none of this is a unit
test — and a fake would be worthless here, because what is under test is
whether `av` turns *these bytes* into the samples the daemon's push path wants.
The player's own tests use a fake decoder for exactly that reason: the two
halves are checked separately, and this is the half where the bytes matter.

R9 is the requirement: Home Assistant's text-to-speech proxy serves **MP3**, and
this wheel's own cues are **FLAC** and **WAV**. Two of the three are read
straight out of the wheel, so those tests are about the files that actually
ship. The MP3 is written here, because none ships — the alternative would be
committing one, and a generated file is a smaller thing to carry than an asset
with a licence.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import math
import wave
from typing import TYPE_CHECKING, Final

import av
import numpy as np
import pytest

from reachy_mini_ha_satellite.adapters.decode import (
    DEFAULT_OUTPUT_RATE,
    DecodeError,
    decode_file,
)
from reachy_mini_ha_satellite.assets.registry import assets_dir

if TYPE_CHECKING:
    from pathlib import Path

# The rate the robot's own playback pipeline pins its caps to.
_RATE: Final = 48000

# What the change document measured these two at, and what the decode has to
# reproduce for the headroom cap to mean anything. A cue mastered at full scale
# gets no boost; one three decibels down gets a little.
_PEAKS: Final = {
    "timer_finished.flac": 0.0,
    "wake_word_triggered.flac": -3.1,
}


@pytest.fixture(name="scratch")
def _scratch(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A directory on the real filesystem.

    **Not `tmp_path`**, which this package's `conftest.py` deliberately
    overrides with a pyfakefs directory so that the carried upstream tests
    write nowhere. `av` is a native library and cannot see a fake filesystem,
    so a test that handed it a path from that fixture would be testing the
    absence of the file rather than what is in it — and the two
    "cannot be decoded" tests below would pass for the wrong reason.

    Args:
        tmp_path_factory: pytest's own, which the override does not touch.

    Returns:
        A real, empty directory.
    """
    return tmp_path_factory.mktemp("decode")


def _cue(name: str) -> str:
    """Locate one of the sounds this wheel ships.

    Args:
        name: The file's name inside the asset directory.

    Returns:
        Its absolute path.
    """
    return str(assets_dir() / "sounds" / name)


def _write_mp3(path: Path, *, seconds: float = 0.25, rate: int = 44100) -> str:
    """Encode a short MP3, because none ships in the wheel.

    Args:
        path: Where to write it.
        seconds: How long it runs for.
        rate: The rate to encode at, deliberately not the daemon's, so that
            decoding it exercises the resampler rather than a copy.

    Returns:
        The file's path.
    """
    target = path / "speech.mp3"
    with av.open(str(target), mode="w") as container:
        stream = container.add_stream("mp3", rate=rate)
        samples = np.zeros(int(seconds * rate), dtype=np.float32)
        # A tone rather than silence: an encoder is free to make very short
        # work of digital silence, and the point is to decode something.
        samples[:] = 0.25 * np.sin(
            2.0 * math.pi * 440.0 * np.arange(samples.size) / rate,
        )
        frame = av.AudioFrame.from_ndarray(
            samples.reshape(1, -1),
            format="flt",
            layout="mono",
        )
        frame.rate = rate
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return str(target)


@pytest.mark.filesystem
class TestEveryFormatTheRobotIsSent:
    """R9, one test per format, against files rather than against a fake."""

    def test_a_shipped_flac_decodes(self) -> None:
        """Which is what the wake chime and most of the cues are."""
        samples = decode_file(_cue("wake_word_triggered.flac"), rate=_RATE)

        assert samples.dtype == np.float32
        assert samples.ndim == 1
        assert samples.size > 0

    def test_a_shipped_wav_decodes(self) -> None:
        """`processing.wav` is the one cue that is not FLAC."""
        samples = decode_file(_cue("processing.wav"), rate=_RATE)

        assert samples.dtype == np.float32
        assert samples.size > 0

    def test_an_mp3_decodes(self, scratch: Path) -> None:
        """Which is what Home Assistant's text-to-speech proxy serves.

        Args:
            scratch: Somewhere real to write one, since none ships.
        """
        samples = decode_file(_write_mp3(scratch), rate=_RATE)

        assert samples.dtype == np.float32
        assert samples.size > 0


@pytest.mark.filesystem
class TestWhatComesOut:
    """The shape and the levels the rest of the change depends on."""

    def test_it_resamples_to_the_rate_it_was_asked_for(self) -> None:
        """R9: the daemon's output rate is asked for, never assumed.

        A cue that shipped at 48 kHz and a daemon that plays at 16 kHz means a
        third of the samples, and pushing the wrong count is pushing audio at
        the wrong speed.
        """
        at_full = decode_file(_cue("wake_word_triggered.flac"), rate=48000)
        at_third = decode_file(_cue("wake_word_triggered.flac"), rate=16000)

        assert at_third.size == pytest.approx(at_full.size / 3, rel=0.01)

    def test_a_daemon_with_no_audio_device_gets_the_default_rate(self) -> None:
        """It answers with a negative rate, which is not a rate to resample to."""
        fallback = decode_file(_cue("processing.wav"), rate=-1)
        explicit = decode_file(_cue("processing.wav"), rate=DEFAULT_OUTPUT_RATE)

        assert fallback.size == explicit.size

    def test_it_is_mono_rather_than_rematrixed_to_stereo(self) -> None:
        """Asking for stereo costs 3 dB, which this change cannot spare.

        `av` rematrixes a mono source into two channels by halving it, and the
        daemon fans a mono array out by copying — so mono is both louder and
        simpler. One dimension is also exactly what the push path takes.
        """
        samples = decode_file(_cue("wake_word_triggered.flac"), rate=_RATE)

        assert samples.ndim == 1

    @pytest.mark.parametrize(("name", "expected"), sorted(_PEAKS.items()))
    def test_the_cues_peak_where_the_change_document_measured_them(
        self,
        name: str,
        expected: float,
    ) -> None:
        """The headroom cap is computed from these, so they are load-bearing.

        Change 0016 measured `timer_finished.flac` at 0.0 dBFS and the wake
        chime at -3.1 dBFS, and R5 exists because of the difference. If a
        decode ever stopped reproducing them, the cap would be capping
        something else.

        Args:
            name: The cue.
            expected: What the change document recorded, in dBFS.
        """
        samples = decode_file(_cue(name), rate=_RATE)

        peak = float(np.abs(samples).max())

        assert 20.0 * math.log10(peak) == pytest.approx(expected, abs=0.05)


@pytest.mark.filesystem
class TestWhatCannotBeDecoded:
    """A media URL Home Assistant cannot serve is Home Assistant's problem."""

    def test_a_file_that_is_not_there_is_refused(self, scratch: Path) -> None:
        """Answering with silence would report the sound as having played.

        Args:
            scratch: A directory with nothing of that name in it.
        """
        with pytest.raises(DecodeError):
            decode_file(str(scratch / "absent.mp3"), rate=_RATE)

    def test_a_file_with_no_audio_stream_is_refused(self, scratch: Path) -> None:
        """A URL that answered with a video is not a sound to play.

        Args:
            scratch: Somewhere real to write one.
        """
        target = scratch / "silent.avi"
        with av.open(str(target), mode="w") as container:
            stream = container.add_stream("mpeg4", rate=5)
            stream.width, stream.height = 16, 16
            stream.pix_fmt = "yuv420p"
            frame = av.VideoFrame.from_ndarray(
                np.zeros((16, 16, 3), dtype=np.uint8),
                format="rgb24",
            )
            for packet in stream.encode(frame):
                container.mux(packet)
            for packet in stream.encode(None):
                container.mux(packet)

        with pytest.raises(DecodeError):
            decode_file(str(target), rate=_RATE)

    def test_an_audio_file_holding_no_audio_is_refused(self, scratch: Path) -> None:
        """A truncated download opens cleanly and decodes to nothing.

        Pushing an empty array would report the sound as having played, and the
        operator would have a robot that says nothing and logs nothing.

        Args:
            scratch: Somewhere real to write one.
        """
        target = scratch / "empty.wav"
        with wave.open(str(target), "wb") as empty:
            empty.setnchannels(1)
            empty.setsampwidth(2)
            empty.setframerate(_RATE)

        with pytest.raises(DecodeError):
            decode_file(str(target), rate=_RATE)

    def test_bytes_that_are_not_audio_are_refused(self, scratch: Path) -> None:
        """Which is what a URL answering with an error page looks like.

        Args:
            scratch: Somewhere real to write them.
        """
        target = scratch / "notaudio.mp3"
        target.write_bytes(b"<html>404</html>" * 64)

        with pytest.raises(DecodeError):
            decode_file(str(target), rate=_RATE)
