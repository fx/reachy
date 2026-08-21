"""Capture and playback, against a daemon that is a fake and never a device.

Every test here drives the real adapter code. What is faked is the thing on the
far side of it — the daemon's media interface — because that is the boundary
ha-satellite REQ-043 is about: what these tests establish is that the adapter
goes *through* that interface and never round it, which is a property of the
calls it makes rather than of any audio anybody heard.

Nothing sleeps and nothing waits. `ReachyCapture` takes its sleep as an argument
so that a test drives a blocking read to completion without spending any wall
time, and `ReachyPlayback` takes its scheduler for the same reason: playback
completion is a timer, and a suite that waited for one would be slow and would
still only be testing `threading.Timer`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pytest
from satellite_support import (
    FakeMedia,
    FakeSoundSource,
    ManualScheduler,
    immediately,
    silence,
    tone,
)

from reachy_mini_ha_satellite.adapters.audio_reachy import (
    DEFAULT_CHANNELS,
    UNKNOWN_LENGTH_SECONDS,
    AudioSourceError,
    ReachyAudio,
    ReachyCapture,
    ReachyPlayback,
    ThreadScheduler,
)
from reachy_mini_ha_satellite.adapters.sounds import Sound
from reachy_mini_ha_satellite.esphome.seams import SAMPLE_RATE, SAMPLE_WIDTH

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


def _never_sleeps(seconds: float) -> None:
    """Stand in for a wait without performing one.

    Args:
        seconds: How long the caller wanted to wait, ignored.
    """
    del seconds


def _samples(chunk: Sequence[bytes], channel: int = 0) -> list[int]:
    """Read one channel of a chunk back as whole numbers.

    Args:
        chunk: What `read_chunk` handed back.
        channel: Which channel to read.

    Returns:
        The samples.
    """
    return [int(value) for value in np.frombuffer(chunk[channel], dtype="<i2")]


class TestCaptureGoesThroughTheDaemon:
    """REQ-043: the microphone array belongs to the daemon, not to this."""

    def test_starting_starts_the_daemons_pipeline(self) -> None:
        """No device is opened, because there is nothing here that could."""
        media = FakeMedia()
        capture = ReachyCapture(media, sleep=_never_sleeps)
        capture.start()
        assert media.recording

    def test_stopping_stops_it_again(self) -> None:
        """REQ-050's release, at the level of the one thing capture holds."""
        media = FakeMedia()
        capture = ReachyCapture(media, sleep=_never_sleeps)
        capture.start()
        capture.stop()
        assert not media.recording

    def test_starting_twice_is_not_an_error(self) -> None:
        """The seam says so, and a restart racing a start is ordinary."""
        media = FakeMedia()
        capture = ReachyCapture(media, sleep=_never_sleeps)
        capture.start()
        capture.start()
        assert media.recording

    def test_a_daemon_at_the_wrong_rate_is_refused_loudly(self) -> None:
        """A silent mismatch is a robot that ignores you and explains nothing.

        Capture at 48 kHz still produces chunks; the wake-word models simply
        never fire on them. That failure has nothing to debug, so it is turned
        into one that names itself at start-up.
        """
        capture = ReachyCapture(FakeMedia(sample_rate=48000), sleep=_never_sleeps)
        with pytest.raises(AudioSourceError, match="48000"):
            capture.start()

    def test_a_daemon_with_no_audio_device_is_refused_too(self) -> None:
        """The daemon reports a negative rate, which is not the pipeline's."""
        capture = ReachyCapture(FakeMedia(sample_rate=-1), sleep=_never_sleeps)
        with pytest.raises(AudioSourceError):
            capture.start()

    def test_the_channel_count_comes_from_the_daemon(self) -> None:
        """The vendored protocol layer reads it while building its entities."""
        capture = ReachyCapture(FakeMedia(channels=1), sleep=_never_sleeps)
        assert capture.channels == 1

    def test_a_daemon_with_no_device_reports_the_default_channel_count(
        self,
    ) -> None:
        """A negative answer is not a channel count."""
        capture = ReachyCapture(FakeMedia(channels=-1), sleep=_never_sleeps)
        assert capture.channels == DEFAULT_CHANNELS


