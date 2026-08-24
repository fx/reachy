"""Which detector answers, and the open question this change had to resolve.

ha-satellite REQ-047 makes the source selectable between three things, and the
scenario it carries is the one at the bottom of this file: an operator with no
groundstation selects local detection, face tracking works, and no session is
attempted.

The rest of the module is about the third selection, and about the decision the
change document left open. **Session loss triggers fallback; staleness triggers
the neutral head.** The tests here are what make that a property rather than a
sentence: a source that is connected and stale keeps answering, so the head goes
back to neutral exactly as it would with no fallback configured, and only a link
that has actually gone starts a second detector on a robot with four cores.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Final

import pytest
from satellite_support import FakeFaceDetector, FakeMedia, FakePerception, face, inline
from session_client_support import (
    FACE,
    ManualClock,
    RecordedSleep,
    ScriptedTransports,
    StubTransport,
    agreement,
    credential,
    hand_control_to_the_event_loop,
)

from reachy_mini_ha_satellite.adapters.groundstation import RemotePerception
from reachy_mini_ha_satellite.adapters.perception_local import LocalPerception
from reachy_mini_ha_satellite.adapters.perception_source import (
    FallbackPerception,
    build_perception,
)
from reachy_mini_ha_satellite.ports import (
    Detections,
    DetectionSource,
    SourceSelection,
)
from reachy_session_client import SessionClient

# RFC 5737 documentation space; this repository is public.
_URL = "ws://192.0.2.10:8765/v1/session"

# The two enum members whose dotted form the repository's leak scanner reads as
# an mDNS hostname. Bound once here, with the per-line marker its own docstring
# says this case is what the marker is for.
_ROBOT: Final = DetectionSource.LOCAL  # leak-scan:allow
_ROBOT_ONLY: Final = SourceSelection.LOCAL  # leak-scan:allow


async def _immediately(seconds: float) -> None:
    """Hand control to the event loop rather than waiting out an interval.

    Args:
        seconds: How long the caller wanted to wait, ignored.
    """
    del seconds
    await asyncio.sleep(0)


def _remote_seeing_somebody() -> FakePerception:
    """Build a connected groundstation source with a face in view.

    Returns:
        The source.
    """
    source = FakePerception()
    source.see(face(0.2, 0.1), source=DetectionSource.REMOTE)
    return source


def _local_seeing_somebody() -> FakePerception:
    """Build a local source with a face in view.

    Returns:
        The source.
    """
    source = FakePerception()
    source.see(face(-0.3, 0.0), source=_ROBOT)
    return source


class TestTheThreeSelections:
    """REQ-047's three, and nothing downstream can tell them apart."""

    def test_remote_is_the_groundstation_alone(self) -> None:
        """Which is the default, because the robot has four cores."""
        remote = _remote_seeing_somebody()
        assert build_perception(SourceSelection.REMOTE, remote=remote) is remote

    def test_local_is_the_robots_own_detector_alone(self) -> None:
        """REQ-047's scenario: an installation with no groundstation."""
        local = _local_seeing_somebody()
        assert build_perception(_ROBOT_ONLY, local=local) is local

    @pytest.mark.asyncio
    async def test_selecting_local_attempts_no_session(self) -> None:
        """The other half of that scenario, and the half worth checking."""
        remote = _remote_seeing_somebody()
        local = _local_seeing_somebody()
        source = build_perception(_ROBOT_ONLY, remote=remote, local=local)
        await source.start()
        assert remote.started == 0
        assert local.started == 1
        await source.aclose()

    def test_the_fallback_selection_composes_the_two(self) -> None:
        """And what comes back is a `PerceptionPort` like the other two."""
        source = build_perception(
            SourceSelection.REMOTE_WITH_LOCAL_FALLBACK,
            remote=_remote_seeing_somebody(),
            local=_local_seeing_somebody(),
        )
        assert isinstance(source, FallbackPerception)

    def test_a_selection_without_the_source_it_needs_is_refused(self) -> None:
        """The failure it would otherwise become is a robot that never tracks."""
        with pytest.raises(ValueError, match="local detector"):
            build_perception(_ROBOT_ONLY)
        with pytest.raises(ValueError, match="groundstation source"):
            build_perception(SourceSelection.REMOTE)
        with pytest.raises(ValueError, match="local detector"):
            build_perception(
                SourceSelection.REMOTE_WITH_LOCAL_FALLBACK,
                remote=_remote_seeing_somebody(),
            )


