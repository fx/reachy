"""Head, antennas and gaze, against a robot handle that is a fake.

Two things are worth pinning here and both of them are silent when they are
wrong.

**The signs.** A positive pitch has to raise the head and a face to the right of
the frame has to turn the robot to its right. Get either backwards and the robot
tracks a face by looking away from it, which no test of "did it move?" catches.

**The resolution independence.** Robot-link REQ-021 says a detection's position
is normalised, and the point of that is that halving the capture resolution
changes nothing about where the head goes. The adapter is what makes that true
in the motion direction, and it is true here because nothing in `look_at` knows
a frame's dimensions.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from satellite_support import FakeRobot

from reachy_contracts import NormalisedPoint
from reachy_mini_ha_satellite.adapters.motion_reachy import (
    DEFAULT_HORIZONTAL_FOV,
    DEFAULT_VERTICAL_FOV,
    ReachyMotion,
    head_pose_matrix,
)
from reachy_mini_ha_satellite.ports import AntennaPose, HeadPose


class TestGazeComesFromNormalisedCoordinates:
    """REQ-021, in the direction the robot acts on."""

    def test_the_centre_of_the_frame_is_straight_ahead(self) -> None:
        """The one target whose direction is not a matter of field of view."""
        robot = FakeRobot()
        ReachyMotion(robot).look_at(NormalisedPoint(x=0.0, y=0.0))
        assert robot.gaze == [(1.0, 0.0, 0.0)]

    def test_a_face_to_the_right_of_the_frame_turns_the_robot_right(
        self,
    ) -> None:
        """The robot's lateral axis points left, so right is negative."""
        robot = FakeRobot()
        ReachyMotion(robot).look_at(NormalisedPoint(x=0.5, y=0.0))
        (_forward, lateral, _up) = robot.gaze[0]
        assert lateral < 0.0

    def test_a_face_above_the_centre_raises_the_gaze(self) -> None:
        """The vertical axis of a detection points up, and so does the robot's."""
        robot = FakeRobot()
        ReachyMotion(robot).look_at(NormalisedPoint(x=0.0, y=0.5))
        (_forward, _lateral, up) = robot.gaze[0]
        assert up > 0.0

    def test_the_edge_of_the_frame_is_half_the_field_of_view_away(self) -> None:
        """Which is the whole content of the conversion, stated as an angle."""
        robot = FakeRobot()
        ReachyMotion(robot, horizontal_fov=math.radians(90.0)).look_at(
            NormalisedPoint(x=1.0, y=0.0),
        )
        (forward, lateral, _up) = robot.gaze[0]
        assert math.atan2(-lateral, forward) == pytest.approx(math.radians(45.0))

    def test_the_same_face_at_two_resolutions_produces_the_same_movement(
        self,
    ) -> None:
        """REQ-021's scenario, at the point the requirement is consumed.

        The two points below are what a face two thirds of the way across the
        frame normalises to at 640 by 480 and at 320 by 240 — the same numbers,
        because the conversion divided the pixels out. Nothing in the adapter
        knows a resolution, so the head goes to the same place.
        """
        wide = FakeRobot()
        narrow = FakeRobot()
        centre_at_640 = NormalisedPoint(x=(426.0 / 640.0) * 2.0 - 1.0, y=0.0)
        centre_at_320 = NormalisedPoint(x=(213.0 / 320.0) * 2.0 - 1.0, y=0.0)
        ReachyMotion(wide).look_at(centre_at_640)
        ReachyMotion(narrow).look_at(centre_at_320)
        assert wide.gaze[0] == pytest.approx(narrow.gaze[0], abs=1e-3)

    def test_the_gaze_is_commanded_without_waiting_for_it(self) -> None:
        """A behaviour tick cannot spend half a second inside a port call."""
        robot = FakeRobot()
        ReachyMotion(robot).look_at(NormalisedPoint(x=0.2, y=0.2))
        assert robot.durations == [0.0]

    def test_a_field_of_view_of_nothing_is_refused(self) -> None:
        """Every face would be dead ahead and the head would never move."""
        with pytest.raises(ValueError, match="horizontal_fov"):
            ReachyMotion(FakeRobot(), horizontal_fov=0.0)

    def test_a_field_of_view_past_half_a_turn_is_refused(self) -> None:
        """It would put the edge of the frame behind the robot."""
        with pytest.raises(ValueError, match="vertical_fov"):
            ReachyMotion(FakeRobot(), vertical_fov=math.pi)

    def test_the_defaults_are_plausible_camera_angles(self) -> None:
        """Derived from the SDK's intrinsics, and a starting point only."""
        assert 0.0 < DEFAULT_VERTICAL_FOV < DEFAULT_HORIZONTAL_FOV < math.pi