class TestCaptureRebuffers:
    """The daemon's blocks are whatever size its pipeline made them."""

    def test_a_chunk_is_the_requested_length_in_the_seams_format(self) -> None:
        """16 kHz, signed 16-bit little-endian, one `bytes` per channel."""
        media = FakeMedia(audio=[silence(160)])
        capture = ReachyCapture(media, samples_per_chunk=160, sleep=_never_sleeps)
        capture.start()
        chunk = capture.read_chunk()
        assert chunk is not None
        assert len(chunk) == 2
        assert len(chunk[0]) == 160 * SAMPLE_WIDTH

    def test_several_small_blocks_become_one_chunk(self) -> None:
        """A pipeline that produces 40 samples at a time still feeds 160."""
        media = FakeMedia(audio=[silence(40) for _ in range(4)])
        capture = ReachyCapture(media, samples_per_chunk=160, sleep=_never_sleeps)
        capture.start()
        chunk = capture.read_chunk()
        assert chunk is not None
        assert len(chunk[0]) == 160 * SAMPLE_WIDTH

    def test_one_large_block_becomes_several_chunks(self) -> None:
        """And the remainder is kept, rather than dropped on the floor."""
        media = FakeMedia(audio=[silence(320)])
        capture = ReachyCapture(media, samples_per_chunk=160, sleep=_never_sleeps)
        capture.start()
        assert capture.read_chunk() is not None
        assert capture.read_chunk() is not None

    def test_a_dry_source_is_waited_on_rather_than_reported_as_the_end(
        self,
    ) -> None:
        """`read_chunk` blocks; only a stop ends the loop."""
        waits: list[float] = []
        media = FakeMedia(audio=[silence(80)])

        def _record(seconds: float) -> None:
            waits.append(seconds)
            if len(waits) == 3:
                # The microphone produces the rest of the chunk, three polls
                # in. Standing in for "the pipeline caught up", without a
                # clock.
                media.audio.append(silence(80))

        capture = ReachyCapture(media, samples_per_chunk=160, sleep=_record)
        capture.start()
        chunk = capture.read_chunk()
        assert chunk is not None
        assert len(waits) == 3

    def test_stopping_unblocks_a_reader_with_the_end_of_the_stream(self) -> None:
        """`None` is how the pump upstream of this learns to finish."""
        capture = ReachyCapture(FakeMedia(audio=[]), sleep=_never_sleeps)
        capture.start()
        capture.stop()
        assert capture.read_chunk() is None

    def test_float_samples_are_scaled_into_the_full_signed_range(self) -> None:
        """Full scale is 32767, so a 1.0 does not wrap to the loudest click."""
        media = FakeMedia(
            audio=[tone([1.0, -1.0, 0.0, 0.5], channels=1)],
            channels=1,
        )
        capture = ReachyCapture(media, samples_per_chunk=4, sleep=_never_sleeps)
        capture.start()
        chunk = capture.read_chunk()
        assert chunk is not None
        assert _samples(chunk) == [32767, -32767, 0, 16384]

    def test_the_channels_stay_apart(self) -> None:
        """Channel 1 is the speaker reference; mixing them would be silent."""
        media = FakeMedia(audio=[tone([0.5, 0.5], channels=2)])
        capture = ReachyCapture(media, samples_per_chunk=2, sleep=_never_sleeps)
        capture.start()
        chunk = capture.read_chunk()
        assert chunk is not None
        assert _samples(chunk, 0) != _samples(chunk, 1)

    def test_a_mono_block_is_widened_to_the_promised_channel_count(self) -> None:
        """The promise was made before any audio arrived; it is kept."""
        flat = np.asarray([0.25, 0.25], dtype=np.float32)
        media = FakeMedia(audio=[flat], channels=2)
        capture = ReachyCapture(media, samples_per_chunk=2, sleep=_never_sleeps)
        capture.start()
        chunk = capture.read_chunk()
        assert chunk is not None
        assert len(chunk) == 2
        assert _samples(chunk, 0) == _samples(chunk, 1)

    def test_an_over_wide_block_is_cropped_to_the_promised_count(self) -> None:
        """A chunk that changed shape mid-session would break the pipeline."""
        media = FakeMedia(audio=[silence(2, channels=4)], channels=2)
        capture = ReachyCapture(media, samples_per_chunk=2, sleep=_never_sleeps)
        capture.start()
        chunk = capture.read_chunk()
        assert chunk is not None
        assert len(chunk) == 2

    def test_a_chunk_of_no_samples_is_refused_at_construction(self) -> None:
        """It would make `read_chunk` a busy loop returning empty bytes."""
        with pytest.raises(ValueError, match="at least one sample"):
            ReachyCapture(FakeMedia(), samples_per_chunk=0)

    def test_the_pipeline_rate_is_the_one_the_seam_fixes(self) -> None:
        """Both sides of the seam agree on it because there is one copy."""
        assert SAMPLE_RATE == 16000


