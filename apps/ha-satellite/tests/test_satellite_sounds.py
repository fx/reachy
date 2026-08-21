"""Resolving what Home Assistant asks for into a file with a known length.

The filesystem here is `pyfakefs`, which performs no input or output at all — it
is an in-memory filesystem, which is why it is a development dependency in this
repository — so these are ordinary unit tests and carry no marker. Nothing
fetches anything either: the source takes its fetcher as an argument, and every
test that resolves a remote URL supplies one.

The WAV and FLAC headers are built here rather than committed as fixture files,
because what is under test is the reading of them and a committed file would
pin whatever tool wrote it.
"""

from __future__ import annotations

import struct
import urllib.request
import wave
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from reachy_mini_ha_satellite.adapters.sounds import (
    _MAX_FETCH_BYTES,  # the bound is the behaviour under test
    FileSoundSource,
    _HttpOnlyRedirects,
    fetch_over_http,
    flac_duration,
    mp3_duration,
    wav_duration,
)

if TYPE_CHECKING:
    from pyfakefs.fake_filesystem import FakeFilesystem


def _wav(frames: int, rate: int = 16000) -> bytes:
    """Build a WAV file of a known length.

    Args:
        frames: How many samples it holds.
        rate: Its sample rate.

    Returns:
        The file's bytes.
    """
    buffer = BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x00\x00" * frames)
    return buffer.getvalue()


def _flac(samples: int, rate: int = 44100) -> bytes:
    """Build the first forty-two bytes of a FLAC stream.

    Only STREAMINFO is built, because only STREAMINFO is read. The format
    requires it be the first metadata block, so a real file starts with exactly
    these bytes.

    Args:
        samples: How many samples the stream claims to hold.
        rate: Its sample rate.

    Returns:
        Enough of a FLAC file to be measured.
    """
    # 20 bits of rate, 3 of channels minus one, 5 of depth minus one, 36 of
    # total samples, packed into eight bytes.
    packed = (rate << 44) | (1 << 41) | (15 << 36) | samples
    streaminfo = bytes(10) + struct.pack(">Q", packed) + bytes(16)
    # Block header: last-block flag set, type 0 (STREAMINFO), length 34.
    return b"fLaC" + bytes([0x80, 0x00, 0x00, 0x22]) + streaminfo


def _mp3(
    *,
    bitrate_kbps: int,
    audio_bytes: int,
    id3_bytes: int = 0,
    xing_frames: int = 0,
    mono: bool = False,
) -> bytes:
    """Build an MPEG-1 Layer III stream with a known first frame.

    Only the first frame's header is real; everything after it is filler,
    because the constant-bitrate arithmetic is over the byte count and the
    variable-bitrate path reads a frame count rather than the frames.

    Args:
        bitrate_kbps: The bitrate to declare. Zero declares "free format",
            which is a stream this cannot size.
        audio_bytes: How many bytes to put after the tag.
        id3_bytes: How large an ID3v2 tag to put in front of it.
        xing_frames: A frame count to write into a Xing header, or zero for a
            constant-bitrate stream with no such header.
        mono: Whether to declare a single channel, which moves the Xing header
            because the frame carries less side information.

    Returns:
        The stream's bytes.
    """
    index = {
        0: 0,
        32: 1,
        64: 5,
        128: 9,
    }[bitrate_kbps]
    # Sync, MPEG-1 Layer III, no CRC; then the bitrate index and sample rate 0
    # (44.1 kHz); then stereo.
    header = bytes([0xFF, 0xFB, (index << 4), 0xC0 if mono else 0x00])
    body = header + bytes(audio_bytes - len(header))
    if xing_frames:
        # The marker follows the four-byte frame header and the frame's side
        # information, which is 32 bytes for MPEG-1 stereo. Written out here
        # as those two lengths rather than as one number, so that this builder
        # and the reader cannot be wrong in the same way — the reader had that
        # header missing from its offset, and a builder that shared the mistake
        # agreed with it.
        side_info = 17 if mono else 32
        marker = b"Xing" + (1).to_bytes(4, "big") + xing_frames.to_bytes(4, "big")
        body = (
            header
            + bytes(side_info)
            + marker
            + bytes(max(audio_bytes - 4 - side_info - len(marker), 0))
        )
    if not id3_bytes:
        return body
    # An ID3v2 tag declares its own length seven bits per byte.
    size = id3_bytes - 10
    declared = bytes(
        (
            (size >> 21) & 0x7F,
            (size >> 14) & 0x7F,
            (size >> 7) & 0x7F,
            size & 0x7F,
        ),
    )
    return b"ID3" + bytes([0x04, 0x00, 0x00]) + declared + bytes(size) + body


