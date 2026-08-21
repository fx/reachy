"""The three movements REQ-046 asks to be distinguishable, checked as shapes.

The requirement is that entering listening, processing and responding each
produce a distinct, observable movement. What makes that testable rather than a
matter of taste is that the three were chosen to differ in *kind* rather than in
amplitude, so the assertions below are about the shape of the trajectory and not
about any number somebody picked:

* listening holds a raised, symmetric pose and does not move;
* processing counter-rotates — one antenna rises as the other falls;
* responding moves both together, and faster.

Still, opposed, together. A test asserting on the constants would pass whatever
they were; these fail if the three ever stop being three different things.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Final

import pytest

from reachy_mini_ha_satellite.behaviour import PipelineState, expression
from reachy_mini_ha_satellite.ports import NEUTRAL_HEAD

# Enough of a second to see a full cycle of the fastest movement and a fair
# slice of the slowest, sampled finely enough that a stationary pose and a
# moving one cannot look alike.
SAMPLES: Final[tuple[float, ...]] = tuple(index * 0.05 for index in range(41))

# The three states REQ-046 names.
PIPELINE_STATES: Final[tuple[PipelineState, ...]] = (
    PipelineState.LISTENING,
    PipelineState.PROCESSING,
    PipelineState.RESPONDING,
)


def _antenna_trace(state: PipelineState) -> tuple[tuple[float, float], ...]:
    """Sample one state's antenna trajectory.

    Args:
        state: Which state.

    Returns:
        The left and right angles at each sample time.
    """
    return tuple(
        (expression(state, at).antennas.left, expression(state, at).antennas.right)
        for at in SAMPLES
    )


class TestTheThreeAreDistinguishable:
    """REQ-046, as three properties nobody can satisfy by accident."""

    def test_listening_holds_still(self) -> None:
        """A raised, symmetric, unmoving pose. Nothing else the robot does is."""
        trace = _antenna_trace(PipelineState.LISTENING)

        assert len(set(trace)) == 1
        assert trace[0][0] == trace[0][1]
        assert trace[0][0] > 0.0

    def test_processing_counter_rotates(self) -> None:
        """One antenna up while the other is down, throughout."""
        trace = _antenna_trace(PipelineState.PROCESSING)

        assert all(left == pytest.approx(-right) for left, right in trace)
        assert len(set(trace)) > 1

    def test_responding_moves_both_together(self) -> None:
        """Symmetric like listening, moving unlike it, opposite to processing."""
        trace = _antenna_trace(PipelineState.RESPONDING)

        assert all(left == pytest.approx(right) for left, right in trace)
        assert len(set(trace)) > 1

    def test_responding_moves_faster_than_processing(self) -> None:
        """So the two moving states are not merely opposite but differently paced.

        Measured as direction reversals rather than as distance travelled: a
        wide slow sweep covers more ground than a narrow quick bob, and it is
        the tempo a person reads from across a room, not the mileage.
        """
        responding = _antenna_trace(PipelineState.RESPONDING)
        processing = _antenna_trace(PipelineState.PROCESSING)

        assert _reversals(responding) > _reversals(processing)

    def test_no_two_of_the_three_share_a_trajectory(self) -> None:
        """The requirement itself, stated once rather than implied three times."""
        traces = {state: _antenna_trace(state) for state in PIPELINE_STATES}

        assert len(set(traces.values())) == len(PIPELINE_STATES)

    def test_each_of_the_three_moves_the_head_differently_when_it_is_free(
        self,
    ) -> None:
        """The head is the secondary channel, and it says the same three things.

        It only carries them when there is no face to follow — face tracking
        wins the head — so this is what a person sees when nobody is in frame.
        """
        heads = {
            state: tuple(
                (
                    expression(state, at).head.yaw,
                    expression(state, at).head.pitch,
                    expression(state, at).head.roll,
                )
                for at in SAMPLES
            )
            for state in PIPELINE_STATES
        }

        assert len(set(heads.values())) == len(PIPELINE_STATES)


class TestTheOtherStates:
    """Idle, muted, disconnected and error each read as themselves."""

    def test_idle_sways_slowly_and_symmetrically(self) -> None:
        """Small enough not to draw the eye, present enough not to look dead."""
        trace = _antenna_trace(PipelineState.IDLE)

        assert all(left == pytest.approx(right) for left, right in trace)
        assert _travelled(trace) < _travelled(_antenna_trace(PipelineState.RESPONDING))

    def test_idle_starts_at_rest(self) -> None:
        """The sway begins where the robot already is rather than mid-cycle."""
        at_rest = expression(PipelineState.IDLE, 0.0)

        assert at_rest.antennas.left == pytest.approx(0.0)
        assert at_rest.head == NEUTRAL_HEAD

    def test_muted_folds_the_antennas_down_and_holds(self) -> None:
        """Present, and deliberately not listening."""
        trace = _antenna_trace(PipelineState.MUTED)

        assert len(set(trace)) == 1
        assert trace[0][0] < 0.0

    def test_disconnected_droops_and_lowers_the_head(self) -> None:
        """Not present at all, which is a different thing from not listening."""
        held = expression(PipelineState.DISCONNECTED, 3.0)

        assert held.antennas.left < 0.0
        assert held.head.pitch < 0.0

    def test_error_shakes_faster_than_it_thinks(self) -> None:
        """A failed thought, and it lasts a moment where processing does not."""
        error = _antenna_trace(PipelineState.ERROR)

        assert all(left == pytest.approx(-right) for left, right in error)
        assert _reversals(error) > _reversals(_antenna_trace(PipelineState.PROCESSING))

    def test_every_state_produces_an_expression(self) -> None:
        """A state added without a movement would leave the robot inert."""
        for state in PipelineState:
            assert expression(state, 0.3) is not None


class TestTimeIsAParameter:
    """The layer never reads a clock, so a phase is whatever it is handed."""

    def test_the_same_moment_produces_the_same_pose(self) -> None:
        """Which is what makes an animation testable at all."""
        first = expression(PipelineState.PROCESSING, 0.4)
        second = expression(PipelineState.PROCESSING, 0.4)

        assert first == second

    def test_a_negative_elapsed_reads_as_the_beginning(self) -> None:
        """A clock that appears to have gone backwards starts the movement."""
        assert expression(PipelineState.PROCESSING, -5.0) == expression(
            PipelineState.PROCESSING,
            0.0,
        )


def _travelled(trace: tuple[tuple[float, float], ...]) -> float:
    """How far the antennas moved over a trace.

    Args:
        trace: The sampled left and right angles.

    Returns:
        The total absolute change, which is the measure of "is this moving, and
        how much" that does not depend on where the movement is centred.
    """
    return sum(
        abs(later[0] - earlier[0]) + abs(later[1] - earlier[1])
        for earlier, later in pairwise(trace)
    )


def _reversals(trace: tuple[tuple[float, float], ...]) -> int:
    """How many times the left antenna changed direction over a trace.

    The measure of tempo that does not depend on amplitude: a wide slow sweep
    and a narrow quick bob travel similar distances and reverse very different
    numbers of times.

    Args:
        trace: The sampled left and right angles.

    Returns:
        The count of direction changes.
    """
    steps = [
        later[0] - earlier[0]
        for earlier, later in pairwise(trace)
        if later[0] != earlier[0]
    ]
    return sum(
        1 for earlier, later in pairwise(steps) if (earlier > 0.0) != (later > 0.0)
    )
