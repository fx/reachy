"""Measured-pose calibration and canonical grouped robot motion commands.

Image calibration remains the daemon's responsibility. This adapter retains a
bounded measured world-pose history, asks the daemon to solve each new image
observation without moving, removes query-time ego rotation at capture time, and
returns an absolute world-gaze anchor to the pure behavior layer.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING, Final

import numpy as np

from reachy_mini_ha_satellite.behaviour.gaze_controller import ControllerConfig
from reachy_mini_ha_satellite.motion_validation import SampleFault, validate_gaze_sample
from reachy_mini_ha_satellite.ports import (
    AntennaPose,
    CalibratedGaze,
    CalibrationStatus,
    DetectionSource,
    GazeCalibration,
    GazeDirective,
    GazeSample,
    HeadPose,
    MotionCommandResult,
    MotionCommandStatus,
    MotionFault,
    MotionMeasurement,
)
from reachy_mini_ha_satellite.timing import MIN_BEHAVIOUR_TICK_SECONDS

if TYPE_CHECKING:
    from reachy_contracts import NormalisedPoint
    from reachy_mini_ha_satellite.adapters.daemon import PoseMatrix, RobotHandle

__all__ = [
    "ReachyMotion",
    "TimedPoseHistory",
    "head_pose_matrix",
    "image_pixel",
    "project_measured_pose",
    "rebase_calibrated_rotation",
]

_DEFAULT_STALENESS_SECONDS: Final = 2.0
_DEFAULT_TICK_SECONDS: Final = 0.05
_HISTORY_ENDPOINT_SAMPLES: Final = 2
_DEFAULT_HISTORY_SAMPLES: Final = (
    math.ceil(_DEFAULT_STALENESS_SECONDS / MIN_BEHAVIOUR_TICK_SECONDS)
    + _HISTORY_ENDPOINT_SAMPLES
)
_BRACKET_LIMIT: Final = math.radians(0.5)
_MEASURED_ROTATION_RESIDUAL_LIMIT: Final = 1e-2
_MEASURED_BOTTOM_ROW_LIMIT: Final = 1e-3


@dataclass(frozen=True, slots=True)
class _PoseSample:
    """One proper measured world rotation at one monotonic timestamp."""

    at: float
    rotation: np.ndarray


class _MeasuredPoseError(ValueError):
    """A conservative measured-pose validation failure."""


def image_pixel(target: NormalisedPoint, width: int, height: int) -> tuple[int, int]:
    """Quantize normalized geometry and clamp it to strict image interior."""
    if width < 3 or height < 3:
        message = f"camera resolution must be at least 3 by 3, not {width} by {height}"
        raise ValueError(message)
    u = round((target.x + 1.0) * width / 2.0)
    v = round((1.0 - target.y) * height / 2.0)
    return min(width - 2, max(1, u)), min(height - 2, max(1, v))


def head_pose_matrix(pose: HeadPose) -> PoseMatrix:
    """Build a zero-translation rigid pose from yaw, upward pitch and roll."""
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


def _proper_rotation(rotation: np.ndarray) -> np.ndarray:
    """Validate a strict proper command/query rotation and copy it."""
    matrix = np.asarray(rotation, dtype=np.float64)
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all():
        message = "rotation must be one finite three-by-three matrix"
        raise ValueError(message)
    if not np.allclose(matrix.T @ matrix, np.eye(3), atol=1e-6):
        message = "rotation must be orthonormal"
        raise ValueError(message)
    if not math.isclose(float(np.linalg.det(matrix)), 1.0, abs_tol=1e-6):
        message = "rotation must have determinant one"
        raise ValueError(message)
    return np.array(matrix, dtype=np.float64, copy=True)


def _pose_rotation(pose: PoseMatrix, *, measured: bool) -> np.ndarray:
    """Extract one validated homogeneous pose rotation."""
    matrix = (
        project_measured_pose(pose) if measured else np.asarray(pose, dtype=np.float64)
    )
    if not measured:
        if matrix.shape != (4, 4) or not np.isfinite(matrix).all():
            message = "pose must be one finite four-by-four matrix"
            raise ValueError(message)
        if not np.allclose(matrix[3], [0.0, 0.0, 0.0, 1.0], atol=1e-7):
            message = "pose must have a canonical homogeneous final row"
            raise ValueError(message)
    return _proper_rotation(matrix[:3, :3])


def project_measured_pose(pose: PoseMatrix) -> PoseMatrix:
    """Project only conservatively small measured rotation drift onto SO(3)."""
    matrix = np.asarray(pose, dtype=np.float64)
    if matrix.shape != (4, 4):
        raise _MeasuredPoseError("measured pose must be four by four")
    if not np.isfinite(matrix).all():
        raise _MeasuredPoseError("measured pose must be finite")
    bottom_residual = float(np.max(np.abs(matrix[3] - np.array([0.0, 0.0, 0.0, 1.0]))))
    if bottom_residual > _MEASURED_BOTTOM_ROW_LIMIT:
        raise _MeasuredPoseError("measured pose final row is malformed")
    rotation = matrix[:3, :3]
    orthogonality = float(np.linalg.norm(rotation.T @ rotation - np.eye(3), ord="fro"))
    determinant_residual = abs(float(np.linalg.det(rotation)) - 1.0)
    if max(orthogonality, determinant_residual) > _MEASURED_ROTATION_RESIDUAL_LIMIT:
        raise _MeasuredPoseError("measured rotation residual is too large")
    try:
        left, _singular, right_t = np.linalg.svd(rotation)
    except np.linalg.LinAlgError as error:
        raise _MeasuredPoseError("measured rotation projection failed") from error
    projected = left @ right_t
    if float(np.linalg.det(projected)) < 0.0:
        left[:, -1] *= -1.0
        projected = left @ right_t
    result = np.array(matrix, dtype=np.float64, copy=True)
    result[:3, :3] = projected
    result[3] = [0.0, 0.0, 0.0, 1.0]
    return result


def _rotation_quaternion(rotation: np.ndarray) -> np.ndarray:
    """Convert a proper rotation into a deterministic w-x-y-z quaternion."""
    matrix = _proper_rotation(rotation)
    trace = float(np.trace(matrix))
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        quaternion = np.array(
            [
                0.25 * scale,
                (matrix[2, 1] - matrix[1, 2]) / scale,
                (matrix[0, 2] - matrix[2, 0]) / scale,
                (matrix[1, 0] - matrix[0, 1]) / scale,
            ]
        )
    else:
        index = int(np.argmax(np.diag(matrix)))
        first, second = (1, 2) if index == 0 else ((0, 2) if index == 1 else (0, 1))
        scale = (
            math.sqrt(
                max(
                    0.0,
                    1.0
                    + matrix[index, index]
                    - matrix[first, first]
                    - matrix[second, second],
                )
            )
            * 2.0
        )
        if scale <= 0.0:
            message = "rotation cannot be represented as a quaternion"
            raise ValueError(message)
        quaternion = np.empty(4, dtype=np.float64)
        cyclic_first = (index + 1) % 3
        cyclic_second = (index + 2) % 3
        quaternion[0] = (
            matrix[cyclic_second, cyclic_first] - matrix[cyclic_first, cyclic_second]
        ) / scale
        quaternion[index + 1] = 0.25 * scale
        quaternion[first + 1] = (matrix[first, index] + matrix[index, first]) / scale
        quaternion[second + 1] = (matrix[second, index] + matrix[index, second]) / scale
    norm = float(np.linalg.norm(quaternion))
    if not math.isfinite(norm) or norm <= 0.0:
        message = "rotation produced no finite quaternion"
        raise ValueError(message)
    return quaternion / norm


def _quaternion_rotation(quaternion: np.ndarray) -> np.ndarray:
    """Convert one unit w-x-y-z quaternion to a proper rotation."""
    w, x, y, z = (float(value) for value in quaternion)
    return np.array(
        [
            [1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - z * w), 2.0 * (x * z + y * w)],
            [2.0 * (x * y + z * w), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - x * w)],
            [2.0 * (x * z - y * w), 2.0 * (y * z + x * w), 1.0 - 2.0 * (x * x + y * y)],
        ],
        dtype=np.float64,
    )


def _slerp(start: np.ndarray, end: np.ndarray, phase: float) -> np.ndarray:
    """Interpolate proper rotations along their shortest quaternion path."""
    first = _rotation_quaternion(start)
    last = _rotation_quaternion(end)
    dot = float(np.dot(first, last))
    if dot < 0.0:
        last = -last
        dot = -dot
    dot = min(1.0, max(-1.0, dot))
    bounded = min(1.0, max(0.0, phase))
    if dot > 0.9995:
        quaternion = first + bounded * (last - first)
        quaternion /= np.linalg.norm(quaternion)
    else:
        angle = math.acos(dot)
        sine = math.sin(angle)
        quaternion = (
            math.sin((1.0 - bounded) * angle) / sine * first
            + math.sin(bounded * angle) / sine * last
        )
    return _proper_rotation(_quaternion_rotation(quaternion))


def _rotation_distance(first: np.ndarray, second: np.ndarray) -> float:
    """Return the shortest angular separation of two proper rotations."""
    relative = _proper_rotation(first).T @ _proper_rotation(second)
    cosine = min(1.0, max(-1.0, (float(np.trace(relative)) - 1.0) / 2.0))
    return math.acos(cosine)


class TimedPoseHistory:
    """Bounded measured world rotations with SO(3) interpolation only."""

    def __init__(
        self,
        *,
        maximum_age: float = _DEFAULT_STALENESS_SECONDS,
        maximum_samples: int = _DEFAULT_HISTORY_SAMPLES,
    ) -> None:
        """Create an empty finite history."""
        if not math.isfinite(maximum_age) or maximum_age <= 0.0:
            message = "pose history maximum age must be finite and positive"
            raise ValueError(message)
        if maximum_samples < 2:
            message = "pose history requires at least two samples"
            raise ValueError(message)
        self._maximum_age = maximum_age
        self._samples: deque[_PoseSample] = deque(maxlen=maximum_samples)

    @property
    def bounds(self) -> tuple[float, float] | None:
        """Return oldest and newest timestamps, or ``None`` while empty."""
        if not self._samples:
            return None
        return self._samples[0].at, self._samples[-1].at

    def append(self, at: float, pose: PoseMatrix) -> None:
        """Append or replace one monotonic measured pose."""
        if not math.isfinite(at):
            message = "pose history timestamp must be finite"
            raise ValueError(message)
        rotation = _pose_rotation(pose, measured=True)
        if self._samples and at < self._samples[-1].at:
            message = "pose history timestamps must not regress"
            raise ValueError(message)
        sample = _PoseSample(at, rotation)
        if self._samples and at == self._samples[-1].at:
            self._samples[-1] = sample
        else:
            self._samples.append(sample)
        cutoff = at - self._maximum_age
        while len(self._samples) > 1 and self._samples[1].at < cutoff:
            self._samples.popleft()

    def rotation_at(self, at: float) -> np.ndarray | None:
        """Interpolate inside retained history and never extrapolate."""
        bounds = self.bounds
        if bounds is None or not bounds[0] <= at <= bounds[1]:
            return None
        for earlier, later in pairwise(self._samples):
            if earlier.at <= at <= later.at:
                interval = later.at - earlier.at
                phase = 0.0 if interval == 0.0 else (at - earlier.at) / interval
                return _slerp(earlier.rotation, later.rotation, phase)
        return np.array(self._samples[-1].rotation, copy=True)


def rebase_calibrated_rotation(
    capture_rotation: np.ndarray,
    query_rotation: np.ndarray,
    target_rotation: np.ndarray,
) -> np.ndarray:
    """Remove query-time ego rotation exactly once in the capture basis."""
    rebased = (
        _proper_rotation(capture_rotation)
        @ _proper_rotation(query_rotation).T
        @ _proper_rotation(target_rotation)
    )
    return _proper_rotation(rebased)


def _direction_angles(rotation: np.ndarray) -> tuple[float, float]:
    """Extract absolute yaw and elevation from a world forward direction."""
    x, y, z = (float(value) for value in _proper_rotation(rotation)[:, 0])
    return math.atan2(y, x), math.atan2(z, math.hypot(x, y))


class ReachyMotion:
    """Measured calibration, grouped gaze commands and independent antennas."""

    def __init__(
        self,
        handle: RobotHandle,
        *,
        controller_config: ControllerConfig | None = None,
        body_enabled: bool = False,
        staleness_seconds: float = _DEFAULT_STALENESS_SECONDS,
        tick_seconds: float = _DEFAULT_TICK_SECONDS,
    ) -> None:
        """Take the daemon handle and one shared validated controller envelope."""
        if controller_config is None:
            controller_config = ControllerConfig(
                body_enabled=body_enabled,
                staleness_seconds=staleness_seconds,
            )
        else:
            body_enabled = controller_config.body_enabled
            staleness_seconds = controller_config.staleness_seconds
        if not math.isfinite(tick_seconds) or tick_seconds < MIN_BEHAVIOUR_TICK_SECONDS:
            message = (
                "motion tick seconds must be finite and at least "
                f"{MIN_BEHAVIOUR_TICK_SECONDS}"
            )
            raise ValueError(message)
        history_samples = (
            math.ceil(staleness_seconds / MIN_BEHAVIOUR_TICK_SECONDS)
            + _HISTORY_ENDPOINT_SAMPLES
        )
        self._handle = handle
        self._config = controller_config
        self._body_enabled = body_enabled
        self._history = TimedPoseHistory(
            maximum_age=staleness_seconds,
            maximum_samples=history_samples,
        )
        self._cache: dict[DetectionSource, tuple[tuple[int, int], GazeCalibration]] = {}
        self._deferred: dict[DetectionSource, tuple[int, int]] = {}
        self._acquired = False
        self._released = False
        self._last_head_measurement: tuple[float, float, float] | None = None
        self._last_body_measurement: tuple[float, float] | None = None
        self._head_fault = MotionFault.NONE
        self._body_fault = MotionFault.NONE
        self._command_calls = 0

    @property
    def released(self) -> bool:
        """Whether terminal release has begun."""
        return self._released

    def _terminal(self) -> bool:
        """Re-read terminal state across daemon callbacks that may release."""
        return self._released

    def acquire(self, now: float) -> MotionMeasurement:
        """Disable competing body yaw and return measured acquisition state."""
        if self._released or self._acquired:
            return self._measurement()
        self._acquired = True
        self._handle.set_automatic_body_yaw(False)
        return self.observe(now) if not self._terminal() else self._measurement()

    def _measurement(self) -> MotionMeasurement:
        """Return only currently valid typed measurements, retaining cache privately."""
        head = (
            self._last_head_measurement
            if self._head_fault is MotionFault.NONE
            else None
        )
        body = (
            self._last_body_measurement
            if self._body_fault is MotionFault.NONE
            else None
        )
        return MotionMeasurement(
            world_yaw=None if head is None else head[0],
            world_elevation=None if head is None else head[1],
            head_measured_at=None if head is None else head[2],
            body_yaw=None if body is None else body[0],
            body_measured_at=None if body is None else body[1],
            head_fault=self._head_fault,
            body_fault=self._body_fault,
        )

    def observe(self, now: float) -> MotionMeasurement:
        """Sample independent measured head direction and optional body yaw."""
        if self._released:
            self._head_fault = MotionFault.RELEASED
            self._body_fault = (
                MotionFault.RELEASED if self._body_enabled else MotionFault.NONE
            )
            return self._measurement()
        try:
            pose = self._handle.get_current_head_pose()
            rotation = _pose_rotation(pose, measured=True)
            self._history.append(now, pose)
            world_yaw, world_elevation = _direction_angles(rotation)
            self._last_head_measurement = (world_yaw, world_elevation, now)
            self._head_fault = MotionFault.NONE
        except (RuntimeError, TypeError, ValueError, np.linalg.LinAlgError):
            self._head_fault = MotionFault.POSE
        if self._body_enabled:
            try:
                head_joints, _antennas = self._handle.get_current_joint_positions()
                measured = float(head_joints[0]) if len(head_joints) == 7 else math.nan
                if not math.isfinite(measured):
                    raise ValueError("body measurement must be finite")
                self._last_body_measurement = (measured, now)
                self._body_fault = MotionFault.NONE
            except (IndexError, RuntimeError, TypeError, ValueError):
                self._body_fault = MotionFault.POSE
        return self._measurement()

    def calibrate(self, directive: GazeDirective, now: float) -> GazeCalibration:
        """Calibrate one new actionable face identity, with bounded retry/reject."""
        if self._released or not directive.actionable or directive.face is None:
            return GazeCalibration(
                CalibrationStatus.REJECTED,
                fault=MotionFault.CALIBRATION,
            )
        identity = directive.identity
        if (
            identity is None
            or directive.captured_at is None
            or directive.received_at is None
        ):
            return GazeCalibration(
                CalibrationStatus.REJECTED,
                fault=MotionFault.CALIBRATION,
            )
        source, generation, sequence = identity
        candidate = generation, sequence
        deferred = self._deferred.get(source)
        if deferred is not None and candidate < deferred:
            return GazeCalibration(
                CalibrationStatus.REJECTED,
                fault=MotionFault.CALIBRATION,
            )
        cached = self._cache.get(source)
        if cached is not None:
            if cached[0] == candidate:
                return cached[1]
            if candidate < cached[0]:
                return GazeCalibration(
                    CalibrationStatus.REJECTED,
                    fault=MotionFault.CALIBRATION,
                )
        if not self._acquired:
            self.acquire(now)
        if self.released:
            return GazeCalibration(
                CalibrationStatus.REJECTED,
                fault=MotionFault.CALIBRATION,
            )

        bounds = self._history.bounds
        if bounds is None:
            return self._cache_result(identity, CalibrationStatus.REJECTED)
        if directive.captured_at < bounds[0]:
            return self._cache_result(identity, CalibrationStatus.REJECTED)
        if directive.captured_at > bounds[1]:
            if deferred != candidate:
                self._deferred[source] = candidate
                return GazeCalibration(CalibrationStatus.DEFERRED)
            return self._cache_result(identity, CalibrationStatus.REJECTED)

        camera = self._handle.media.camera
        if camera is None:
            return self._cache_result(identity, CalibrationStatus.REJECTED)
        try:
            capture_rotation = self._history.rotation_at(directive.captured_at)
            if capture_rotation is None:
                return self._cache_result(identity, CalibrationStatus.REJECTED)
            u, v = image_pixel(directive.face.centre, *camera.resolution)
            bracket: tuple[np.ndarray, np.ndarray] | None = None
            target_rotation: np.ndarray | None = None
            for _attempt in range(2):
                if self._terminal():
                    return GazeCalibration(
                        CalibrationStatus.REJECTED,
                        fault=MotionFault.CALIBRATION,
                    )
                pre_pose = self._handle.get_current_head_pose()
                pre = _pose_rotation(pre_pose, measured=True)
                if self._terminal():
                    return GazeCalibration(
                        CalibrationStatus.REJECTED,
                        fault=MotionFault.CALIBRATION,
                    )
                target_pose = self._handle.look_at_image(
                    u,
                    v,
                    duration=0.0,
                    perform_movement=False,
                )
                if self._terminal():
                    return GazeCalibration(
                        CalibrationStatus.REJECTED,
                        fault=MotionFault.CALIBRATION,
                    )
                post_pose = self._handle.get_current_head_pose()
                if self._terminal():
                    return GazeCalibration(
                        CalibrationStatus.REJECTED,
                        fault=MotionFault.CALIBRATION,
                    )
                post = _pose_rotation(post_pose, measured=True)
                if _rotation_distance(pre, post) <= _BRACKET_LIMIT:
                    bracket = pre, post
                    target_rotation = _pose_rotation(target_pose, measured=False)
                    break
            if bracket is None or target_rotation is None:
                return self._cache_result(identity, CalibrationStatus.REJECTED)
            query_rotation = _slerp(bracket[0], bracket[1], 0.5)
            rebased = rebase_calibrated_rotation(
                capture_rotation,
                query_rotation,
                target_rotation,
            )
            yaw, elevation = _direction_angles(rebased)
            target = CalibratedGaze(
                source=source,
                generation=generation,
                sequence=sequence,
                captured_at=directive.captured_at,
                received_at=directive.received_at,
                target_epoch=directive.target_epoch,
                world_yaw=yaw,
                world_elevation=elevation,
            )
        except (IndexError, RuntimeError, TypeError, ValueError, np.linalg.LinAlgError):
            return self._cache_result(identity, CalibrationStatus.REJECTED)
        result = GazeCalibration(CalibrationStatus.ACCEPTED, target)
        self._cache[source] = ((generation, sequence), result)
        self._deferred.pop(source, None)
        return result

    def command_gaze(self, sample: GazeSample) -> MotionCommandResult:
        """Defend and transactionally send one canonical grouped daemon command."""
        self._command_calls += 1
        call = self._command_calls
        if self._released:
            return MotionCommandResult(
                MotionCommandStatus.REJECTED,
                MotionFault.RELEASED,
                call,
            )
        if not self._acquired:
            return MotionCommandResult(
                MotionCommandStatus.REJECTED,
                MotionFault.COMMAND,
                call,
            )
        if validate_gaze_sample(sample, self._config) is not SampleFault.NONE:
            return MotionCommandResult(
                MotionCommandStatus.REJECTED,
                MotionFault.COMMAND,
                call,
            )
        pose = head_pose_matrix(
            HeadPose(yaw=sample.world_yaw, pitch=sample.elevation, roll=0.0)
        )
        try:
            _pose_rotation(pose, measured=False)
            if not np.allclose(pose[:3, 3], np.zeros(3), atol=1e-12):
                raise ValueError("tracking pose translation must be canonical")
            world_yaw, elevation = _direction_angles(pose[:3, :3])
            if not math.isclose(
                world_yaw, sample.world_yaw, abs_tol=1e-9
            ) or not math.isclose(
                elevation,
                sample.elevation,
                abs_tol=1e-9,
            ):
                raise ValueError("tracking pose direction must match the sample")
            if self._body_enabled:
                self._handle.set_target(head=pose, body_yaw=sample.body_yaw)
            else:
                self._handle.set_target(head=pose)
        except (RuntimeError, TypeError, ValueError, np.linalg.LinAlgError):
            return MotionCommandResult(
                MotionCommandStatus.REJECTED,
                MotionFault.COMMAND,
                call,
            )
        return MotionCommandResult(MotionCommandStatus.ACCEPTED, call=call)

    def move_head(self, pose: HeadPose) -> None:
        """Command a pipeline head pose while the adapter remains live."""
        if self._released:
            return
        self._handle.set_target(head=head_pose_matrix(pose))

    def move_antennas(self, pose: AntennaPose) -> None:
        """Command independent antenna angles right then left."""
        if self._released:
            return
        self._handle.set_target(antennas=[pose.right, pose.left])

    def release(self) -> None:
        """Become terminal first and restore daemon automatic yaw exactly once."""
        if self._released:
            return
        self._released = True
        if self._acquired:
            self._handle.set_automatic_body_yaw(True)

    def _cache_result(
        self,
        identity: tuple[DetectionSource, int, int],
        state: CalibrationStatus,
    ) -> GazeCalibration:
        """Cache one terminal calibration decision per detection source."""
        source, generation, sequence = identity
        result = GazeCalibration(
            state,
            fault=(
                MotionFault.CALIBRATION
                if state is CalibrationStatus.REJECTED
                else MotionFault.NONE
            ),
        )
        self._cache[source] = ((generation, sequence), result)
        self._deferred.pop(source, None)
        return result
