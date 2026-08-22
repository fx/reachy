"""Capture and playback, both of them through the daemon rather than a device.

This is ha-satellite REQ-043 in code. The daemon holds the microphone array and
the speaker open for its own purposes, so nothing here opens either: audio comes
out of the daemon's capture pipeline and goes into its playback one, and the
application never names a device.

It is also what fills the two seams change 0011 cut into the vendored ESPHome
protocol layer. `ReachyCapture` is what `ServerState.audio_capture` holds and
`ReachyPlayback` is what `ServerState.music_player` and `ServerState.tts_player`
hold — satisfied structurally, so the vendored package imports nothing from
here and the dependency keeps running one way. The two **constants** are
imported from `esphome.seams` rather than restated, which is the opposite case
and deliberately so: they are there precisely so that both sides of the seam
agree on the sample format, and a second copy here would be free to disagree.

**Playback pushes samples rather than naming a file, and that is the whole of
change 0016.** The daemon offers both: `play_sound(path)`, which is a thing you
hand over, and `start_playing` / `push_audio_sample` / `stop_playing`, which is a
thing you can multiply. Nothing in the first path ever holds a sample, so there
was nowhere to apply gain — and the robot measured too quiet to hold a
conversation with, on a speaker whose one hardware control was already at
`0.00dB`. `decode.py` turns a resolved file into float samples and
`output_gain.py` amplifies them; this module paces them into the daemon.

Two things the old path documented as limitations are gone with it:

* **Completion is observed rather than timed.** The player used to schedule a
  timer for the sound's own length, read out of the file's header, and to fall
  back to a five-minute bound for a format it could not size. Now the end of the
  push loop *is* the end of the sound, so a `done_callback` is neither early nor
  late, and no magic constant stands in for a measurement.
* **The volume control does something.** `set_volume`, `duck` and `unduck` used
  to be recorded and reported back while changing nothing audible. They are read
  once per pushed chunk now, so Home Assistant's slider and the ducking the
  vendored layer performs when a wake word fires both take effect part way
  through an utterance rather than at the next one.

One limitation is unchanged and still worth stating: **there is one output.**
The music player and the announcement player push into the same daemon pipeline,
so a caller that plays on both at once hears them mixed. The vendored protocol
layer pauses music before an announcement and resumes afterwards, so the two are
coordinated in practice.

None of this has been near a speaker. Every part of it is exercised through the
fake daemon here, and change 0016's own last task is the listening test.
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from typing import TYPE_CHECKING, Final

import numpy as np

from reachy_mini_ha_satellite.adapters.decode import decode_file
from reachy_mini_ha_satellite.adapters.output_gain import (
    DEFAULT_BOOST_PERCENT,
    LevelMeter,
    Samples,
    amplify,
    effective_gain,
    headroom_gain,
)
from reachy_mini_ha_satellite.esphome.seams import SAMPLE_RATE, SAMPLE_WIDTH

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from reachy_mini_ha_satellite.adapters.daemon import AudioSamples, MediaInterface
    from reachy_mini_ha_satellite.adapters.sounds import Sound, SoundSource

__all__ = [
    "DEFAULT_CHANNELS",
    "DEFAULT_SAMPLES_PER_CHUNK",
    "AudioSourceError",
    "ReachyAudio",
    "ReachyCapture",
    "ReachyPlayback",
    "decode_for_playback",
    "run_detached",
]

_LOGGER: Final = logging.getLogger(__name__)

# Ten milliseconds at 16 kHz. The carried wake-word runtimes consume audio in
# 10 ms frames, so a chunk that is not this size makes the detector buffer on
# the other side of the seam for no reason.
DEFAULT_SAMPLES_PER_CHUNK: Final = 160

# What to assume when the daemon reports no audio device. Two, because the
# microphone array is stereo and channel 1 carries the speaker reference the
# server-side echo canceller wants; a capture that then produces nothing is a
# quieter failure than one that has already changed the shape of every chunk.
DEFAULT_CHANNELS: Final = 2

# The sample format the seam fixes, spelled the way numpy spells it: signed,
# little-endian, `SAMPLE_WIDTH` bytes wide. Derived from the constant rather
# than written out, so the two cannot drift apart.
_SAMPLE_DTYPE: Final = np.dtype(f"<i{SAMPLE_WIDTH}")

# The largest value that format holds: 32767 for two bytes, not 32768, so that
# a float of exactly 1.0 stays inside the range instead of wrapping round to
# the most negative sample there is — which is the loudest possible click.
_FULL_SCALE: Final = float(2 ** (8 * SAMPLE_WIDTH - 1) - 1)

# How much audio one pushed chunk carries. The daemon's playback `appsrc` has a
# bounded queue and does not block, so a caller that pushes faster than the
# speaker drains has the excess dropped rather than buffered — the pushes are
# paced instead. Fifty milliseconds is short enough that a volume change or a
# duck is heard within one chunk, and long enough that a minute of audio is a
# thousand pushes rather than sixty thousand.
_CHUNK_SECONDS: Final = 0.05

# How far ahead of the speaker to stay. The pacing loop is deliberately this
# much early throughout, so that a late wakeup or a slow push costs some of the
# lead rather than producing a gap in the audio — and the same amount is waited
# out at the end, which is the queued audio finishing rather than the pushing.
_LEAD_SECONDS: Final = 0.2

# How long to wait before asking the daemon for audio again when it had none.
# Half a chunk: short enough that the buffer never runs dry between reads, long
# enough that an idle microphone does not spin a core.
_POLL_SECONDS: Final = 0.005


class AudioSourceError(RuntimeError):
    """The daemon cannot supply audio in the form the pipeline requires.

    Raised rather than worked around. A capture at the wrong rate still
    produces chunks, and the wake word then simply never fires — which looks
    exactly like a robot that is ignoring you and gives nothing to debug.
    """


def decode_for_playback(path: str, rate: int) -> Samples:
    """Decode one file for the player, at the rate the daemon reports.

    A named function rather than a lambda over `decode_file` because it is the
    player's default and therefore part of its shape: injecting something else
    is how a test drives playback without reading a file, which the
    no-input-or-output rule for unit tests requires.

    Args:
        path: The local file to decode.
        rate: The sample rate to resample to.

    Returns:
        The decoded mono samples.
    """
    return decode_file(path, rate=rate)


def run_detached(work: Callable[[], None]) -> None:
    """Run something on a thread of its own, which is what runs on the robot.

    `ReachyPlayback.play` is called from the event loop that also runs the
    ESPHome protocol — `process_packet` invokes it directly — and resolving a
    media URL Home Assistant supplied can mean an HTTP fetch. Doing that inline
    would stop the satellite answering anything, pings included, for as long as
    the fetch took, and would delay releasing the media interface on shutdown
    by the same amount.

    Injected, so a test runs the same code inline and asserts on the result
    rather than on when a thread got round to it.

    Args:
        work: What to run.
    """
    thread = threading.Thread(target=work, daemon=True)
    thread.start()


#:= docs/specs/ha-satellite/index.md#req-043-hardware-access-goes-through-the-daemon-s-media-layer
#:% Microphone capture and audio playback MUST be performed through the robot
#:% daemon's media interface rather than by opening audio devices directly.
class ReachyCapture:
    """Microphone audio from the daemon, in the chunks the pipeline wants.

    The daemon hands over float samples in whatever size blocks its pipeline
    produced; the wake-word runtimes want fixed-size chunks of signed 16-bit
    little-endian audio, one `bytes` per channel. Rebuffering between the two is
    all this class does.
    """

    def __init__(
        self,
        media: MediaInterface,
        *,
        samples_per_chunk: int = DEFAULT_SAMPLES_PER_CHUNK,
        sleep: Callable[[float], None] = time.sleep,
        poll_seconds: float = _POLL_SECONDS,
    ) -> None:
        """Describe the capture without starting it.

        Args:
            media: The daemon's media interface.
            samples_per_chunk: How many samples per channel each chunk carries.
            sleep: How to wait when the daemon has nothing yet. Injected so the
                test suite drives capture without spending any wall time, which
                the no-sleeping rule for unit tests requires.
            poll_seconds: How long to wait between attempts.

        Raises:
            ValueError: If the chunk length is not positive.
        """
        if samples_per_chunk <= 0:
            message = f"a chunk must hold at least one sample, not {samples_per_chunk}"
            raise ValueError(message)
        self._media = media
        self._samples_per_chunk = samples_per_chunk
        self._sleep = sleep
        self._poll_seconds = poll_seconds
        self._channels: int | None = None
        self._buffer: AudioSamples | None = None
        self._running = threading.Event()
        self._released = False

    @property
    def channels(self) -> int:
        """How many channels each chunk carries.

        Read from the daemon once and remembered, because the vendored protocol
        layer reads it while it is building its entities and must get the same
        answer as the chunks it will later be handed.

        Returns:
            The channel count, or `DEFAULT_CHANNELS` when the daemon has no
            audio device to ask.
        """
        if self._channels is None:
            reported = self._media.get_input_channels()
            self._channels = reported if reported > 0 else DEFAULT_CHANNELS
        return self._channels

    @property
    def samples_per_chunk(self) -> int:
        """How many samples per channel each chunk carries.

        Returns:
            The chunk length in samples.
        """
        return self._samples_per_chunk

    def start(self) -> None:
        """Start the daemon's capture pipeline. Calling this twice is harmless.

        Raises:
            AudioSourceError: If the daemon captures at a rate other than the
                one the wake-word models were trained for, including when it
                reports no audio device at all.
        """
        if self._released or self._running.is_set():
            return
        rate = self._media.get_input_audio_samplerate()
        if rate != SAMPLE_RATE:
            message = (
                f"the daemon captures at {rate} Hz and the wake-word models "
                f"require {SAMPLE_RATE} Hz"
            )
            raise AudioSourceError(message)
        self._buffer = None
        self._running.set()
        self._media.start_recording()

    def stop(self) -> None:
        """Stop the daemon's capture pipeline and unblock any waiting reader."""
        if not self._running.is_set():
            return
        self._running.clear()
        self._media.stop_recording()

    def release(self) -> None:
        """Stop capturing, for good.

        Distinct from `stop`, and the difference is REQ-050's. A termination
        signal releases the daemon's media interface, and a behaviour tick or
        an ESPHome packet already in flight can reach this object afterwards —
        so `start` has to stop being something that takes the microphone back.
        """
        self._released = True
        self.stop()

    @property
    def released(self) -> bool:
        """Whether capture has been released for good.

        Returns:
            True once `release` has been called.
        """
        return self._released

    def read_chunk(self) -> Sequence[bytes] | None:
        """Wait for the next chunk of audio.

        Returns:
            One `bytes` per channel, or `None` once capture has been stopped —
            which is how the reader loop upstream of this learns to end.
        """
        while self._running.is_set():
            chunk = self._take()
            if chunk is not None:
                return chunk
            block = self._media.get_audio_sample()
            if block is None:
                self._sleep(self._poll_seconds)
                continue
            self._append(block)
        return None

    def _append(self, block: AudioSamples) -> None:
        """Add one block from the daemon to the pending buffer.

        Args:
            block: Float samples, shaped samples by channels, or a flat array
                when the source is mono.
        """
        shaped = self._fit_channels(
            block.reshape(-1, 1) if block.ndim == 1 else block,
        )
        if self._buffer is None:
            self._buffer = shaped
        else:
            self._buffer = np.vstack((self._buffer, shaped))

    def _fit_channels(self, block: AudioSamples) -> AudioSamples:
        """Make a block carry exactly the number of channels this promised.

        `channels` is a promise made to the vendored protocol layer before any
        audio has arrived, so a block that disagrees with it is reshaped rather
        than allowed to change the shape of a chunk halfway through a session.

        Args:
            block: Float samples, shaped samples by channels.

        Returns:
            The same samples with the promised number of columns: extra ones
            dropped, missing ones filled by repeating the first.
        """
        wanted = self.channels
        have = int(block.shape[1])
        if have == wanted:
            return block
        if have > wanted:
            return block[:, :wanted]
        # The channels that arrived are kept and the shortfall is padded from
        # the first. Rebuilding every column from channel 0 would be simpler
        # and wrong: channel 1 carries the speaker reference the server-side
        # echo canceller subtracts, and handing it a copy of the microphone
        # would make the canceller subtract the signal from itself.
        return np.column_stack([block, *([block[:, 0]] * (wanted - have))])

    def _take(self) -> list[bytes] | None:
        """Remove one chunk from the buffer, if there is one.

        Returns:
            One `bytes` per channel, or `None` when the buffer is short.
        """
        if self._buffer is None or self._buffer.shape[0] < self._samples_per_chunk:
            return None
        head = self._buffer[: self._samples_per_chunk]
        rest = self._buffer[self._samples_per_chunk :]
        self._buffer = rest if rest.shape[0] else None
        scaled = np.clip(head, -1.0, 1.0) * _FULL_SCALE
        # `astype` truncates towards zero, which biases every sample; rounding
        # first is what makes the conversion the one the models were trained on.
        samples = np.rint(scaled).astype(_SAMPLE_DTYPE)
        return [samples[:, index].tobytes() for index in range(self.channels)]