class TestPlaybackGoesThroughTheDaemon:
    """REQ-043 again, for the speaker."""

    def test_playing_hands_the_daemon_a_local_file(self) -> None:
        """Nothing here opens an output; the daemon does."""
        media = FakeMedia()
        sounds = FakeSoundSource()
        sounds.add("chime", "/sounds/chime.flac", 0.5)
        player = ReachyPlayback(
            media, sounds, scheduler=ManualScheduler(), detach=immediately
        )
        player.play("chime")
        assert media.played == ["/sounds/chime.flac"]
        assert player.is_playing

    def test_a_sound_ends_when_its_own_length_has_elapsed(self) -> None:
        """The daemon reports no completion, so the length is the signal."""
        scheduler = ManualScheduler()
        sounds = FakeSoundSource()
        sounds.add("chime", "/sounds/chime.flac", 0.75)
        player = ReachyPlayback(
            FakeMedia(), sounds, scheduler=scheduler, detach=immediately
        )
        finished: list[str] = []
        player.play("chime", done_callback=lambda: finished.append("done"))
        assert scheduler.pending is not None
        assert scheduler.pending.delay == 0.75
        scheduler.fire()
        assert finished == ["done"]
        assert not player.is_playing

    def test_a_list_plays_in_order_and_reports_once_at_the_end(self) -> None:
        """Which is what an announcement made of several parts needs."""
        scheduler = ManualScheduler()
        media = FakeMedia()
        sounds = FakeSoundSource()
        sounds.add("one", "/sounds/one.wav", 0.1)
        sounds.add("two", "/sounds/two.wav", 0.2)
        player = ReachyPlayback(media, sounds, scheduler=scheduler, detach=immediately)
        finished: list[str] = []
        player.play(["one", "two"], done_callback=lambda: finished.append("done"))
        assert media.played == ["/sounds/one.wav"]
        scheduler.fire()
        assert media.played == ["/sounds/one.wav", "/sounds/two.wav"]
        assert finished == []
        scheduler.fire()
        assert finished == ["done"]

    def test_stopping_invokes_the_callback_the_caller_was_owed(self) -> None:
        """The vendored protocol layer stops the player *in order to* do this.

        `satellite.py` calls `tts_player.stop()` and relies on the completion
        callback firing, which is how it makes the "announcement finished"
        transition happen. A stop that swallowed it would hang the state
        machine.
        """
        sounds = FakeSoundSource()
        sounds.add("chime", "/sounds/chime.flac", 5.0)
        player = ReachyPlayback(
            FakeMedia(), sounds, scheduler=ManualScheduler(), detach=immediately
        )
        finished: list[str] = []
        player.play("chime", done_callback=lambda: finished.append("done"))
        player.stop()
        assert finished == ["done"]
        assert not player.is_playing

    def test_stopping_silences_the_daemons_output(self) -> None:
        """There is one output, so stopping means stopping the daemon's."""
        media = FakeMedia()
        player = ReachyPlayback(
            media,
            FakeSoundSource(),
            scheduler=ManualScheduler(),
            detach=immediately,
        )
        player.stop()
        assert media.stop_playing_calls == 1

    def test_superseding_a_sound_reports_the_one_it_replaced(self) -> None:
        """A callback that silently never fires leaves a caller waiting."""
        scheduler = ManualScheduler()
        sounds = FakeSoundSource()
        sounds.add("first", "/sounds/first.wav", 5.0)
        sounds.add("second", "/sounds/second.wav", 5.0)
        player = ReachyPlayback(
            FakeMedia(), sounds, scheduler=scheduler, detach=immediately
        )
        finished: list[str] = []
        player.play("first", done_callback=lambda: finished.append("first"))
        player.play("second", done_callback=lambda: finished.append("second"))
        assert finished == ["first"]
        scheduler.fire()
        assert finished == ["first", "second"]

    def test_the_superseded_sounds_timer_does_not_fire_later(self) -> None:
        """Otherwise the replacement would end when its predecessor would."""
        scheduler = ManualScheduler()
        sounds = FakeSoundSource()
        sounds.add("first", "/sounds/first.wav", 5.0)
        sounds.add("second", "/sounds/second.wav", 1.0)
        player = ReachyPlayback(
            FakeMedia(), sounds, scheduler=scheduler, detach=immediately
        )
        player.play("first")
        player.play("second")
        pending = scheduler.pending
        assert pending is not None
        assert pending.delay == 1.0
        assert scheduler.scheduled[0].cancelled

    def test_an_unresolvable_sound_is_skipped_and_the_rest_still_play(
        self,
    ) -> None:
        """A media URL Home Assistant cannot serve is its problem, not ours."""
        media = FakeMedia()
        sounds = FakeSoundSource()
        sounds.add("good", "/sounds/good.wav", 0.1)
        player = ReachyPlayback(
            media, sounds, scheduler=ManualScheduler(), detach=immediately
        )
        player.play(["missing", "good"])
        assert media.played == ["/sounds/good.wav"]

    def test_a_request_that_resolves_to_nothing_still_reports_completion(
        self,
    ) -> None:
        """The caller is waiting to be told it finished, and it has."""
        player = ReachyPlayback(
            FakeMedia(),
            FakeSoundSource(),
            scheduler=ManualScheduler(),
            detach=immediately,
        )
        finished: list[str] = []
        player.play("missing", done_callback=lambda: finished.append("done"))
        assert finished == ["done"]
        assert not player.is_playing

    def test_a_sound_of_unreadable_length_is_bounded_rather_than_measured(
        self,
    ) -> None:
        """The daemon offers no end-of-stream, and not every format has a length.

        WAV, FLAC and MP3 are read — between them, everything this application
        ships and everything Home Assistant's text-to-speech serves. What is
        left is bounded so that a completion cannot be lost outright.
        """
        scheduler = ManualScheduler()
        sounds = FakeSoundSource()
        sounds.add("speech", "/cache/speech.opus", None)
        player = ReachyPlayback(
            FakeMedia(), sounds, scheduler=scheduler, detach=immediately
        )
        player.play("speech")
        pending = scheduler.pending
        assert pending is not None
        assert pending.delay == UNKNOWN_LENGTH_SECONDS
        assert player.is_playing

    def test_pausing_silences_the_output_and_keeps_the_sound(self) -> None:
        """Which is what the vendored code does before an announcement."""
        media = FakeMedia()
        scheduler = ManualScheduler()
        sounds = FakeSoundSource()
        sounds.add("music", "/sounds/music.wav", 30.0)
        player = ReachyPlayback(media, sounds, scheduler=scheduler, detach=immediately)
        player.play("music")
        player.pause()
        assert media.stop_playing_calls == 1
        assert player.is_playing
        assert scheduler.pending is None

    def test_resuming_restarts_the_item_because_there_is_no_seek(self) -> None:
        """Stated rather than hidden: the daemon's interface has no position.

        Restarting the item *and* re-timing the whole of it is the only pair of
        choices that leaves the audio and the completion callback agreeing.
        """
        media = FakeMedia()
        scheduler = ManualScheduler()
        sounds = FakeSoundSource()
        sounds.add("music", "/sounds/music.wav", 30.0)
        player = ReachyPlayback(media, sounds, scheduler=scheduler, detach=immediately)
        player.play("music")
        player.pause()
        player.resume()
        assert media.played == ["/sounds/music.wav", "/sounds/music.wav"]
        assert scheduler.pending is not None
        assert scheduler.pending.delay == 30.0

    def test_resuming_something_that_was_not_paused_does_nothing(self) -> None:
        """The vendored code resumes music that may never have been playing."""
        media = FakeMedia()
        player = ReachyPlayback(
            media,
            FakeSoundSource(),
            scheduler=ManualScheduler(),
            detach=immediately,
        )
        player.resume()
        assert media.played == []

    def test_a_volume_is_recorded_and_reported(self) -> None:
        """It changes nothing audible; the daemon exposes no output gain."""
        player = ReachyPlayback(FakeMedia(), FakeSoundSource(), detach=immediately)
        player.set_volume(40.0)
        assert player.volume == pytest.approx(40.0)

    def test_ducking_scales_the_reported_level_and_unducking_restores_it(
        self,
    ) -> None:
        """So a settings interface can show what was asked for."""
        player = ReachyPlayback(FakeMedia(), FakeSoundSource(), detach=immediately)
        player.set_volume(80.0)
        player.duck(0.25)
        assert player.volume == pytest.approx(20.0)
        player.unduck()
        assert player.volume == pytest.approx(80.0)

    def test_a_failing_completion_callback_does_not_escape(self) -> None:
        """It runs on a timer thread, where an exception has nowhere to go."""
        scheduler = ManualScheduler()
        sounds = FakeSoundSource()
        sounds.add("chime", "/sounds/chime.flac", 0.1)
        player = ReachyPlayback(
            FakeMedia(), sounds, scheduler=scheduler, detach=immediately
        )

        def _explode() -> None:
            message = "the caller's callback failed"
            raise RuntimeError(message)

        player.play("chime", done_callback=_explode)
        scheduler.fire()
        assert not player.is_playing


