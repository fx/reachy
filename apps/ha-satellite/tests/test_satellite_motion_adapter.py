"""Measured calibration and canonical commands against a fake daemon handle.

The tests pin normalized-to-interior-pixel conversion, capture/query pose
rebasing, bounded SO(3) history, grouped head-body semantics, measured feedback
failures, automatic-yaw ownership and terminal release without robot hardware.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from satellite_support import FakeMedia, FakeRobot, face

from reachy_contracts import NormalisedPoint
from reachy_mini_ha_satellite.adapters.daemon import PoseMatrix
from reachy_mini_ha_satellite.adapters.motion_reachy import (
    ReachyMotion,
    TimedPoseHistory,
    head_pose_matrix,
    image_pixel,
    project_measured_pose,
    rebase_calibrated_rotation,
)
from reachy_mini_ha_satellite.behaviour.gaze_controller import GazeSample
from reachy_mini_ha_satellite.behaviour.tracking import GazeDirective, GazeSelector
from reachy_mini_ha_satellite.ports import (
    AntennaPose,
    CalibrationStatus,
    Detections,
    DetectionSource,
    HeadPose,
)


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


def _directive(
    *,
    sequence: int = 1,
    captured_at: float = 0.0,
    received_at: float = 0.1,
    x: float = 0.2,
    y: float = 0.0,
) -> GazeDirective:
    """Build one selected source-qualified directive."""
    detections = Detections(
        faces=(face(x, y),),
        fresh=True,
        source=DetectionSource.REMOTE,
        age_seconds=received_at - captured_at,
        generation=0,
        sequence=sequence,
        captured_at=captured_at,
        received_at=received_at,
    )
    return GazeSelector().select(detections)


class TestMeasuredPoseHistory:
    """Measured capture rotations are bounded and interpolated on SO(3)."""

    def test_history_never_extrapolates(self) -> None:
        """A capture outside retained endpoints has no invented rotation."""
        history = TimedPoseHistory()
        history.append(1.0, head_pose_matrix(HeadPose(yaw=0.1)))
        history.append(2.0, head_pose_matrix(HeadPose(yaw=0.2)))

        assert history.rotation_at(0.99) is None
        assert history.rotation_at(2.01) is None

    def test_large_rotation_uses_so3_interpolation(self) -> None:
        """A 170-degree sweep remains a finite proper midpoint rotation."""
        history = TimedPoseHistory()
        history.append(0.0, head_pose_matrix(HeadPose()))
        history.append(1.0, head_pose_matrix(HeadPose(yaw=math.radians(170.0))))

        midpoint = history.rotation_at(0.5)

        assert midpoint is not None
        forward = midpoint @ np.array([1.0, 0.0, 0.0])
        assert math.atan2(forward[1], forward[0]) == pytest.approx(math.radians(85.0))
        assert np.linalg.det(midpoint) == pytest.approx(1.0)


class TestCaptureTimeCalibration:
    """Each new identity is queried once and rebased to its capture pose."""

    def test_normalized_pixels_are_quantized_and_strictly_interior(self) -> None:
        """Clamp both normalized image edges away from SDK-invalid borders."""
        assert image_pixel(NormalisedPoint(x=-1.0, y=1.0), 640, 480) == (1, 1)
        assert image_pixel(NormalisedPoint(x=1.0, y=-1.0), 640, 480) == (
            638,
            478,
        )

    @pytest.mark.parametrize(("width", "height"), [(2, 480), (640, 2)])
    def test_camera_requires_a_strict_interior_pixel(
        self,
        width: int,
        height: int,
    ) -> None:
        """A one-pixel border leaves no valid query on a shorter axis."""
        with pytest.raises(ValueError, match="at least 3 by 3"):
            image_pixel(NormalisedPoint(x=0.0, y=0.0), width, height)

    @pytest.mark.parametrize(
        "pose",
        [
            np.eye(3, dtype=np.float64),
            np.array(
                [
                    [math.nan, 0.0, 0.0, 0.0],
                    [0.0, 1.0, 0.0, 0.0],
                    [0.0, 0.0, 1.0, 0.0],
                    [0.0, 0.0, 0.0, 1.0],
                ]
            ),
            np.array(
                [
                    [1.0, 0.0, 0.0, 0.0],
                    [0.0, 1.0, 0.0, 0.0],
                    [0.0, 0.0, 1.0, 0.0],
                    [0.01, 0.0, 0.0, 1.0],
                ]
            ),
            np.diag([1.2, 1.0, 1.0, 1.0]),
        ],
    )
    def test_malformed_measured_poses_are_rejected(self, pose: np.ndarray) -> None:
        """Projection accepts small drift, not malformed measured semantics."""
        with pytest.raises(ValueError, match="measured"):
            project_measured_pose(pose)

    @pytest.mark.parametrize(
        "rotation",
        [
            np.full((2, 2), math.nan),
            np.diag([2.0, 1.0, 1.0]),
            np.diag([-1.0, 1.0, 1.0]),
        ],
    )
    def test_rebase_rejects_malformed_strict_rotations(
        self,
        rotation: np.ndarray,
    ) -> None:
        """A query or target matrix cannot be normalized into plausible motion."""
        with pytest.raises(ValueError, match="rotation"):
            rebase_calibrated_rotation(rotation, np.eye(3), np.eye(3))

    def test_query_motion_is_removed_at_capture_time_not_receipt_time(self) -> None:
        """Compute capture @ query.T @ target during active head motion."""
        capture = head_pose_matrix(HeadPose(yaw=math.radians(10.0)))
        query = head_pose_matrix(HeadPose(yaw=math.radians(30.0)))
        sdk_target = head_pose_matrix(HeadPose(yaw=math.radians(55.0)))
        robot = FakeRobot(
            measured_head_poses=(capture, query, query),
            image_gaze_poses=(sdk_target,),
        )
        motion = ReachyMotion(robot)
        motion.acquire(0.0)
        motion.observe(1.0)

        result = motion.calibrate(_directive(received_at=0.4), 1.0)

        assert result.state is CalibrationStatus.ACCEPTED
        assert result.target is not None
        assert result.target.world_yaw == pytest.approx(math.radians(35.0))
        assert robot.image_gaze[0][2:] == (0.0, False)

    def test_cached_identity_never_requeries_the_daemon(self) -> None:
        """Faster behavior ticks consume no second cached calibration."""
        robot = FakeRobot(measured_head_poses=(np.eye(4), np.eye(4), np.eye(4)))
        motion = ReachyMotion(robot)
        motion.acquire(0.0)
        directive = _directive()

        first = motion.calibrate(directive, 0.0)
        cached = motion.calibrate(directive, 0.1)

        assert cached is first
        assert len(robot.image_gaze) == 1

    def test_unstable_query_bracket_retries_once_then_accepts(self) -> None:
        """One bracket over half a degree may be retried, never looped."""
        stable = head_pose_matrix(HeadPose(yaw=math.radians(2.0)))
        robot = FakeRobot(
            measured_head_poses=(
                np.eye(4),
                np.eye(4),
                head_pose_matrix(HeadPose(yaw=math.radians(1.0))),
                stable,
                head_pose_matrix(HeadPose(yaw=math.radians(2.2))),
            ),
            image_gaze_poses=(stable, stable),
        )
        motion = ReachyMotion(robot)
        motion.acquire(0.0)

        result = motion.calibrate(_directive(), 0.0)

        assert result.state is CalibrationStatus.ACCEPTED
        assert len(robot.image_gaze) == 2

    def test_capture_after_newest_history_defers_once_then_rejects(self) -> None:
        """Future capture is bounded defer, not pose extrapolation or hot retry."""
        robot = FakeRobot()
        motion = ReachyMotion(robot)
        motion.acquire(0.0)
        directive = _directive(captured_at=0.2, received_at=0.3)

        first = motion.calibrate(directive, 0.0)
        second = motion.calibrate(directive, 0.0)
        third = motion.calibrate(directive, 0.0)

        assert first.state is CalibrationStatus.DEFERRED
        assert second.state is CalibrationStatus.REJECTED
        assert third is second
        assert robot.image_gaze == []

    def test_unavailable_camera_is_rejected_and_cached(self) -> None:
        """A missing camera neither commands nor hammers calibration."""
        robot = FakeRobot(FakeMedia(camera_available=False))
        motion = ReachyMotion(robot)
        motion.acquire(0.0)
        directive = _directive()

        first = motion.calibrate(directive, 0.0)
        cached = motion.calibrate(directive, 0.1)

        assert first.state is CalibrationStatus.REJECTED
        assert cached is first
        assert robot.image_gaze == []


class TestMeasuredFeedbackFailures:
    """Unavailable or malformed feedback is represented as missing, never a crash."""

    def test_pose_failure_preserves_independent_body_measurement(self) -> None:
        """A failed pose-history read cannot manufacture a body-feedback loss."""
        robot = FakeRobot(measured_head_poses=(RuntimeError("pose unavailable"),))

        measured = ReachyMotion(robot, body_enabled=True).observe(0.0)

        assert measured == 0.0

    @pytest.mark.parametrize(
        "joints",
        [
            ([0.0], [0.0, 0.0]),
            ([math.nan, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], [0.0, 0.0]),
            RuntimeError("joints unavailable"),
        ],
    )
    def test_malformed_or_missing_joint_feedback_returns_none(
        self,
        joints: tuple[list[float], list[float]] | BaseException,
    ) -> None:
        """The pure controller owns persistence and recovery for missing feedback."""
        robot = FakeRobot(measured_joints=(joints,))

        measured = ReachyMotion(robot, body_enabled=True).observe(0.0)

        assert measured is None


class TestCommandingTheRobot:
    """What actually reaches the daemon's handle."""

    def test_moving_the_head_commands_a_pose(self) -> None:
        """And nothing else: the antennas are left where they are."""
        robot = FakeRobot()
        ReachyMotion(robot).move_head(HeadPose(pitch=0.1))
        assert len(robot.heads) == 1
        assert robot.antennas == []

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

    def test_head_only_world_gaze_omits_body_argument(self) -> None:
        """Canonical gaze is absolute world yaw, with no body field when disabled."""
        robot = FakeRobot()
        motion = ReachyMotion(robot)
        motion.acquire(0.0)
        sample = GazeSample(0.4, 0.2, 0.0, 0.4, False)

        motion.command_gaze(sample)

        head, antennas, body = robot.targets[-1]
        assert head is not None
        assert antennas is None
        assert body is None
        forward = head[:3, :3] @ np.array([1.0, 0.0, 0.0])
        assert math.atan2(forward[1], forward[0]) == pytest.approx(0.4)

    def test_body_enabled_world_gaze_is_one_grouped_command(self) -> None:
        """One set_target carries absolute world head pose and body target."""
        robot = FakeRobot()
        motion = ReachyMotion(robot, body_enabled=True)
        motion.acquire(0.0)
        sample = GazeSample(0.4, 0.1, 0.15, 0.25, True)

        motion.command_gaze(sample)

        head, antennas, body = robot.targets[-1]
        assert head is not None
        assert antennas is None
        assert body == pytest.approx(0.15)
        forward = head[:3, :3] @ np.array([1.0, 0.0, 0.0])
        world = math.atan2(forward[1], forward[0])
        assert world == pytest.approx(sample.body_yaw + sample.head_yaw)


