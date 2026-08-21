"""The whole of the robot's behaviour, in one object with no input or output.

Events and detections in, motion intents out. This is the composition of the two
pure pieces — the pipeline state machine and the face tracker — plus the one
decision neither of them can make on its own: **who owns the head**.

Face tracking owns it whenever there is a face. A robot that stopped looking at
the person in order to act out its own internal state would have the priority
backwards, so the pipeline's head movements are what happens in the head's spare
time. The antennas are never contended for, which is why they carry the
distinguishable-movement requirement.

"Spare time" includes startup, and that is worth stating because it looks at
first like a contradiction of `behaviour.tracking`. The tracker produces no
movement at all until something has produced a detection — but the pipeline's own
pose is not a detection, so the idle expression still reaches the head, and a
robot that has just been handed control centres it. What the tracker's rule
actually protects is `LookAhead`: the movement REQ-048 names is produced only
where results were arriving and stopped, never on the strength of a detector that
has not answered yet.

Two rules keep the motor bus quiet, and both matter on a robot with four cores
that is also running motion control, audio and a wake-word model:

* a movement is commanded only when it differs from the last one by more than a
  threshold a person could see;
* entering a state always commands one, so the transition is visible
  immediately rather than at whatever point the animation happens to cross the
  threshold.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from reachy_mini_ha_satellite.behaviour.intents import (
    LookAhead,
    MotionIntent,
    MoveAntennas,
    MoveHead,
)
from reachy_mini_ha_satellite.behaviour.movement import Expression, expression
from reachy_mini_ha_satellite.behaviour.pipeline import (
    PipelineEvent,
    PipelineMachine,
    PipelineState,
)
from reachy_mini_ha_satellite.behaviour.tracking import FaceTracker, GazeOutcome
from reachy_mini_ha_satellite.ports import NEUTRAL_ANTENNAS, NEUTRAL_HEAD

if TYPE_CHECKING:
    from reachy_mini_ha_satellite.behaviour.tracking import TrackingDecision
    from reachy_mini_ha_satellite.ports import AntennaPose, Detections, HeadPose

__all__ = ["BehaviourStatus", "SatelliteBehaviour"]

# How far a pose has to move before it is worth commanding again. Roughly a
# third of a degree, which is below what anybody can see on an antenna and above
# the step one tick of the slowest animation takes.
_POSE_EPSILON: Final = 0.005


def _head_changed(pose: HeadPose, previous: HeadPose | None) -> bool:
    """Whether a head pose is worth commanding.

    Args:
        pose: What is wanted.
        previous: What was last commanded, or `None` when nothing was.

    Returns:
        True when the difference would be visible.
    """
    if previous is None:
        return True
    return (
        math.fabs(pose.yaw - previous.yaw) > _POSE_EPSILON
        or math.fabs(pose.pitch - previous.pitch) > _POSE_EPSILON
        or math.fabs(pose.roll - previous.roll) > _POSE_EPSILON
    )


def _antennas_changed(pose: AntennaPose, previous: AntennaPose | None) -> bool:
    """Whether an antenna pose is worth commanding.

    Args:
        pose: What is wanted.
        previous: What was last commanded, or `None` when nothing was.

    Returns:
        True when the difference would be visible.
    """
    if previous is None:
        return True
    return (
        math.fabs(pose.left - previous.left) > _POSE_EPSILON
        or math.fabs(pose.right - previous.right) > _POSE_EPSILON
    )


@dataclass(frozen=True, slots=True)
class BehaviourStatus:
    """What the robot is doing, for anything that wants to report it.

    Attributes:
        state: Where the voice pipeline is.
        outcome: Why the head is where it is.
        idle: Whether the robot has gone long enough without a face to settle
            into its idle behaviour.
        tracking: Whether a face is being followed right now.
    """

    state: PipelineState
    outcome: GazeOutcome
    idle: bool
    tracking: bool


#:= docs/specs/ha-satellite/index.md#req-042-decision-logic-is-free-of-input-and-output
#:% The logic that maps voice-pipeline events and detections to motion intents MUST
#:% be implemented without performing input or output.
class SatelliteBehaviour:
    """Everything the robot decides, decided without touching the robot."""

    def __init__(
        self,
        *,
        deadzone: float = 0.02,
        smoothing: float = 0.35,
        idle_seconds: float = 6.0,
        tracking_enabled: bool = True,
        now: float = 0.0,
    ) -> None:
        """Describe how the robot behaves.

        Args:
            deadzone: How far a face must move before the head follows.
            smoothing: How much of the way towards a new target one tick moves.
            idle_seconds: How long without a face before the idle behaviour
                starts.
            tracking_enabled: Whether the head follows a face at all. Off means
                the head is free for the pipeline's own movements the whole
                time, which is the configuration for a robot with no detector.
            now: The caller's clock at construction.

        Raises:
            ValueError: If the idle interval is not a positive number of
                seconds, or if the tracker refuses its own arguments.
        """
        if idle_seconds <= 0.0:
            message = f"the idle interval must be positive, not {idle_seconds}"
            raise ValueError(message)
        self._machine = PipelineMachine(now=now)
        self._tracker = FaceTracker(deadzone=deadzone, smoothing=smoothing, now=now)
        self._idle_seconds = idle_seconds
        self._tracking_enabled = tracking_enabled
        self._last_head: HeadPose | None = None
        self._last_antennas: AntennaPose | None = None
        self._outcome = GazeOutcome.UNKNOWN
        self._idle_since: float | None = now

    @property
    def state(self) -> PipelineState:
        """Where the voice pipeline is.

        Returns:
            The current state.
        """
        return self._machine.state

    def retune(
        self,
        *,
        deadzone: float,
        smoothing: float,
        idle_seconds: float,
        tracking_enabled: bool,
    ) -> None:
        """Adopt new tuning without forgetting what the robot is doing.

        This is what the settings interface calls for the settings that can be
        changed while the application runs. Rebuilding the behaviour layer
        instead would reset the pipeline state and the committed gaze, so an
        operator adjusting a threshold mid-conversation would see the robot
        forget the conversation.

        Args:
            deadzone: How far a face must move before the head follows.
            smoothing: How much of the way towards a new target one tick moves.
            idle_seconds: How long without a face before the idle behaviour
                starts.
            tracking_enabled: Whether the head follows a face at all.

        Raises:
            ValueError: On the same values the constructor refuses.
        """
        if idle_seconds <= 0.0:
            message = f"the idle interval must be positive, not {idle_seconds}"
            raise ValueError(message)
        self._tracker.retune(deadzone=deadzone, smoothing=smoothing)
        self._idle_seconds = idle_seconds
        self._tracking_enabled = tracking_enabled

    def status(self, now: float) -> BehaviourStatus:
        """Say what the robot is doing, for the settings interface to report.

        Args:
            now: The caller's clock.

        Returns:
            The current state, the reason the head is where it is, and whether
            the robot has settled into idling.
        """
        return BehaviourStatus(
            state=self._machine.state,
            outcome=self._outcome,
            idle=self._is_idle(now),
            tracking=self._tracker.committed is not None,
        )

    def handle(self, event: PipelineEvent, now: float) -> tuple[MotionIntent, ...]:
        """Apply one pipeline event and say what the robot should do about it.

        Args:
            event: What the pipeline did.
            now: The caller's clock.

        Returns:
            The movements to command. Empty when the event changed nothing —
            a repeated `listening` while already listening restarts no
            animation.
        """
        transition = self._machine.handle(event, now)
        if not transition.changed:
            return ()
        return self._express(now, forced=True)

    def tick(self, detections: Detections, now: float) -> tuple[MotionIntent, ...]:
        """Say what the robot should be doing at this instant.

        Args:
            detections: What the perception source last saw.
            now: The caller's clock.

        Returns:
            The movements to command, gaze first. Often empty: a robot that is
            already where it should be is commanded nothing.
        """
        expired = self._machine.tick(now)
        intents: list[MotionIntent] = []

        decision = self._track(detections, now)
        self._outcome = decision.outcome
        self._idle_since = decision.idle_since
        if decision.intent is not None:
            intents.append(decision.intent)
            if isinstance(decision.intent, LookAhead):
                # `look_ahead` puts the head at neutral, so that is what was
                # last commanded — without this the expression below would
                # command neutral a second time.
                self._last_head = NEUTRAL_HEAD

        intents.extend(self._express(now, forced=expired is not None))
        return tuple(intents)

    def _track(self, detections: Detections, now: float) -> TrackingDecision:
        """Ask the tracker what to do, or stand the head down entirely.

        Args:
            detections: What the perception source last saw.
            now: The caller's clock.

        Returns:
            The tracker's decision, or its standing down when face tracking is
            switched off.

            Standing down rather than simply not asking, and that matters when
            tracking is switched off from the settings page mid-run: a tracker
            that was never told would keep its committed aim, the head would
            stay claimed by something that has stopped deciding, and the last
            face's pose would be held for the life of the process.
        """
        if self._tracking_enabled:
            return self._tracker.update(detections, now)
        return self._tracker.stand_down(now)

    def _is_idle(self, now: float) -> bool:
        """Whether the robot has been without a face long enough to settle.

        Args:
            now: The caller's clock.

        Returns:
            True once `idle_seconds` have passed with nobody to look at.
        """
        if self._idle_since is None:
            return False
        return now - self._idle_since >= self._idle_seconds

    def _expression(self, now: float) -> Expression:
        """Work out how the robot should be holding itself.

        Args:
            now: The caller's clock.

        Returns:
            The antenna pose, and the head pose to use if the head is free.
        """
        state = self._machine.state
        if state is not PipelineState.IDLE:
            return expression(state, self._machine.elapsed(now))

        idle_since = self._idle_since
        if idle_since is None or now - idle_since < self._idle_seconds:
            # Waiting, with somebody in the room. Still, rather than swaying:
            # the sway is what the robot does when it has been left alone.
            return Expression(antennas=NEUTRAL_ANTENNAS, head=NEUTRAL_HEAD)

        # The phase starts when idling starts, so the sway begins at rest
        # rather than wherever the sine happened to be.
        return expression(PipelineState.IDLE, now - idle_since - self._idle_seconds)

    def _express(self, now: float, *, forced: bool) -> tuple[MotionIntent, ...]:
        """Command the antennas, and the head when nothing is tracking with it.

        Args:
            now: The caller's clock.
            forced: Whether to command even where nothing has changed, which is
                what makes a state transition immediately visible.

        Returns:
            The movements to command.
        """
        wanted = self._expression(now)
        intents: list[MotionIntent] = []

        if forced or _antennas_changed(wanted.antennas, self._last_antennas):
            self._last_antennas = wanted.antennas
            intents.append(MoveAntennas(wanted.antennas))

        if self._tracker.committed is None and (
            forced or _head_changed(wanted.head, self._last_head)
        ):
            self._last_head = wanted.head
            intents.append(MoveHead(wanted.head))

        return tuple(intents)
