"""The two audio seams cut into the vendored ESPHome satellite.

This module is NOT vendored. It is the deliberate shape of the two holes left
where upstream reached for a desktop audio library, and it is the only file in
this directory that is type-checked in strict mode.

Upstream captured microphone audio through the `soundcard` package and played
media back through an `mpv` subprocess. On this robot both belong to the Reachy
Mini daemon, which owns the microphone array and the speaker, so neither library
can be carried. Each becomes a narrow interface here, at the point the vendored
code used the library directly:

* `MediaPlayback` replaces `linux_voice_assistant.mpv_player.MpvMediaPlayer`.
  It is the exact surface the carried code calls on `ServerState.music_player`,
  `ServerState.tts_player` and `MediaPlayerEntity`, and nothing more.
* `AudioCapture` replaces the `soundcard` recorder that upstream opened inside
  its command-line entry point, which is not carried. It is reached through
  `ServerState.audio_capture`.

Both are `Protocol`s, so an implementation satisfies them structurally and never
imports anything from this directory — the dependency runs one way, from the
robot side towards the vendored code, and a lint rule enforces it (see the
`flake8-tidy-imports` configuration in the repository-root `pyproject.toml`).

**Neither seam is implemented by this change.** Change 0012 supplies the
daemon-backed adapters. Until it does, the vendored package imports and its
carried tests pass with both holes open, because nothing here constructs an
implementation: the fields that hold one are optional or supplied by the caller.
"""

#:= docs/specs/architecture/index.md#req-005-behaviour-is-testable-without-hardware
#:% Every workspace member MUST expose its behaviour through interfaces that allow
#:% its full test suite to run without a robot, a camera, or a microphone attached.

from collections.abc import Callable, Sequence
from typing import Protocol, runtime_checkable

# Upstream captured at 16 kHz, signed 16-bit little-endian PCM, and the
# wake-word models it carries are trained for exactly that. The constants are
# here rather than in an adapter so both sides of the seam agree on them.
SAMPLE_RATE = 16000
"""Sample rate, in hertz, of every chunk crossing the capture seam."""

SAMPLE_WIDTH = 2
"""Bytes per sample: signed 16-bit little-endian PCM."""


@runtime_checkable
class MediaPlayback(Protocol):
    """Plays a media URL, and ducks under speech.

    Replaces upstream's `MpvMediaPlayer`. Two instances exist at run time: one
    for music, which Home Assistant drives through the media-player entity, and
    one for announcements and the satellite's own chimes.

    `done_callback` is invoked from whatever thread the implementation finishes
    playback on, which is not necessarily the event loop thread; the carried
    code already assumes that and hops threads where it matters.
    """

    def play(
        self,
        url: str | list[str],
        done_callback: Callable[[], None] | None = None,
        stop_first: bool = False,
    ) -> None:
        """Play one URL, or a list of URLs in sequence."""
        ...

    def pause(self) -> None:
        """Pause playback, keeping the current position."""
        ...

    def resume(self) -> None:
        """Resume playback paused by `pause`."""
        ...

    def stop(self) -> None:
        """Stop playback and invoke any pending `done_callback`."""
        ...

    @property
    def is_playing(self) -> bool:
        """Whether media is currently playing, loading or paused."""
        ...

    def set_volume(self, volume: float) -> None:
        """Set the playback volume, in percent (0.0-100.0)."""
        ...

    def duck(self, factor: float = 0.5) -> None:
        """Temporarily scale the volume down by `factor`."""
        ...

    def unduck(self) -> None:
        """Restore the volume a `duck` scaled down."""
        ...


@runtime_checkable
class AudioCapture(Protocol):
    """Yields microphone audio, one fixed-size chunk at a time.

    Replaces the `soundcard` recorder upstream opened in its command-line entry
    point. `read_chunk` returns one `bytes` per channel rather than an
    interleaved buffer, because that is the shape the carried code needs:
    channel 0 drives wake-word detection and is what Home Assistant transcribes,
    while channel 1, when the device has one, carries the speaker reference the
    server-side echo canceller wants.

    `read_chunk` blocks until a chunk is available and returns `None` once the
    source is closed, so a caller loops until it sees `None`.
    """

    @property
    def channels(self) -> int:
        """Number of channels each `read_chunk` returns."""
        ...

    @property
    def samples_per_chunk(self) -> int:
        """Samples per channel in each chunk `read_chunk` returns."""
        ...

    def start(self) -> None:
        """Begin capturing. Calling this twice is not an error."""
        ...

    def stop(self) -> None:
        """Stop capturing and release the device."""
        ...

    def read_chunk(self) -> Sequence[bytes] | None:
        """Return one chunk per channel, or `None` when the source is closed."""
        ...