class ReachyPlayback:
    """One of the robot's two audio outputs, driven through the daemon.

    Satisfies `esphome.seams.MediaPlayback` structurally, which is how the
    vendored protocol layer plays a chime, an announcement or a media URL
    without knowing that a Reachy Mini daemon exists.
    """

    def __init__(
        self,
        media: MediaInterface,
        sounds: SoundSource,
        *,
        detach: Callable[[Callable[[], None]], None] = run_detached,
        sleep: Callable[[float], None] = time.sleep,
        decode: Callable[[str, int], Samples] = decode_for_playback,
        name: str = "playback",
        volume: float = 100.0,
        boost_percent: float = DEFAULT_BOOST_PERCENT,
    ) -> None:
        """Wire an output up without playing anything.

        Args:
            media: The daemon's media interface.
            sounds: How a requested URL becomes a local file.
            detach: How to get resolving, decoding and pushing a sound off the
                calling thread, which is the event loop the ESPHome protocol
                runs on.
            sleep: How the push loop paces itself against the speaker. Injected
                so the test suite drives a whole playlist through without
                spending its length, which the no-sleeping rule for unit tests
                requires.
            decode: How a resolved file becomes samples. Injected for the same
                kind of reason: decoding reads a file, and a unit test may not.
                What the real one does to the wheel's own assets is a contract
                test of its own.
            name: What to call this output in a log line — "music" or "speech".
            volume: The level to report until something sets another.
            boost_percent: The software boost, in percent. Makeup gain for
                Home Assistant's text-to-speech, which arrives quiet; see
                `output_gain.py` for where the number comes from.
        """
        self._media = media
        self._sounds = sounds
        self._detach = detach
        self._sleep = sleep
        self._decode = decode
        self._name = name
        self._volume = volume
        self._boost_percent = boost_percent
        self._duck_factor: float | None = None
        # One lock over all of the state below, because the push loop runs on a
        # thread of its own while `play`, `stop` and `duck` are called from the
        # event loop.
        self._lock = threading.RLock()
        self._pending: list[str] = []
        self._current: Sound | None = None
        # The decoded source, held un-amplified so that `resume` can play it
        # again — and so that a volume change part way through an utterance
        # applies to what is left of it rather than to the next one.
        self._samples: Samples | None = None
        self._callback: Callable[[], None] | None = None
        self._output_rate: int | None = None
        self._paused = False
        # Which start the pending completion and the pending resolution belong
        # to. See `_finished` and `_resolve_and_begin`.
        self._generation = 0
        # True between a request arriving and its first sound reaching the
        # daemon. The seam counts loading as playing, and it has to: `play`
        # returns before the sound has been resolved.
        self._loading = False
        # The URL a detached resolution is working on. Held so that a `pause`
        # arriving mid-fetch can put it back at the head of the queue: the item
        # has left `_pending` and has not reached `_current`, so without this
        # it would be the one thing a pause could lose.
        self._resolving: str | None = None
        self._released = False

    @property
    def is_playing(self) -> bool:
        """Whether this output has something to play.

        Returns:
            True while a sound is playing or paused, which is what the vendored
            protocol layer means by the question — it asks in order to decide
            whether music has to be paused before an announcement.
        """
        with self._lock:
            return self._current is not None or self._loading

    @property
    def volume(self) -> float:
        """The level that was last asked for.

        Returns:
            The volume in percent, ducking included. Since change 0016 this is
            also what is *applied*: the push loop reads it once per chunk and
            scales the samples by it.
        """
        with self._lock:
            # Tested against `None` rather than for truth: a duck factor of
            # zero is a legitimate request for silence, and it is falsy, so
            # `or 1.0` would report the unducked level for it — making
            # "ducked to silence" and "not ducked" the same answer.
            factor = 1.0 if self._duck_factor is None else self._duck_factor
            return self._volume * factor

    def play(
        self,
        url: str | list[str],
        done_callback: Callable[[], None] | None = None,
        stop_first: bool = False,
    ) -> None:
        """Play one sound, or a list of them in order.

        A `play` while something is already playing supersedes it, and the
        superseded item's `done_callback` is invoked on the way past. That is
        deliberate: the vendored protocol layer uses those callbacks to advance
        its own state — a conversation resumes when the announcement finishes —
        so a callback that silently never fired would leave it waiting for
        something that has already been replaced.

        An item that cannot be resolved is skipped and the next one is tried.
        A media URL Home Assistant cannot serve is Home Assistant's problem;
        losing the rest of the playlist over it would make it this
        application's.

        Args:
            url: What to play: a local path, a `file://` URL, or an `http(s)`
                URL. A list is played in sequence, and `done_callback` fires
                once at the end of it.
            done_callback: Invoked when the last item ends, is stopped, or is
                superseded. It may run on a timer thread.
            stop_first: Whether to silence the daemon's output before starting.
        """
        with self._lock:
            superseded = self._take_over()
            if self._released:
                # Released on shutdown. The request is dropped rather than
                # refused, and the caller is still told it finished, because a
                # packet that arrived a moment before a termination signal is
                # not a caller with anything left to wait for.
                unwanted, self._callback = done_callback, None
                _invoke(superseded)
                _invoke(unwanted)
                return
            if stop_first:
                self._media.stop_playing()
            self._pending = [url] if isinstance(url, str) else list(url)
            self._callback = done_callback
            self._paused = False
            self._loading = True
            generation = self._generation
        _invoke(superseded)
        # Resolving is what fetches, and this method is called from the event
        # loop that also runs the ESPHome protocol. It returns now and the
        # sound starts when it has been resolved.
        self._detach(functools.partial(self._resolve_and_begin, generation))

    def pause(self) -> None:
        """Silence the current sound, keeping it as the thing being played.

        There is no seek in the daemon's media interface, so `resume` restarts
        the current item from its beginning rather than from here. Restarting
        and re-timing the whole item is the only pair of choices that keeps the
        audio and the completion callback agreeing with each other.
        """
        with self._lock:
            if self._paused or (self._current is None and not self._loading):
                return
            # Advances the generation, which is what abandons a resolution
            # still in flight. Without it a fetch that finished after this
            # would start playing a sound the caller had already paused.
            self._cancel_playback()
            if self._resolving is not None:
                self._pending.insert(0, self._resolving)
                self._resolving = None
            self._loading = False
            self._paused = True
            self._media.stop_playing()

    def resume(self) -> None:
        """Play the current item again, or resolve the one a pause interrupted.

        There are two states a pause can leave behind: a sound that had already
        started, which is played again from its beginning, and one that was
        still being resolved, which goes back through the resolver.
        """
        with self._lock:
            if not self._paused or self._released:
                return
            self._paused = False
            if self._current is not None and self._samples is not None:
                # Already decoded, so this restarts it without going back to
                # the resolver — and from its beginning, because the daemon's
                # media interface has no seek and re-pushing from an offset
                # would be a position this player never knew.
                self._begin(self._current, self._samples)
                return
            if not self._pending:
                return
            self._loading = True
            self._generation += 1
            generation = self._generation
        self._detach(functools.partial(self._resolve_and_begin, generation))

    def stop(self) -> None:
        """Stop playing and drop whatever was queued behind it.

        Any pending `done_callback` is invoked, because the vendored protocol
        layer calls this expecting exactly that — it stops the announcement
        player in order to make the "announcement finished" transition happen.
        """
        with self._lock:
            pending = self._take_over()
            self._paused = False
            self._media.stop_playing()
        _invoke(pending)

    def set_volume(self, volume: float) -> None:
        """Adopt the level Home Assistant asked for.

        Heard from the next pushed chunk onwards, which is within
        `_CHUNK_SECONDS` — so turning the volume down during a long answer
        turns *that* answer down.

        Args:
            volume: The level in percent, from 0.0 to 100.0.
        """
        with self._lock:
            self._volume = volume
        self._note_volume()

    def duck(self, factor: float = 0.5) -> None:
        """Make this output quieter for a while.

        The vendored protocol layer ducks music when a wake word fires, so this
        lands part way through whatever is playing and has to be audible there
        rather than at the next sound.

        Args:
            factor: What the volume should be multiplied by.
        """
        with self._lock:
            self._duck_factor = factor
        self._note_volume()

    def unduck(self) -> None:
        """Return this output to its full level."""
        with self._lock:
            self._duck_factor = None

    def release(self) -> None:
        """Stop playing, for good.

        REQ-050 again: after the media interface has been released, a `play`
        that a racing packet delivers must not hand the daemon another sound.
        """
        with self._lock:
            self._released = True
        self.stop()

    @property
    def released(self) -> bool:
        """Whether this output has been released for good.

        Returns:
            True once `release` has been called.
        """
        with self._lock:
            return self._released

    def _note_volume(self) -> None:
        """Record in the log what the output was asked to do.

        Debug rather than info: Home Assistant sets the volume on connecting and
        the vendored layer ducks on every wake word, so this is several lines a
        conversation.
        """
        _LOGGER.debug("%s: level is now %.0f%%", self._name, self.volume)

    def _take_over(self) -> Callable[[], None] | None:
        """Clear everything queued and hand back the callback that was owed.

        Returns:
            The `done_callback` nobody has called yet, or `None`. The caller
            invokes it after releasing the lock, because a callback that calls
            back into this player would otherwise re-enter it mid-change.
        """
        self._cancel_playback()
        self._pending = []
        self._current = None
        self._loading = False
        self._resolving = None
        callback, self._callback = self._callback, None
        return callback

    def _cancel_playback(self) -> None:
        """Abandon whatever is being pushed, if anything is.

        Moving the generation on is the whole mechanism. A push loop already
        running cannot be interrupted from outside, so it checks the generation
        between chunks and returns when it no longer matches — and a `_finished`
        that was already on its way in is harmless for the same reason.
        """
        self._generation += 1

    def _resolve_and_begin(self, generation: int) -> None:
        """Resolve the next playable item and start it. Runs off the loop.

        The lock is **not** held across the resolution, which is the whole
        reason this is a method of its own: resolving an `http(s)` URL fetches
        it, and holding the lock — or the event loop — for that long would stop
        the satellite answering Home Assistant at all.

        An item that cannot be resolved is skipped and the next is tried. A
        media URL Home Assistant cannot serve is Home Assistant's problem;
        losing the rest of the playlist over it would make it this
        application's.

        Args:
            generation: Which request this is resolving for. A `play`, a
                `stop` or a `pause` that arrives while a fetch is in flight
                moves the generation on, and this abandons the result rather
                than starting a sound nobody asked for any more.
        """
        callback: Callable[[], None] | None = None
        while True:
            with self._lock:
                if generation != self._generation:
                    # A `play`, a `pause` or a `stop` has taken over. Whichever
                    # it was has already decided what happens to the URL below.
                    return
                self._resolving = None
                if not self._pending:
                    self._loading = False
                    self._current = None
                    callback, self._callback = self._callback, None
                    break
                url = self._pending.pop(0)
                self._resolving = url
            try:
                sound = self._sounds.resolve(url)
            except Exception:
                # A resolver is contracted to answer `None` for a sound it
                # cannot obtain, and this catch is the backstop for one that
                # raises instead. It is not defensive padding: this runs on a
                # detached thread, so an exception escaping here would kill
                # that thread silently, leaving `_loading` set for ever — the
                # player would report itself playing, the `done_callback`
                # would never fire, and the vendored protocol layer would wait
                # out the rest of the conversation for an announcement that
                # never started. A skipped sound is a far smaller failure.
                _LOGGER.exception(
                    "%s: a sound could not be resolved",
                    self._name,
                )
                sound = None
            if sound is None:
                _LOGGER.warning(
                    "%s: skipping a sound that could not be read",
                    self._name,
                )
                continue
            # Decoding happens here, on this detached thread, for the same
            # reason resolving does: a long file is not something the ESPHome
            # protocol should wait behind.
            try:
                samples = self._decode(sound.path, self._rate())
            except Exception:
                # `DecodeError` for a file that is not audio this can read, and
                # anything the decoder raises underneath it. Either way the next
                # item is tried, exactly as an unresolvable one is: a media URL
                # Home Assistant cannot serve must not end a conversation.
                _LOGGER.exception(
                    "%s: %s could not be decoded",
                    self._name,
                    sound.path,
                )
                continue
            with self._lock:
                if generation != self._generation:
                    return
                self._resolving = None
                self._loading = False
                self._begin(sound, samples)
            return
        # Nothing in the request could be resolved, so the caller is owed its
        # callback now rather than never: it is waiting to be told the
        # announcement has finished, and as far as it can be, it has.
        _invoke(callback)

    def _begin(self, sound: Sound, samples: Samples) -> None:
        """Start pushing one decoded sound at the daemon.

        Called with the lock held, and returns without pushing anything: the
        loop runs on a thread of its own, because it lasts as long as the sound
        does and `resume` calls this from the event loop.

        Args:
            sound: The resolved file, kept so that `resume` knows what is
                current and a log line can name it.
            samples: Its decoded, un-amplified audio.
        """
        self._current = sound
        self._samples = samples
        # Moves the generation on, which is what makes a push loop still
        # running for the previous sound abandon itself.
        self._cancel_playback()
        self._detach(functools.partial(self._push, self._generation, samples))

    def _push(self, generation: int, samples: Samples) -> None:
        """Feed one sound to the daemon, paced against the speaker.

        **Pacing is not politeness.** The daemon feeds pushed samples into a
        live GStreamer `appsrc` whose queue is bounded and which does not block,
        so pushing a whole file at once has most of it dropped. The loop stays
        `_LEAD_SECONDS` ahead of the speaker and no further.

        The gain is read per chunk rather than once, which is what makes Home
        Assistant's volume control and the ducking the vendored layer performs
        on a wake word audible *during* an utterance. Only the headroom cap is
        computed once, over the whole source: it is a property of the material
        (change 0016, R5), not of where the loop has got to.

        Args:
            generation: Which sound this loop belongs to. A `play`, a `pause`,
                a `stop` or a `release` moves the generation on, and this
                returns rather than pushing audio nobody asked for any more.
            samples: The decoded, un-amplified source.
        """
        headroom = headroom_gain(samples)
        meter = LevelMeter()
        rate = self._rate()
        per_chunk = max(1, int(rate * _CHUNK_SECONDS))
        chunk_seconds = per_chunk / rate
        self._media.start_playing()
        # The deadline the *next* push is due by, kept as an absolute time so
        # that a slow chunk is caught up with rather than accumulated into a
        # drift that ends in an underrun.
        due = time.monotonic() + _LEAD_SECONDS
        for start in range(0, samples.size, per_chunk):
            with self._lock:
                if generation != self._generation:
                    return
                gain = self._gain(headroom)
            block, levels = amplify(samples[start : start + per_chunk], gain)
            meter.add(levels, int(block.size))
            self._media.push_audio_sample(block)
            due += chunk_seconds
            delay = due - time.monotonic()
            if delay > 0:
                self._sleep(delay)
        _LOGGER.info("%s: %s", self._name, meter.levels().describe())
        # What is still queued at the daemon has not been heard yet, so the
        # sound is not over until it has drained. Without this the callback —
        # which the vendored layer reads as "the announcement finished" — would
        # arrive a fifth of a second early, every time.
        self._sleep(_LEAD_SECONDS)
        self._finished(generation)

    def _gain(self, headroom: float) -> float:
        """Say what the current settings ask one chunk to be multiplied by.

        Called with the lock held.

        Args:
            headroom: The cap this source's own peak allows, measured once over
                the whole of it.

        Returns:
            The gain for this chunk.
        """
        # Tested against `None` rather than for truth, for the reason `volume`
        # records: a duck factor of zero is a legitimate request for silence and
        # is falsy, so `or 1.0` would play it at full level.
        duck = 1.0 if self._duck_factor is None else self._duck_factor
        return effective_gain(
            volume=self._volume,
            boost_percent=self._boost_percent,
            duck=duck,
            headroom=headroom,
        )

    def _rate(self) -> int:
        """Say what rate to decode and push at.

        Read from the daemon once and remembered, because every chunk of every
        sound is resampled to it and asking per chunk would be asking the same
        question of the same device thousands of times a minute.

        Returns:
            The daemon's output sample rate.
        """
        if self._output_rate is None:
            self._output_rate = self._media.get_output_audio_samplerate()
        return self._output_rate

    def _finished(self, generation: int) -> None:
        """Move on once a sound has actually been played. Runs on any thread.

        The generation is what makes this safe against a `play`, a `pause` or a
        `stop` that landed while the last chunk was draining: a completion whose
        generation is not the current one belongs to a sound this player has
        already moved past, and firing its callback would tell the caller that
        the sound now playing had ended.

        Args:
            generation: Which sound this completion was reached for.
        """
        with self._lock:
            if generation != self._generation:
                return
            if self._paused or self._current is None:
                return
            self._current = None
            self._samples = None
            if self._pending:
                # The next item still has to be resolved and decoded, and
                # resolving can fetch. This runs on the push thread, which must
                # not spend a fetch's worth of time inside the lock either.
                self._loading = True
                self._generation += 1
                self._detach(
                    functools.partial(self._resolve_and_begin, self._generation),
                )
                return
            callback, self._callback = self._callback, None
        _invoke(callback)


