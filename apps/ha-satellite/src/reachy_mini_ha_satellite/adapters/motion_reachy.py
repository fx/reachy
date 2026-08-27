"""Measured-pose calibration and canonical grouped robot motion commands.

Image calibration remains the daemon's responsibility. This adapter retains a
bounded measured world-pose history, asks the daemon to solve each new image
observation without moving, removes query-time ego rotation at capture time, and
returns an absolute world-gaze anchor to the pure behavior layer.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING, Final

import numpy as np

from reachy_mini_ha_satellite.behaviour.gaze_controller import (
    BodyMeasurement,
    ControllerConfig,
    HeadMeasurement,
)
from reachy_mini_ha_satellite.motion_validation import SampleFault, validate_gaze_sample
from reachy_mini_ha_satellite.motor_control import (
    MotorGroup,
    MotorGroupCoordinator,
    MotorGroupLifecycle,
)
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
    from collections.abc import Callable

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


@dataclass(frozen=True, slots=True)
class _MotionReseedSample:
    """Immutable measured hardware input carried from worker to event loop."""

    group: MotorGroup
    at: float
    pose: np.ndarray | None
    head: HeadMeasurement | None
    body: BodyMeasurement | None
    antennas: AntennaPose | None


@dataclass(frozen=True, slots=True)
class _BodyPolicySnapshot:
    """Generation-stamped loop ownership captured before daemon quiescence."""

    generation: int
    automatic_yaw: bool


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
        coordinator: MotorGroupCoordinator | None = None,
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
        self._coordinator = coordinator
        self._history = TimedPoseHistory(
            maximum_age=staleness_seconds,
            maximum_samples=history_samples,
        )
        self._cache: dict[DetectionSource, tuple[tuple[int, int], GazeCalibration]] = {}
        self._deferred: dict[DetectionSource, tuple[int, int]] = {}
        self._acquired = False
        self._temporary_ownership = False
        self._released = False
        self._state_lock = threading.RLock()
        self._generation = 0
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

    def _release_requested(self) -> bool:
        """Whether terminal release has been asked for anywhere yet.

        Wider than `_terminal`, and only the daemon policy writes ask it.
        `release` asks the coordinator to become terminal *before* it sets
        `_released`, so a check reading this adapter's own flag alone answers
        "not released" throughout that window — long enough for a worker-side
        policy write to report a restore that the coordinator has already
        stopped anyone from undoing.
        """
        if self._released:
            return True
        coordinator = self._coordinator
        return coordinator is not None and coordinator.terminal_requested

    def acquire(self, now: float) -> MotionMeasurement:
        """Disable competing body yaw and invalidate every older lifecycle snapshot."""
        with self._state_lock:
            if self._released:
                return self._measurement()
            if self._acquired and not self._temporary_ownership:
                return self._measurement()
            needs_daemon_quiesce = not self._acquired
            self._acquired = True
            self._temporary_ownership = False
            self._generation += 1
            generation = self._generation
        if needs_daemon_quiesce:
            try:
                self._handle.set_automatic_body_yaw(False)
            except Exception:
                with self._state_lock:
                    if self._generation == generation:
                        self._acquired = False
                        self._generation += 1
                raise
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
                MotionFault.RELEASED if self._config.body_enabled else MotionFault.NONE
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
        if self._config.body_enabled:
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
        if self._terminal() or not directive.actionable or directive.face is None:
            return GazeCalibration(
                CalibrationStatus.REJECTED,
                fault=MotionFault.CALIBRATION,
            )
        # `_generation` counts ownership and measured state. Calibrating changes
        # neither — it repopulates a per-source cache from history it has
        # already read — so this reads the counter and never advances it.
        # Advancing it made every tick with a face in view invalidate an
        # in-flight measured reseed: a torque re-enable that confirms, opens no
        # gate, and leaves the group unusable until the application restarts.
        #
        # **This method can still advance it indirectly**, through the ownership
        # fallback further down: with `_acquired` false it calls `acquire`,
        # which bumps the counter and samples. Two hops keep production out of
        # that branch, and neither is local to this file — `SatelliteApplication`
        # acquires at startup whenever `face_tracking_enabled`, and
        # `SatelliteBehaviour.prepare` stands down rather than yielding a face
        # to calibrate when it is not. A change to either is what makes the
        # fallback reachable again, so both are pinned by tests rather than
        # assumed here.
        with self._state_lock:
            if self._released:
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
            required = [MotorGroup.HEAD]
            if self._config.body_enabled:
                required.append(MotorGroup.BODY)
                sent = self._command(
                    required,
                    lambda: self._handle.set_target(
                        head=pose,
                        body_yaw=sample.body_yaw,
                    ),
                )
            else:
                sent = self._command(
                    required,
                    lambda: self._handle.set_target(head=pose),
                )
            if not sent:
                raise RuntimeError("a required motor command gate is closed")
        except (RuntimeError, TypeError, ValueError, np.linalg.LinAlgError):
            return MotionCommandResult(
                MotionCommandStatus.REJECTED,
                MotionFault.COMMAND,
                call,
            )
        return MotionCommandResult(MotionCommandStatus.ACCEPTED, call=call)

    def move_head(self, pose: HeadPose) -> None:
        """Command a pipeline head pose while the adapter remains live and gated."""
        if self._released:
            return
        self._command(
            (MotorGroup.HEAD,),
            lambda: self._handle.set_target(head=head_pose_matrix(pose)),
        )

    def move_antennas(self, pose: AntennaPose) -> None:
        """Command independent antenna angles right then left while gated."""
        if self._released:
            return
        self._command(
            (MotorGroup.ANTENNAS,),
            lambda: self._handle.set_target(antennas=[pose.right, pose.left]),
        )

    def _command(
        self,
        groups: list[MotorGroup] | tuple[MotorGroup, ...],
        action: Callable[[], None],
    ) -> bool:
        """Run one adapter producer through the shared serialized gate."""
        if self._released:
            return False
        coordinator = self._coordinator
        if coordinator is None:
            action()
            return True
        return coordinator.command(groups, action)

    def motor_lifecycle(
        self,
        group: MotorGroup,
        clock: Callable[[], float],
        finalize: Callable[
            [HeadMeasurement | None, BodyMeasurement | None, AntennaPose | None],
            None,
        ],
    ) -> MotorGroupLifecycle:
        """Create one generation-stamped loop/worker lifecycle for a motor group."""
        return _ReachyMotionLifecycle(self, group, clock, finalize)

    def _sample_reseed(self, group: MotorGroup, now: float) -> _MotionReseedSample:
        """Read and validate hardware without mutating loop-owned adapter state."""
        pose: np.ndarray | None = None
        head: HeadMeasurement | None = None
        body: BodyMeasurement | None = None
        antennas: AntennaPose | None = None
        if group in {MotorGroup.HEAD, MotorGroup.BODY}:
            measured_pose = self._handle.get_current_head_pose()
            pose = project_measured_pose(measured_pose)
            pose.setflags(write=False)
            world_yaw, world_elevation = _direction_angles(pose[:3, :3])
            head = HeadMeasurement(world_yaw, world_elevation, now)
        if group is MotorGroup.BODY or (
            group is MotorGroup.HEAD and self._config.body_enabled
        ):
            head_joints, _antenna_joints = self._handle.get_current_joint_positions()
            if len(head_joints) != 7 or not math.isfinite(float(head_joints[0])):
                raise ValueError("body reseed requires one complete finite joint read")
            body = BodyMeasurement(float(head_joints[0]), now)
        if group is MotorGroup.ANTENNAS:
            _head_joints, antenna_joints = self._handle.get_current_joint_positions()
            if len(antenna_joints) != 2 or not all(
                math.isfinite(float(value)) for value in antenna_joints
            ):
                raise ValueError("antenna reseed requires two finite joint values")
            antennas = AntennaPose(
                right=float(antenna_joints[0]),
                left=float(antenna_joints[1]),
            )
        return _MotionReseedSample(group, now, pose, head, body, antennas)

    def _commit_reseed(
        self,
        sample: _MotionReseedSample,
        expected_generation: int,
    ) -> int | None:
        """Commit a measured sample only if no newer loop-owned motion won."""
        with self._state_lock:
            if self._released or self._generation != expected_generation:
                return None
            if sample.pose is not None and sample.head is not None:
                self._history = TimedPoseHistory(
                    maximum_age=self._config.staleness_seconds,
                    maximum_samples=(
                        math.ceil(
                            self._config.staleness_seconds / MIN_BEHAVIOUR_TICK_SECONDS
                        )
                        + _HISTORY_ENDPOINT_SAMPLES
                    ),
                )
                self._history.append(sample.at, sample.pose)
                self._cache.clear()
                self._deferred.clear()
                self._last_head_measurement = (
                    sample.head.world_yaw,
                    sample.head.world_elevation,
                    sample.at,
                )
                self._head_fault = MotionFault.NONE
            if sample.body is not None:
                self._last_body_measurement = (sample.body.yaw, sample.at)
                self._body_fault = MotionFault.NONE
            self._generation += 1
            return self._generation

    def _begin_lifecycle(self) -> _BodyPolicySnapshot:
        with self._state_lock:
            return _BodyPolicySnapshot(self._generation, not self._acquired)

    def _commit_quiesce(self, snapshot: _BodyPolicySnapshot) -> int | None:
        with self._state_lock:
            if self._released or self._generation != snapshot.generation:
                return None
            self._acquired = True
            self._temporary_ownership = True
            self._generation += 1
            return self._generation

    def _restore_policy_worker(
        self,
        expected_generation: int,
        automatic_yaw: bool,
    ) -> bool:
        """Hand the daemon its policy back, or leave its body producer off.

        The post-write check asks exactly what the pre-write check asked. A
        generation-only answer would report success from inside the window
        `release` opens between asking the coordinator to become terminal and
        setting `_released`, and the caller would then take a retained capture
        for a restored one — leaving daemon automatic yaw enabled with nothing
        left that would turn it off.
        """
        with self._state_lock:
            if self._release_requested() or self._generation != expected_generation:
                return False
        restored = False
        try:
            self._handle.set_automatic_body_yaw(automatic_yaw)
            with self._state_lock:
                restored = (
                    not self._release_requested()
                    and self._generation == expected_generation
                )
        finally:
            # Every path that did not keep the policy re-asserts the safe state,
            # a call that raised included: the daemon may have adopted the write
            # before it failed, and this is the last thread that knows the write
            # was attempted at all.
            if not restored and automatic_yaw:
                self._handle.set_automatic_body_yaw(False)
        return restored

    def _commit_restore(
        self,
        expected_generation: int,
        automatic_yaw: bool,
        restored: bool,
    ) -> int | None:
        with self._state_lock:
            if (
                not restored
                or self._released
                or self._generation != expected_generation
            ):
                return None
            self._acquired = not automatic_yaw
            self._temporary_ownership = False
            self._generation += 1
            return self._generation

    def release(self) -> None:
        """Become terminal first and restore only after all reservations drain."""
        coordinator = self._coordinator
        if coordinator is not None:
            coordinator.terminal()
        with self._state_lock:
            if self._released:
                return
            acquired = self._acquired
            self._released = True
            self._generation += 1
        if acquired and (
            coordinator is None or coordinator.safe_to_restore_body_policy()
        ):
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


#:= docs/specs/home-assistant-configuration-and-camera-feed/index.md#req-094-motor-groups-change-safely-at-run-time
#:% The satellite MUST apply independent head, body and antenna motor-group switches
#:% immediately by quiescing every application- and daemon-owned command producer for
#:% the group before torque-off, establishing exclusive body-command ownership when
#:% needed, confirming each physical grouped-torque transition, and reacquiring and
#:% seeding measured state before movement or the preceding ownership policy resumes,
#:% without weakening existing trajectory, workspace, safe-hold or terminal-release
#:% guarantees.
class _ReachyMotionLifecycle(MotorGroupLifecycle):
    """Generation-check motion state around blocking daemon and sampling calls."""

    def __init__(
        self,
        motion: ReachyMotion,
        group: MotorGroup,
        clock: Callable[[], float],
        finalize: Callable[
            [HeadMeasurement | None, BodyMeasurement | None, AntennaPose | None],
            None,
        ],
    ) -> None:
        self._motion = motion
        self._group = group
        self._clock = clock
        self._finalize = finalize
        self._expected_generation: int | None = None

    def prepare_is_blocking(self) -> bool:
        return self._group is MotorGroup.BODY

    def prepare_worker(self) -> object:
        if self._group is not MotorGroup.BODY:
            return None
        snapshot = self._motion._begin_lifecycle()
        self._motion._handle.set_automatic_body_yaw(False)
        return snapshot

    def prepare_loop(self, prepared: object) -> None:
        if self._group is not MotorGroup.BODY:
            with self._motion._state_lock:
                if self._motion._released:
                    raise RuntimeError("motion was released before motor preparation")
                self._expected_generation = self._motion._generation
            return
        if not isinstance(prepared, _BodyPolicySnapshot):
            raise RuntimeError("body quiesce produced no policy snapshot")
        generation = self._motion._commit_quiesce(prepared)
        if generation is None:
            raise RuntimeError("newer motion ownership superseded body quiesce")
        self._expected_generation = generation

    def captured_policy(self, prepared: object) -> bool | None:
        if isinstance(prepared, _BodyPolicySnapshot):
            return prepared.automatic_yaw
        return None

    def sample_worker(self) -> object:
        return self._motion._sample_reseed(self._group, self._clock())

    def sample_loop(self, sample: object) -> None:
        expected = self._expected_generation
        if not isinstance(sample, _MotionReseedSample) or expected is None:
            raise RuntimeError("motor reseed has no current generation")
        generation = self._motion._commit_reseed(sample, expected)
        if generation is None:
            raise RuntimeError("newer motion state superseded measured reseed")
        self._expected_generation = generation
        self._finalize(sample.head, sample.body, sample.antennas)

    def restore_worker(self, policy: bool | None) -> object:
        expected = self._expected_generation
        if self._group is not MotorGroup.BODY or policy is None:
            return None
        if expected is None:
            raise RuntimeError("body restore has no current generation")
        restored = self._motion._restore_policy_worker(expected, policy)
        return (expected, policy, restored)

    def restore_loop(self, restored: object) -> None:
        if self._group is not MotorGroup.BODY:
            return
        if not (
            isinstance(restored, tuple)
            and len(restored) == 3
            and type(restored[0]) is int
            and type(restored[1]) is bool
            and type(restored[2]) is bool
        ):
            raise RuntimeError("body restore produced no generation result")
        expected, policy, succeeded = restored
        generation = self._motion._commit_restore(expected, policy, succeeded)
        if generation is None:
            raise RuntimeError("newer motion ownership superseded body restore")
        self._expected_generation = generation
