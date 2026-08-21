"""The fakes every port ships with, and the builders the tests share.

These are a deliverable rather than scaffolding. Change 0013's behaviour suite
runs against them — that is how it satisfies ha-satellite REQ-042 and
architecture REQ-005 at once — so a port whose only implementation touched a
device would make that suite impossible to write. Each of the three ports has a
fake here, and each fake is the simplest thing that answers the port honestly:
it records what it was told and returns what a test scripted, and it emulates no
socket, no device and no SDK.

Two groups of things live here and they sit at different depths.

**Port fakes** — `FakeAudio`, `FakeMotion`, `FakePerception` — stand in for the
adapters. They are what the behaviour layer is handed.

**Daemon fakes** — `FakeMedia`, `FakeRobot`, `FakeSoundSource`,
`FakeFaceDetector` — stand in for what the adapters themselves talk to. They are
what the *adapter* tests are handed, and they are the reason those tests
exercise the real conversion code rather than mocking it out.

`ManualClock` and `ManualScheduler` are here for the usual reason: the staleness
window and playback completion are the two pieces of behaviour a test would
otherwise have to wait for, and a suite that slept would be slower and less
certain about exactly the property it was checking.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any, Final, cast

import numpy as np

from reachy_contracts import FaceDetection, NormalisedPoint
from reachy_mini_ha_satellite.adapters.sounds import Sound
from reachy_mini_ha_satellite.ports import (
    AntennaPose,
    Detections,
    DetectionSource,
    HeadPose,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence
    from types import ModuleType

    from reachy_mini_ha_satellite.adapters.daemon import (
        AudioSamples,
        ImageArray,
        PoseMatrix,
    )
    from reachy_mini_ha_satellite.adapters.perception_local import PixelFace
    from reachy_mini_ha_satellite.esphome.models import ServerState
    from reachy_mini_ha_satellite.esphome.satellite import VoiceSatelliteProtocol

__all__ = [
    "CREDENTIAL",
    "FakeAudio",
    "FakeCapture",
    "FakeFaceDetector",
    "FakeMedia",
    "FakePerception",
    "FakePlayback",
    "FakeRobot",
    "FakeSoundSource",
    "ManualClock",
    "ManualScheduler",
    "ScheduledCall",
    "face",
    "frame",
    "immediately",
    "inline",
    "sent_packets",
    "silence",
    "tone",
    "vendored_satellite",
    "vendored_server_state",
]

# A placeholder credential. Not anybody's — see the root AGENTS.md on what may
# enter a tracked file in a public repository.
CREDENTIAL: Final = "example-credential"


async def inline[ResultT](function: Callable[[], ResultT]) -> ResultT:
    """Run a blocking call right here rather than on a worker thread.

    Stands in for `adapters.daemon.in_thread`. The adapters offload the camera
    read and the model run so that neither stalls the event loop; a test that
    went through a real thread pool would be asserting on when the pool got
    round to it, which is a thing that is true most of the time.

    Args:
        function: What to run.

    Returns:
        What it returned.
    """
    return function()


def immediately(work: Callable[[], None]) -> None:
    """Run something here rather than on a thread of its own.

    Stands in for `adapters.audio_reachy.run_detached`. The player resolves a
    sound off the calling thread so that a media fetch cannot stall the event
    loop the ESPHome protocol runs on; a test wants the same code path without
    having to wait for a thread to be scheduled.

    Args:
        work: What to run.
    """
    work()


# --- Time --------------------------------------------------------------------


class ManualClock:
    """A monotonic clock that only moves when a test says so."""

    def __init__(self, now: float = 1000.0) -> None:
        """Start the clock somewhere that is not zero.

        Args:
            now: The reading to start at. Not zero, so that a test which
                accidentally compares against a default catches itself.
        """
        self.now = now

    def __call__(self) -> float:
        """Read the clock.

        Returns:
            The current reading.
        """
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward.

        Args:
            seconds: How far.
        """
        self.now += seconds