class TestTheAudioPortIsOneLifecycle:
    """The microphone and the speaker are one piece of hardware."""

    def test_starting_takes_up_capture(self) -> None:
        """Playback needs nothing started: the daemon builds it per sound."""
        media = FakeMedia()
        audio = ReachyAudio(
            media,
            FakeSoundSource(),
            scheduler=ManualScheduler(),
            detach=immediately,
        )
        audio.start()
        assert media.recording

    def test_stopping_releases_everything_the_application_held(self) -> None:
        """REQ-050: stop capturing, stop both outputs, let the daemon have it."""
        media = FakeMedia()
        sounds = FakeSoundSource()
        sounds.add("music", "/sounds/music.wav", 30.0)
        sounds.add("chime", "/sounds/chime.flac", 1.0)
        audio = ReachyAudio(
            media,
            sounds,
            scheduler=ManualScheduler(),
            detach=immediately,
        )
        audio.start()
        audio.music.play("music")
        audio.speech.play("chime")
        audio.stop()
        assert not media.recording
        assert not audio.music.is_playing
        assert not audio.speech.is_playing
        assert not media.playing

    def test_stopping_twice_is_harmless(self) -> None:
        """A termination signal and an ordinary shutdown can both arrive."""
        media = FakeMedia()
        audio = ReachyAudio(
            media,
            FakeSoundSource(),
            scheduler=ManualScheduler(),
            detach=immediately,
        )
        audio.start()
        audio.stop()
        audio.stop()
        assert not media.recording

    def test_shutdown_is_terminal_for_capture(self) -> None:
        """REQ-050: released, not released and taken straight back.

        A behaviour tick or an ESPHome packet already in flight can reach any
        of the three surfaces after the termination signal, and the port's
        motion half is explicitly terminal. This one has to be too, or a
        shutdown hands the microphone back a moment after letting go of it.
        """
        media = FakeMedia()
        audio = ReachyAudio(
            media,
            FakeSoundSource(),
            scheduler=ManualScheduler(),
            detach=immediately,
        )
        audio.start()
        audio.stop()
        audio.capture.start()
        assert not media.recording
        assert audio.released

    def test_shutdown_is_terminal_for_both_outputs(self) -> None:
        """A packet that arrived a moment early must not reopen the speaker."""
        media = FakeMedia()
        sounds = FakeSoundSource()
        sounds.add("chime", "/sounds/chime.flac", 1.0)
        audio = ReachyAudio(
            media,
            sounds,
            scheduler=ManualScheduler(),
            detach=immediately,
        )
        audio.start()
        audio.stop()
        finished: list[str] = []
        audio.music.play("chime")
        audio.speech.play("chime", done_callback=lambda: finished.append("done"))
        assert media.played == []
        assert not audio.music.is_playing
        assert not audio.speech.is_playing
        # Still told it finished: a caller waiting on an announcement that
        # arrived a moment before the shutdown has nothing left to wait for.
        assert finished == ["done"]

    def test_a_released_output_does_not_resume(self) -> None:
        """Resuming paused music would reopen the speaker just as `play` would."""
        media = FakeMedia()
        sounds = FakeSoundSource()
        sounds.add("music", "/sounds/music.wav", 30.0)
        player = ReachyPlayback(
            media,
            sounds,
            scheduler=ManualScheduler(),
            detach=immediately,
        )
        player.play("music")
        player.pause()
        player.release()
        player.resume()
        assert media.played == ["/sounds/music.wav"]
        assert player.released

    def test_the_two_outputs_are_separate_objects(self) -> None:
        """Music and speech are ducked and paused independently."""
        audio = ReachyAudio(FakeMedia(), FakeSoundSource(), detach=immediately)
        assert audio.music is not audio.speech