class TestNothingSeenYetNamesNoSource:
    """`Detections.source` describes what produced a view, not who was asked.

    "Nothing has been produced yet" and "a source produced an empty result" are
    different facts and the behaviour layer acts on them differently: an empty
    result from a live source is robot-link REQ-013's ordinary success and the
    head has truthfully been told nobody is there, while nothing-yet is the
    startup state and is also what the staleness path collapses to, which
    ha-satellite REQ-048 answers with a neutral head.

    A source that filled `source` in from "which adapter am I" made the two
    indistinguishable at the moment the application starts.
    """

    @pytest.mark.asyncio
    async def test_a_fallback_source_names_no_source_before_its_first_result(
        self,
    ) -> None:
        """The composite forwards whichever source is active, so it inherits it."""
        remote = FakePerception()
        local = FakePerception()
        source = FallbackPerception(remote, local, sleep=_immediately)
        await source.start()
        view = source.latest()
        assert view.source is None
        assert view.faces == ()
        assert not view.fresh
        await source.aclose()

    @pytest.mark.asyncio
    async def test_a_fallback_source_names_the_remote_once_it_answers(
        self,
    ) -> None:
        """And the right one, rather than merely a non-`None` one."""
        remote = _remote_seeing_somebody()
        source = FallbackPerception(
            remote,
            _local_seeing_somebody(),
            sleep=_immediately,
        )
        await source.start()
        assert source.latest().source is DetectionSource.REMOTE
        await source.aclose()

    @pytest.mark.asyncio
    async def test_a_fallback_source_names_the_robot_once_it_takes_over(
        self,
    ) -> None:
        """The other side of the same question, after the session is lost."""
        remote = _remote_seeing_somebody()
        local = _local_seeing_somebody()
        source = FallbackPerception(remote, local, sleep=_immediately)
        await source.start()
        remote.connected = False
        await source.check()
        assert source.latest().source is _ROBOT
        await source.aclose()

    @pytest.mark.asyncio
    async def test_a_fallback_source_that_has_lost_the_link_and_seen_nothing(
        self,
    ) -> None:
        """Falling back is not itself a detection.

        The local detector is running and its model may still be loading, so
        the honest answer is still that nothing has produced anything — which
        is what returns the head to neutral rather than leaving it wherever the
        groundstation last pointed it.
        """
        remote = _remote_seeing_somebody()
        remote.connected = False
        local = FakePerception()
        source = FallbackPerception(remote, local, sleep=_immediately)
        await source.start()
        await source.check()
        view = source.latest()
        assert source.falling_back
        assert view.source is None
        assert not view.fresh
        await source.aclose()