class ScheduledCall:
    """Something a `ManualScheduler` was asked to do later."""

    def __init__(self, delay: float, action: Callable[[], None]) -> None:
        """Record what was scheduled.

        Args:
            delay: How long it was to have waited.
            action: What it was to have done.
        """
        self.delay = delay
        self.action = action
        self.cancelled = False

    def cancel(self) -> None:
        """Stop it happening. Cancelling twice is not an error."""
        self.cancelled = True


class ManualScheduler:
    """A scheduler that never waits, and fires only when a test tells it to."""

    def __init__(self) -> None:
        """Start with nothing scheduled."""
        self.scheduled: list[ScheduledCall] = []

    def call_after(self, delay: float, action: Callable[[], None]) -> ScheduledCall:
        """Record something to do later.

        Args:
            delay: How long it should have waited.
            action: What to do.

        Returns:
            The handle that cancels it.
        """
        call = ScheduledCall(delay, action)
        self.scheduled.append(call)
        return call

    @property
    def pending(self) -> ScheduledCall | None:
        """The most recent call that has neither fired nor been cancelled.

        Returns:
            The pending call, or `None` when nothing is waiting.
        """
        for call in reversed(self.scheduled):
            if not call.cancelled:
                return call
        return None

    def fire(self) -> bool:
        """Run the pending call as though its delay had elapsed.

        Returns:
            True if something ran, False if nothing was pending.
        """
        call = self.pending
        if call is None:
            return False
        call.cancelled = True
        call.action()
        return True


# --- Audio -------------------------------------------------------------------


def silence(samples: int, channels: int = 2) -> AudioSamples:
    """Build a block of quiet audio in the shape the daemon produces.

    Args:
        samples: How many samples per channel.
        channels: How many channels.

    Returns:
        The block, float32 and zero throughout.
    """
    return np.zeros((samples, channels), dtype=np.float32)


def tone(values: Sequence[float], channels: int = 2) -> AudioSamples:
    """Build a block carrying known sample values.

    Args:
        values: One value per sample, repeated across every channel with each
            channel offset by its own index divided by a thousand, so a test
            can tell the channels apart in the bytes that come out.
        channels: How many channels.

    Returns:
        The block.
    """
    block = np.zeros((len(values), channels), dtype=np.float32)
    for index in range(channels):
        block[:, index] = np.asarray(values, dtype=np.float32) + index / 1000.0
    return block


class FakeSoundSource:
    """Resolves whatever a test registered, and nothing else."""

    def __init__(self, sounds: dict[str, Sound] | None = None) -> None:
        """Start with a set of known sounds.

        Args:
            sounds: What each URL resolves to.
        """
        self.sounds = dict(sounds or {})
        self.asked: list[str] = []

    def add(self, url: str, path: str, duration: float | None) -> None:
        """Register one resolvable sound.

        Args:
            url: What will be asked for.
            path: What it resolves to.
            duration: How long it plays for, or `None` when unreadable.
        """
        self.sounds[url] = Sound(path=path, duration_seconds=duration)

    def resolve(self, url: str) -> Sound | None:
        """Look a URL up.

        Args:
            url: What was asked for.

        Returns:
            The registered sound, or `None` when nothing was registered — which
            is how a test drives the "this media URL cannot be played" path.
        """
        self.asked.append(url)
        return self.sounds.get(url)


