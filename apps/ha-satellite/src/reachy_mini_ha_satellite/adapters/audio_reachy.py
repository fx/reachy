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

Three things about this module are shaped by what the daemon does *not* offer,
and each of them is a limitation rather than a design:

* **There is one output.** `play_sound` replaces whatever the daemon was
  already playing, so the music player and the announcement player share a
  single channel; starting an announcement silences music at the daemon whether
  or not the music player knows. The vendored protocol layer already pauses
  music before an announcement and resumes afterwards, so the two are
  coordinated in practice — but a caller that plays on both at once gets one of
  them.
* **There is no completion signal.** Nothing reports that a sound has ended, so
  the player schedules a timer for the sound's own length, read out of the
  file's header by `sounds.py` — WAV and FLAC, which is what this application
  ships, and MP3, which is what Home Assistant's text-to-speech proxy serves.
  A format whose length cannot be read is scheduled at `UNKNOWN_LENGTH_SECONDS`
  instead: far enough out that a stop or the next `play` gets there first in
  ordinary use, and finite, so a `done_callback` cannot be lost outright. What
  is not available is a *measurement* of a format nobody here can size; a
  completion that arrives late is the cost, and one that never arrives would
  wedge the caller.
* **There is no output gain.** `set_volume` and `duck` record what was asked
  for and report it back, and change nothing audible. Home Assistant's volume
  control therefore round-trips through the media-player entity correctly and
  does nothing, which is stated here rather than hidden because the fix is a
  choice between the daemon growing a volume control and this adapter moving to
  the daemon's push-based playback path, and neither can be decided without the
  robot.

All three are exercised through fakes here and none of them has been near a
speaker; they are the first things the end-to-end session has to settle.
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from typing import TYPE_CHECKING, Final, Protocol

import numpy as np

from reachy_mini_ha_satellite.esphome.seams import SAMPLE_RATE, SAMPLE_WIDTH

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from reachy_mini_ha_satellite.adapters.daemon import AudioSamples, MediaInterface
    from reachy_mini_ha_satellite.adapters.sounds import Sound, SoundSource