def _invoke(callback: Callable[[], None] | None) -> None:
    """Run a completion callback, if there is one, without letting it escape.

    A `done_callback` belongs to the vendored protocol layer and may be invoked
    from a timer thread, where an exception has nowhere to go: it would kill the
    thread silently and leave whatever the callback was supposed to advance
    waiting for ever. It is logged instead.

    Args:
        callback: What to run, or `None`.
    """
    if callback is None:
        return
    try:
        callback()
    except Exception:
        _LOGGER.exception("a playback completion callback failed")


class ReachyAudio:
    """Everything the application hears and everything it says.

    One object over the daemon's one media interface, because acquiring and
    releasing it is a single lifecycle: the microphone and the speaker are the
    same piece of hardware from the daemon's point of view, and REQ-050 asks
    for both to be let go of together.
    """

    def __init__(
        self,
        media: MediaInterface,
        sounds: SoundSource,
        *,
        detach: Callable[[Callable[[], None]], None] = run_detached,
        samples_per_chunk: int = DEFAULT_SAMPLES_PER_CHUNK,
        sleep: Callable[[float], None] = time.sleep,
        decode: Callable[[str, int], Samples] = decode_for_playback,
        boost_percent: float = DEFAULT_BOOST_PERCENT,
    ) -> None:
        """Build the three surfaces over one media interface.

        Args:
            media: The daemon's media interface.
            sounds: How a requested URL becomes a local file.
            detach: How each output gets resolving, decoding and pushing a sound
                off the calling thread, which is the event loop the ESPHome
                protocol runs on.
            samples_per_chunk: How many samples per channel a capture chunk
                carries.
            sleep: How capture waits when the daemon has nothing yet, and how
                each output paces its pushes against the speaker.
            decode: How each output turns a resolved file into samples.
            boost_percent: The software boost both outputs apply.
        """
        self._media = media
        self._capture = ReachyCapture(
            media,
            samples_per_chunk=samples_per_chunk,
            sleep=sleep,
        )
        self._music = ReachyPlayback(
            media,
            sounds,
            detach=detach,
            sleep=sleep,
            decode=decode,
            name="music",
            boost_percent=boost_percent,
        )
        self._speech = ReachyPlayback(
            media,
            sounds,
            detach=detach,
            sleep=sleep,
            decode=decode,
            name="speech",
            boost_percent=boost_percent,
        )

    @property
    def capture(self) -> ReachyCapture:
        """What the wake word listens to.

        Returns:
            The microphone.
        """
        return self._capture

    @property
    def music(self) -> ReachyPlayback:
        """The output Home Assistant drives through the media-player entity.

        Returns:
            The music player.
        """
        return self._music

    @property
    def speech(self) -> ReachyPlayback:
        """The output announcements and the satellite's own chimes go to.

        Returns:
            The announcement player.
        """
        return self._speech

    def start(self) -> None:
        """Take up the daemon's media interface.

        Raises:
            AudioSourceError: If the daemon cannot capture at the rate the
                wake-word models require.
        """
        self._capture.start()

    @property
    def released(self) -> bool:
        """Whether the media interface has been released for good.

        Returns:
            True once `stop` has been called.
        """
        return self._capture.released

    #:= docs/specs/ha-satellite/index.md#req-050-shutdown-is-graceful-and-leaves-the-robot-safe
    #:% On receiving a termination signal the application MUST stop commanding movement,
    #:% release the media interface, and exit.
    def stop(self) -> None:
        """Let go of the daemon's media interface.

        Capture stops, both outputs stop — which invokes any completion
        callback still owed, so nothing upstream is left waiting on a sound
        that will never finish — and the daemon's playback pipeline is stopped.
        Idempotent, because a termination signal and an ordinary shutdown can
        both arrive.

        **Terminal, like `MotionPort.release`.** A behaviour tick or an ESPHome
        packet already in flight can reach any of the three surfaces after this
        returns, and REQ-050 asks for the media interface to be released rather
        than released and taken straight back. Each surface is therefore
        released rather than merely stopped: a later `start` or `play` is
        ignored, exactly as a later movement is.
        """
        self._capture.release()
        self._music.release()
        self._speech.release()
        self._media.stop_playing()
