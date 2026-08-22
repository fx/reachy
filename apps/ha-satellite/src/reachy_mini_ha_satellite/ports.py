"""What the behaviour layer is allowed to know about the robot.

Three interfaces — audio, motion, perception — and a handful of value types they
speak in. Everything behind them is an adapter; nothing in front of them
performs input or output. That division is the whole reason this file exists,
because the robot is one device on a desk and anything only testable on it is
effectively untested.

**These are written in the behaviour layer's vocabulary, not the SDK's.** The
question asked of every method below was "would the state machine phrase it this
way?", and where the answer was "no, that is how the Reachy Mini SDK phrases it"
the method was rewritten. The SDK wants a four-by-four pose matrix and a pixel
pair; the behaviour layer wants "look at that face" and "tilt the head up". A
port shaped like the SDK would carry every SDK change through to the state
machine and would turn the fakes into SDK emulators, which is the opposite of
what makes them useful.

Two consequences are worth stating outright.

**The perception port hides where a detection came from.** A caller asks what is
in front of the robot and is answered; whether the answer came over a robot-link
session or out of a model running on the robot's own cores is a property of the
adapter, and so is falling back from one to the other. A branch on transport
failure in the state machine would be the state machine having opinions about
sockets.

**The gaze target is normalised, never pixels.** Robot-link REQ-021 fixes the
coordinates a detection is reported in — origin at the image centre, both axes
running to plus or minus one, vertical axis pointing up — and the port carries
them through unchanged so that changing the capture resolution changes nothing
the behaviour layer sees. Converting to something the robot's motion layer can
be commanded with is the motion adapter's job, because that conversion needs the
camera's geometry and the behaviour layer has no business knowing it.

`FaceDetection` and `NormalisedPoint` are imported from `reachy_contracts`
rather than restated. They are the shared vocabulary for "a face's centre and
how much the detector believes itself", which is exactly what the behaviour
layer wants to hear; declaring a second pair of types here would put a copy of a
shape that already exists one import away, free to drift from it — see the root
`AGENTS.md` on wire types being declared once.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from reachy_contracts import FaceDetection, NormalisedPoint

__all__ = [
    "NEUTRAL_ANTENNAS",
    "NEUTRAL_HEAD",
    "AntennaPose",
    "AudioPort",
    "CapturePort",
    "DetectionSource",
    "Detections",
    "HeadPose",
    "MotionPort",
    "PerceptionPort",
    "PlaybackPort",
    "SourceSelection",
]


#:= docs/specs/architecture/index.md#req-005-behaviour-is-testable-without-hardware
#:% Every workspace member MUST expose its behaviour through interfaces that allow
#:% its full test suite to run without a robot, a camera, or a microphone attached.
#
#:= docs/specs/ha-satellite/index.md#req-042-decision-logic-is-free-of-input-and-output
#:% The logic that maps voice-pipeline events and detections to motion intents MUST
#:% be implemented without performing input or output.
@runtime_checkable
class PlaybackPort(Protocol):
    """One thing the robot can put a sound on, and duck under speech.

    Two of these exist at run time: one carries music, which Home Assistant
    drives through the media-player entity, and one carries announcements and
    the satellite's own chimes.

    The shape is deliberately congruent with `esphome.seams.MediaPlayback`, and
    that is a constraint rather than a preference: the vendored protocol layer
    calls exactly these methods on `ServerState.music_player` and
    `ServerState.tts_player`, so one object has to satisfy both this port and
    that seam or there would be a translating wrapper between them earning
    nothing. Structural typing is what lets one class answer to both names
    without either side importing the other.

    `done_callback` may be invoked from whatever thread playback finishes on,
    which is not necessarily the event loop's; the vendored code already assumes
    that and hops threads where it matters.
    """

    def play(
        self,
        url: str | list[str],
        done_callback: Callable[[], None] | None = None,
        stop_first: bool = False,
    ) -> None:
        """Play one sound, or a list of them in sequence.

        Args:
            url: What to play. A local path, a `file://` URL, or an `http(s)`
                URL Home Assistant supplied.
            done_callback: Invoked once the last item finishes, is stopped, or
                is superseded by another `play`.
            stop_first: Whether to stop whatever is playing before starting.
        """
        ...

    def pause(self) -> None:
        """Pause playback, keeping the current position."""
        ...

    def resume(self) -> None:
        """Resume playback a `pause` suspended."""
        ...

    def stop(self) -> None:
        """Stop playback and invoke any pending `done_callback`."""
        ...

    @property
    def is_playing(self) -> bool:
        """Whether a sound is currently playing, loading or paused.

        Returns:
            True while this output has something to play.
        """
        ...

    def set_volume(self, volume: float) -> None:
        """Set the playback volume.

        Args:
            volume: The level, in percent, from 0.0 to 100.0.
        """
        ...

    def duck(self, factor: float = 0.5) -> None:
        """Scale the volume down temporarily, so speech can be heard over it.

        Args:
            factor: What to multiply the volume by while ducked.
        """
        ...

    def unduck(self) -> None:
        """Restore the volume a `duck` scaled down."""
        ...


@runtime_checkable
class CapturePort(Protocol):
    """Microphone audio, one fixed-size chunk at a time.

    Congruent with `esphome.seams.AudioCapture` for the same reason
    `PlaybackPort` is congruent with the playback seam: the vendored protocol
    layer reads capture through `ServerState.audio_capture`, and one object
    satisfies both shapes structurally.

    A chunk is one `bytes` per channel rather than an interleaved buffer,
    because that is the shape the vendored code needs: channel 0 drives
    wake-word detection and is what Home Assistant transcribes, while channel 1,
    where the device has one, carries the speaker reference a server-side echo
    canceller wants.
    """

    @property
    def channels(self) -> int:
        """How many channels each chunk carries.

        Returns:
            The channel count.
        """
        ...

    @property
    def samples_per_chunk(self) -> int:
        """How many samples per channel each chunk carries.

        Returns:
            The chunk length in samples.
        """
        ...

    def start(self) -> None:
        """Begin capturing. Calling this twice is not an error."""
        ...

    def stop(self) -> None:
        """Stop capturing and let go of the source."""
        ...

    def read_chunk(self) -> Sequence[bytes] | None:
        """Wait for the next chunk.

        Returns:
            One `bytes` per channel, or `None` once the source is closed — so a
            caller loops until it sees `None`.
        """
        ...


#:= docs/specs/ha-satellite/index.md#req-043-hardware-access-goes-through-the-daemon-s-media-layer
#:% Microphone capture and audio playback MUST be performed through the robot
#:% daemon's media interface rather than by opening audio devices directly.
@runtime_checkable
class AudioPort(Protocol):
    """Everything the application hears and everything it says.

    One object owns the three audio surfaces together because they share one
    piece of hardware, and the daemon owns that hardware: acquiring and
    releasing it is a single lifecycle rather than three.
    """

    @property
    def capture(self) -> CapturePort:
        """What the wake word listens to.

        Returns:
            The microphone.
        """
        ...

    @property
    def music(self) -> PlaybackPort:
        """The output Home Assistant drives through the media-player entity.

        Returns:
            The music player.
        """
        ...

    @property
    def speech(self) -> PlaybackPort:
        """The output announcements and the satellite's own chimes go to.

        Returns:
            The announcement player.
        """
        ...

    def start(self) -> None:
        """Take up the daemon's media interface. Idempotent."""
        ...

    #:= docs/specs/ha-satellite/index.md#req-050-shutdown-is-graceful-and-leaves-the-robot-safe
    #:% On receiving a termination signal the application MUST stop commanding movement,
    #:% release the media interface, and exit.
    def stop(self) -> None:
        """Let go of the daemon's media interface. Idempotent."""
        ...

    def set_boost(self, percent: float) -> None:
        """Set the software boost both outputs amplify by.

        **Here rather than on `PlaybackPort`, and deliberately.** That port is
        congruent with `esphome.seams.MediaPlayback` — one object satisfies both
        so that nothing translates between them — and the vendored protocol
        layer never asks for a boost. Declaring it there would break a stated
        constraint to gain nothing, since no vendored caller would ever reach
        it.

        The boost is set for the device rather than for one output: it is makeup
        gain for how quietly Home Assistant's text-to-speech arrives, and both
        outputs play through the same speaker. So this reaches music and speech
        at once.

        Args:
            percent: The boost, in percent, where 100 is unity. Validated by
                `Settings` and clamped again by the entity that offers it; an
                adapter is not the place a second bound is invented.
        """
        ...