class TestReadingHowLongASoundIs:
    """The daemon reports no completion, so the header is the only signal."""

    def test_a_wav_reports_its_length(self) -> None:
        """Frames divided by rate, which is what the header carries."""
        assert wav_duration(_wav(8000)) == pytest.approx(0.5)

    def test_a_flac_reports_its_length(self) -> None:
        """From STREAMINFO, without reading the audio that follows it."""
        assert flac_duration(_flac(22050)) == pytest.approx(0.5)

    def test_something_that_is_not_a_wav_is_not_measured_as_one(self) -> None:
        """The magic bytes are checked, never the file extension.

        A `.wav` that is really an MP3 would otherwise report a length that is
        wrong, which is worse than reporting none.
        """
        assert wav_duration(b"ID3\x04not a wav at all") is None

    def test_something_that_is_not_a_flac_is_not_measured_as_one(self) -> None:
        """The same, the other way round."""
        assert flac_duration(_wav(100)) is None

    def test_a_truncated_flac_header_is_not_guessed_at(self) -> None:
        """Half a STREAMINFO is not a length."""
        assert flac_duration(_flac(22050)[:20]) is None

    def test_a_wav_claiming_no_frames_has_no_length(self) -> None:
        """Zero is not a duration, and a zero-length timer fires instantly."""
        assert wav_duration(_wav(0)) is None

    def test_a_flac_claiming_no_samples_has_no_length(self) -> None:
        """The same: a stream of unknown length writes zero here."""
        assert flac_duration(_flac(0)) is None

    def test_a_damaged_wav_is_refused_rather_than_raising(self) -> None:
        """A truncated download should cost a sound, not the process."""
        assert wav_duration(b"RIFF" + b"\x00" * 8) is None