class FakePlayback:
    """One audio output that records what it was asked to do.

    Satisfies both `ports.PlaybackPort` and `esphome.seams.MediaPlayback`, so
    the behaviour suite and the vendored protocol layer can both be driven
    against it.
    """

    def __init__(self) -> None:
        """Start silent."""
        self.played: list[list[str]] = []
        self.stopped = 0
        self.paused = 0
        self.resumed = 0
        self.volume = 100.0
        self.duck_factor: float | None = None
        self._playing = False
        self._callback: Callable[[], None] | None = None

    def play(
        self,
        url: str | list[str],
        done_callback: Callable[[], None] | None = None,
        stop_first: bool = False,
    ) -> None:
        """Record a request to play something.

        Args:
            url: What was asked for.
            done_callback: What to invoke when `finish` is called.
            stop_first: Recorded and otherwise ignored.
        """
        del stop_first
        self.played.append([url] if isinstance(url, str) else list(url))
        self._callback = done_callback
        self._playing = True

    def pause(self) -> None:
        """Record a pause."""
        self.paused += 1

    def resume(self) -> None:
        """Record a resume."""
        self.resumed += 1

    def stop(self) -> None:
        """Record a stop and invoke any pending callback, as a real one does."""
        self.stopped += 1
        self._playing = False
        self.finish()

    @property
    def is_playing(self) -> bool:
        """Whether this output has something to play.

        Returns:
            True after `play` and before `stop` or `finish`.
        """
        return self._playing

    def set_volume(self, volume: float) -> None:
        """Record a volume.

        Args:
            volume: The level in percent.
        """
        self.volume = volume

    def duck(self, factor: float = 0.5) -> None:
        """Record a duck.

        Args:
            factor: What the volume should be multiplied by.
        """
        self.duck_factor = factor

    def unduck(self) -> None:
        """Record an unduck."""
        self.duck_factor = None

    def finish(self) -> None:
        """Pretend the sound ended, invoking the callback exactly once."""
        callback, self._callback = self._callback, None
        self._playing = False
        if callback is not None:
            callback()


class FakeCapture:
    """A microphone that hands out chunks a test wrote.

    Satisfies both `ports.CapturePort` and `esphome.seams.AudioCapture`.
    """

    def __init__(
        self,
        chunks: Iterable[Sequence[bytes]] = (),
        *,
        channels: int = 2,
        samples_per_chunk: int = 160,
    ) -> None:
        """Load the chunks this microphone will produce.

        Args:
            chunks: What `read_chunk` hands back, in order. Once they run out
                it answers `None`, which is how a reader learns to stop.
            channels: How many channels each chunk carries.
            samples_per_chunk: How many samples per channel.
        """
        self._chunks = list(chunks)
        self._channels = channels
        self._samples_per_chunk = samples_per_chunk
        self.started = 0
        self.stopped = 0

    @property
    def channels(self) -> int:
        """How many channels each chunk carries.

        Returns:
            The channel count.
        """
        return self._channels

    @property
    def samples_per_chunk(self) -> int:
        """How many samples per channel each chunk carries.

        Returns:
            The chunk length.
        """
        return self._samples_per_chunk

    def start(self) -> None:
        """Record a start."""
        self.started += 1

    def stop(self) -> None:
        """Record a stop."""
        self.stopped += 1

    def read_chunk(self) -> Sequence[bytes] | None:
        """Hand back the next scripted chunk.

        Returns:
            The chunk, or `None` once they have run out.
        """
        if not self._chunks:
            return None
        return self._chunks.pop(0)


class FakeAudio:
    """Everything the application hears and says, without any of it happening."""

    def __init__(
        self,
        *,
        capture: FakeCapture | None = None,
        music: FakePlayback | None = None,
        speech: FakePlayback | None = None,
    ) -> None:
        """Compose the three surfaces.

        Args:
            capture: The microphone, or a silent one.
            music: The music output, or a fresh one.
            speech: The announcement output, or a fresh one.
        """
        self._capture = capture if capture is not None else FakeCapture()
        self._music = music if music is not None else FakePlayback()
        self._speech = speech if speech is not None else FakePlayback()
        self.started = 0
        self.stopped = 0

    @property
    def capture(self) -> FakeCapture:
        """What the wake word listens to.

        Returns:
            The microphone.
        """
        return self._capture

    @property
    def music(self) -> FakePlayback:
        """The output Home Assistant drives.

        Returns:
            The music player.
        """
        return self._music

    @property
    def speech(self) -> FakePlayback:
        """The output announcements and chimes go to.

        Returns:
            The announcement player.
        """
        return self._speech

    def start(self) -> None:
        """Record a start."""
        self.started += 1

    def stop(self) -> None:
        """Record a stop, and stop both outputs as a real one does."""
        self.stopped += 1
        self._music.stop()
        self._speech.stop()


