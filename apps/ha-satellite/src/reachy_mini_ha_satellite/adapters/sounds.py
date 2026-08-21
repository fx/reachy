"""Turning what Home Assistant asks for into a file the daemon can play.

The daemon plays a **local file**. Home Assistant asks for a URL — an
`http(s)` address on its own host for text-to-speech and for media, and the
satellite's own chimes as paths inside the wheel. Something has to close that
gap, and it is a separate module from the player because it is a separate
concern: the player owns a queue, a position and a callback, and this owns
"where are the bytes and how long are they".

**Duration is why this exists at all.** The daemon's media interface has no
completion signal — no end-of-stream callback, no "is it still playing" — so the
only way the player can tell that a sound has finished is to know how long it
was, and the vendored protocol layer advances its conversation on that callback.
Three formats are read here and they are not an arbitrary three: **WAV** and
**FLAC** are what this application ships as chimes, and **MP3** is what Home
Assistant's text-to-speech proxy serves. Between them they cover every sound the
satellite plays in ordinary use. Anything else is reported as unknown, and
`ReachyPlayback` bounds it rather than waiting for ever.

Nothing here is guessed from a file extension. A `.wav` that is really an MP3
would report a length that is wrong rather than absent, which is worse, so every
reader checks the magic bytes first and answers `None` when it does not
recognise what it is looking at.
"""

from __future__ import annotations

import hashlib
import io
import os
import struct
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol
from urllib.parse import unquote, urlsplit

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "FileSoundSource",
    "Sound",
    "SoundSource",
    "fetch_over_http",
    "flac_duration",
    "mp3_duration",
    "wav_duration",
]

# What may be fetched. Anything else — `file:` is handled without a fetch,
# `data:` and `ftp:` are not handled at all — is refused rather than handed to a
# URL opener, which is what keeps the opener below from being a way to read an
# arbitrary local path off a URL Home Assistant supplied.
_FETCHABLE: Final = frozenset({"http", "https"})

# How long to wait for Home Assistant to answer. It is on the same WLAN, whose
# idle round-trip is 100-170 ms with 700 ms spikes, so this is several spikes'
# worth of grace — and a bound rather than none at all, because a fetch that
# hangs holds the player's queue behind it.
_FETCH_TIMEOUT_SECONDS: Final = 10.0

# The most a fetch will read. A bound rather than trust: the robot has a small
# disk and a small amount of memory, the response is read whole before anything
# looks at it, and nothing this application plays is anywhere near this size —
# an announcement is tens of kilobytes and a track is a few megabytes. Without
# it, one address that answers for ever fills both.
_MAX_FETCH_BYTES: Final = 64 * 1024 * 1024

# The first four bytes of each format this module can measure.
_RIFF: Final = b"RIFF"
_FLAC: Final = b"fLaC"

# Where FLAC keeps the numbers: the STREAMINFO block starts four bytes in, after
# the `fLaC` marker, and then four more after its own block header. Sample rate
# is 20 bits at bit offset 80 within STREAMINFO, and the total sample count is
# 36 bits at bit offset 108 — straddling bytes either way, hence the shifting.
_STREAMINFO_OFFSET: Final = 8
_STREAMINFO_LENGTH: Final = 34

# --- MPEG audio, as much of it as Layer III needs ---------------------------
#
# One frame header is four bytes: eleven sync bits, then the version, the layer,
# a protection bit, the bitrate index, the sample-rate index, a padding bit and
# the channel mode. Everything below indexes off those fields.
_ID3: Final = b"ID3"
_ID3_HEADER_LENGTH: Final = 10
_ID3_FOOTER_FLAG: Final = 0x10

# How far past the tag to look for the first frame. A tag declares its own
# length, so the sync is normally the very next byte; the window covers a writer
# that padded after it, and bounds the search rather than scanning a whole file
# for something that is not there.
_SYNC_SEARCH_BYTES: Final = 8192

# Version, from the two bits above the layer.
_MPEG_2_5: Final = 0
_MPEG_2: Final = 2
_MPEG_1: Final = 3

# Layer III, which is what "MP3" names. The other two layers are not read: no
# part of this system produces one.
_LAYER_3: Final = 1

