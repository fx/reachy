"""Head, antennas and gaze, commanded through the daemon's control handle.

Three angles in, one four-by-four transformation out — that conversion is the
whole of this module, and it lives here rather than in the behaviour layer for
the reason the ports module states: the behaviour layer wants to say "tilt the
head up", and a state machine that had to build a homogeneous transformation to
say it would be carrying the robot's kinematics around inside its transitions.

Gaze is the same argument with more force behind it. A detection arrives in
normalised image coordinates — robot-link REQ-021 — and turning those into a
direction the head can be aimed along needs the camera's field of view, which is
a property of this robot and of nothing the behaviour layer knows. So the port
takes the normalised point and this module owns the geometry.

Nothing here blocks. Every method hands the daemon a target and returns; the
daemon interpolates towards it at its own rate. The alternative the SDK offers —
a call that waits for the movement to finish — would put a half-second sleep
inside a behaviour tick, and a robot asked to stop cannot spend that long
deciding to.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Final

import numpy as np

from reachy_mini_ha_satellite.ports import AntennaPose, HeadPose

if TYPE_CHECKING:
    from reachy_contracts import NormalisedPoint
    from reachy_mini_ha_satellite.adapters.daemon import PoseMatrix, RobotHandle

__all__ = [
    "DEFAULT_HORIZONTAL_FOV",
    "DEFAULT_VERTICAL_FOV",
    "ReachyMotion",
    "head_pose_matrix",
]

# The camera's angular coverage, in radians, which is what a normalised
# coordinate has to be multiplied by to become a direction.
#
# Derived from the intrinsics the SDK records for this robot's camera: a focal
# length of 2001.8 pixels against a principal point at 1905.9 of 3840 gives a
# half-angle of atan(1905.9 / 2001.8), and 2003.1 against 1328.3 of 2592 gives
# the vertical one. Both are rounded, and both are approximations — the lens
# carries a large distortion model that the ratio above ignores, and a capture
# at less than the full sensor covers less than this.
#
# They are constructor arguments rather than constants for exactly that reason.
# What a value here gets wrong is how far the head turns for a face at the edge
# of the frame, which is a thing to measure on the robot rather than to derive
# on paper; these are the starting point, not the answer.
DEFAULT_HORIZONTAL_FOV: Final = math.radians(87.0)
DEFAULT_VERTICAL_FOV: Final = math.radians(67.0)

# How far away the gaze target is placed. It cancels: the robot's motion layer
# normalises the direction vector, so this only fixes the units the two
# transverse offsets are computed in.
_TARGET_DISTANCE: Final = 1.0


def head_pose_matrix(pose: HeadPose) -> PoseMatrix:
    """Turn three angles into the transformation the robot is commanded with.

    The rotations compose extrinsically about the robot's own axes, in the order
    roll, pitch, yaw — which is what `scipy`'s lowercase `"xyz"` means, and what
    the SDK's own movements are written in, so a pose built here and a pose
    built by the SDK mean the same thing.

    The two sign conventions worth stating, because both are silently wrong for
    a whole release when they are wrong: a positive rotation about the lateral
    axis takes the forward axis *downwards*, so `pitch` is negated to make
    positive mean up; and a positive rotation about the forward axis lifts the
    robot's left, which tilts the head towards its right.

    Args:
        pose: Where the head should point, relative to neutral.

    Returns:
        The four-by-four homogeneous transformation for that orientation. It
        carries no translation: the head stays where it is and only turns.
    """
    roll, pitch, yaw = pose.roll, -pose.pitch, pose.yaw
    cos_roll, sin_roll = math.cos(roll), math.sin(roll)
    cos_pitch, sin_pitch = math.cos(pitch), math.sin(pitch)
    cos_yaw, sin_yaw = math.cos(yaw), math.sin(yaw)

    about_x = np.array(
        [[1.0, 0.0, 0.0], [0.0, cos_roll, -sin_roll], [0.0, sin_roll, cos_roll]],
        dtype=np.float64,
    )
    about_y = np.array(
        [[cos_pitch, 0.0, sin_pitch], [0.0, 1.0, 0.0], [-sin_pitch, 0.0, cos_pitch]],
        dtype=np.float64,
    )
    about_z = np.array(
        [[cos_yaw, -sin_yaw, 0.0], [sin_yaw, cos_yaw, 0.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )

    matrix = np.eye(4, dtype=np.float64)
    matrix[:3, :3] = about_z @ about_y @ about_x
    return matrix


class ReachyMotion:
    """Everything the application moves, through the daemon's control handle."""

    def __init__(
        self,
        handle: RobotHandle,
        *,
        horizontal_fov: float = DEFAULT_HORIZONTAL_FOV,
        vertical_fov: float = DEFAULT_VERTICAL_FOV,
    ) -> None:
        """Take the handle the daemon supplied.

        Args:
            handle: What the daemon hands a running application.
            horizontal_fov: How much of the scene the camera sees across, in
                radians. A face at the horizontal edge of the frame is half
                this angle away from centre.
            vertical_fov: The same, vertically.

        Raises:
            ValueError: If either angle is not a positive, finite number of
                radians below half a turn. A zero would make every face appear
                to be dead ahead and the head would never move; a value at or
                past half a turn puts the edge of the frame behind the robot.
                Both are configuration mistakes, and a robot that simply stopped
                tracking is the least debuggable way to be told about one.
        """
        for name, angle in (
            ("horizontal_fov", horizontal_fov),
            ("vertical_fov", vertical_fov),
        ):
            if not math.isfinite(angle) or not 0.0 < angle < math.pi:
                message = (
                    f"{name} must be a finite angle in radians, greater than "
                    f"zero and less than pi, not {angle}"
                )
                raise ValueError(message)
        self._handle = handle
        self._half_horizontal = math.tan(horizontal_fov / 2.0)
        self._half_vertical = math.tan(vertical_fov / 2.0)
        self._released = False

    @property
    def released(self) -> bool:
        """Whether this adapter has stopped commanding movement.

        Returns:
            True once `release` has been called.
        """
        return self._released

    #:= docs/specs/robot-link/index.md#req-021-detection-geometry-is-resolution-independent
    #:% Positions in results MUST be expressed in normalised image coordinates rather
    #:% than pixels.
    def look_at(self, target: NormalisedPoint) -> None:
        """Aim the head at something seen in the frame.

        The target's coordinates have the origin at the image centre, run to
        plus or minus one at the edges, and point upwards — so the same face in
        the same place produces the same movement whatever resolution the frame
        was captured at. That is the property REQ-021 exists for, and it holds
        here because nothing in this method knows the frame's dimensions.

        The two signs: a face to the *right* of the image centre is to the
        robot's right, which is the negative side of an axis that points left;
        a face *above* the centre is upwards, which the vertical axis already
        agrees with.

        Args:
            target: Where the thing is, in normalised image coordinates.
        """
        if self._released:
            return
        self._handle.look_at_world(
            x=_TARGET_DISTANCE,
            y=-target.x * self._half_horizontal * _TARGET_DISTANCE,
            z=target.y * self._half_vertical * _TARGET_DISTANCE,
            duration=0.0,
            perform_movement=True,
        )

    #:= docs/specs/ha-satellite/index.md#req-048-the-head-returns-to-neutral-when-tracking-data-goes-stale
    #:% When results stop arriving within the staleness window, the application MUST
    #:% return the head to its neutral position rather than holding its last commanded
    #:% pose.
    def look_ahead(self) -> None:
        """Return the head to neutral, leaving the antennas alone."""
        self.move_head(HeadPose())

    def move_head(self, pose: HeadPose) -> None:
        """Command a head orientation.

        Args:
            pose: Where to point the head, relative to neutral.
        """
        if self._released:
            return
        self._handle.set_target(head=head_pose_matrix(pose))

    def move_antennas(self, pose: AntennaPose) -> None:
        """Command both antenna angles.

        The daemon takes the pair the other way round — right first, then left —
        so the swap happens here rather than being a thing every caller has to
        remember. Getting it wrong is invisible until the two are asked to do
        different things.

        Args:
            pose: Where to put them, relative to resting.
        """
        if self._released:
            return
        self._handle.set_target(antennas=[pose.right, pose.left])

    #:= docs/specs/ha-satellite/index.md#req-050-shutdown-is-graceful-and-leaves-the-robot-safe
    #:% On receiving a termination signal the application MUST stop commanding movement,
    #:% release the media interface, and exit.
    def release(self) -> None:
        """Stop commanding movement, for good.

        Nothing is commanded on the way out — not a last move to neutral, not a
        motor release. The daemon returns the robot to its default position once
        the application stops talking to it, and a parting command would be this
        application racing the daemon over the same joints while it is trying to
        leave.

        Later calls are ignored rather than refused, so a behaviour tick that
        was already in flight when the termination signal arrived ends quietly
        instead of raising out of a task nobody is left to await.
        """
        self._released = True