class TestTheRealSelectionsNameNoSourceBeforeTheyHaveSeenAnything:
    """The same question through `build_perception`, over the real adapters.

    The tests above drive the composite over fakes, which pins that it forwards
    whichever source is active — but a fake answers whatever it was told, so
    they cannot catch an adapter that fills `source` in from its own identity.
    These build the selections an operator actually chooses, over a fake daemon
    and a stub link, so a regression at either adapter fails here too.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "selection",
        [
            SourceSelection.REMOTE,
            _ROBOT_ONLY,
            SourceSelection.REMOTE_WITH_LOCAL_FALLBACK,
        ],
        ids=["remote", "local", "remote-with-fallback"],
    )
    async def test_a_freshly_built_selection_names_no_source(
        self,
        selection: SourceSelection,
    ) -> None:
        """Whichever was chosen, nothing has produced anything at start-up.

        Args:
            selection: The source an operator asked for.
        """
        clock = ManualClock()
        sleep = RecordedSleep()
        transport = StubTransport()
        transport.push(agreement(FACE))
        media = FakeMedia()
        remote = RemotePerception(
            media,
            SessionClient(
                url=_URL,
                credential=credential(),
                capabilities=(FACE,),
                open_transport=ScriptedTransports(transport),
                clock=clock,
                sleep=sleep,
            ),
            clock=clock,
            sleep=sleep,
            offload=inline,
        )
        local = LocalPerception(
            media,
            detector=FakeFaceDetector,
            clock=clock,
            sleep=_immediately,
            offload=inline,
        )
        source = build_perception(selection, remote=remote, local=local)
        await source.start()
        await hand_control_to_the_event_loop()
        view = source.latest()
        assert view == Detections()
        assert view.source is None
        await source.aclose()


class TestFallbackHappensOnSessionLoss:
    """The change document's first open question, resolved and pinned."""

    @pytest.mark.asyncio
    async def test_a_healthy_session_never_starts_the_local_detector(
        self,
    ) -> None:
        """A robot whose groundstation is fine pays nothing for the fallback."""
        remote = _remote_seeing_somebody()
        local = _local_seeing_somebody()
        source = FallbackPerception(remote, local, sleep=_immediately)
        await source.start()
        await _settle()
        assert remote.started == 1
        assert local.started == 0
        assert not source.falling_back
        assert source.latest().source is DetectionSource.REMOTE
        await source.aclose()

    @pytest.mark.asyncio
    async def test_a_lost_session_starts_the_local_detector(self) -> None:
        """Nothing is coming until something changes, so the robot takes over."""
        remote = _remote_seeing_somebody()
        local = _local_seeing_somebody()
        source = FallbackPerception(remote, local, sleep=_immediately)
        await source.start()
        remote.connected = False
        await source.check()
        assert local.started == 1
        assert source.falling_back
        assert source.latest().source is _ROBOT
        await source.aclose()

    @pytest.mark.asyncio
    async def test_recovery_can_resurface_the_same_cached_remote_identity(self) -> None:
        """The composite exposes remote, local, then the cached remote result again."""
        remote_view = Detections(
            faces=(face(0.2, 0.1),),
            fresh=True,
            source=DetectionSource.REMOTE,
            generation=0,
            sequence=7,
            captured_at=10.0,
            received_at=10.1,
        )
        local_view = Detections(
            faces=(face(-0.3, 0.0),),
            fresh=True,
            source=_ROBOT,
            generation=0,
            sequence=3,
            captured_at=10.2,
            received_at=10.3,
        )
        remote = FakePerception(remote_view)
        local = FakePerception(local_view)
        source = FallbackPerception(remote, local, sleep=_immediately)
        await source.start()
        first = source.latest()
        remote.connected = False
        await source.check()
        fallback = source.latest()
        remote.connected = True
        resurfaced = source.latest()

        assert [first.identity, fallback.identity, resurfaced.identity] == [
            (DetectionSource.REMOTE, 0, 7),
            (_ROBOT, 0, 3),
            (DetectionSource.REMOTE, 0, 7),
        ]
        await source.aclose()

    @pytest.mark.asyncio
    async def test_stale_results_on_a_live_session_do_not_start_it(
        self,
    ) -> None:
        """The decision, stated as a test.

        A groundstation that has gone quiet is REQ-048's case and its answer is
        a neutral head. Starting a second detector on the strength of a stall
        that may last one frame would burn the cores the rest of the
        application needs, and would make the two failures indistinguishable
        from outside.
        """
        remote = _remote_seeing_somebody()
        remote.go_stale()
        local = _local_seeing_somebody()
        source = FallbackPerception(remote, local, sleep=_immediately)
        await source.start()
        await source.check()
        assert local.started == 0
        assert not source.falling_back
        view = source.latest()
        assert not view.fresh
        assert view.faces == ()
        await source.aclose()

    @pytest.mark.asyncio
    async def test_the_local_detector_stops_once_the_session_has_held(
        self,
    ) -> None:
        """Giving the robot its cores back, which is the point of the default."""
        remote = _remote_seeing_somebody()
        local = _local_seeing_somebody()
        clock = _Clock()
        source = FallbackPerception(
            remote,
            local,
            recovery_seconds=5.0,
            clock=clock,
            sleep=_immediately,
        )
        await source.start()
        remote.connected = False
        await source.check()
        # Read into locals rather than asserting on the property twice: an
        # assertion narrows it for the rest of the function, and the second
        # assertion would then be reported as dead code.
        after_the_loss = source.falling_back
        remote.connected = True
        await source.check()
        before_recovery = source.falling_back
        clock.advance(6.0)
        await source.check()
        after_recovery = source.falling_back
        assert after_the_loss
        assert before_recovery
        assert not after_recovery
        assert local.closed == 1
        await source.aclose()

    @pytest.mark.asyncio
    async def test_a_flapping_link_does_not_reload_the_model_each_time(
        self,
    ) -> None:
        """Reloading costs more than leaving the detector running would."""
        remote = _remote_seeing_somebody()
        local = _local_seeing_somebody()
        clock = _Clock()
        source = FallbackPerception(
            remote,
            local,
            recovery_seconds=5.0,
            clock=clock,
            sleep=_immediately,
        )
        await source.start()
        for _ in range(4):
            remote.connected = False
            await source.check()
            remote.connected = True
            clock.advance(1.0)
            await source.check()
        assert local.started == 1
        assert local.closed == 0
        await source.aclose()

    @pytest.mark.asyncio
    async def test_the_supervisor_runs_on_its_own(self) -> None:
        """`check` is the decision; the loop that calls it is the mechanism."""
        remote = _remote_seeing_somebody()
        local = _local_seeing_somebody()
        source = FallbackPerception(remote, local, sleep=_immediately)
        await source.start()
        remote.connected = False
        await _settle()
        assert source.falling_back
        await source.aclose()

    @pytest.mark.asyncio
    async def test_closing_stops_both_sources(self) -> None:
        """REQ-050's shutdown reaches whichever of them is running."""
        remote = _remote_seeing_somebody()
        local = _local_seeing_somebody()
        source = FallbackPerception(remote, local, sleep=_immediately)
        await source.start()
        remote.connected = False
        await source.check()
        await source.aclose()
        assert remote.closed == 1
        assert local.closed == 1

    @pytest.mark.asyncio
    async def test_closing_before_the_fallback_ever_ran_closes_the_remote(
        self,
    ) -> None:
        """And does not close a local detector that was never started."""
        remote = _remote_seeing_somebody()
        local = _local_seeing_somebody()
        source = FallbackPerception(remote, local, sleep=_immediately)
        await source.start()
        await source.aclose()
        assert remote.closed == 1
        assert local.closed == 0

    @pytest.mark.asyncio
    async def test_starting_twice_starts_one_supervisor(self) -> None:
        """Composition roots are allowed to be careless about this."""
        remote = _remote_seeing_somebody()
        source = FallbackPerception(
            remote,
            _local_seeing_somebody(),
            sleep=_immediately,
        )
        await source.start()
        await source.start()
        assert remote.started == 1
        await source.aclose()

    @pytest.mark.asyncio
    async def test_a_fallback_that_has_seen_nothing_yet_is_not_fresh(
        self,
    ) -> None:
        """Which returns the head to neutral while the model is still loading."""
        remote = _remote_seeing_somebody()
        remote.connected = False
        local = FakePerception(Detections())
        source = FallbackPerception(remote, local, sleep=_immediately)
        await source.start()
        await source.check()
        assert not source.latest().fresh
        await source.aclose()


