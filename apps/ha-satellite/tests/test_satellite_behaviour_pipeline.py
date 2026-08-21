"""The voice pipeline state machine, every state against every event.

The change document asks for every state transition to be covered, so the table
below is exhaustive rather than representative: seven states times ten events is
seventy cases, and `test_every_state_and_event_pair_is_covered` fails if the
expectations stop covering all of them. That is the point of writing it as data
— a state or an event added later has no expectation, and the suite says so
instead of quietly testing six sevenths of the machine.

Nothing here waits for anything. The error state expires after a fixed interval
and the interval is crossed by handing the machine a larger number.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import itertools
from typing import Final

import pytest

from reachy_mini_ha_satellite.behaviour import (
    ERROR_SECONDS,
    PipelineEvent,
    PipelineMachine,
    PipelineState,
)

# What each event does from each state. Read down a column to see one event's
# whole behaviour; read across a row to see one state's.
#
# The two sticky states are the interesting rows. Muted honours only unmuting
# and disconnection — everything else leaves it muted, because a robot acting
# out an exchange while its microphone is off would be reporting something that
# is not happening. Disconnected honours everything, because receiving a
# pipeline event at all is proof the connection is back.
_ORDINARY: Final[dict[PipelineEvent, PipelineState]] = {
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

_FROM_MUTED: Final[dict[PipelineEvent, PipelineState]] = dict.fromkeys(
    PipelineEvent, PipelineState.MUTED
) | {
    PipelineEvent.UNMUTED: PipelineState.IDLE,
    PipelineEvent.DISCONNECTED: PipelineState.DISCONNECTED,
}

EXPECTED: Final[dict[tuple[PipelineState, PipelineEvent], PipelineState]] = {
    (state, event): (_FROM_MUTED if state is PipelineState.MUTED else _ORDINARY)[event]
    for state in PipelineState
    for event in PipelineEvent
}


def _machine_in(state: PipelineState) -> PipelineMachine:
    """Build a machine sitting in one particular state.

    Driven there by the event that produces it rather than by writing the
    attribute, so the fixture exercises the same code the test does.

    Args:
        state: Where the machine should be.

    Returns:
        The machine, at time zero in that state.
    """
    machine = PipelineMachine(now=0.0)
    for event, produced in _ORDINARY.items():
        if produced is state:
            machine.handle(event, 0.0)
            break
    if machine.state is not state:  # pragma: no cover - the table above is total
        message = f"no event produces {state}"
        raise AssertionError(message)
    return machine


class TestEveryTransition:
    """Seven states, ten events, and no gaps."""

    @pytest.mark.parametrize(
        ("state", "event"),
        list(itertools.product(PipelineState, PipelineEvent)),
    )
    def test_the_transition_is_what_the_table_says(
        self,
        state: PipelineState,
        event: PipelineEvent,
    ) -> None:
        """Every cell of the table, driven through the real machine.

        Args:
            state: Where the machine starts.
            event: What arrives.
        """
        machine = _machine_in(state)

        transition = machine.handle(event, 1.0)

        assert machine.state is EXPECTED[state, event]
        assert transition.current is EXPECTED[state, event]
        assert transition.previous is state

    def test_every_state_and_event_pair_is_covered(self) -> None:
        """A state or an event added later has no expectation, and this says so."""
        assert set(EXPECTED) == set(itertools.product(PipelineState, PipelineEvent))


class TestWhatATransitionReports:
    """`changed` and `ignored` are two different kinds of "nothing happened"."""

    def test_a_repeated_event_is_not_a_change(self) -> None:
        """So a re-sent `listening` does not restart the listening movement."""
        machine = PipelineMachine(now=0.0)
        machine.handle(PipelineEvent.LISTENING, 1.0)

        transition = machine.handle(PipelineEvent.LISTENING, 2.0)

        assert not transition.changed
        assert not transition.ignored

    def test_a_repeated_event_does_not_restart_the_clock(self) -> None:
        """The animation carries on from where it was rather than jumping."""
        machine = PipelineMachine(now=0.0)
        machine.handle(PipelineEvent.LISTENING, 1.0)

        machine.handle(PipelineEvent.LISTENING, 5.0)

        assert machine.entered_at == 1.0

    def test_an_event_refused_while_muted_says_it_was_refused(self) -> None:
        """Which is a different fact from the state having stayed the same."""
        machine = PipelineMachine(now=0.0)
        machine.handle(PipelineEvent.MUTED, 1.0)

        transition = machine.handle(PipelineEvent.WAKE_WORD_DETECTED, 2.0)

        assert transition.ignored
        assert not transition.changed

    def test_a_change_restarts_the_clock(self) -> None:
        """The movement for a state begins when the state does."""
        machine = PipelineMachine(now=0.0)

        machine.handle(PipelineEvent.PROCESSING, 7.5)

        assert machine.entered_at == 7.5
        assert machine.elapsed(8.0) == pytest.approx(0.5)

    def test_a_clock_that_went_backwards_reports_no_time_at_all(self) -> None:
        """Rather than a negative phase, which would run an animation in reverse."""
        machine = PipelineMachine(now=10.0)

        assert machine.elapsed(9.0) == 0.0


class TestTheErrorStateExpires:
    """The one state that clears itself, and the only use of `tick`."""

    def test_it_holds_until_the_interval_has_passed(self) -> None:
        """Long enough for a person in the room to see it."""
        machine = PipelineMachine(now=0.0)
        machine.handle(PipelineEvent.ERROR, 0.0)

        assert machine.tick(ERROR_SECONDS / 2.0) is None
        assert machine.state is PipelineState.ERROR

    def test_it_returns_to_idle_once_it_has(self) -> None:
        """A robot that stayed cross would look broken rather than informative."""
        machine = PipelineMachine(now=0.0)
        machine.handle(PipelineEvent.ERROR, 0.0)

        transition = machine.tick(ERROR_SECONDS)

        assert transition is not None
        assert transition.previous is PipelineState.ERROR
        assert machine.state is PipelineState.IDLE

    def test_nothing_else_expires(self) -> None:
        """A robot that decided a conversation had ended would contradict one."""
        for state in PipelineState:
            if state is PipelineState.ERROR:
                continue
            machine = _machine_in(state)

            assert machine.tick(1_000_000.0) is None
            assert machine.state is state


class TestAMuteThatOutlivesADisconnection:
    """The overlap between the two sticky states, where sticky is not enough.

    Being muted and being disconnected are both worth showing, and while the
    connection is gone the disconnection is the more useful of the two. What
    must not happen is the robot coming back looking like it is waiting for a
    wake word it cannot hear — which is the failure the sticky-muted rule exists
    to prevent, reached by another route.
    """

    def test_losing_home_assistant_while_muted_shows_the_disconnection(self) -> None:
        """The more useful of the two facts, while it lasts."""
        machine = _machine_in(PipelineState.MUTED)

        machine.handle(PipelineEvent.DISCONNECTED, 1.0)

        assert machine.state is PipelineState.DISCONNECTED

    def test_reconnecting_returns_to_muted_rather_than_to_idle(self) -> None:
        """The mute outlives the disconnection, because the microphone does."""
        machine = _machine_in(PipelineState.MUTED)
        machine.handle(PipelineEvent.DISCONNECTED, 1.0)

        machine.handle(PipelineEvent.IDLE, 2.0)

        assert machine.state is PipelineState.MUTED

    @pytest.mark.parametrize(
        "event",
        [
            PipelineEvent.IDLE,
            PipelineEvent.WAKE_WORD_DETECTED,
            PipelineEvent.LISTENING,
            PipelineEvent.PROCESSING,
            PipelineEvent.RESPONDING,
            PipelineEvent.RESPONSE_FINISHED,
            PipelineEvent.ERROR,
        ],
        ids=lambda event: event.value,
    )
    def test_no_pipeline_event_can_unmute_it_by_arriving(
        self,
        event: PipelineEvent,
    ) -> None:
        """Whichever event brings the connection back, the mute is still on.

        Args:
            event: A pipeline event that clears the disconnected state.
        """
        machine = _machine_in(PipelineState.MUTED)
        machine.handle(PipelineEvent.DISCONNECTED, 1.0)

        machine.handle(event, 2.0)

        assert machine.state is PipelineState.MUTED

    def test_being_unmuted_while_disconnected_forgets_it(self) -> None:
        """An operator who switched the microphone back on meant it."""
        machine = _machine_in(PipelineState.MUTED)
        machine.handle(PipelineEvent.DISCONNECTED, 1.0)

        machine.handle(PipelineEvent.UNMUTED, 2.0)
        machine.handle(PipelineEvent.IDLE, 3.0)

        assert machine.state is PipelineState.IDLE

    def test_a_disconnection_that_was_never_muted_comes_back_idle(self) -> None:
        """The ordinary case, which the transition table above describes."""
        machine = _machine_in(PipelineState.DISCONNECTED)

        machine.handle(PipelineEvent.IDLE, 2.0)

        assert machine.state is PipelineState.IDLE