@dataclass(frozen=True, slots=True)
class HeadPose:
    """Where the head points, relative to looking straight ahead.

    Angles rather than a transformation matrix, because a state machine that
    wants the head tilted up by a tenth of a radian should be able to say so.
    Turning three angles into whatever the robot's motion layer is commanded
    with belongs to the motion adapter.

    Attributes:
        yaw: Rotation about the vertical axis, in radians, positive to the
            robot's left.
        pitch: Rotation about the lateral axis, in radians, positive upwards.
        roll: Rotation about the forward axis, in radians, positive tilting the
            head towards its right.
    """

    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0


@dataclass(frozen=True, slots=True)
class AntennaPose:
    """Where the two antennas are, relative to resting.

    Attributes:
        left: The left antenna's angle, in radians.
        right: The right antenna's angle, in radians.
    """

    left: float = 0.0
    right: float = 0.0


# Straight ahead, antennas at rest. Named so that "return to neutral" is a
# value the behaviour layer can compare against as well as command — REQ-048 is
# about arriving here, and a test that asserts it should not have to know that
# neutral happens to be three zeroes.
NEUTRAL_HEAD = HeadPose()
NEUTRAL_ANTENNAS = AntennaPose()


@runtime_checkable
class MotionPort(Protocol):
    """Everything the application can move.

    Every method commands a target and returns immediately; none of them waits
    for the robot to arrive. Movement over time is produced by commanding a
    sequence of targets, which is what the behaviour layer is already doing to
    follow a face, and it keeps a port method from being a thing that blocks an
    event loop for half a second.
    """

    #:= docs/specs/robot-link/index.md#req-021-detection-geometry-is-resolution-independent
    #:% Positions in results MUST be expressed in normalised image coordinates rather
    #:% than pixels.
    def look_at(self, target: NormalisedPoint) -> None:
        """Point the head at something seen in the frame.

        The target is in normalised image coordinates, never pixels, so the
        same face at the same place in the scene produces the same movement
        whatever resolution it was captured at. The adapter owns the conversion
        because it is the only thing that knows the camera's geometry.

        Args:
            target: Where the thing is, with the origin at the image centre and
                the vertical axis pointing up.
        """
        ...

    #:= docs/specs/ha-satellite/index.md#req-048-the-head-returns-to-neutral-when-tracking-data-goes-stale
    #:% When results stop arriving within the staleness window, the application MUST
    #:% return the head to its neutral position rather than holding its last commanded
    #:% pose.
    def look_ahead(self) -> None:
        """Return the head to neutral.

        This is what a caller does when the detections go stale. Holding the
        last commanded pose looks like successfully tracking somebody who has
        left the room, which is worse than visibly giving up.
        """
        ...

    def move_head(self, pose: HeadPose) -> None:
        """Command a head pose.

        Args:
            pose: Where to put the head, relative to neutral.
        """
        ...

    def move_antennas(self, pose: AntennaPose) -> None:
        """Command both antenna angles.

        Args:
            pose: Where to put them, relative to resting.
        """
        ...

    #:= docs/specs/ha-satellite/index.md#req-050-shutdown-is-graceful-and-leaves-the-robot-safe
    #:% On receiving a termination signal the application MUST stop commanding movement,
    #:% release the media interface, and exit.
    def release(self) -> None:
        """Stop commanding movement, for good.

        Every later call on this port is ignored rather than refused, so a
        shutdown racing a behaviour tick ends quietly instead of raising out of
        a task nobody is waiting on. The daemon is left free to return the
        robot to its default position.
        """
        ...

    @property
    def released(self) -> bool:
        """Whether this port has stopped commanding movement.

        Returns:
            True once `release` has been called.
        """
        ...