class TestTheDefaultScheduler:
    """The one piece that is a timer thread rather than a fake."""

    def test_a_cancelled_call_never_runs(self) -> None:
        """Cancelling is what supersession and stopping both do."""
        scheduler = ThreadScheduler()
        ran: list[str] = []
        # An hour away, so the assertion below is about the cancellation and
        # not about how quickly this test got to the next line.
        handle = scheduler.call_after(3600.0, lambda: ran.append("late"))
        handle.cancel()
        assert ran == []


class TestTheHazardsOfTimersAndChannels:
    """Three defects that are silent when they are present."""

    def test_a_completion_already_running_cannot_finish_its_replacement(
        self,
    ) -> None:
        """`Timer.cancel` cannot stop a callback that has already begun.

        The window is real: `play` takes the lock, and a timer action that
        entered `_finished` just before it waits there and then runs against
        state that now belongs to a different sound. Without a generation it
        would mark the replacement finished — firing a callback the vendored
        protocol layer reads as "the announcement ended" while it is still
        playing.
        """
        scheduler = ManualScheduler()
        sounds = FakeSoundSource()
        sounds.add("first", "/sounds/first.wav", 1.0)
        sounds.add("second", "/sounds/second.wav", 30.0)
        player = ReachyPlayback(
            FakeMedia(), sounds, scheduler=scheduler, detach=immediately
        )
        player.play("first")
        stale = scheduler.scheduled[0]
        finished: list[str] = []
        player.play("second", done_callback=lambda: finished.append("second"))
        # The superseded sound's action, arriving after the replacement is
        # already playing. Invoked directly, which is what a timer thread that
        # had already started would do.
        stale.action()
        assert finished == []
        assert player.is_playing

    def test_a_sound_of_unreadable_length_still_completes_eventually(
        self,
    ) -> None:
        """A callback that never fires wedges the caller's state machine.

        The daemon reports no end of stream and an MP3 fetched from Home
        Assistant may not carry a readable length, so the completion is bounded
        rather than measured. Late is worse than exact and better than never.
        """
        scheduler = ManualScheduler()
        sounds = FakeSoundSource()
        sounds.add("speech", "/cache/speech.opus", None)
        player = ReachyPlayback(
            FakeMedia(), sounds, scheduler=scheduler, detach=immediately
        )
        finished: list[str] = []
        player.play("speech", done_callback=lambda: finished.append("done"))
        pending = scheduler.pending
        assert pending is not None
        assert pending.delay == UNKNOWN_LENGTH_SECONDS
        scheduler.fire()
        assert finished == ["done"]

    def test_ducking_to_silence_is_not_reported_as_full_volume(self) -> None:
        """Zero is falsy, and a factor of zero is a request rather than absence."""
        player = ReachyPlayback(FakeMedia(), FakeSoundSource(), detach=immediately)
        player.set_volume(80.0)
        player.duck(0.0)
        assert player.volume == pytest.approx(0.0)

    def test_a_narrow_block_keeps_the_channels_it_brought(self) -> None:
        """Only the shortfall is padded.

        Rebuilding every channel from the first would hand the server-side
        echo canceller a copy of the microphone as its own speaker reference,
        which makes it subtract the signal from itself.
        """
        block = np.zeros((2, 2), dtype=np.float32)
        block[:, 0] = 0.25
        block[:, 1] = 0.75
        media = FakeMedia(audio=[block], channels=3)
        capture = ReachyCapture(media, samples_per_chunk=2, sleep=_never_sleeps)
        capture.start()
        chunk = capture.read_chunk()
        assert chunk is not None
        assert len(chunk) == 3
        assert _samples(chunk, 0) != _samples(chunk, 1)
        assert _samples(chunk, 2) == _samples(chunk, 0)


