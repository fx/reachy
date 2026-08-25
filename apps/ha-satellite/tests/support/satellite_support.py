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
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

import numpy as np

# pylint: disable=no-name-in-module
from aioesphomeapi.api_pb2 import (  # type: ignore[attr-defined]  # generated protobuf module, which mypy cannot see the message classes inside
    NumberStateResponse,
)

from reachy_contracts import FaceDetection, NormalisedPoint
from reachy_mini_ha_satellite.adapters.sounds import Sound
from reachy_mini_ha_satellite.esphome.models import AvailableWakeWord, WakeWordType
from reachy_mini_ha_satellite.ports import (
    AntennaPose,
    CalibratedGaze,
    CalibrationStatus,
    Detections,
    DetectionSource,
    GazeCalibration,
    GazeDirective,
    GazeSample,
    HeadPose,
    MotionMeasurement,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence
    from types import ModuleType

    from google.protobuf import message

    from reachy_mini_ha_satellite.adapters.daemon import (
        AudioSamples,
        ImageArray,
        PoseMatrix,
    )
    from reachy_mini_ha_satellite.adapters.output_gain import Samples
    from reachy_mini_ha_satellite.adapters.perception_local import PixelFace
    from reachy_mini_ha_satellite.esphome.models import ServerState
    from reachy_mini_ha_satellite.esphome.satellite import VoiceSatelliteProtocol
    from reachy_mini_ha_satellite.wake_word import ModelInput

__all__ = [
    "CREDENTIAL",
    "FakeAudio",
    "FakeCamera",
    "FakeCapture",
    "FakeConnection",
    "FakeDecoder",
    "FakeFaceDetector",
    "FakeMedia",
    "FakeMicroWakeWord",
    "FakeOpenWakeWord",
    "FakePerception",
    "FakePlayback",
    "FakeRobot",
    "FakeSoundSource",
    "FakeWakeWordFeatures",
    "ManualClock",
    "ManualDetach",
    "available_wake_word",
    "connected",
    "face",
    "frame",
    "immediately",
    "inline",
    "no_sleep",
    "playable",
    "pushed_numbers",
    "sent_packets",
    "silence",
    "steady",
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


class ManualDetach:
    """A `detach` that queues work instead of starting a thread.

    Change 0016 made playback a loop rather than a timer: `play` resolves,
    decodes and pushes on a detached thread, and completion is the end of that
    loop. So this is what a test uses to stand between the two — `play` queues
    the work here, the test asserts the player reports itself playing, and then
    runs it.

    `immediately` is the other half of the pair, for a test that wants the whole
    thing to have happened by the time `play` returns.
    """

    def __init__(self) -> None:
        """Start with nothing queued."""
        self.queued: list[Callable[[], None]] = []

    def __call__(self, work: Callable[[], None]) -> None:
        """Queue something instead of running it.

        Args:
            work: What a thread would have run.
        """
        self.queued.append(work)

    def run(self) -> bool:
        """Run the oldest queued piece of work.

        Returns:
            True if something ran, False if nothing was queued.
        """
        if not self.queued:
            return False
        self.queued.pop(0)()
        return True

    def run_all(self) -> int:
        """Run everything queued, including work that queuing produced.

        A playlist advances by queuing the next item as the last one finishes,
        so draining is a loop rather than one pass.

        Returns:
            How many pieces of work ran.

        Raises:
            AssertionError: If the queue will not drain, which means the player
                is re-queuing for ever rather than making progress.
        """
        ran = 0
        while self.run():
            ran += 1
            if ran > _DRAIN_LIMIT:
                message = f"detached work did not drain after {_DRAIN_LIMIT} runs"
                raise AssertionError(message)
        return ran


# How much queued work counts as a playlist rather than a loop that will not
# end. Far more than any test queues, and finite, so a player that re-queues
# itself fails the test instead of hanging the suite.
_DRAIN_LIMIT: Final = 1000


def no_sleep(seconds: float) -> None:
    """Stand in for the pacing the push loop does against the speaker.

    `ReachyPlayback` sleeps between pushed chunks so that it stays just ahead of
    the daemon rather than overrunning its bounded queue. A test wants every
    chunk pushed and none of the waiting, which is also what the no-sleeping
    rule for unit tests requires.

    Args:
        seconds: How long it would have waited.
    """
    del seconds


class FakeDecoder:
    """Turns a path into samples without reading anything.

    Decoding is file input, so a unit test may not do it — the real decoder is
    covered by contract tests over the wheel's own assets instead. This answers
    with whatever the test scripted, and records what it was asked for.
    """

    def __init__(
        self,
        samples: dict[str, Samples | None] | None = None,
        *,
        default: Samples | None = None,
    ) -> None:
        """Script what each path decodes to.

        Args:
            samples: What to answer for each path. `None` scripts a path as
                undecodable, which is what a URL that answered with something
                that is not audio looks like.
            default: What to answer for a path not in `samples`. A tenth of a
                second of quiet audio when nothing is given, so a test that does
                not care what is playing does not have to say.
        """
        self.samples = dict(samples or {})
        self.default = default if default is not None else playable(0.1)
        self.decoded: list[tuple[str, int]] = []

    def __call__(self, path: str, rate: int) -> Samples:
        """Answer with this path's scripted samples.

        Args:
            path: The file that would have been read.
            rate: The rate it would have been resampled to.

        Returns:
            The scripted samples.

        Raises:
            RuntimeError: If the test scripted this path as undecodable, which
                is what a media URL that is not audio looks like.
        """
        self.decoded.append((path, rate))
        scripted = self.samples.get(path, self.default)
        if scripted is None:
            message = f"{path!r} cannot be decoded"
            raise RuntimeError(message)
        return scripted


def playable(seconds: float, *, rate: int = 48000, peak: float = 0.5) -> Samples:
    """Build audio that is not silence, so a gain has something to act on.

    Args:
        seconds: How long it runs for.
        rate: The sample rate it is meant to be played at.
        peak: Its loudest sample, which is what decides its headroom.

    Returns:
        A ramp from zero to `peak`, as float32 — a shape no limiter rounds off
        by accident, so a test asserting on peaks is asserting on the gain.
    """
    count = max(1, int(seconds * rate))
    return np.linspace(0.0, peak, count, dtype=np.float32)


def steady(seconds: float, *, rate: int = 48000, level: float = 0.1) -> Samples:
    """Build audio whose every sample is the same size.

    The counterpart to `playable`: a ramp is what a test wants when it is
    asserting on one peak, and a constant is what it wants when it is comparing
    peaks between chunks — a ramp's own shape would be indistinguishable from
    the gain changing.

    Args:
        seconds: How long it runs for.
        rate: The sample rate it is meant to be played at.
        level: Every sample's magnitude.

    Returns:
        The audio, as float32.
    """
    count = max(1, int(seconds * rate))
    return np.full(count, level, dtype=np.float32)


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
        self.boost = 100.0
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

    def set_boost(self, percent: float) -> None:
        """Record a boost.

        Args:
            percent: The software boost, in percent.
        """
        self.boost = percent

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
        self.boosts: list[float] = []

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

    def set_boost(self, percent: float) -> None:
        """Record a boost, and fan it out to both outputs as a real one does.

        Args:
            percent: The software boost, in percent.
        """
        self.boosts.append(percent)
        self._music.set_boost(percent)
        self._speech.set_boost(percent)


# --- Motion ------------------------------------------------------------------


class FakeMotion:
    """A robot that records every movement it was asked to make."""

    def __init__(self) -> None:
        """Start at neutral, commanding nothing."""
        self.coordinated_gaze: list[GazeSample] = []
        self.heads: list[HeadPose] = []
        self.antennas: list[AntennaPose] = []
        self.acquired: list[float] = []
        self.observed: list[float] = []
        self.calibrated: list[tuple[GazeDirective, float]] = []
        self.world_measurements: list[tuple[float, float] | BaseException | None] = []
        self.body_measurements: list[float | BaseException | None] = []
        self._released = False

    @property
    def released(self) -> bool:
        """Whether this port has stopped commanding movement.

        Returns:
            True once `release` has been called.
        """
        return self._released

    def acquire(self, now: float) -> MotionMeasurement:
        """Record idempotent predictive gaze acquisition."""
        if not self._released and not self.acquired:
            self.acquired.append(now)
        return MotionMeasurement(None, None, None, None, None)

    def observe(self, now: float) -> MotionMeasurement:
        """Return the next scripted measured world direction and body yaw."""
        if self._released:
            return MotionMeasurement(None, None, None, None, None)
        self.observed.append(now)
        world = (
            self.world_measurements.pop(0) if self.world_measurements else (0.0, 0.0)
        )
        body = self.body_measurements.pop(0) if self.body_measurements else None
        if isinstance(world, BaseException):
            raise world
        if isinstance(body, BaseException):
            raise body
        world_yaw, world_elevation = (None, None) if world is None else world
        return MotionMeasurement(
            world_yaw=world_yaw,
            world_elevation=world_elevation,
            head_measured_at=now,
            body_yaw=body,
            body_measured_at=now if body is not None else None,
        )

    def calibrate(self, directive: GazeDirective, now: float) -> GazeCalibration:
        """Return a deterministic world anchor for an actionable fake directive."""
        if self._released or directive.identity is None or directive.face is None:
            return GazeCalibration(CalibrationStatus.REJECTED)
        self.calibrated.append((directive, now))
        source, generation, sequence = directive.identity
        assert directive.captured_at is not None
        assert directive.received_at is not None
        return GazeCalibration(
            CalibrationStatus.ACCEPTED,
            CalibratedGaze(
                source=source,
                generation=generation,
                sequence=sequence,
                captured_at=directive.captured_at,
                received_at=directive.received_at,
                target_epoch=directive.target_epoch,
                world_yaw=-directive.face.centre.x * 0.5,
                world_elevation=directive.face.centre.y * 0.5,
            ),
        )

    def command_gaze(self, sample: GazeSample) -> None:
        """Record one coordinated gaze sample unless terminally released."""
        if not self._released:
            self.coordinated_gaze.append(sample)

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
        self._sequence = 0

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
        self._sequence += 1
        completed_at = self._sequence * 0.1
        self.detections = Detections(
            faces=faces,
            fresh=True,
            source=source,
            age_seconds=0.0,
            generation=0,
            sequence=self._sequence,
            captured_at=completed_at,
            received_at=completed_at,
        )

    def go_stale(self) -> None:
        """Script the view the staleness window produces."""
        self.detections = Detections(
            fresh=False,
            source=self.detections.source,
            age_seconds=99.0,
            generation=self.detections.generation,
            sequence=self.detections.sequence,
            captured_at=self.detections.captured_at,
            received_at=self.detections.received_at,
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


class FakeCamera:
    """An initialized camera exposing only its width-first resolution."""

    def __init__(self, resolution: tuple[int, int] = (640, 480)) -> None:
        """Store a scripted width and height."""
        self.resolution = resolution


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
        output_rate: int = 48000,
        camera: FakeCamera | None = None,
        camera_available: bool = True,
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
            output_rate: What the daemon says it plays back at, which is the
                rate playback is decoded and resampled to.
            camera: The initialized camera, or a default camera.
            camera_available: Whether the daemon reports any camera.
        """
        self.camera = (
            (camera if camera is not None else FakeCamera())
            if camera_available
            else None
        )
        self.jpeg = jpeg
        self.image = image
        self.audio = list(audio)
        self.sample_rate = sample_rate
        self.channels = channels
        self.output_rate = output_rate
        self.pushed: list[AudioSamples] = []
        self.recording = False
        self.playing = True
        self.jpeg_reads = 0
        self.image_reads = 0
        self.start_playing_calls = 0
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

    def start_recording(self) -> None:
        """Record that capture started."""
        self.recording = True

    def stop_recording(self) -> None:
        """Record that capture stopped."""
        self.recording = False

    def start_playing(self) -> None:
        """Record that the playback pipeline started."""
        self.start_playing_calls += 1
        self.playing = True

    def push_audio_sample(self, data: AudioSamples) -> None:
        """Record one chunk of audio the daemon was asked to play.

        Args:
            data: The samples, which the test reads back to check what was
                actually going to reach the speaker.
        """
        self.pushed.append(data)

    def get_output_audio_samplerate(self) -> int:
        """Say what rate playback runs at.

        Returns:
            The scripted output rate.
        """
        return self.output_rate

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
    """The daemon handle with scripted measured and calibrated motion feedback."""

    def __init__(
        self,
        media: FakeMedia | None = None,
        *,
        measured_head_poses: Iterable[PoseMatrix | BaseException] = (),
        measured_joints: Iterable[tuple[list[float], list[float]] | BaseException] = (),
        image_gaze_poses: Iterable[PoseMatrix | BaseException] = (),
        events: list[str] | None = None,
    ) -> None:
        """Wrap media and load deterministic feedback scripts.

        Args:
            media: The daemon media layer, or a fresh fake one.
            measured_head_poses: Values or failures returned by measured-pose reads.
            measured_joints: Values or failures returned by measured-joint reads.
            image_gaze_poses: Values or failures returned by calibration queries.
            events: Optional shared lifecycle event record.
        """
        self._media = media if media is not None else FakeMedia()
        self.measured_head_pose: PoseMatrix = np.eye(4, dtype=np.float64)
        self.measured_body_yaw = 0.0
        self.measured_head_poses = list(measured_head_poses)
        self.measured_joints = list(measured_joints)
        self.image_gaze_poses = list(image_gaze_poses)
        self.events = events if events is not None else []
        self.heads: list[PoseMatrix] = []
        self.antennas: list[list[float]] = []
        self.body_yaws: list[float] = []
        self.targets: list[
            tuple[PoseMatrix | None, list[float] | None, float | None]
        ] = []
        self.image_gaze: list[tuple[int, int, float, bool]] = []
        self.automatic_body_yaw: list[bool] = []
        self.motor_enables = 0
        self.wake_ups = 0

    def enable_motors(self, ids: list[str] | None = None) -> None:
        """Record that startup enabled the requested motors."""
        del ids
        self.motor_enables += 1
        self.events.append("motors.enable")

    def wake_up(self) -> None:
        """Record the SDK-controlled wake sequence."""
        self.wake_ups += 1
        self.events.append("robot.wake")

    @property
    def media(self) -> FakeMedia:
        """Return the fake daemon media layer."""
        return self._media

    def set_target(
        self,
        head: PoseMatrix | None = None,
        antennas: list[float] | None = None,
        body_yaw: float | None = None,
    ) -> None:
        """Record one grouped SDK command without implying linearizability."""
        copied_head = None if head is None else np.array(head, copy=True)
        copied_antennas = None if antennas is None else list(antennas)
        self.targets.append((copied_head, copied_antennas, body_yaw))
        self.events.append("motion.command")
        if copied_head is not None:
            self.heads.append(copied_head)
        if copied_antennas is not None:
            self.antennas.append(copied_antennas)
        if body_yaw is not None:
            self.body_yaws.append(body_yaw)

    def get_current_head_pose(self) -> PoseMatrix:
        """Return the next scripted world pose or the current fallback pose."""
        self.events.append("motion.pose")
        scripted = (
            self.measured_head_poses.pop(0)
            if self.measured_head_poses
            else self.measured_head_pose
        )
        if isinstance(scripted, BaseException):
            raise scripted
        self.measured_head_pose = np.array(scripted, dtype=np.float64, copy=True)
        return np.array(self.measured_head_pose, copy=True)

    def get_current_joint_positions(self) -> tuple[list[float], list[float]]:
        """Return seven scripted head joints and two antenna joints."""
        self.events.append("motion.joints")
        scripted = self.measured_joints.pop(0) if self.measured_joints else None
        if isinstance(scripted, BaseException):
            raise scripted
        if scripted is not None:
            return list(scripted[0]), list(scripted[1])
        return [self.measured_body_yaw, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], [0.0, 0.0]

    def look_at_image(
        self,
        u: int,
        v: int,
        duration: float = 1.0,
        perform_movement: bool = True,
    ) -> PoseMatrix:
        """Record a non-moving image query and return its scripted world pose."""
        self.image_gaze.append((u, v, duration, perform_movement))
        self.events.append("motion.calibrate")
        scripted = self.image_gaze_poses.pop(0) if self.image_gaze_poses else None
        if isinstance(scripted, BaseException):
            raise scripted
        return np.array(
            np.eye(4, dtype=np.float64) if scripted is None else scripted,
            copy=True,
        )

    def set_automatic_body_yaw(self, enabled: bool) -> None:
        """Record daemon automatic-body-yaw ownership changes."""
        self.automatic_body_yaw.append(enabled)
        self.events.append(f"motion.auto_yaw.{str(enabled).lower()}")


# --- Wake words ---------------------------------------------------------------
#
# The three fakes below are what makes wake-word detection testable at all. A
# real model answers a question about a recording of somebody speaking, which
# this repository does not have and a runner could not act on if it did; these
# answer whatever the test scripted, which turns "does it fire twice inside the
# refractory window" into an assertion instead of a thing somebody stands in
# front of a robot to find out.


class FakeWakeWordFeatures:
    """A feature frontend that turns each chunk into exactly one model input.

    The real ones turn a chunk into zero, one or several inputs depending on
    how full their window is, which is a detail of the runtimes rather than of
    anything the detector decides. One-in-one-out makes "the model fired on
    this chunk" a thing a test can state.
    """

    def __init__(self) -> None:
        """Start with nothing having been seen."""
        self.chunks: list[bytes] = []

    def process_streaming(self, audio_chunk: bytes, /) -> Iterable[ModelInput]:
        """Record the chunk and hand back one input standing for it.

        Args:
            audio_chunk: The audio, which is not examined.

        Returns:
            Exactly one input.
        """
        self.chunks.append(audio_chunk)
        return [np.frombuffer(audio_chunk, dtype="<i2")]


class FakeMicroWakeWord:
    """A microWakeWord model that fires when a test says so."""

    def __init__(
        self,
        model_id: str,
        *,
        wake_word: str = "Fake Word",
        fires: Sequence[bool] = (),
        fails: bool = False,
    ) -> None:
        """Script what this model answers.

        Args:
            model_id: Its identifier, which is what the active set holds.
            wake_word: The phrase it listens for.
            fires: What it answers, one entry per input it is given. Once they
                run out it answers `False`, which is what a model does for
                nearly every ten milliseconds of its life.
            fails: Whether it raises instead of answering. A model whose native
                runtime has gone wrong is a thing that happens, and what the
                caller does about it is worth asserting.
        """
        self.id = model_id
        self.wake_word = wake_word
        self.probability_cutoff = 0.0
        self.cutoffs: list[float] = []
        self.inputs: list[ModelInput] = []
        self._fires = list(fires)
        self._fails = fails

    def process_streaming(self, features: ModelInput, /) -> bool | None:
        """Answer whatever this model was scripted to answer.

        Args:
            features: One input from the frontend.

        Returns:
            Whether the wake word fired.

        Raises:
            RuntimeError: When this model was built to fail.
        """
        self.inputs.append(features)
        self.cutoffs.append(self.probability_cutoff)
        if self._fails:
            message = "the model is broken"
            raise RuntimeError(message)
        return self._fires.pop(0) if self._fires else False


class FakeOpenWakeWord:
    """An openWakeWord model, which answers with probabilities rather than a verdict."""

    def __init__(self, model_id: str, *, probabilities: Sequence[float] = ()) -> None:
        """Script what this model answers.

        Args:
            model_id: Its identifier.
            probabilities: What it yields for each input it is given, one entry
                per input. Once they run out it yields zero.
        """
        self.id = model_id
        self.wake_word = "Fake Open Word"
        self.inputs: list[ModelInput] = []
        self._probabilities = list(probabilities)

    def process_streaming(self, embeddings: ModelInput, /) -> Iterable[float]:
        """Answer whatever this model was scripted to answer.

        Args:
            embeddings: One input from the frontend.

        Returns:
            The probabilities for this input.
        """
        self.inputs.append(embeddings)
        return [self._probabilities.pop(0) if self._probabilities else 0.0]


def available_wake_word(
    model_id: str,
    *,
    cutoff: float = 0.7,
    kind: WakeWordType = WakeWordType.MICRO_WAKE_WORD,
) -> AvailableWakeWord:
    """Describe a wake word the way the registry that discovers them does.

    Args:
        model_id: Its identifier.
        cutoff: The probability cutoff its own configuration declares.
        kind: Which runtime it belongs to. The detector reads this rather than
            testing the model's class, which is what lets the fakes above stand
            in for either engine.

    Returns:
        The registry entry.
    """
    return AvailableWakeWord(
        id=model_id,
        type=kind,
        wake_word=model_id,
        # Never opened: the registry entry is read for its type and its cutoff,
        # and the model it would point at is one of the fakes above.
        wake_word_path=Path("/reachy-satellite-tests") / f"{model_id}.json",
        trained_languages=["en"],
        probability_cutoff=cutoff,
    )


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


class FakeConnection:
    """A connected client, for the fan-out an unprompted state change goes out on.

    `ServerState.broadcast` walks `ServerState.connections` and calls one method
    on each entry — `send_messages` — which the real `VoiceSatelliteProtocol`
    serialises and writes to a transport. This records the messages instead, so
    a test can read a push back without a socket while everything the push
    travelled through to reach it stays real.

    Attributes:
        sent: Every message pushed to this client, in order.
    """

    def __init__(self) -> None:
        """Start having been sent nothing."""
        self.sent: list[message.Message] = []

    def send_messages(self, msgs: Iterable[message.Message]) -> None:
        """Record what was pushed to this client.

        Args:
            msgs: What the state broadcast.
        """
        self.sent.extend(msgs)


def pushed_numbers(client: FakeConnection, key: int) -> list[float]:
    """Read the number states one client was pushed for one entity.

    Args:
        client: The recording client.
        key: The entity every state must be addressed to.

    Returns:
        The value each state carried, in the order they arrived.

    Raises:
        AssertionError: If anything but a number state, or one addressed to
            another entity, reached this client — either would be a message
            Home Assistant is not expecting on this path.
    """
    values: list[float] = []
    for msg in client.sent:
        assert isinstance(msg, NumberStateResponse), f"pushed a {type(msg).__name__}"
        assert msg.key == key, f"pushed for entity {msg.key} rather than {key}"
        values.append(float(msg.state))
    return values


def connected(state: ServerState, count: int = 1) -> list[FakeConnection]:
    """Register that many clients on a state, as `connection_made` would.

    Args:
        state: The state the clients connect to.
        count: How many of them. More than one is how a test tells a broadcast
            from a reply to whichever connection an entity happens to hold.

    Returns:
        The recording clients, in the order they were registered.
    """
    clients = [FakeConnection() for _ in range(count)]
    for client in clients:
        # The vendored list is annotated as holding protocols; what the
        # broadcast reaches for is the one method these doubles have.
        state.connections.append(cast("VoiceSatelliteProtocol", client))
    return clients


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
