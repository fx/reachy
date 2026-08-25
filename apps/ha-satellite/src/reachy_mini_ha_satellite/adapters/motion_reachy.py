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
from enum import StrEnum
from itertools import pairwise
from typing import TYPE_CHECKING, Final

import numpy as np

from reachy_mini_ha_satellite.ports import AntennaPose, HeadPose

if TYPE_CHECKING:
    from reachy_contracts import NormalisedPoint
    from reachy_mini_ha_satellite.adapters.daemon import PoseMatrix, RobotHandle
    from reachy_mini_ha_satellite.behaviour.gaze_controller import GazeSample
    from reachy_mini_ha_satellite.behaviour.tracking import GazeDirective
    from reachy_mini_ha_satellite.ports import DetectionSource

__all__ = [
    "DEFAULT_HORIZONTAL_FOV",
    "DEFAULT_VERTICAL_FOV",
    "CalibratedTarget",
    "CalibrationResult",
    "CalibrationState",
    "ReachyMotion",
    "TimedPoseHistory",
    "head_pose_matrix",
    "image_pixel",
    "project_measured_pose",
    "rebase_calibrated_rotation",
]

DEFAULT_HORIZONTAL_FOV: Final = math.radians(87.0)
DEFAULT_VERTICAL_FOV: Final = math.radians(67.0)
_TARGET_DISTANCE: Final = 1.0
_POSE_HISTORY_SECONDS: Final = 3.0
_POSE_HISTORY_COUNT: Final = 256
_BRACKET_LIMIT: Final = math.radians(0.5)
_MEASURED_ROTATION_RESIDUAL_LIMIT: Final = 1e-2
_MEASURED_BOTTOM_ROW_LIMIT: Final = 1e-3