class TestResolvingASoundStaysOffTheCallingThread:
    """`play` is called from the loop the ESPHome protocol runs on."""

    def test_play_returns_before_the_sound_has_been_resolved(self) -> None:
        """`process_packet` invokes this directly, so it cannot block on a fetch.

        A media URL Home Assistant supplied is fetched during resolution. Doing
        that inline would stop the satellite answering anything — pings
        included — for as long as the fetch took, and would delay releasing the
        media interface on shutdown by the same amount.
        """
        media = FakeMedia()
        sounds = _SlowSource()
        detached: list[Callable[[], None]] = []
        player = ReachyPlayback(
            media,
            sounds,
            scheduler=ManualScheduler(),
            detach=detached.append,
        )
        player.play("https://198.51.100.10/track.mp3")
        assert media.played == []
        assert detached
        # The seam counts loading as playing, and it has to: `play` returns
        # before the sound exists.
        assert player.is_playing
        detached[0]()
        assert media.played == ["/cache/track.mp3"]

    def test_a_resolution_the_caller_superseded_is_abandoned(self) -> None:
        """A fetch in flight when a new request lands must not start late."""
        media = FakeMedia()
        sounds = FakeSoundSource()
        sounds.add("slow", "/cache/slow.mp3", 5.0)
        sounds.add("quick", "/sounds/quick.wav", 1.0)
        detached: list[Callable[[], None]] = []
        player = ReachyPlayback(
            media,
            sounds,
            scheduler=ManualScheduler(),
            detach=detached.append,
        )
        player.play("slow")
        player.play("quick")
        # The second request's resolution runs first; the first one's arrives
        # afterwards and finds its generation gone.
        detached[1]()
        detached[0]()
        assert media.played == ["/sounds/quick.wav"]

    def test_a_stop_during_a_resolution_leaves_nothing_playing(self) -> None:
        """The vendored code stops the announcement player to cancel one."""
        media = FakeMedia()
        sounds = FakeSoundSource()
        sounds.add("slow", "/cache/slow.mp3", 5.0)
        detached: list[Callable[[], None]] = []
        player = ReachyPlayback(
            media,
            sounds,
            scheduler=ManualScheduler(),
            detach=detached.append,
        )
        finished: list[str] = []
        player.play("slow", done_callback=lambda: finished.append("done"))
        player.stop()
        assert finished == ["done"]
        detached[0]()
        assert media.played == []
        assert not player.is_playing

    def test_the_next_item_in_a_list_is_resolved_off_the_thread_too(
        self,
    ) -> None:
        """Completion arrives on a timer thread, which must not fetch either."""
        media = FakeMedia()
        sounds = FakeSoundSource()
        sounds.add("one", "/sounds/one.wav", 0.1)
        sounds.add("two", "/sounds/two.wav", 0.2)
        scheduler = ManualScheduler()
        detached: list[Callable[[], None]] = []
        player = ReachyPlayback(
            media,
            sounds,
            scheduler=scheduler,
            detach=detached.append,
        )
        player.play(["one", "two"])
        detached[0]()
        assert media.played == ["/sounds/one.wav"]
        scheduler.fire()
        assert media.played == ["/sounds/one.wav"]
        detached[1]()
        assert media.played == ["/sounds/one.wav", "/sounds/two.wav"]

    def test_pausing_during_a_resolution_does_not_start_the_sound(self) -> None:
        """The one thing a pause could otherwise fail to stop.

        The item has left the queue and has not reached the daemon, so a pause
        that only looked at what was playing would let the fetch finish and
        start it anyway.
        """
        media = FakeMedia()
        sounds = FakeSoundSource()
        sounds.add("slow", "/cache/slow.mp3", 5.0)
        detached: list[Callable[[], None]] = []
        player = ReachyPlayback(
            media,
            sounds,
            scheduler=ManualScheduler(),
            detach=detached.append,
        )
        player.play("slow")
        player.pause()
        detached[0]()
        assert media.played == []

    def test_resuming_after_that_pause_resolves_the_item_again(self) -> None:
        """It went back to the head of the queue rather than being lost."""
        media = FakeMedia()
        sounds = FakeSoundSource()
        sounds.add("slow", "/cache/slow.mp3", 5.0)
        detached: list[Callable[[], None]] = []
        player = ReachyPlayback(
            media,
            sounds,
            scheduler=ManualScheduler(),
            detach=detached.append,
        )
        player.play("slow")
        player.pause()
        detached[0]()
        player.resume()
        detached[1]()
        assert media.played == ["/cache/slow.mp3"]