class _Clock:
    """A monotonic clock a test moves by hand."""

    def __init__(self) -> None:
        """Start somewhere that is not zero."""
        self._now = 1000.0

    def __call__(self) -> float:
        """Read the clock.

        Returns:
            The current reading.
        """
        return self._now

    def advance(self, seconds: float) -> None:
        """Move it forward.

        Args:
            seconds: How far.
        """
        self._now += seconds


async def _settle() -> None:
    """Let the supervisor run a few turns without waiting for a clock."""
    for _ in range(20):
        await asyncio.sleep(0)


class TestFallbackDoesNotClaimALocalDetectorItCouldNotStart:
    """The failure mode this whole selection exists to prevent.

    `remote_with_local_fallback` is worth having only because it works when the
    groundstation stops. A composite that marked the robot as having taken over
    on a detector that never loaded would leave it with detections from neither
    source, reporting that fallback was engaged, and would never retry — the
    silent failure of the mechanism whose entire job is to survive a failure.

    The tests that **count** build attempts drive `check` without calling
    `start`, deliberately. `start` spawns the supervisor, which retries on its
    own schedule, so a count taken with it running measures both and is
    deterministic only by the accident of a fake that never yields. The last
    test here is the other half: the supervisor left to do it by itself,
    asserted on its outcome rather than on a count.
    """

    @pytest.mark.asyncio
    async def test_a_local_detector_that_will_not_load_is_not_marked_running(
        self,
    ) -> None:
        """`_local_running` is set after a successful start, never before."""
        remote = _remote_seeing_somebody()
        remote.connected = False
        local = _LocalThatWillNotLoad()
        source = FallbackPerception(remote, local, sleep=_immediately)
        await _turn(source)
        assert local.attempts == 1
        assert not source.falling_back
        await source.aclose()

    @pytest.mark.asyncio
    async def test_it_reports_honestly_that_neither_source_is_available(
        self,
    ) -> None:
        """Rather than a stale view, or a fresh one attributed to nothing."""
        remote = _remote_seeing_somebody()
        remote.connected = False
        source = FallbackPerception(
            remote,
            _LocalThatWillNotLoad(),
            sleep=_immediately,
        )
        await _turn(source)
        view = source.latest()
        assert view.faces == ()
        assert not view.fresh
        assert view.source is None
        await source.aclose()

    @pytest.mark.asyncio
    async def test_every_turn_tries_the_local_detector_again(self) -> None:
        """A model that failed once may load on the next turn.

        The disk was busy, the file was still being written, the runtime was
        out of memory for a moment. Trying once and giving up silently is what
        turned this into a permanent outage.
        """
        remote = _remote_seeing_somebody()
        remote.connected = False
        local = _LocalThatWillNotLoad()
        source = FallbackPerception(remote, local, sleep=_immediately)
        for _ in range(4):
            await _turn(source)
        assert local.attempts == 4
        assert not source.falling_back
        await source.aclose()

    @pytest.mark.asyncio
    async def test_fallback_engages_once_the_detector_can_be_built(
        self,
    ) -> None:
        """And the retry is what gets there: it fails twice, then loads."""
        remote = _remote_seeing_somebody()
        remote.connected = False
        local = _LocalThatWillNotLoad(fails=2)
        source = FallbackPerception(remote, local, sleep=_immediately)
        for _ in range(3):
            await _turn(source)
        assert local.attempts == 3
        assert source.falling_back
        assert source.latest().source is _ROBOT
        await source.aclose()

    @pytest.mark.asyncio
    async def test_the_supervisor_recovers_from_it_without_being_driven(
        self,
    ) -> None:
        """The production path: nothing calls `check`, and it still gets there.

        Asserted on the outcome rather than on a count, because the number of
        turns the supervisor takes to reach a working model is its own business
        and pinning it would make this test about the scheduler.
        """
        remote = _remote_seeing_somebody()
        remote.connected = False
        local = _LocalThatWillNotLoad(fails=2)
        source = FallbackPerception(remote, local, sleep=_immediately)
        await source.start()
        await _settle()
        assert source.falling_back
        assert source.latest().source is _ROBOT
        await source.aclose()