# Bitrates in kilobits per second, indexed by the header's four-bit field. Index
# 0 is "free format" and index 15 is invalid; both read as zero here and make
# the frame unusable, which is the honest answer for a stream this cannot size.
_BITRATES_MPEG1_LAYER3: Final = (
    0,
    32,
    40,
    48,
    56,
    64,
    80,
    96,
    112,
    128,
    160,
    192,
    224,
    256,
    320,
    0,
)
_BITRATES_MPEG2_LAYER3: Final = (
    0,
    8,
    16,
    24,
    32,
    40,
    48,
    56,
    64,
    80,
    96,
    112,
    128,
    144,
    160,
    0,
)

# Sampling rates in hertz, indexed by the header's two-bit field. Index 3 is
# reserved and reads as zero.
_RATES: Final = {
    _MPEG_1: (44100, 48000, 32000, 0),
    _MPEG_2: (22050, 24000, 16000, 0),
    _MPEG_2_5: (11025, 12000, 8000, 0),
}

# Samples in one Layer III frame, which is what turns a frame count into a
# duration.
_SAMPLES_PER_FRAME: Final = {_MPEG_1: 1152, _MPEG_2: 576, _MPEG_2_5: 576}

# Where a Xing or Info header sits inside the first frame. A variable-bitrate
# encoder writes the true frame count there, which is exact where the bitrate
# arithmetic below is an estimate.
#
# The offset is the four-byte frame header plus the frame's side information,
# and the header is easy to leave out: the numbers below are the *side
# information* lengths, by version and by whether the stream is mono, and
# `_FRAME_HEADER_BYTES` is added to each. Getting it wrong does not fail
# loudly — the marker is simply not found, and every variable-bitrate stream
# quietly falls back to arithmetic that assumes a constant one.
_FRAME_HEADER_BYTES: Final = 4
_SIDE_INFO: Final = {(_MPEG_1, False): 32, (_MPEG_1, True): 17}
_SIDE_INFO_MPEG2: Final = {False: 17, True: 9}
_XING_MARKERS: Final = (b"Xing", b"Info")
_XING_HAS_FRAMES: Final = 0x0001
_MONO: Final = 3


@dataclass(frozen=True, slots=True)
class Sound:
    """One playable sound, resolved to something the daemon can open.

    Attributes:
        path: The local file, as an absolute path.
        duration_seconds: How long it plays for, or `None` when the format is
            not one whose length this module can read.
    """

    path: str
    duration_seconds: float | None


class SoundSource(Protocol):
    """What a player needs in order to turn a request into a file."""

    def resolve(self, url: str) -> Sound | None:
        """Find the bytes a URL names.

        Args:
            url: What was asked for: a local path, a `file://` URL, or an
                `http(s)` URL.

        Returns:
            The sound, or `None` when it cannot be obtained — an address this
            source will not fetch, a file that is not there, or a fetch that
            failed. A player skips to the next item rather than raising, because
            a broken media URL is Home Assistant's problem and should not take
            the satellite down.
        """
        ...


def wav_duration(data: bytes) -> float | None:
    """Read how long a WAV file plays for.

    Args:
        data: The file's bytes.

    Returns:
        The length in seconds, or `None` when the bytes are not a WAV this
        module can measure — a truncated download included.
    """
    if not data.startswith(_RIFF):
        return None
    try:
        with wave.open(io.BytesIO(data), "rb") as opened:
            rate = opened.getframerate()
            frames = opened.getnframes()
    except (wave.Error, EOFError, ValueError, struct.error):
        # This is the one reader that hands untrusted bytes to a parser this
        # repository does not own, so the list is what `wave` is documented and
        # observed to raise, plus `struct.error` — which it reaches for through
        # `chunk`, and which is **not** a `ValueError` subclass. Probing every
        # truncation and every malformed header produced only the first two;
        # the fourth is here so the "a bad download answers `None`" guarantee
        # holds by construction rather than by what anybody happened to try.
        return None
    if rate <= 0 or frames <= 0:
        return None
    return frames / rate