class TestAResolverThatRaisesCannotWedgeTheOutput:
    """The blast radius of the truncated-file defect, closed at the player."""

    def test_a_resolver_that_raises_is_treated_as_an_unplayable_sound(
        self,
    ) -> None:
        """It runs on a detached thread, where an exception has nowhere to go.

        Left to escape, it kills that thread silently: `_loading` stays set, so
        the player reports itself playing for ever, the `done_callback` never
        fires, and the vendored protocol layer waits out the rest of the
        conversation for an announcement that never started. A skipped sound is
        a far smaller failure than a wedged one.
        """
        media = FakeMedia()
        player = ReachyPlayback(
            media,
            _RefusesToResolve(),
            scheduler=ManualScheduler(),
            detach=immediately,
        )
        finished: list[str] = []
        player.play(
            "https://198.51.100.10/x.mp3", done_callback=lambda: finished.append("done")
        )
        assert media.played == []
        assert not player.is_playing
        assert finished == ["done"]

    def test_the_rest_of_a_playlist_survives_one_that_raises(self) -> None:
        """One broken media URL must not cost the items behind it."""
        media = FakeMedia()
        sounds = _RaisesOnce()
        sounds.add("good", "/sounds/good.wav", 0.1)
        player = ReachyPlayback(
            media,
            sounds,
            scheduler=ManualScheduler(),
            detach=immediately,
        )
        player.play(["broken", "good"])
        assert media.played == ["/sounds/good.wav"]