# --- Motion ------------------------------------------------------------------


class FakeMotion:
    """A robot that records every movement it was asked to make."""

    def __init__(self) -> None:
        """Start at neutral, commanding nothing."""
        self.gaze: list[NormalisedPoint] = []
        self.heads: list[HeadPose] = []
        self.antennas: list[AntennaPose] = []
        self._released = False

    @property
    def released(self) -> bool:
        """Whether this port has stopped commanding movement.

        Returns:
            True once `release` has been called.
        """
        return self._released

    def look_at(self, target: NormalisedPoint) -> None:
        """Record a gaze target.

        Args:
            target: Where to look, in normalised image coordinates.
        """
        if self._released:
            return
        self.gaze.append(target)

    def look_ahead(self) -> None:
        """Record a return to neutral."""
        self.move_head(HeadPose())

    def move_head(self, pose: HeadPose) -> None:
        """Record a head pose.

        Args:
            pose: Where to put the head.
        """
        if self._released:
            return
        self.heads.append(pose)

    def move_antennas(self, pose: AntennaPose) -> None:
        """Record an antenna pose.

        Args:
            pose: Where to put them.
        """
        if self._released:
            return
        self.antennas.append(pose)

    def release(self) -> None:
        """Stop recording movements, the way a real one stops commanding them."""
        self._released = True

    @property
    def last_head(self) -> HeadPose | None:
        """The most recently commanded head pose.

        Returns:
            The pose, or `None` when none has been commanded.
        """
        return self.heads[-1] if self.heads else None


# --- Perception --------------------------------------------------------------


def face(x: float, y: float, confidence: float = 0.9) -> FaceDetection:
    """Build one face detection at a normalised position.

    Args:
        x: Horizontal position, negative to the left of centre.
        y: Vertical position, negative below centre.
        confidence: How much the detector is to have believed itself.

    Returns:
        The detection.
    """
    return FaceDetection(centre=NormalisedPoint(x=x, y=y), confidence=confidence)


class FakePerception:
    """A view of the scene a test writes directly."""

    def __init__(
        self,
        detections: Detections | None = None,
        *,
        connected: bool = True,
    ) -> None:
        """Start with a scripted view.

        Args:
            detections: What `latest` answers, or an empty, not-fresh view.
            connected: Whether this source's link is up. A plain attribute
                rather than a property, so a test flips it mid-run; it
                satisfies `ConnectableSource` either way, which is what lets
                one fake stand in for both the plain port and the
                groundstation source that a fallback watches.
        """
        self.detections = detections if detections is not None else Detections()
        self.connected = connected
        self.started = 0
        self.closed = 0

    async def start(self) -> None:
        """Record a start."""
        self.started += 1

    def latest(self) -> Detections:
        """Answer with whatever was scripted.

        Returns:
            The current view.
        """
        return self.detections

    async def aclose(self) -> None:
        """Record a close."""
        self.closed += 1

    def see(self, *faces: FaceDetection, source: DetectionSource | None = None) -> None:
        """Script a fresh view carrying these faces.

        Args:
            faces: What is in front of the robot.
            source: Which detector is to have produced it.
        """
        self.detections = Detections(
            faces=faces,
            fresh=True,
            source=source,
            age_seconds=0.0,
        )

    def go_stale(self) -> None:
        """Script the view the staleness window produces."""
        self.detections = Detections(
            fresh=False,
            source=self.detections.source,
            age_seconds=99.0,
        )