class TestResolvingWhatWasAskedFor:
    """A path, a `file://` URL, or something that has to be fetched."""

    def test_a_bare_path_resolves_to_itself(self) -> None:
        """The satellite's own chimes are paths inside the wheel."""
        source = FileSoundSource(Path("/cache"), fetch=_must_not_fetch)
        Path("/sounds").mkdir(parents=True)
        Path("/sounds/chime.wav").write_bytes(_wav(16000))
        sound = source.resolve("/sounds/chime.wav")
        assert sound is not None
        assert sound.path == "/sounds/chime.wav"
        assert sound.duration_seconds == pytest.approx(1.0)

    def test_a_file_url_resolves_to_the_path_inside_it(self) -> None:
        """Percent-encoding is undone, because Home Assistant applies it."""
        source = FileSoundSource(Path("/cache"), fetch=_must_not_fetch)
        Path("/sounds").mkdir(parents=True)
        Path("/sounds/a chime.flac").write_bytes(_flac(44100))
        sound = source.resolve("file:///sounds/a%20chime.flac")
        assert sound is not None
        assert sound.path == "/sounds/a chime.flac"
        assert sound.duration_seconds == pytest.approx(1.0)

    def test_a_missing_file_resolves_to_nothing(self) -> None:
        """The player skips it and plays the next thing."""
        source = FileSoundSource(Path("/cache"), fetch=_must_not_fetch)
        assert source.resolve("/sounds/absent.wav") is None

    def test_a_remote_url_is_fetched_into_the_cache(self) -> None:
        """Which is the only way the daemon can be given text-to-speech."""
        body = _wav(32000)
        source = FileSoundSource(Path("/cache"), fetch=lambda _: body)
        sound = source.resolve("http://198.51.100.10:8123/api/tts_proxy/x.wav")
        assert sound is not None
        assert Path(sound.path).read_bytes() == body
        assert sound.duration_seconds == pytest.approx(2.0)

    def test_the_same_url_is_cached_under_the_same_name(self) -> None:
        """An announcement heard twice occupies one file, not two."""
        source = FileSoundSource(Path("/cache"), fetch=lambda _: _wav(1600))
        first = source.resolve("https://198.51.100.10/one.wav")
        second = source.resolve("https://198.51.100.10/one.wav")
        assert first is not None
        assert second is not None
        assert first.path == second.path

    def test_two_urls_do_not_collide(self) -> None:
        """The name is derived from the whole URL, not from its last part."""
        source = FileSoundSource(Path("/cache"), fetch=lambda _: _wav(1600))
        first = source.resolve("https://198.51.100.10/a/one.wav")
        second = source.resolve("https://198.51.100.10/b/one.wav")
        assert first is not None
        assert second is not None
        assert first.path != second.path

    def test_a_cached_name_cannot_escape_the_cache_directory(self) -> None:
        """The extension is carried across, and only when it is plain.

        A name assembled from a remote value is exactly where a path separator
        would otherwise arrive, so anything that is not a short alphanumeric
        suffix is dropped rather than sanitised.
        """
        source = FileSoundSource(Path("/cache"), fetch=lambda _: _wav(1600))
        sound = source.resolve("https://198.51.100.10/x.%2e%2e%2fetc%2fpasswd")
        assert sound is not None
        assert Path(sound.path).parent == Path("/cache")

    def test_a_cached_file_is_published_whole_or_not_at_all(self) -> None:
        """Music and speech are two players over one cache directory.

        Both can resolve the same URL at once. Writing into the target would
        let the one that finished first hand the daemon a path the other was
        in the middle of truncating, so the bytes are moved onto it instead.
        """
        seen: list[bytes] = []

        def _watching(url: str) -> bytes:
            del url
            # Whatever is at the destination while a write is in progress.
            target = Path("/cache")
            if target.is_dir():
                seen.extend(path.read_bytes() for path in sorted(target.iterdir()))
            return _wav(1600)

        source = FileSoundSource(Path("/cache"), fetch=_watching)
        first = source.resolve("https://198.51.100.10/one.wav")
        second = source.resolve("https://198.51.100.10/one.wav")
        assert first is not None
        assert second is not None
        # Nothing partial was ever left where a reader would look, and the
        # temporary file is gone.
        assert all(body == _wav(1600) for body in seen)
        assert [path.name for path in sorted(Path("/cache").iterdir())] == [
            Path(first.path).name,
        ]

    def test_a_fetch_that_fails_resolves_to_nothing(self) -> None:
        """A media URL Home Assistant cannot serve is Home Assistant's problem."""

        def _refused(url: str) -> bytes:
            del url
            message = "connection refused"
            raise OSError(message)

        source = FileSoundSource(Path("/cache"), fetch=_refused)
        assert source.resolve("https://198.51.100.10/absent.mp3") is None

    def test_an_unfetchable_scheme_is_never_handed_to_the_fetcher(self) -> None:
        """`data:` and `ftp:` are refused before anything opens them."""
        source = FileSoundSource(Path("/cache"), fetch=_must_not_fetch)
        assert source.resolve("ftp://198.51.100.10/tune.wav") is None
        assert source.resolve("data:audio/wav;base64,AAAA") is None

    def test_a_path_that_is_not_a_file_resolves_to_nothing(self) -> None:
        """A directory where a sound was expected costs a sound, not a crash."""
        source = FileSoundSource(Path("/cache"), fetch=_must_not_fetch)
        Path("/sounds/chime.wav").mkdir(parents=True)
        assert source.resolve("/sounds/chime.wav") is None

    def test_a_cache_that_cannot_be_written_resolves_to_nothing(self) -> None:
        """A full or unwritable disk should not end the conversation."""
        Path("/blocked").write_bytes(b"not a directory")
        source = FileSoundSource(Path("/blocked/cache"), fetch=lambda _: _wav(1600))
        assert source.resolve("https://198.51.100.10/one.wav") is None

    def test_an_unfetchable_scheme_is_refused_by_the_default_fetcher_too(
        self,
    ) -> None:
        """The allowlist is in the fetcher as well as in front of it.

        Two checks for one rule, deliberately: the fetcher is the thing that
        opens a URL, so the scheme it will open has to be its own invariant
        rather than something its only current caller happens to guarantee.
        """
        with pytest.raises(OSError, match="refusing to fetch"):
            fetch_over_http("ftp://198.51.100.10/tune.wav")

    def test_a_compressed_sound_resolves_without_a_length(self) -> None:
        """Which is the case the player has to cope with, so it is pinned."""
        source = FileSoundSource(Path("/cache"), fetch=lambda _: b"ID3\x04mp3")
        sound = source.resolve("https://198.51.100.10/speech.mp3")
        assert sound is not None
        assert sound.duration_seconds is None


def _must_not_fetch(url: str) -> bytes:
    """Fail the test if anything tries to fetch.

    Args:
        url: What was asked for.

    Returns:
        Never.

    Raises:
        AssertionError: Always.
    """
    message = f"nothing should have been fetched, but {url!r} was"
    raise AssertionError(message)


@pytest.fixture(autouse=True)
def _in_memory_filesystem(fs: FakeFilesystem) -> None:
    """Give every test in this module a filesystem that is not a disk.

    Args:
        fs: The fake filesystem `pyfakefs` installs.
    """
    del fs