class TestTheHeadPose:
    """Three angles into the transformation the robot is commanded with."""

    def test_neutral_is_the_identity(self) -> None:
        """Which is what the SDK's own initial head pose is."""
        assert np.allclose(head_pose_matrix(HeadPose()), np.eye(4))

    def test_a_pose_carries_no_translation(self) -> None:
        """The head turns; it does not move."""
        matrix = head_pose_matrix(HeadPose(yaw=0.3, pitch=0.2, roll=0.1))
        assert np.allclose(matrix[:3, 3], np.zeros(3))
        assert matrix[3, 3] == pytest.approx(1.0)

    def test_a_pose_is_a_rotation(self) -> None:
        """Orthonormal with a positive determinant, or the arithmetic is wrong."""
        rotation = head_pose_matrix(HeadPose(yaw=0.4, pitch=-0.2, roll=0.15))[:3, :3]
        assert np.allclose(rotation @ rotation.T, np.eye(3))
        assert np.linalg.det(rotation) == pytest.approx(1.0)

    def test_a_positive_yaw_turns_the_head_to_the_robots_left(self) -> None:
        """The forward axis swings towards the lateral one, which points left."""
        forward = head_pose_matrix(HeadPose(yaw=0.5))[:3, :3] @ np.array(
            [1.0, 0.0, 0.0],
        )
        assert forward[1] > 0.0

    def test_a_positive_pitch_raises_the_head(self) -> None:
        """The sign the SDK's own axis convention would otherwise invert."""
        forward = head_pose_matrix(HeadPose(pitch=0.5))[:3, :3] @ np.array(
            [1.0, 0.0, 0.0],
        )
        assert forward[2] > 0.0

    def test_a_positive_roll_tilts_the_head_towards_its_right(self) -> None:
        """The robot's left rises, which is the head leaning right."""
        left = head_pose_matrix(HeadPose(roll=0.5))[:3, :3] @ np.array(
            [0.0, 1.0, 0.0],
        )
        assert left[2] > 0.0


class TestCommandingTheRobot:
    """What actually reaches the daemon's handle."""

    def test_moving_the_head_commands_a_pose(self) -> None:
        """And nothing else: the antennas are left where they are."""
        robot = FakeRobot()
        ReachyMotion(robot).move_head(HeadPose(pitch=0.1))
        assert len(robot.heads) == 1
        assert robot.antennas == []

    def test_returning_to_neutral_commands_the_identity(self) -> None:
        """REQ-048's movement, at the point it is made."""
        robot = FakeRobot()
        ReachyMotion(robot).look_ahead()
        assert np.allclose(robot.heads[0], np.eye(4))

    def test_the_antennas_are_sent_right_first(self) -> None:
        """The daemon takes the pair the other way round from the port.

        Invisible until the two are asked to do different things, which is
        exactly when somebody is looking at the robot rather than at a test.
        """
        robot = FakeRobot()
        ReachyMotion(robot).move_antennas(AntennaPose(left=0.2, right=-0.4))
        assert robot.antennas == [[-0.4, 0.2]]

    def test_moving_the_antennas_leaves_the_head_alone(self) -> None:
        """A port call commands what it names and nothing more."""
        robot = FakeRobot()
        ReachyMotion(robot).move_antennas(AntennaPose(left=0.1, right=0.1))
        assert robot.heads == []


class TestReleasingOnShutdown:
    """REQ-050: stop commanding movement and let the daemon have the robot."""

    def test_nothing_is_commanded_after_a_release(self) -> None:
        """Including the gaze, which is the one a tracking loop keeps sending."""
        robot = FakeRobot()
        motion = ReachyMotion(robot)
        motion.release()
        motion.look_at(NormalisedPoint(x=0.5, y=0.5))
        motion.move_head(HeadPose(pitch=0.3))
        motion.move_antennas(AntennaPose(left=1.0, right=1.0))
        motion.look_ahead()
        assert robot.gaze == []
        assert robot.heads == []
        assert robot.antennas == []

    def test_releasing_commands_nothing_on_the_way_out(self) -> None:
        """Nothing at all is commanded on the way out.

        Not even a last move to neutral: the daemon returns the robot to its
        default position once the application stops talking to it, and a
        parting command would be this application racing it over the same
        joints while trying to leave.
        """
        robot = FakeRobot()
        ReachyMotion(robot).release()
        assert robot.heads == []
        assert robot.antennas == []
        assert robot.gaze == []

    def test_a_released_port_says_so(self) -> None:
        """So a shutdown path can tell whether it has already run."""
        motion = ReachyMotion(FakeRobot())
        assert not motion.released
        motion.release()
        assert motion.released

    def test_releasing_twice_is_harmless(self) -> None:
        """A termination signal and an ordinary shutdown can both arrive."""
        motion = ReachyMotion(FakeRobot())
        motion.release()
        motion.release()
        assert motion.released
