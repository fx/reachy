"""The voice pipeline as a state machine, with time passed in.

Ten events, seven states, and one transition table. Nothing here reads a clock:
`now` arrives as a parameter, which is what makes a timed transition — the error
flash that lasts a moment and then clears — testable without the suite spending
that moment.

The events are this layer's own vocabulary rather than the vendored protocol's.
`adapters.pipeline_events` is where one becomes the other, and it lives on the
adapter side because a state machine that imported the ESPHome protocol would be
a state machine with opinions about protobuf.

Two states are sticky and the reason is the same for both. **Muted** means an
operator switched the microphone off, so a stray pipeline event arriving
afterwards must not make the robot look like it is listening. **Disconnected**
means Home Assistant has gone, so the robot should visibly stop rather than sit
in whatever pose the last exchange left it in — but any pipeline event at all
clears it, because receiving one is proof the connection is back.

The two overlap, and the overlap is where being sticky stops being enough. A
muted robot that loses Home Assistant *does* leave the muted state — showing
"not present" is the more useful of the two facts while the connection is gone.
What must not happen is what happened before `_muted_underneath` existed: Home
Assistant reconnects, emits `IDLE`, and the robot goes back to looking like it
is waiting for a wake word while its microphone is still off. So the mute is
remembered underneath the disconnection and restored when it clears, and the
only thing that forgets it is being unmuted.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "ERROR_SECONDS",
    "PipelineEvent",
    "PipelineMachine",
    "PipelineState",
    "Transition",
]

# How long the error expression lasts before the robot settles back to idle. A
# failed pipeline is worth showing and not worth dwelling on: long enough for a
# person in the room to see it, short enough that the robot does not look broken
# once the next exchange starts.
ERROR_SECONDS: Final = 1.5


class PipelineEvent(StrEnum):
    """Something the voice pipeline did, in the behaviour layer's own words.

    Attributes:
        WAKE_WORD_DETECTED: The wake word fired on the robot.
        LISTENING: The microphone is open and audio is going to Home Assistant.
        PROCESSING: Home Assistant is working out what was asked.
        RESPONDING: The answer is being spoken.
        RESPONSE_FINISHED: The answer finished playing.
        ERROR: The pipeline failed.
        IDLE: The pipeline went back to waiting. Also what the protocol emits
            when a connection is established, which is why it clears the
            disconnected state.
        MUTED: The microphone was switched off.
        UNMUTED: The microphone was switched back on.
        DISCONNECTED: Home Assistant went away.
    """

    WAKE_WORD_DETECTED = "wake_word_detected"
    LISTENING = "listening"
    PROCESSING = "processing"
    RESPONDING = "responding"
    RESPONSE_FINISHED = "response_finished"
    ERROR = "error"
    IDLE = "idle"
    MUTED = "muted"
    UNMUTED = "unmuted"
    DISCONNECTED = "disconnected"


class PipelineState(StrEnum):
    """What the robot is doing, which is what its movement expresses.

    The three ha-satellite REQ-046 names — `LISTENING`, `PROCESSING` and
    `RESPONDING` — each produce a movement a person in the room can tell apart
    without watching Home Assistant. See `behaviour.movement` for which is
    which.

    Attributes:
        IDLE: Waiting for the wake word.
        LISTENING: The microphone is open.
        PROCESSING: Home Assistant is thinking.
        RESPONDING: The answer is being spoken.
        ERROR: The pipeline failed; clears itself after `ERROR_SECONDS`.
        MUTED: The microphone is off, and stays off until it is switched on.
        DISCONNECTED: Home Assistant is not there.
    """

    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    RESPONDING = "responding"
    ERROR = "error"
    MUTED = "muted"
    DISCONNECTED = "disconnected"


# Every event that names a state to move to. `UNMUTED` and `RESPONSE_FINISHED`
# both settle back to idle; the two are separate events because what they mean
# is different, and a table that folded them together would make the machine's
# history unreadable.
_NEXT_STATE: Final[dict[PipelineEvent, PipelineState]] = {
    PipelineEvent.WAKE_WORD_DETECTED: PipelineState.LISTENING,
    PipelineEvent.LISTENING: PipelineState.LISTENING,
    PipelineEvent.PROCESSING: PipelineState.PROCESSING,
    PipelineEvent.RESPONDING: PipelineState.RESPONDING,
    PipelineEvent.RESPONSE_FINISHED: PipelineState.IDLE,
    PipelineEvent.ERROR: PipelineState.ERROR,
    PipelineEvent.IDLE: PipelineState.IDLE,
    PipelineEvent.MUTED: PipelineState.MUTED,
    PipelineEvent.UNMUTED: PipelineState.IDLE,
    PipelineEvent.DISCONNECTED: PipelineState.DISCONNECTED,
}

# What is honoured while the microphone is off. Everything else is ignored: a
# pipeline event arriving while muted would otherwise make the robot act out an
# exchange that cannot be happening.
_HONOURED_WHILE_MUTED: Final[frozenset[PipelineEvent]] = frozenset(
    {PipelineEvent.UNMUTED, PipelineEvent.DISCONNECTED}
)


@dataclass(frozen=True, slots=True)
class Transition:
    """What one event did to the machine.

    Attributes:
        previous: The state before.
        current: The state after.
        changed: Whether the two differ. A repeated event is not a transition,
            and a movement that restarted every time Home Assistant re-sent
            `listening` would stutter.
        ignored: Whether the event was refused outright — which today means it
            arrived while the microphone was muted.
    """

    previous: PipelineState
    current: PipelineState
    changed: bool
    ignored: bool


class PipelineMachine:
    """Where the voice pipeline is, and how long it has been there.

    Time is a parameter to every method rather than something this object
    reads. That is what makes `ERROR_SECONDS` testable without waiting for it,
    and it is the same decision the change document records for the layer as a
    whole.
    """

    def __init__(self, *, now: float = 0.0) -> None:
        """Start idle.

        Args:
            now: The reading of the caller's clock at construction, which is
                what the first `entered_at` is measured from.
        """
        self._state = PipelineState.IDLE
        self._entered_at = now
        # Whether the microphone was off when Home Assistant went away. The
        # disconnection is the more useful thing to show while it lasts, but
        # the mute outlives it, and a robot that came back looking like it was
        # listening would be reporting something that is not happening.
        self._muted_underneath = False

    @property
    def state(self) -> PipelineState:
        """What the robot is doing.

        Returns:
            The current state.
        """
        return self._state

    @property
    def entered_at(self) -> float:
        """When the current state began, on the caller's clock.

        Returns:
            The reading passed in with the event that produced this state.
        """
        return self._entered_at

    def elapsed(self, now: float) -> float:
        """How long the machine has been in its current state.

        Args:
            now: The caller's clock.

        Returns:
            The elapsed seconds, never negative — a clock that appears to have
            gone backwards is reported as no time at all rather than as a
            negative phase, which would run an animation in reverse.
        """
        return max(0.0, now - self._entered_at)

    def handle(self, event: PipelineEvent, now: float) -> Transition:
        """Apply one event.

        Args:
            event: What the pipeline did.
            now: The caller's clock.

        Returns:
            What the event did to the machine.
        """
        previous = self._state
        if previous is PipelineState.MUTED and event not in _HONOURED_WHILE_MUTED:
            return Transition(
                previous=previous,
                current=previous,
                changed=False,
                ignored=True,
            )

        if event is PipelineEvent.UNMUTED:
            self._muted_underneath = False
        elif previous is PipelineState.MUTED and event is PipelineEvent.DISCONNECTED:
            self._muted_underneath = True

        current = _NEXT_STATE[event]
        if (
            previous is PipelineState.DISCONNECTED
            and self._muted_underneath
            and current is not PipelineState.DISCONNECTED
        ):
            # The connection came back and the microphone is still off. Anything
            # else here would show a robot waiting for a wake word it cannot
            # hear.
            current = PipelineState.MUTED
        changed = current is not previous
        if changed:
            self._state = current
            self._entered_at = now
        return Transition(
            previous=previous,
            current=current,
            changed=changed,
            ignored=False,
        )

    def tick(self, now: float) -> Transition | None:
        """Let a state that expires do so.

        Only the error state expires. Everything else is left where it is until
        an event moves it, because a robot that decided on its own that a
        conversation had ended would contradict the pipeline that is still in
        one.

        Args:
            now: The caller's clock.

        Returns:
            The transition, or `None` when nothing expired.
        """
        if self._state is not PipelineState.ERROR:
            return None
        if self.elapsed(now) < ERROR_SECONDS:
            return None
        previous = self._state
        self._state = PipelineState.IDLE
        self._entered_at = now
        return Transition(
            previous=previous,
            current=self._state,
            changed=True,
            ignored=False,
        )