class FakeFaceDetector:
    """A detector that reports whatever pixels a test told it to."""

    def __init__(self, faces: Sequence[PixelFace] = ()) -> None:
        """Load the detector with a fixed answer.

        Args:
            faces: What every frame is to produce, in pixels.
        """
        self.faces = tuple(faces)
        self.seen: list[tuple[int, int]] = []
        self.closed = 0

    def detect(self, image: ImageArray) -> Sequence[PixelFace]:
        """Report the scripted faces and record the frame's shape.

        Args:
            image: The frame, whose dimensions are recorded so a test can check
                that the normalisation divided by the right ones.

        Returns:
            The scripted faces.
        """
        self.seen.append((int(image.shape[0]), int(image.shape[1])))
        return self.faces

    def close(self) -> None:
        """Record a close."""
        self.closed += 1


# --- The daemon --------------------------------------------------------------


def frame(height: int = 480, width: int = 640, fill: int = 128) -> ImageArray:
    """Build a plain decoded frame of a given size.

    Args:
        height: How many rows.
        width: How many columns.
        fill: The grey level.

    Returns:
        The frame.
    """
    image: ImageArray = np.zeros((height, width, 3), dtype=np.uint8)
    image[:] = fill
    return image


class FakeMedia:
    """The daemon's media layer, recording what it was asked for.

    Everything it hands back is scripted. Nothing here emulates GStreamer: the
    adapters are what is under test, and a fake that reproduced a pipeline would
    be testing the fake.
    """

    def __init__(
        self,
        *,
        jpeg: bytes | None = b"jpeg-bytes",
        image: ImageArray | None = None,
        audio: Iterable[AudioSamples] = (),
        sample_rate: int = 16000,
        channels: int = 2,
    ) -> None:
        """Script what the daemon will produce.

        Args:
            jpeg: What `get_frame_jpeg` answers every time, or `None` for a
                daemon with no camera.
            image: What `get_frame` answers every time, or `None`.
            audio: The blocks `get_audio_sample` hands out in order, answering
                `None` once they run out — which is what an idle microphone
                looks like.
            sample_rate: What the daemon says it captures at.
            channels: How many channels it says it captures.
        """
        self.jpeg = jpeg
        self.image = image
        self.audio = list(audio)
        self.sample_rate = sample_rate
        self.channels = channels
        self.played: list[str] = []
        self.recording = False
        self.playing = True
        self.jpeg_reads = 0
        self.image_reads = 0
        self.stop_playing_calls = 0

    def get_frame_jpeg(self) -> bytes | None:
        """Hand back the scripted compressed frame.

        Returns:
            The bytes, or `None`.
        """
        self.jpeg_reads += 1
        return self.jpeg

    def get_frame(self) -> ImageArray | None:
        """Hand back the scripted decoded frame.

        Returns:
            The pixels, or `None`.
        """
        self.image_reads += 1
        return self.image

    def play_sound(self, sound_file: str) -> None:
        """Record a sound the daemon was asked to play.

        Args:
            sound_file: The local path.
        """
        self.played.append(sound_file)
        self.playing = True

    def start_recording(self) -> None:
        """Record that capture started."""
        self.recording = True

    def stop_recording(self) -> None:
        """Record that capture stopped."""
        self.recording = False

    def start_playing(self) -> None:
        """Record that the playback pipeline started."""
        self.playing = True

    def stop_playing(self) -> None:
        """Record that the playback pipeline stopped."""
        self.stop_playing_calls += 1
        self.playing = False

    def get_audio_sample(self) -> AudioSamples | None:
        """Hand back the next scripted block of recorded audio.

        Returns:
            The block, or `None` once the script runs out.
        """
        if not self.audio:
            return None
        return self.audio.pop(0)

    def get_input_audio_samplerate(self) -> int:
        """Say what rate capture runs at.

        Returns:
            The scripted rate.
        """
        return self.sample_rate

    def get_input_channels(self) -> int:
        """Say how many channels capture produces.

        Returns:
            The scripted channel count.
        """
        return self.channels


