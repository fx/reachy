"""What each pipeline state looks like from across the room.

ha-satellite REQ-046 requires entering listening, processing and responding each
to produce a distinct, observable movement, and deliberately does not say which.
This module is where that choice is made, and it is made on one principle: the
three have to be told apart by their *shape*, not by their amplitude, because a
person glancing at a robot two metres away reads motion long before they read
position.

So the antennas carry the signature, and the three signatures are different
kinds of motion rather than different sizes of the same one:

| State | The antennas |
|---|---|
| Listening | both raised, and **still** |
| Processing | **counter-rotating** — one rises as the other falls |
| Responding | both **bobbing together**, twice as fast |

Still, opposed, together. Those are distinguishable in peripheral vision, in a
photograph, and — which matters here — in a test, without a test having to
assert on a number somebody chose.

The head carries the same state more quietly: attentive and slightly raised
while listening, lowered and drifting while processing, nodding while
responding. It is the secondary channel and not the primary one, because the
head is also what follows a face, and face tracking wins whenever there is a
face to follow. A robot that stopped looking at the person in order to perform
its own state would have the priority backwards.

Everything here is a pure function of the state and how long it has been in it.
Nothing reads a clock; `elapsed` is passed in.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from reachy_mini_ha_satellite.behaviour.pipeline import PipelineState
from reachy_mini_ha_satellite.ports import NEUTRAL_HEAD, AntennaPose, HeadPose

__all__ = ["Expression", "expression"]

# --- Listening ---------------------------------------------------------------
# Both antennas up and held there. The stillness is the signal: nothing else the
# robot does holds a raised, symmetric pose.
_LISTENING_ANTENNA: Final = 0.55
_LISTENING_PITCH: Final = 0.10

# --- Processing --------------------------------------------------------------
# Counter-rotation, slow enough to read as deliberate rather than agitated.
_PROCESSING_ANTENNA: Final = 0.45
_PROCESSING_PERIOD: Final = 1.2
_PROCESSING_PITCH: Final = -0.12
_PROCESSING_YAW: Final = 0.10
_PROCESSING_YAW_PERIOD: Final = 2.4

# --- Responding --------------------------------------------------------------
# Both together, at twice the processing rate, about a raised centre — so it is
# neither the still raised pose of listening nor the opposed sweep of thinking.
_RESPONDING_CENTRE: Final = 0.30
_RESPONDING_AMPLITUDE: Final = 0.20
_RESPONDING_PERIOD: Final = 0.6
_RESPONDING_PITCH: Final = 0.10

# --- Error -------------------------------------------------------------------
# A fast opposed shake. It shares its kind with processing on purpose: an error
# is a failed thought, and it lasts a moment where processing does not.
_ERROR_ANTENNA: Final = 0.35
_ERROR_PERIOD: Final = 0.25
_ERROR_ROLL: Final = 0.12

# --- Muted and disconnected --------------------------------------------------
# Both are stillness, at different depths. Muted folds the antennas down and
# keeps the head level: the robot is present and deliberately not listening.
# Disconnected droops both: it is not present at all.
_MUTED_ANTENNA: Final = -0.55
_DISCONNECTED_ANTENNA: Final = -0.25
_DISCONNECTED_PITCH: Final = -0.15

# --- Idle --------------------------------------------------------------------
# A slow symmetric sway, small enough not to draw the eye and large enough that
# the robot does not read as switched off. Every other movement has something
# that ends it — an event, or the error interval — and this one is what a robot
# left alone in a room does for hours, which is why it has to be the one nobody
# notices.
_IDLE_ANTENNA: Final = 0.08
_IDLE_PERIOD: Final = 6.0


@dataclass(frozen=True, slots=True)
class Expression:
    """How the robot holds itself in one pipeline state, at one moment.

    Attributes:
        antennas: Where the antennas are. Always commanded: the antennas are
            the primary channel and nothing competes for them.
        head: Where the head would be if it were not following a face. The
            caller applies it only when there is no face to track, which is
            what keeps face tracking the higher priority.
    """

    antennas: AntennaPose
    head: HeadPose


def _wave(elapsed: float, period: float) -> float:
    """One cycle of a sine, expressed in seconds rather than radians.

    Args:
        elapsed: How long the state has been in effect.
        period: How long one full cycle takes.

    Returns:
        A value in [-1, 1].
    """
    return math.sin(2.0 * math.pi * elapsed / period)


#:= docs/specs/ha-satellite/index.md#req-046-voice-pipeline-state-is-expressed-through-movement
#:% The application MUST produce a distinct, observable movement for entering
#:% listening, for processing, and for responding.
def expression(state: PipelineState, elapsed: float) -> Expression:
    """Say how the robot should be holding itself.

    Args:
        state: What the pipeline is doing.
        elapsed: How long it has been doing it, in seconds. Negative readings
            are treated as zero, so a clock that appears to have gone backwards
            starts the movement rather than running it in reverse.

    Returns:
        The antenna pose, and the head pose to use when no face is being
        followed.
    """
    phase = max(0.0, elapsed)

    if state is PipelineState.LISTENING:
        return Expression(
            antennas=AntennaPose(left=_LISTENING_ANTENNA, right=_LISTENING_ANTENNA),
            head=HeadPose(pitch=_LISTENING_PITCH),
        )

    if state is PipelineState.PROCESSING:
        swing = _PROCESSING_ANTENNA * _wave(phase, _PROCESSING_PERIOD)
        return Expression(
            antennas=AntennaPose(left=swing, right=-swing),
            head=HeadPose(
                yaw=_PROCESSING_YAW * _wave(phase, _PROCESSING_YAW_PERIOD),
                pitch=_PROCESSING_PITCH,
            ),
        )

    if state is PipelineState.RESPONDING:
        bob = _RESPONDING_CENTRE + _RESPONDING_AMPLITUDE * _wave(
            phase,
            _RESPONDING_PERIOD,
        )
        return Expression(
            antennas=AntennaPose(left=bob, right=bob),
            head=HeadPose(pitch=_RESPONDING_PITCH * _wave(phase, _RESPONDING_PERIOD)),
        )

    if state is PipelineState.ERROR:
        shake = _ERROR_ANTENNA * _wave(phase, _ERROR_PERIOD)
        return Expression(
            antennas=AntennaPose(left=shake, right=-shake),
            head=HeadPose(roll=_ERROR_ROLL * _wave(phase, _ERROR_PERIOD)),
        )

    if state is PipelineState.MUTED:
        return Expression(
            antennas=AntennaPose(left=_MUTED_ANTENNA, right=_MUTED_ANTENNA),
            head=NEUTRAL_HEAD,
        )

    if state is PipelineState.DISCONNECTED:
        return Expression(
            antennas=AntennaPose(
                left=_DISCONNECTED_ANTENNA,
                right=_DISCONNECTED_ANTENNA,
            ),
            head=HeadPose(pitch=_DISCONNECTED_PITCH),
        )

    sway = _IDLE_ANTENNA * _wave(phase, _IDLE_PERIOD)
    return Expression(antennas=AntennaPose(left=sway, right=sway), head=NEUTRAL_HEAD)