class TestReleasingOnShutdown:
    """REQ-050: stop commanding movement and let the daemon have the robot."""

    def test_nothing_is_commanded_after_a_release(self) -> None:
        """Including the gaze, which is the one a tracking loop keeps sending."""
        robot = FakeRobot()
        motion = ReachyMotion(robot)
        motion.release()
        motion.command_gaze(GazeSample(0.2, 0.1, 0.0, 0.2, False))
        motion.move_head(HeadPose(pitch=0.3))
        motion.move_antennas(AntennaPose(left=1.0, right=1.0))
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

    def test_acquire_disables_auto_yaw_before_any_head_command(self) -> None:
        """Daemon body modulation is disabled even for head-only ownership."""
        events: list[str] = []
        robot = FakeRobot(events=events)
        motion = ReachyMotion(robot)

        motion.acquire(0.0)
        motion.move_head(HeadPose(yaw=0.1))

        assert events.index("motion.auto_yaw.false") < events.index("motion.command")

    def test_release_racing_acquire_restores_without_sampling_or_commanding(
        self,
    ) -> None:
        """Terminal release during the disable call blocks acquisition's next edge."""

        class ReleaseDuringAcquire(FakeRobot):
            """Release its adapter from inside automatic-yaw disable."""

            motion: ReachyMotion | None = None

            def set_automatic_body_yaw(self, enabled: bool) -> None:
                super().set_automatic_body_yaw(enabled)
                if not enabled:
                    assert self.motion is not None
                    self.motion.release()

        robot = ReleaseDuringAcquire()
        motion = ReachyMotion(robot)
        robot.motion = motion

        motion.acquire(0.0)

        assert motion.released
        assert robot.automatic_body_yaw == [False, True]
        assert "motion.pose" not in robot.events
        assert robot.targets == []

    def test_release_after_pre_bracket_blocks_calibration_query(self) -> None:
        """A terminal transition between measured pose and query sends no query."""

        class ReleaseAfterPrePose(FakeRobot):
            """Release when calibration reads its pre-query measured pose."""

            motion: ReachyMotion | None = None
            pose_reads = 0

            def get_current_head_pose(self) -> PoseMatrix:
                pose = super().get_current_head_pose()
                self.pose_reads += 1
                if self.pose_reads == 2:
                    assert self.motion is not None
                    self.motion.release()
                return pose

        robot = ReleaseAfterPrePose()
        motion = ReachyMotion(robot)
        robot.motion = motion
        motion.acquire(0.0)

        result = motion.calibrate(_directive(), 0.0)

        assert result.state is CalibrationStatus.REJECTED
        assert robot.image_gaze == []
        assert robot.automatic_body_yaw == [False, True]

    def test_terminal_release_restores_auto_yaw_once_and_blocks_later_calls(
        self,
    ) -> None:
        """Restore daemon ownership before ignoring racing gaze and antenna work."""
        robot = FakeRobot()
        motion = ReachyMotion(robot)
        motion.acquire(0.0)
        targets_before = len(robot.targets)

        motion.release()
        motion.release()
        motion.command_gaze(GazeSample(0.1, 0.0, 0.0, 0.1, False))
        motion.move_head(HeadPose(yaw=0.2))
        motion.move_antennas(AntennaPose(left=0.2, right=0.2))

        assert robot.automatic_body_yaw == [False, True]
        assert len(robot.targets) == targets_before
