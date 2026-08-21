"""What the behaviour layer produces: movements, described rather than performed.

An intent is a value. Nothing here commands a motor, reads a clock or touches a
port — the layer decides what the robot should be doing and hands back a
description, and `main.py` is what applies it. That separation is ha-satellite
REQ-042, and it is why the whole of this package can be exercised on a machine
with no robot attached.

The four kinds are the four movements `ports.MotionPort` accepts — one each for
`look_at`, `look_ahead`, `move_head` and `move_antennas` — and that
correspondence is deliberate: an intent with no port method behind it would be a
decision nobody could carry out. The port's other two members, `release` and
`released`, are lifecycle rather than movement and have no intent, because
letting go of the robot is not something the behaviour layer decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from reachy_mini_ha_satellite.ports import AntennaPose, HeadPose

if TYPE_CHECKING:
    from reachy_contracts import NormalisedPoint

__all__ = [
    "LookAhead",
    "LookAt",
    "MotionIntent",
    "MoveAntennas",
    "MoveHead",
]


@dataclass(frozen=True, slots=True)
class LookAt:
    """Point the head at something seen in the frame.

    Attributes:
        target: Where it is, in normalised image coordinates.
    """

    target: NormalisedPoint


@dataclass(frozen=True, slots=True)
class LookAhead:
    """Return the head to neutral.

    Distinct from `MoveHead(NEUTRAL_HEAD)`, and the distinction is the whole of
    ha-satellite REQ-048: this is what the layer produces when results have
    stopped arriving, and the port's own `look_ahead` is the method whose
    docstring says why holding the last pose would be a lie.
    """


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


type MotionIntent = LookAt | LookAhead | MoveHead | MoveAntennas
"""Everything the behaviour layer can ask for."""
