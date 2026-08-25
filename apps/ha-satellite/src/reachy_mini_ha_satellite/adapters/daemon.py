"""The daemon's surface, declared here so that nothing imports the SDK to use it.

The Reachy Mini daemon owns the microphone array, the speaker, the camera and
the motors, and hands a running application a handle onto all four. That handle
is an SDK object — but an adapter that *imported* the SDK to name its type would
drag PyGObject and GStreamer into every test run, and architecture REQ-005 says
the suite runs with nothing attached.

So the surface is declared here as protocols, and satisfied structurally: on the
robot by the SDK's `ReachyMini` and its `MediaManager`, in the test suite by the
fakes in `tests/support/satellite_support.py`. The signatures below mirror the
SDK's exactly — parameter names, defaults and all — because that is what makes
`handle: RobotHandle = reachy_mini` type-check at the composition root rather
than merely look plausible.

**This module is the SDK's shape and the only place that is true.** Every port
in `ports.py` is phrased in the behaviour layer's terms instead; the adapters
between them are where one becomes the other. Widening anything here means the
daemon offers something new that an adapter needs, and it should be a visible
edit rather than an attribute reached for at a call site.

Nothing here reads a device. These are types.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import numpy as np
import numpy.typing as npt

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

__all__ = [
    "AudioSamples",
    "Camera",
    "ImageArray",
    "MediaInterface",
    "Offload",
    "PoseMatrix",
    "RobotHandle",
    "in_thread",
]

# The aliases are evaluated at run time rather than declared under
# `TYPE_CHECKING`, for the same reason the groundstation's `ImageArray` is:
# anything that resolves an annotation — `typing.get_type_hints` over a
# dataclass, a documentation tool — would otherwise raise `NameError` on a name
# that existed only for the type checker.

# One decoded camera frame: eight-bit pixels, height by width by three colour
# channels, in the blue-green-red order the daemon and OpenCV both use.
type ImageArray = npt.NDArray[np.uint8]

# One block of recorded audio: float32 samples in the range [-1, 1], shaped
# samples by channels. This is what the daemon's capture pipeline produces; the
# conversion to the signed 16-bit little-endian bytes the wake-word models want
# happens in the audio adapter, not here.
type AudioSamples = npt.NDArray[np.float32]

# A head pose as the robot's motion layer is commanded with it: a four-by-four
# homogeneous transformation. The behaviour layer never sees one — `ports.HeadPose`
# is three angles — and building one is the motion adapter's job.
type PoseMatrix = npt.NDArray[np.float64]


@runtime_checkable
class Camera(Protocol):
    """The initialized daemon camera needed for calibrated image gaze."""

    @property
    def resolution(self) -> tuple[int, int]:
        """Return image width then height, matching the SDK surface."""
        ...


#:= docs/specs/ha-satellite/index.md#req-043-hardware-access-goes-through-the-daemon-s-media-layer
#:% Microphone capture and audio playback MUST be performed through the robot
#:% daemon's media interface rather than by opening audio devices directly.
@runtime_checkable
class MediaInterface(Protocol):
    """The daemon's media layer: camera in, microphone in, speaker out.

    Every one of these goes through the daemon rather than to a device. That is
    REQ-043 and it is not a preference: the daemon holds the microphone array
    and the speaker open for its own purposes, so an application that opened
    either directly would be contending with it for hardware it does not own.
    """

    @property
    def camera(self) -> Camera | None:
        """Return the initialized camera, or ``None`` when unavailable."""
        ...

    def get_frame_jpeg(self) -> bytes | None:
        """Take the current camera frame, already compressed.

        The capture hardware produces JPEG, so these bytes cost nothing to
        obtain and are what travels on a robot-link session unmodified.
        Decoding them on the robot in order to re-encode them for transport
        would spend the robot's scarce cores to achieve nothing.

        Returns:
            The frame's bytes, or `None` when no camera is available.
        """
        ...

    def get_frame(self) -> ImageArray | None:
        """Take the current camera frame, decoded.

        Wanted only by a detector running on the robot itself, which needs
        pixels rather than bytes. The daemon has the decoded frame already, so
        asking for it here is cheaper than decoding the JPEG above.

        Returns:
            The frame's pixels, or `None` when no camera is available.
        """
        ...

    def start_recording(self) -> None:
        """Start the daemon's capture pipeline."""
        ...

    def stop_recording(self) -> None:
        """Stop the daemon's capture pipeline."""
        ...

    def start_playing(self) -> None:
        """Start the daemon's playback pipeline."""
        ...

    def push_audio_sample(self, data: AudioSamples) -> None:
        """Hand the daemon audio to play, as samples rather than as a file.

        This is the path that makes gain expressible. The daemon also offers a
        `play_sound(name)` that plays a file for you, and it is deliberately not
        declared here: it never lets the application hold a sample, so there is
        nowhere in it to multiply, and change 0016 moved playback off it for
        exactly that reason.

        Faster than real time is not free. The daemon feeds these into a live
        GStreamer `appsrc` whose queue is bounded and which does not block, so a
        caller that pushes a whole file at once has most of it dropped with a
        warning. `audio_reachy.ReachyPlayback` paces its pushes accordingly.

        Args:
            data: Float32 samples in [-1, 1]. One dimension is mono, which the
                daemon copies out to however many channels the output device
                has; two dimensions are samples by channels.
        """
        ...

    def stop_playing(self) -> None:
        """Stop the daemon's playback pipeline."""
        ...

    def get_output_audio_samplerate(self) -> int:
        """Say what rate playback runs at.

        Returns:
            The sample rate in hertz, or a negative number when there is no
            audio device. Pushed samples are resampled to it rather than
            assumed — ha-satellite change 0016, R9.
        """
        ...

    def get_audio_sample(self) -> AudioSamples | None:
        """Take whatever recorded audio is waiting.

        Returns:
            The samples, shaped samples by channels, or `None` when nothing has
            arrived yet. It does not block.
        """
        ...

    def get_input_audio_samplerate(self) -> int:
        """Say what rate capture runs at.

        Returns:
            The sample rate in hertz, or a negative number when there is no
            audio device.
        """
        ...

    def get_input_channels(self) -> int:
        """Say how many channels capture produces.

        Returns:
            The channel count, or a negative number when there is no audio
            device.
        """
        ...


