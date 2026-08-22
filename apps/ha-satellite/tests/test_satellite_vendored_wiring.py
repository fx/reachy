"""The vendored protocol layer, running against the adapters, with both seams shut.

Change 0011 landed the ESPHome protocol layer with two holes in it: upstream
captured through a desktop sound library and played back through a media-player
process, and on this robot both belong to the Reachy Mini daemon. Its own tests
pass with the holes open, because nothing constructs an implementation of
either. These are them filled.

Everything below drives *vendored* code — the satellite's mute path, its ducking,
its audio handler — and what is on the other side of each seam is the real
adapter from this change, over a fake daemon. Neither side imports the other:
the vendored package holds `esphome.seams.MediaPlayback` and
`esphome.seams.AudioCapture`, the adapters implement those shapes structurally,
and `just lint-boundary` is what proves the import direction has not been
inverted to make it work.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from satellite_support import (
    FakeDecoder,
    FakeMedia,
    FakeSoundSource,
    ManualDetach,
    immediately,
    no_sleep,
    sent_packets,
    silence,
    vendored_satellite,
    vendored_server_state,
)

from reachy_mini_ha_satellite.adapters.audio_reachy import (
    ReachyCapture,
    ReachyPlayback,
)
from reachy_mini_ha_satellite.adapters.sounds import SoundSource
from reachy_mini_ha_satellite.esphome.seams import SAMPLE_WIDTH

if TYPE_CHECKING:
    from collections.abc import Callable

_MUTE_SOUND = "/sounds/mute.flac"
_UNMUTE_SOUND = "/sounds/unmute.flac"


def _sounds() -> FakeSoundSource:
    """Register the chimes the vendored code plays.

    Returns:
        A sound source that can resolve them.
    """
    sounds = FakeSoundSource()
    sounds.add(_MUTE_SOUND, "/sounds/mute.flac", 0.4)
    sounds.add(_UNMUTE_SOUND, "/sounds/unmute.flac", 0.4)
    return sounds


class TestTheSeamsAreFilledRatherThanOpen:
    """What `ServerState` holds, once change 0012 has supplied it."""

    def test_the_state_holds_a_real_capture_source(self) -> None:
        """The field 0011 left optional, with something in it."""
        media = FakeMedia()
        state = vendored_server_state(audio_capture=ReachyCapture(media))
        assert isinstance(state.audio_capture, ReachyCapture)

    def test_the_state_holds_real_players_on_both_outputs(self) -> None:
        """Music and announcements, which the vendored code drives separately."""
        media = FakeMedia()
        sounds = _sounds()
        state = vendored_server_state(
            music_player=_playback(media, sounds, name="music"),
            tts_player=_playback(media, sounds, name="speech"),
        )
        assert isinstance(state.music_player, ReachyPlayback)
        assert isinstance(state.tts_player, ReachyPlayback)
        assert state.music_player is not state.tts_player


class TestTheVendoredCodeDrivesTheAdapters:
    """Vendored call sites, real adapters, a fake daemon underneath."""

    def test_muting_plays_the_chime_through_the_daemon(self) -> None:
        """`satellite._set_muted` stops the announcement player and plays.

        Reaching for a private method of a derived file is deliberate: it is
        the vendored code's own mute path, the one its mute-switch entity
        calls, and driving it is what makes this a test of the seam rather than
        of the adapter on its own.
        """
        media = FakeMedia()
        decoder = FakeDecoder()
        detach = ManualDetach()
        sounds = _sounds()
        satellite = vendored_satellite(
            music_player=_playback(media, sounds, decoder=decoder, detach=detach),
            tts_player=_playback(media, sounds, decoder=decoder, detach=detach),
            mute_sound=_MUTE_SOUND,
            unmute_sound=_UNMUTE_SOUND,
        )

        satellite._set_muted(True)
        detach.run()

        assert [path for path, _rate in decoder.decoded] == ["/sounds/mute.flac"]
        assert satellite.state.tts_player.is_playing

    def test_unmuting_plays_the_other_chime(self) -> None:
        """The same path, the other way, so both call sites are exercised."""
        media = FakeMedia()
        decoder = FakeDecoder()
        sounds = _sounds()
        satellite = vendored_satellite(
            music_player=_playback(media, sounds, decoder=decoder),
            tts_player=_playback(media, sounds, decoder=decoder),
            mute_sound=_MUTE_SOUND,
            unmute_sound=_UNMUTE_SOUND,
        )

        satellite._set_muted(False)

        assert [path for path, _rate in decoder.decoded] == ["/sounds/unmute.flac"]

    def test_the_chime_ends_when_its_samples_have_been_played(self) -> None:
        """Which is how the vendored code learns an announcement finished."""
        media = FakeMedia()
        detach = ManualDetach()
        sounds = _sounds()
        satellite = vendored_satellite(
            music_player=_playback(media, sounds, detach=detach),
            tts_player=_playback(media, sounds, detach=detach),
            mute_sound=_MUTE_SOUND,
            unmute_sound=_UNMUTE_SOUND,
        )

        satellite._set_muted(True)
        detach.run_all()

        assert not satellite.state.tts_player.is_playing

    def test_ducking_music_reaches_the_music_adapter_alone(self) -> None:
        """`satellite.duck()` is what runs while the assistant is speaking."""
        media = FakeMedia()
        sounds = _sounds()
        music = _playback(media, sounds, name="music")
        speech = _playback(media, sounds, name="speech")
        satellite = vendored_satellite(music_player=music, tts_player=speech)
        music.set_volume(80.0)
        speech.set_volume(80.0)
        satellite.duck()
        assert music.volume < 80.0
        assert speech.volume == 80.0
        satellite.unduck()
        assert music.volume == 80.0


class TestCapturedAudioReachesTheProtocolLayer:
    """The pump change 0013 writes, run once here to prove the shape fits."""

    def test_chunks_from_the_adapter_are_what_handle_audio_takes(self) -> None:
        """One `bytes` per channel, at the sample width the seam fixes.

        `handle_audio` is the vendored entry point for microphone audio, and
        the discarded upstream entry point is what used to feed it. The loop
        below is that feed, written against the capture seam rather than
        against a sound library.
        """
        media = FakeMedia(audio=[silence(320)])
        capture = ReachyCapture(media, samples_per_chunk=160, sleep=_no_wait)
        satellite = vendored_satellite(audio_capture=capture)
        satellite._is_streaming_audio = True
        satellite.state.muted = False
        capture.start()

        chunks = 0
        while chunks < 2:
            chunk = capture.read_chunk()
            assert chunk is not None
            assert len(chunk[0]) == 160 * SAMPLE_WIDTH
            satellite.handle_audio(chunk[0], chunk[1] if len(chunk) > 1 else None)
            chunks += 1
        capture.stop()

        assert sent_packets(satellite) == 2

    def test_a_muted_satellite_sends_nothing_it_was_handed(self) -> None:
        """The vendored gate, still gating, with a real source behind it."""
        media = FakeMedia(audio=[silence(160)])
        capture = ReachyCapture(media, samples_per_chunk=160, sleep=_no_wait)
        satellite = vendored_satellite(audio_capture=capture)
        satellite._is_streaming_audio = True
        satellite.state.muted = True
        capture.start()
        chunk = capture.read_chunk()
        assert chunk is not None
        satellite.handle_audio(chunk[0])
        assert sent_packets(satellite) == 0


def _no_wait(seconds: float) -> None:
    """Stand in for a wait between capture polls without performing one.

    Args:
        seconds: How long the caller wanted to wait, ignored.
    """
    del seconds


def _playback(
    media: FakeMedia,
    sounds: SoundSource,
    *,
    name: str = "playback",
    decoder: FakeDecoder | None = None,
    detach: Callable[[Callable[[], None]], None] = immediately,
) -> ReachyPlayback:
    """Build a real output over the fake daemon, with decoding faked.

    The point of this file is that the *vendored* code drives real adapters, so
    the player is the real one. Only the two things a unit test may not do —
    read a file and start a thread — are stood in for.

    Args:
        media: The fake daemon's media layer.
        sounds: How a URL becomes a path.
        name: What the output calls itself in a log line.
        decoder: How a path becomes samples.
        detach: How work leaves the calling thread.

    Returns:
        The output.
    """
    return ReachyPlayback(
        media,
        sounds,
        name=name,
        detach=detach,
        sleep=no_sleep,
        decode=decoder if decoder is not None else FakeDecoder(),
    )