def flac_duration(data: bytes) -> float | None:
    """Read how long a FLAC file plays for.

    The length is in STREAMINFO, which the format requires be the first
    metadata block, so the whole file never has to be read — the header is
    enough, and the satellite's chimes are read at start-up rather than while
    something is waiting for one.

    Args:
        data: The file's bytes, of which only the first forty-two matter.

    Returns:
        The length in seconds, or `None` when the bytes are not a FLAC stream
        or the header claims a rate or a length of zero.
    """
    if not data.startswith(_FLAC):
        return None
    if len(data) < _STREAMINFO_OFFSET + _STREAMINFO_LENGTH:
        return None
    info = data[_STREAMINFO_OFFSET : _STREAMINFO_OFFSET + _STREAMINFO_LENGTH]
    # Bytes 10-17 of STREAMINFO hold, packed end to end: 20 bits of sample
    # rate, 3 of channel count, 5 of bit depth and 36 of total samples. Read as
    # one 64-bit big-endian number, the fields fall out by shifting.
    packed = int(struct.unpack(">Q", info[10:18])[0])
    rate = packed >> 44
    samples = packed & ((1 << 36) - 1)
    if rate <= 0 or samples <= 0:
        return None
    return samples / rate


def _id3_length(data: bytes) -> int:
    """Say how many bytes an ID3v2 tag occupies at the front of a stream.

    Args:
        data: The file's bytes.

    Returns:
        The tag's total length, or zero when there is no tag. The declared size
        is seven bits per byte — the format keeps every byte below 0x80 so a
        tag cannot contain something that looks like a frame sync.
    """
    if not data.startswith(_ID3) or len(data) < _ID3_HEADER_LENGTH:
        return 0
    size = 0
    for byte in data[6:10]:
        size = (size << 7) | (byte & 0x7F)
    total = size + _ID3_HEADER_LENGTH
    if data[5] & _ID3_FOOTER_FLAG:
        total += _ID3_HEADER_LENGTH
    return total


def _frame_header(data: bytes, start: int) -> tuple[int, int, int, int, bool] | None:
    """Find and unpack the first Layer III frame header from `start`.

    Args:
        data: The file's bytes.
        start: Where to begin looking, which is the length of any ID3 tag and
            is therefore never negative. It may be past the end of `data`, for
            a tag that declares more length than the response carried.

    Returns:
        The offset of the header, its version, its bitrate in kilobits per
        second, its sampling rate in hertz, and whether the stream is mono — or
        `None` when no usable Layer III header is within the search window,
        which includes there being no room for one at all.
    """
    # The last offset at which a whole header still fits. When `data` is
    # shorter than a header this is negative, and the loop below must then run
    # zero times — an empty or truncated response is an ordinary outcome for a
    # media URL, and this function's contract is to answer `None` for it.
    # Flooring the bound at zero instead would make `range` yield offset 0 with
    # nothing there to read, turning "no usable header" into an `IndexError`
    # three lines later.
    last = min(len(data) - _FRAME_HEADER_BYTES, start + _SYNC_SEARCH_BYTES)
    for offset in range(start, last + 1):
        if data[offset] != 0xFF or (data[offset + 1] & 0xE0) != 0xE0:
            continue
        version = (data[offset + 1] >> 3) & 0x03
        layer = (data[offset + 1] >> 1) & 0x03
        if version == 1 or layer != _LAYER_3:
            continue
        table = _BITRATES_MPEG1_LAYER3 if version == _MPEG_1 else _BITRATES_MPEG2_LAYER3
        bitrate = table[(data[offset + 2] >> 4) & 0x0F]
        rate = _RATES[version][(data[offset + 2] >> 2) & 0x03]
        if not bitrate or not rate:
            continue
        mono = ((data[offset + 3] >> 6) & 0x03) == _MONO
        return (offset, version, bitrate, rate, mono)
    return None


def _xing_frames(data: bytes, header: int, version: int, mono: bool) -> int:
    """Read the frame count a variable-bitrate encoder wrote into the stream.

    Args:
        data: The file's bytes.
        header: Where the first frame header starts.
        version: The MPEG version from that header.
        mono: Whether the stream is mono.

    Returns:
        The frame count, or zero when there is no Xing or Info header — which
        is an ordinary constant-bitrate stream, sized by arithmetic instead.
    """
    side_info = (
        _SIDE_INFO[version, mono] if version == _MPEG_1 else _SIDE_INFO_MPEG2[mono]
    )
    offset = header + _FRAME_HEADER_BYTES + side_info
    if len(data) < offset + 12:
        return 0
    if data[offset : offset + 4] not in _XING_MARKERS:
        return 0
    flags = int.from_bytes(data[offset + 4 : offset + 8], "big")
    if not flags & _XING_HAS_FRAMES:
        return 0
    return int.from_bytes(data[offset + 8 : offset + 12], "big")