class FakeRobot:
    """The handle the daemon hands an application, recording every command."""

    def __init__(self, media: FakeMedia | None = None) -> None:
        """Wrap a media layer.

        Args:
            media: The daemon's media layer, or a fresh fake one.
        """
        self._media = media if media is not None else FakeMedia()
        self.heads: list[PoseMatrix] = []
        self.antennas: list[list[float]] = []
        self.body_yaws: list[float] = []
        self.gaze: list[tuple[float, float, float]] = []
        self.durations: list[float] = []

    @property
    def media(self) -> FakeMedia:
        """The daemon's media layer.

        Returns:
            The fake.
        """
        return self._media

    def set_target(
        self,
        head: PoseMatrix | None = None,
        antennas: list[float] | None = None,
        body_yaw: float | None = None,
    ) -> None:
        """Record a commanded pose.

        Args:
            head: The head pose, or `None`.
            antennas: The two antenna angles, right then left, or `None`.
            body_yaw: The body rotation, or `None`.
        """
        if head is not None:
            self.heads.append(head)
        if antennas is not None:
            self.antennas.append(list(antennas))
        if body_yaw is not None:
            self.body_yaws.append(body_yaw)

    def look_at_world(
        self,
        x: float,
        y: float,
        z: float,
        duration: float = 1.0,
        perform_movement: bool = True,
    ) -> PoseMatrix:
        """Record a gaze target and hand back an identity pose.

        Args:
            x: Forward, in metres.
            y: To the robot's left, in metres.
            z: Upwards, in metres.
            duration: Recorded via `durations`.
            perform_movement: Ignored.

        Returns:
            An identity transformation, which is what a target dead ahead works
            out to.
        """
        del perform_movement
        self.gaze.append((x, y, z))
        self.durations.append(duration)
        return np.eye(4, dtype=np.float64)


# --- The vendored protocol layer ---------------------------------------------


def _carried_helpers() -> ModuleType:
    """Reach the test helpers carried from the vendored upstream.

    Imported through `import_module` rather than by a plain `import`, and the
    reason is the type checker rather than the loader. The carried module is
    unannotated and is named in a `[[tool.mypy.overrides]]` block that says so;
    this module is checked in strict mode, where an ordinary import would make
    every call into it an untyped call. One dynamically-imported module object
    is a smaller thing to explain than a suppression on each call site — and it
    is honest about the boundary, because what is on the other side of it is a
    derived file this repository does not annotate.

    Returns:
        The carried helpers module.
    """
    return importlib.import_module("esphome_test_support")


def vendored_server_state(**overrides: object) -> ServerState:
    """Build the vendored `ServerState` with these pieces replaced.

    Args:
        overrides: Fields to set, most usefully `music_player`, `tts_player`
            and `audio_capture` — which is exactly the wiring the two seams cut
            in change 0011 exist for.

    Returns:
        The state, with everything not named here filled in with something
        inert.
    """
    return cast("ServerState", _carried_helpers().make_state(**overrides))


def vendored_satellite(**overrides: object) -> VoiceSatelliteProtocol:
    """Build the vendored voice satellite over a state with these pieces.

    Args:
        overrides: Fields of `ServerState` to set.

    Returns:
        The satellite, with its outbound writer replaced so that nothing it
        sends reaches a socket.
    """
    return cast(
        "VoiceSatelliteProtocol",
        _carried_helpers().make_satellite(state_overrides=dict(overrides)),
    )


def sent_packets(satellite: VoiceSatelliteProtocol) -> int:
    """How many outbound writes the vendored protocol layer has made.

    The carried helper replaces the satellite's writer with a mock, so nothing
    it sends reaches a socket and what it tried to send is countable. The
    attribute is reached through an untyped view because the vendored module
    declares it as `None` until a connection is made, and this repository's
    strict typing would otherwise want a suppression at every call site rather
    than one explanation here.

    Args:
        satellite: The satellite built by `vendored_satellite`.

    Returns:
        How many times it wrote.
    """
    writer: Any = satellite._writelines
    return int(writer.call_count)
