"""What the behaviour layer produces: movements, described rather than performed.

An intent is a value. Nothing here commands a motor, reads a clock or touches a
port — the layer decides what the robot should be doing and hands back a
description, and `main.py` is what applies it. That separation is ha-satellite
REQ-042, and it is why the whole of this package can be exercised on a machine
with no robot attached.

The three kinds are the three commands behavior arbitrates: canonical
coordinated gaze, a pipeline head pose, and independent antennas. Each has one
`ports.MotionPort` method behind it. Measurement, calibration, acquisition and
release are composition-root lifecycle rather than movements and have no intent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from reachy_mini_ha_satellite.ports import AntennaPose, HeadPose

if TYPE_CHECKING:
    from reachy_mini_ha_satellite.behaviour.gaze_controller import GazeSample

__all__ = [
    "CommandGaze",
    "MotionIntent",
    "MoveAntennas",
    "MoveHead",
]


@dataclass(frozen=True, slots=True)
class CommandGaze:
    """Command one canonical world-gaze and optional body sample."""

    sample: GazeSample


@dataclass(frozen=True, slots=True)
class MoveHead:
    """Command a head pose relative to neutral.

    Attributes:
        pose: Where to put the head.
    """

    pose: HeadPose


@dataclass(frozen=True, slots=True)
class MoveAntennas:
    """Command both antenna angles.

    Attributes:
        pose: Where to put them.
    """

    pose: AntennaPose


type MotionIntent = CommandGaze | MoveHead | MoveAntennas
"""Everything the behaviour layer can ask for."""