@runtime_checkable
class RobotHandle(Protocol):
    """The handle the daemon hands a running application.

    Deliberately five members. The SDK's own object has forty, and naming only
    what startup and the adapters call is what keeps "which parts of the SDK does
    this application depend on?" a question with a short answer.
    """

    def enable_motors(self, ids: list[str] | None = None) -> None:
        """Enable all motors, or only the SDK identifiers supplied.

        Args:
            ids: Motor identifiers, or `None` for every motor.
        """
        ...

    def wake_up(self) -> None:
        """Perform the SDK's controlled wake motion and sound."""
        ...

    @property
    def media(self) -> MediaInterface:
        """The daemon's media layer.

        Returns:
            The interface audio capture, playback and frames all go through.
        """
        ...

    def set_target(
        self,
        head: PoseMatrix | None = None,
        antennas: list[float] | None = None,
        body_yaw: float | None = None,
    ) -> None:
        """Command a pose, without waiting for the robot to reach it.

        Args:
            head: The head pose as a four-by-four homogeneous transformation,
                or `None` to leave the head where it is.
            antennas: The two antenna angles in radians, right then left, or
                `None` to leave them where they are. A `list` rather than a
                `Sequence`, deliberately: that is what the SDK's own signature
                takes, and a wider type here would stop its object satisfying
                this protocol.
            body_yaw: The body's rotation in radians, or `None` to leave it.
        """
        ...

    def get_current_head_pose(self) -> PoseMatrix:
        """Return the measured world-frame head pose, including body yaw."""
        ...

    def get_current_joint_positions(self) -> tuple[list[float], list[float]]:
        """Return seven head joints and two antennas in radians."""
        ...

    def look_at_image(
        self,
        u: int,
        v: int,
        duration: float = 1.0,
        perform_movement: bool = True,
    ) -> PoseMatrix:
        """Compute or perform calibrated gaze toward one image pixel."""
        ...

    def set_automatic_body_yaw(self, enabled: bool) -> None:
        """Enable or disable the daemon's independent body-yaw modulation."""
        ...

    def look_at_world(
        self,
        x: float,
        y: float,
        z: float,
        duration: float = 1.0,
        perform_movement: bool = True,
    ) -> PoseMatrix:
        """Aim the head at a point in the robot's own frame.

        The frame's origin is the neutral head position, with x forward, y to
        the robot's left and z up. Only the direction matters: the SDK
        normalises the vector, so a target twice as far away in the same
        direction is the same movement.

        Args:
            x: Forward, in metres.
            y: To the robot's left, in metres.
            z: Upwards, in metres.
            duration: How long to take. Zero commands the pose immediately and
                returns without waiting, which is the only value the motion
                adapter uses — see `motion_reachy.py`.
            perform_movement: Whether to move, or only to compute the pose.

        Returns:
            The head pose the target works out to.
        """
        ...


class Offload(Protocol):
    """How an adapter runs a blocking daemon call without stalling the loop.

    Reading the camera pulls from a GStreamer pipeline and running a detector
    is a hundred milliseconds of arithmetic; doing either inline would stop the
    ESPHome protocol handling that shares the event loop for exactly that long,
    several times a second.

    It is a parameter rather than a call to `asyncio.to_thread` at each site so
    that a test can run the same code inline. A test that went through a real
    thread pool would be asserting on when the pool got round to it, which is
    a thing that is true most of the time.
    """

    def __call__[ResultT](
        self, function: Callable[[], ResultT], /
    ) -> Awaitable[ResultT]:
        """Run something blocking, somewhere that is not the event loop.

        Args:
            function: What to run.

        Returns:
            What it returned.
        """
        ...


async def in_thread[ResultT](function: Callable[[], ResultT]) -> ResultT:
    """Run a blocking call on a worker thread, which is what the robot does.

    Args:
        function: What to run.

    Returns:
        What it returned.
    """
    return await asyncio.to_thread(function)