class DetectionSource(StrEnum):
    """Where a detection was actually computed.

    Attributes:
        REMOTE: The groundstation, over a robot-link session.
        LOCAL: A model running on the robot's own cores.
    """

    REMOTE = "remote"
    LOCAL = "local"


#:= docs/specs/ha-satellite/index.md#req-047-detection-source-is-selectable
#:% The source of face detections MUST be selectable between the groundstation, the
#:% robot's own detector, and the groundstation with local fallback.
class SourceSelection(StrEnum):
    """Which detector an operator asked for.

    Distinct from `DetectionSource`, which says what answered. The two are
    different questions: `REMOTE_WITH_LOCAL_FALLBACK` is one selection and
    either source may be the one that produced the answer being read.

    Attributes:
        REMOTE: The groundstation only. No local model is ever loaded.
        LOCAL: The robot's own detector only. No session is attempted.
        REMOTE_WITH_LOCAL_FALLBACK: The groundstation while the session is up,
            the robot's own detector while it is not.
    """

    REMOTE = "remote"
    LOCAL = "local"
    REMOTE_WITH_LOCAL_FALLBACK = "remote_with_local_fallback"


@dataclass(frozen=True, slots=True)
class Detections:
    """What is in front of the robot right now, and whether that is still true.

    Freshness is part of this type rather than something a caller works out,
    because REQ-048's neutral-head behaviour turns on it and a caller that had
    to remember to check would eventually not. `faces` is empty whenever
    `fresh` is false, so acting on a stale detection is not merely discouraged
    but unavailable — which is robot-link REQ-017 expressed as a shape.

    An empty `faces` with `fresh` true is an ordinary answer: nobody is in the
    room. It is not the same event as the results having stopped, and the two
    must stay distinguishable — REQ-048 mandates the neutral head for the second
    and says nothing about the first, so a consumer that folded them together
    could not tell an operator which of the two it is acting on.
    `behaviour.tracking` reports them as `NOBODY` and `STALE` and returns the
    head to neutral for both, for reasons it records; what this type owes it is
    the ability to tell them apart.

    Attributes:
        faces: Every face currently visible, possibly none.
        fresh: Whether a result arrived inside the staleness window.
        source: Which detector produced this, or `None` when nothing has.
        age_seconds: How long ago it was produced, on the robot's own clock, or
            `None` when nothing has been produced yet.
    """

    faces: tuple[FaceDetection, ...] = ()
    fresh: bool = False
    source: DetectionSource | None = None
    age_seconds: float | None = None


#:= docs/specs/robot-link/index.md#req-017-stale-results-stop-being-acted-on
#:% A consumer MUST stop acting on results once none has arrived within a configured
#:% staleness window.
@runtime_checkable
class PerceptionPort(Protocol):
    """What the robot can see, without saying where it was worked out.

    Pulled rather than pushed. A caller ticks and asks what is there, which is
    what a state machine wants: it already has a loop, and a callback arriving
    from a network task in between two of its transitions would be a second
    source of truth about the same instant.
    """

    async def start(self) -> None:
        """Begin producing detections. Idempotent."""
        ...

    def latest(self) -> Detections:
        """Say what is in front of the robot.

        Returns:
            The current view, which is always a value: before anything has
            arrived it carries no faces and is not fresh.
        """
        ...

    async def aclose(self) -> None:
        """Stop producing detections and release whatever was held."""
        ...