def mp3_duration(data: bytes) -> float | None:
    """Read how long an MPEG Layer III stream plays for.

    Two answers, in order of how much they can be trusted. A variable-bitrate
    encoder writes the exact frame count into the first frame, and that is used
    where it is there. Otherwise the length is the audio's byte count divided by
    the first frame's bitrate, which is exact for a constant-bitrate stream and
    close for anything else — and constant bitrate is what a text-to-speech
    engine produces.

    Args:
        data: The whole file's bytes. The whole file, not a header: the
            constant-bitrate arithmetic is over the byte count.

    Returns:
        The length in seconds, or `None` when the bytes are not a Layer III
        stream this can size.
    """
    tag = _id3_length(data)
    found = _frame_header(data, tag)
    if found is None:
        return None
    header, version, bitrate, rate, mono = found
    frames = _xing_frames(data, header, version, mono)
    if frames:
        return frames * _SAMPLES_PER_FRAME[version] / rate
    audio_bytes = len(data) - header
    if audio_bytes <= 0:
        return None
    return audio_bytes * 8 / (bitrate * 1000)


def _duration_of(data: bytes) -> float | None:
    """Measure a sound however its own bytes say it should be measured.

    Args:
        data: The file's bytes.

    Returns:
        The length in seconds, or `None` for a format this module cannot read.
    """
    return wav_duration(data) or flac_duration(data) or mp3_duration(data)