class TestReadingHowLongAnMp3Is:
    """The format Home Assistant's text-to-speech proxy serves."""

    def test_a_constant_bitrate_stream_is_sized_from_its_bitrate(self) -> None:
        """Byte count over bitrate, which is exact for constant bitrate."""
        # 32 kbps at 44.1 kHz: 4000 bytes of audio is exactly one second.
        assert mp3_duration(_mp3(bitrate_kbps=32, audio_bytes=4000)) == pytest.approx(
            1.0,
            rel=0.01,
        )

    def test_a_variable_bitrate_stream_is_sized_from_its_frame_count(
        self,
    ) -> None:
        """The encoder wrote the exact count; arithmetic would only estimate."""
        # 38 frames of 1152 samples at 44.1 kHz is a bit under a second.
        assert mp3_duration(
            _mp3(bitrate_kbps=128, audio_bytes=99999, xing_frames=38),
        ) == pytest.approx(38 * 1152 / 44100)

    def test_a_mono_stream_carries_its_frame_count_somewhere_else(self) -> None:
        """A mono frame holds less side information, so the marker moves.

        The same off-by-a-header mistake fits here as in the stereo case, and
        it fails the same silent way: the marker is missed and the stream is
        sized by arithmetic that assumes a constant bitrate.
        """
        assert mp3_duration(
            _mp3(bitrate_kbps=128, audio_bytes=99999, xing_frames=38, mono=True),
        ) == pytest.approx(38 * 1152 / 44100)

    def test_an_id3_tag_is_skipped_rather_than_sized(self) -> None:
        """A tag can be tens of kilobytes of album art, and is not audio."""
        without = mp3_duration(_mp3(bitrate_kbps=32, audio_bytes=4000))
        with_tag = mp3_duration(
            _mp3(bitrate_kbps=32, audio_bytes=4000, id3_bytes=5000),
        )
        assert without is not None
        assert with_tag is not None
        assert with_tag == pytest.approx(without, rel=0.01)

    def test_something_that_is_not_an_mpeg_stream_is_not_measured_as_one(
        self,
    ) -> None:
        """The sync word is searched for, not assumed."""
        assert mp3_duration(b"not audio at all" * 100) is None

    def test_a_free_format_frame_is_not_guessed_at(self) -> None:
        """Bitrate index zero declares no bitrate, so there is nothing to divide by."""
        assert mp3_duration(_mp3(bitrate_kbps=0, audio_bytes=4000)) is None

    def test_an_mp3_resolves_with_a_length(self) -> None:
        """Which is what stops a text-to-speech announcement wedging a caller."""
        source = FileSoundSource(
            Path("/cache"),
            fetch=lambda _: _mp3(bitrate_kbps=32, audio_bytes=4000),
        )
        sound = source.resolve("http://198.51.100.10:8123/api/tts_proxy/x.mp3")
        assert sound is not None
        assert sound.duration_seconds == pytest.approx(1.0, rel=0.01)


class TestTheDefaultFetcher:
    """It opens a URL Home Assistant supplied, so its rules are its own."""

    def test_a_body_larger_than_the_bound_is_refused(self) -> None:
        """The robot has a small disk and the body is read whole."""
        with pytest.raises(OSError, match="larger than"):
            _read_through(b"x" * (_MAX_FETCH_BYTES + 1))

    def test_a_body_at_the_bound_is_accepted(self) -> None:
        """The limit is a limit, not an off-by-one that rejects the last byte."""
        assert len(_read_through(b"x" * 16)) == 16

    def test_a_redirect_off_the_allowlist_is_refused(self) -> None:
        """Checking only the first address makes the allowlist true of one hop."""
        handler = _HttpOnlyRedirects()
        request = urllib.request.Request("https://198.51.100.10/one.mp3")
        with pytest.raises(OSError, match="redirect"):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                None,
                "ftp://198.51.100.10/one.mp3",
            )


def _read_through(body: bytes) -> bytes:
    """Drive `fetch_over_http` against a response that is not a socket.

    Args:
        body: What the response holds.

    Returns:
        What the fetcher read.
    """

    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self, amount: int) -> bytes:
            return body[:amount]

    class _Opener:
        def open(self, request: object, timeout: float) -> _Response:
            del request, timeout
            return _Response()

    original = urllib.request.build_opener
    urllib.request.build_opener = lambda *_a, **_k: _Opener()  # type: ignore[assignment]  # the opener is what performs the one piece of real input in this module, and substituting it is how the bound is exercised without a socket
    try:
        return fetch_over_http("https://198.51.100.10/one.mp3")
    finally:
        urllib.request.build_opener = original