__all__ = [
    "DEFAULT_CHANNELS",
    "DEFAULT_SAMPLES_PER_CHUNK",
    "UNKNOWN_LENGTH_SECONDS",
    "AudioSourceError",
    "Cancellable",
    "ReachyAudio",
    "ReachyCapture",
    "ReachyPlayback",
    "Scheduler",
    "ThreadScheduler",
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

# What a sound of unreadable length is timed at. Five minutes: longer than any
# announcement Home Assistant sends and longer than most tracks, so in ordinary
# use a stop or the next `play` supersedes it long before it fires — and finite,
# so a `done_callback` cannot be lost outright. The formats whose length *is*
# readable are in `sounds.py`, and they cover everything this application ships
# as well as the text-to-speech Home Assistant produces.
UNKNOWN_LENGTH_SECONDS: Final = 300.0

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


class Cancellable(Protocol):
    """Something scheduled that has not happened yet."""

    def cancel(self) -> None:
        """Stop it happening. Calling this after it has happened is not an error."""
        ...


class Scheduler(Protocol):
    """How the player arranges to be told that a sound has ended.

    Injected because it is the one piece of the player that is about time. A
    test drives a whole playlist through without waiting for any of it, and
    what it is testing is the queue, the callbacks and the state — not
    `threading.Timer`.
    """

    def call_after(self, delay: float, action: Callable[[], None]) -> Cancellable:
        """Arrange for something to happen later.

        Args:
            delay: How long to wait, in seconds.
            action: What to do. It may run on any thread.

        Returns:
            A handle that cancels it.
        """
        ...


class ThreadScheduler:
    """A scheduler backed by timer threads, which is what runs on the robot."""

    def call_after(self, delay: float, action: Callable[[], None]) -> Cancellable:
        """Run something on a timer thread after a delay.

        Args:
            delay: How long to wait, in seconds.
            action: What to do.

        Returns:
            The timer, which is `Cancellable`.
        """
        timer = threading.Timer(delay, action)
        # A daemon thread, so a timer waiting out the tail of an announcement
        # cannot hold the process open after the daemon has asked it to stop.
        timer.daemon = True
        timer.start()
        return timer


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
        scheduler: Scheduler | None = None,
        detach: Callable[[Callable[[], None]], None] = run_detached,
        name: str = "playback",
        volume: float = 100.0,
    ) -> None:
        """Wire an output up without playing anything.

        Args:
            media: The daemon's media interface.
            sounds: How a requested URL becomes a local file with a length.
            scheduler: How completion is arranged. Defaults to timer threads.
            detach: How to get resolving a sound off the calling thread.
            name: What to call this output in a log line — "music" or "speech".
            volume: The level to report until something sets another.
        """
        self._media = media
        self._sounds = sounds
        self._scheduler = scheduler if scheduler is not None else ThreadScheduler()
        self._detach = detach
        self._name = name
        self._volume = volume
        self._duck_factor: float | None = None
        # One lock over all of the state below, because completion arrives on a
        # timer thread while `play` and `stop` are called from the event loop.
        self._lock = threading.RLock()
        self._pending: list[str] = []
        self._current: Sound | None = None
        self._callback: Callable[[], None] | None = None
        self._timer: Cancellable | None = None
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
            The volume in percent. It is reported, not applied: see this
            module's docstring on what the daemon's media interface offers.
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
            self._cancel_timer()
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
            if self._current is not None:
                self._begin(self._current)
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
        """Record the level Home Assistant asked for.

        Args:
            volume: The level in percent, from 0.0 to 100.0.
        """
        with self._lock:
            self._volume = volume
        self._note_volume()

    def duck(self, factor: float = 0.5) -> None:
        """Record that this output should be quieter for a while.

        Args:
            factor: What the volume should be multiplied by.
        """
        with self._lock:
            self._duck_factor = factor
        self._note_volume()

    def unduck(self) -> None:
        """Record that this output should be at its full level again."""
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
        """Record in the log that a level was set and changed nothing audible."""
        _LOGGER.debug(
            "%s: volume is recorded but not applied; the daemon's media "
            "interface exposes no output gain",
            self._name,
        )

    def _take_over(self) -> Callable[[], None] | None:
        """Clear everything queued and hand back the callback that was owed.

        Returns:
            The `done_callback` nobody has called yet, or `None`. The caller
            invokes it after releasing the lock, because a callback that calls
            back into this player would otherwise re-enter it mid-change.
        """
        self._cancel_timer()
        self._pending = []
        self._current = None
        self._loading = False
        self._resolving = None
        callback, self._callback = self._callback, None
        return callback

    def _cancel_timer(self) -> None:
        """Stop the pending completion, if there is one.

        The generation moves on as well as the timer being cancelled, because
        cancelling cannot stop a callback that has already begun running — the
        generation is what makes such a callback harmless when it arrives.
        """
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
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
            sound = self._sounds.resolve(url)
            if sound is None:
                _LOGGER.warning(
                    "%s: skipping a sound that could not be read",
                    self._name,
                )
                continue
            with self._lock:
                if generation != self._generation:
                    return
                self._resolving = None
                self._loading = False
                self._begin(sound)
            return
        # Nothing in the request could be resolved, so the caller is owed its
        # callback now rather than never: it is waiting to be told the
        # announcement has finished, and as far as it can be, it has.
        _invoke(callback)

    def _begin(self, sound: Sound) -> None:
        """Hand one sound to the daemon and arrange to be told it ended.

        Called with the lock held.

        Args:
            sound: The resolved file to play.
        """
        self._current = sound
        # Cancelling moves the generation on, which is what makes a callback
        # already in flight for the previous sound harmless.
        self._cancel_timer()
        self._media.play_sound(sound.path)
        delay = sound.duration_seconds
        if delay is None:
            # The format's length is unreadable. Something is still scheduled,
            # far enough out that it is never the reason a sound is considered
            # finished in ordinary use — a stop or the next `play` will get
            # there first — and near enough that nothing waits for ever. A
            # completion that never arrives wedges the caller's state machine,
            # which is worse than one that arrives late.
            delay = UNKNOWN_LENGTH_SECONDS
            _LOGGER.debug(
                "%s: %s has no readable length; completion is bounded at %.0fs "
                "rather than measured",
                self._name,
                sound.path,
                delay,
            )
        self._timer = self._scheduler.call_after(
            delay,
            functools.partial(self._finished, self._generation),
        )

    def _finished(self, generation: int) -> None:
        """Move on once a sound's own length has elapsed. Runs on any thread.

        The generation is what makes cancellation reliable. `Timer.cancel`
        cannot stop a callback that has already started running, so a `play`,
        a `pause` or a `stop` that lands in that window would otherwise have
        its *replacement* sound marked finished here — firing a callback the
        caller reads as "the announcement ended" while the announcement is
        still playing. A callback whose generation is not the current one
        belongs to a sound this player has already moved past.

        Args:
            generation: Which sound this callback was scheduled for.
        """
        with self._lock:
            if generation != self._generation:
                return
            self._timer = None
            if self._paused or self._current is None:
                return
            self._current = None
            if self._pending:
                # The next item still has to be resolved, and resolving can
                # fetch. This runs on a timer thread, which must not spend
                # a fetch's worth of time inside the lock either.
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
        scheduler: Scheduler | None = None,
        detach: Callable[[Callable[[], None]], None] = run_detached,
        samples_per_chunk: int = DEFAULT_SAMPLES_PER_CHUNK,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Build the three surfaces over one media interface.

        Args:
            media: The daemon's media interface.
            sounds: How a requested URL becomes a local file with a length.
            scheduler: How playback completion is arranged.
            detach: How each output gets resolving a sound off the calling
                thread, which is the event loop the ESPHome protocol runs on.
            samples_per_chunk: How many samples per channel a capture chunk
                carries.
            sleep: How capture waits when the daemon has nothing yet.
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
            scheduler=scheduler,
            detach=detach,
            name="music",
        )
        self._speech = ReachyPlayback(
            media,
            sounds,
            scheduler=scheduler,
            detach=detach,
            name="speech",
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