class CalibrationState(StrEnum):
    """The bounded outcome of calibrating one qualified observation."""

    ACCEPTED = "accepted"
    DEFERRED = "deferred"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class CalibratedTarget:
    """One absolute world anchor preserving its selected observation facts."""

    source: DetectionSource
    generation: int
    sequence: int
    captured_at: float
    received_at: float
    target_epoch: int
    world_yaw: float
    world_elevation: float

    @property
    def identity(self) -> tuple[DetectionSource, int, int]:
        """Return the qualified observation identity."""
        return self.source, self.generation, self.sequence


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    """Accepted target or a bounded defer/reject decision."""

    state: CalibrationState
    target: CalibratedTarget | None = None

    def __post_init__(self) -> None:
        """Keep accepted and non-accepted shapes unambiguous."""
        if (self.state is CalibrationState.ACCEPTED) != (self.target is not None):
            message = "only accepted calibration may carry a target"
            raise ValueError(message)


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
        quaternion[0] = (matrix[second, first] - matrix[first, second]) / scale
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
        maximum_age: float = _POSE_HISTORY_SECONDS,
        maximum_samples: int = _POSE_HISTORY_COUNT,
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
        horizontal_fov: float = DEFAULT_HORIZONTAL_FOV,
        vertical_fov: float = DEFAULT_VERTICAL_FOV,
        body_enabled: bool = False,
    ) -> None:
        """Take the structural daemon handle without importing its SDK."""
        for name, angle in (
            ("horizontal_fov", horizontal_fov),
            ("vertical_fov", vertical_fov),
        ):
            if not math.isfinite(angle) or not 0.0 < angle < math.pi:
                message = f"{name} must be finite, positive and lower than pi"
                raise ValueError(message)
        self._handle = handle
        self._half_horizontal = math.tan(horizontal_fov / 2.0)
        self._half_vertical = math.tan(vertical_fov / 2.0)
        self._body_enabled = body_enabled
        self._history = TimedPoseHistory()
        self._cache: dict[
            DetectionSource, tuple[tuple[int, int], CalibrationResult]
        ] = {}
        self._deferred: dict[tuple[DetectionSource, int, int], int] = {}
        self._acquired = False
        self._released = False
        self._auto_yaw_restored = False
        self._measured_body_yaw: float | None = None

    @property
    def released(self) -> bool:
        """Whether terminal release has begun."""
        return self._released

    @property
    def measured_body_yaw(self) -> float | None:
        """Return the latest valid measured body yaw."""
        return self._measured_body_yaw

    def acquire(self, now: float) -> None:
        """Disable competing body yaw and seed measured state before gaze owns head."""
        if self._released or self._acquired:
            return
        self._handle.set_automatic_body_yaw(False)
        self._acquired = True
        self.observe(now)

    def observe(self, now: float) -> float | None:
        """Append measured world pose and optional body feedback for this tick."""
        if self._released:
            return None
        self._history.append(now, self._handle.get_current_head_pose())
        if self._body_enabled:
            head_joints, _antennas = self._handle.get_current_joint_positions()
            if len(head_joints) != 7:
                message = "body feedback requires seven measured head joints"
                raise ValueError(message)
            measured = float(head_joints[0])
            if not math.isfinite(measured):
                message = "measured body yaw must be finite"
                raise ValueError(message)
            self._measured_body_yaw = measured
        return self._measured_body_yaw

    def calibrate(self, directive: GazeDirective, now: float) -> CalibrationResult:
        """Calibrate one new actionable face identity, with bounded retry/reject."""
        if self._released or not directive.actionable or directive.face is None:
            return CalibrationResult(CalibrationState.REJECTED)
        identity = directive.identity
        if (
            identity is None
            or directive.captured_at is None
            or directive.received_at is None
        ):
            return CalibrationResult(CalibrationState.REJECTED)
        source, generation, sequence = identity
        cached = self._cache.get(source)
        if cached is not None and cached[0] == (generation, sequence):
            return cached[1]
        if not self._acquired:
            self.acquire(now)
        if self.released:
            return CalibrationResult(CalibrationState.REJECTED)

        bounds = self._history.bounds
        if bounds is None:
            return self._cache_result(identity, CalibrationState.REJECTED)
        if directive.captured_at < bounds[0]:
            return self._cache_result(identity, CalibrationState.REJECTED)
        if directive.captured_at > bounds[1]:
            deferred = self._deferred.get(identity, 0)
            if deferred < 1:
                self._deferred[identity] = deferred + 1
                return CalibrationResult(CalibrationState.DEFERRED)
            return self._cache_result(identity, CalibrationState.REJECTED)

        camera = self._handle.media.camera
        if camera is None:
            return self._cache_result(identity, CalibrationState.REJECTED)
        try:
            capture_rotation = self._history.rotation_at(directive.captured_at)
            if capture_rotation is None:
                return self._cache_result(identity, CalibrationState.REJECTED)
            u, v = image_pixel(directive.face.centre, *camera.resolution)
            bracket: tuple[np.ndarray, np.ndarray] | None = None
            target_rotation: np.ndarray | None = None
            for _attempt in range(2):
                pre_pose = self._handle.get_current_head_pose()
                pre = _pose_rotation(pre_pose, measured=True)
                target_pose = self._handle.look_at_image(
                    u,
                    v,
                    duration=0.0,
                    perform_movement=False,
                )
                post_pose = self._handle.get_current_head_pose()
                post = _pose_rotation(post_pose, measured=True)
                if _rotation_distance(pre, post) <= _BRACKET_LIMIT:
                    bracket = pre, post
                    target_rotation = _pose_rotation(target_pose, measured=False)
                    break
            if bracket is None or target_rotation is None:
                return self._cache_result(identity, CalibrationState.REJECTED)
            query_rotation = _slerp(bracket[0], bracket[1], 0.5)
            query_pose = np.eye(4, dtype=np.float64)
            query_pose[:3, :3] = query_rotation
            self._history.append(now, query_pose)
            rebased = rebase_calibrated_rotation(
                capture_rotation,
                query_rotation,
                target_rotation,
            )
            yaw, elevation = _direction_angles(rebased)
            target = CalibratedTarget(
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
            return self._cache_result(identity, CalibrationState.REJECTED)
        result = CalibrationResult(CalibrationState.ACCEPTED, target)
        self._cache[source] = ((generation, sequence), result)
        self._deferred.pop(identity, None)
        return result

    def command_gaze(self, sample: GazeSample) -> None:
        """Send an absolute canonical world head pose and optional grouped body yaw."""
        if self._released:
            return
        if not self._acquired:
            message = "gaze motion must be acquired before its first command"
            raise RuntimeError(message)
        pose = head_pose_matrix(
            HeadPose(yaw=sample.world_yaw, pitch=sample.elevation, roll=0.0)
        )
        if self._body_enabled:
            self._handle.set_target(head=pose, body_yaw=sample.body_yaw)
        else:
            self._handle.set_target(head=pose)

    def look_at(self, target: NormalisedPoint) -> None:
        """Retain the pre-integration direct gaze seam until runtime replacement."""
        if self._released:
            return
        self._handle.look_at_world(
            x=_TARGET_DISTANCE,
            y=-target.x * self._half_horizontal * _TARGET_DISTANCE,
            z=target.y * self._half_vertical * _TARGET_DISTANCE,
            duration=0.0,
            perform_movement=True,
        )

    def look_ahead(self) -> None:
        """Retain the pre-integration direct neutral seam."""
        self.move_head(HeadPose())

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
        if self._acquired and not self._auto_yaw_restored:
            self._auto_yaw_restored = True
            self._handle.set_automatic_body_yaw(True)

    def _cache_result(
        self,
        identity: tuple[DetectionSource, int, int],
        state: CalibrationState,
    ) -> CalibrationResult:
        """Cache one terminal calibration decision per detection source."""
        source, generation, sequence = identity
        result = CalibrationResult(state)
        self._cache[source] = ((generation, sequence), result)
        self._deferred.pop(identity, None)
        return result