class _LocalThatWillNotLoad(FakePerception):
    """A local source whose model fails to load for its first few starts."""

    def __init__(self, fails: int = 99) -> None:
        """Say how many starts fail before one succeeds.

        Args:
            fails: How many attempts raise. The default never succeeds.
        """
        super().__init__()
        self.attempts = 0
        self._fails = fails

    async def start(self) -> None:
        """Fail the way a missing or corrupt model does, then load.

        Raises:
            RuntimeError: Until `fails` attempts have been made.
        """
        self.attempts += 1
        if self.attempts <= self._fails:
            message = "the local model could not be loaded"
            raise RuntimeError(message)
        self.see(face(-0.3, 0.0), source=_ROBOT)


async def _turn(source: FallbackPerception) -> None:
    """Run one supervision turn the way `_supervise` runs it.

    `check` propagates a failing `start`, and that is the contract: the
    supervisor is what decides a failed turn is survivable, so a test that
    drives `check` directly has to supply the same tolerance rather than
    pretend the exception does not happen.

    Args:
        source: The composite to advance by one turn.
    """
    with contextlib.suppress(Exception):
        await source.check()


class TestTheSupervisorSurvivesItsOwnFailures:
    """It runs in a task nobody awaits until shutdown."""

    @pytest.mark.asyncio
    async def test_a_local_detector_that_will_not_start_does_not_end_supervision(
        self,
    ) -> None:
        """It would leave the source stuck on one branch with nothing saying so.

        The failure would also come back out of `aclose` — reporting something
        that happened minutes earlier as though shutting down had caused it.
        """
        remote = _remote_seeing_somebody()
        local = _RefusesToStart()
        source = FallbackPerception(remote, local, sleep=_immediately)
        await source.start()
        remote.connected = False
        await _settle()
        assert local.attempts > 1
        remote.connected = True
        await _settle()
        await source.aclose()

    @pytest.mark.asyncio
    async def test_closing_after_a_failed_turn_still_closes_cleanly(
        self,
    ) -> None:
        """A shutdown must not raise the failure of a turn from long before."""
        remote = _remote_seeing_somebody()
        remote.connected = False
        source = FallbackPerception(remote, _RefusesToStart(), sleep=_immediately)
        await source.start()
        await _settle()
        await source.aclose()
        assert remote.closed == 1


class _RefusesToStart(FakePerception):
    """A local detector whose model never loads."""

    def __init__(self) -> None:
        """Start with nothing seen and nothing loadable."""
        super().__init__()
        self.attempts = 0

    async def start(self) -> None:
        """Fail the way a missing or corrupt model would.

        Raises:
            RuntimeError: Always.
        """
        self.attempts += 1
        message = "the local model could not be loaded"
        raise RuntimeError(message)