class _RefusesToResolve:
    """A sound source that raises where it is contracted to answer `None`."""

    def resolve(self, url: str) -> Sound | None:
        """Fail the way a truncated download once did.

        Args:
            url: What was asked for.

        Returns:
            Never.

        Raises:
            IndexError: Always. The type is the one the frame scan raised on an
                empty response, so this stands in for that defect's shape
                rather than for an invented one.
        """
        del url
        raise IndexError("index out of range")


class _RaisesOnce(FakeSoundSource):
    """A sound source whose first resolution raises and whose rest behave."""

    def __init__(self) -> None:
        """Start with nothing registered and nothing yet asked for."""
        super().__init__()
        self.calls = 0

    def resolve(self, url: str) -> Sound | None:
        """Raise the first time and behave after that.

        Args:
            url: What was asked for.

        Returns:
            Whatever was registered, after the first call.

        Raises:
            IndexError: On the first call only.
        """
        self.calls += 1
        if self.calls == 1:
            raise IndexError("index out of range")
        return super().resolve(url)


class _SlowSource:
    """A sound source standing in for one that has to fetch."""

    def resolve(self, url: str) -> Sound | None:
        """Resolve a remote URL into the cache.

        Args:
            url: What was asked for.

        Returns:
            The sound.
        """
        del url
        return Sound(path="/cache/track.mp3", duration_seconds=None)