class FileSoundSource:
    """Resolves paths straight through and fetches remote URLs into a cache."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        fetch: Callable[[str], bytes] | None = None,
    ) -> None:
        """Say where fetched media is kept.

        Args:
            cache_dir: A directory this source may write into. Created on first
                use rather than here, so constructing one touches no disk.
            fetch: How to obtain a remote URL's bytes. Injected so the test
                suite resolves an `http` URL without a socket; the default
                reads it over HTTP with a bounded timeout.
        """
        self._cache_dir = cache_dir
        self._fetch = fetch if fetch is not None else fetch_over_http

    def resolve(self, url: str) -> Sound | None:
        """Find the bytes a URL names, fetching it if it is remote.

        Args:
            url: A local path, a `file://` URL, or an `http(s)` URL.

        Returns:
            The sound, or `None` when it cannot be obtained.
        """
        local = _local_path(url)
        if local is not None:
            return self._measure(local)
        parts = urlsplit(url)
        if parts.scheme not in _FETCHABLE:
            return None
        try:
            data = self._fetch(url)
        except OSError:
            # Every failure a fetch has: refused, timed out, a 404, a name that
            # does not resolve. None of them is this application's fault and
            # none of them is worth ending a conversation over.
            return None
        return self._cache(url, data)

    def _measure(self, path: Path) -> Sound | None:
        """Read a local file's length without copying it anywhere.

        Args:
            path: The file.

        Returns:
            The sound, or `None` when the file is not there.
        """
        try:
            # The whole file, not a header window. Two of the three readers
            # need it — `wave` wants a stream, and the constant-bitrate MPEG
            # arithmetic is over the byte count — and the only local files this
            # resolves are the chimes inside the wheel, which are tens of
            # kilobytes and are read once each at start-up.
            data = path.read_bytes()
        except OSError:
            return None
        return Sound(path=str(path), duration_seconds=_duration_of(data))

    def _cache(self, url: str, data: bytes) -> Sound | None:
        """Write fetched bytes where the daemon can open them.

        The name is derived from the URL rather than from a counter, so the
        same announcement fetched twice occupies one file instead of filling
        the cache with copies.

        Args:
            url: What was fetched, used to name the file.
            data: What came back.

        Returns:
            The sound, or `None` when the cache could not be written.
        """
        target = self._cache_dir / _cache_name(url)
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            # Written beside the target and moved onto it, rather than into it.
            # Two outputs can resolve the same URL at the same time — music and
            # speech are separate players over one cache — and `write_bytes`
            # truncates in place, so the one that finished first could hand the
            # daemon a path the other was in the middle of rewriting. A rename
            # within a directory is atomic, so a reader sees the old bytes or
            # the new ones and never half of either.
            partial = target.with_name(f"{target.name}.{os.getpid()}-{id(data):x}")
            partial.write_bytes(data)
            partial.replace(target)
        except OSError:
            return None
        return Sound(path=str(target), duration_seconds=_duration_of(data))


def _local_path(url: str) -> Path | None:
    """Work out whether a URL already names a file on this machine.

    Args:
        url: What was asked for.

    Returns:
        The path, or `None` when the address is remote. A bare path with no
        scheme is a path; a `file://` URL is unquoted first, because Home
        Assistant percent-encodes what it sends.
    """
    parts = urlsplit(url)
    if parts.scheme == "file":
        return Path(unquote(parts.path))
    if not parts.scheme:
        return Path(url)
    # A single-letter scheme is a Windows drive letter, not a scheme. This
    # application runs on the robot, which is Linux, so it is not a case that
    # arises — it is excluded so that "has a scheme" means what it says.
    if len(parts.scheme) == 1:
        return Path(url)
    return None


def _cache_name(url: str) -> str:
    """Name the file a fetched URL is cached as.

    Args:
        url: What was fetched.

    Returns:
        A file name derived from the URL: stable for a given URL, containing
        nothing that could escape the cache directory, and keeping the original
        extension where there is one so the daemon's demuxer has the hint it
        would have had.
    """
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    suffix = Path(urlsplit(url).path).suffix
    # Only a short, plain extension is carried across; anything else is
    # discarded rather than sanitised, because a name assembled from a remote
    # value is exactly where a path separator would otherwise arrive.
    if not suffix[1:].isalnum() or len(suffix) > 6:
        return digest
    return f"{digest}{suffix.lower()}"


class _HttpOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect handler that re-applies the scheme allowlist every hop.

    The default one permits `ftp:` as well as `http:` and `https:`, so a
    checked address that redirects is no longer a checked address. Checking
    only the address that was asked for would make the allowlist true of the
    first request and of nothing after it.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request | None:
        """Follow a redirect only when its target is still fetchable.

        Args:
            req: The request being redirected.
            fp: The response body, passed through.
            code: The redirect status.
            msg: The status message.
            headers: The response headers, passed through.
            newurl: Where it points.

        Returns:
            The new request, or `None` to stop following.

        Raises:
            OSError: If the redirect leaves the allowlist.
        """
        if urlsplit(newurl).scheme not in _FETCHABLE:
            message = (
                f"refusing to follow a redirect to a {urlsplit(newurl).scheme!r} URL"
            )
            raise OSError(message)
        return super().redirect_request(req, fp, code, msg, headers, newurl)  # type: ignore[arg-type]  # `fp` and `headers` are declared here as `object` because nothing in this override reads them, and narrowing them would restate two urllib types for no gain


def fetch_over_http(url: str) -> bytes:
    """Read a remote URL, refusing anything that is not HTTP.

    Args:
        url: The address to read.

    Returns:
        The body's bytes.

    Raises:
        OSError: If the address is not one this function will open, if a
            redirect leaves the allowlist, if the body is larger than this will
            read, or if the request fails. All are reported the same way
            because the caller treats them the same way: the sound cannot be
            obtained.
    """
    if urlsplit(url).scheme not in _FETCHABLE:
        message = f"refusing to fetch a {urlsplit(url).scheme!r} URL"
        raise OSError(message)
    request = urllib.request.Request(url)  # noqa: S310  # the scheme is checked against an http/https allowlist on the line above, and every redirect is checked again by the handler below, which is precisely what S310 asks for; the URL itself comes from Home Assistant, which is this satellite's configured controller
    opener = urllib.request.build_opener(_HttpOnlyRedirects)
    with opener.open(request, timeout=_FETCH_TIMEOUT_SECONDS) as response:
        # One byte more than the bound, so that a body exactly at the limit is
        # accepted and anything past it is refused rather than truncated into a
        # file the daemon would then try to play.
        body: bytes = response.read(_MAX_FETCH_BYTES + 1)
    if len(body) > _MAX_FETCH_BYTES:
        message = f"refusing a body larger than {_MAX_FETCH_BYTES} bytes"
        raise OSError(message)
    return body
