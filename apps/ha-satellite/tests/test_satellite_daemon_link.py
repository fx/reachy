"""A lost daemon link keeps the robot diagnosable instead of killing the process.

The defect this pins was observed on a real Reachy Mini. At 22:11 the
application died with an uncaught `ConnectionError: Lost connection with the
server.` raised from `set_target` under `ReachyMotion.move_antennas`. The daemon
itself never restarted — same process, `state: running` throughout — and only
the SDK's websocket to it was dead. Nothing caught it: `command_gaze` catches
`(RuntimeError, TypeError, ValueError, LinAlgError)` and `ConnectionError` is an
`OSError`, while `move_head` and `move_antennas` caught nothing at all. Because
`reachy-mini-ha-app.service` is `Type=oneshot`, nothing restarted the
application either, and every attempt to start it again died in the wake
sequence, which moves the antennas. The robot stayed silent until a person
restarted the daemon service.

It is a pre-existing defect rather than a regression from motion gating. A
daemon that *does* offer torque confirmation reaches the same `action()` through
`MotorGroupCoordinator.command` and raises identically; what the gating change
altered is that a stock robot reaches it at all, because before it the closed
gate meant the action never ran. Both modes are therefore exercised here.

What each class asserts:

- the link record itself, which is what keeps "the daemon did not answer" apart
  from "the command was refused";
- that only a link fault is swallowed, so a bad pose is still a bad pose;
- the ungated command path, on the antennas, the head and the gaze sample;
- the gated path, over a real coordinator with its gates open;
- the wake sequence, including a link that is already down before the
  application exists;
- and the two surfaces an operator reads.

No test here needs hardware: `FakeRobot.link_down` raises what the SDK raises
from every method that sends and answers normally from every method that reads,
which is what the released `WSClient` does.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final, cast

import pytest
from satellite_support import (
    LOST_LINK_MESSAGE,
    FakeAudio,
    FakePerception,
    FakeRobot,
    ManualClock,
    face,
)

from reachy_mini_ha_satellite import main as satellite_main
from reachy_mini_ha_satellite.adapters.motion_reachy import (
    ReachyMotion,
    head_pose_matrix,
)
from reachy_mini_ha_satellite.adapters.network import NetworkIdentity
from reachy_mini_ha_satellite.behaviour import SatelliteBehaviour
from reachy_mini_ha_satellite.behaviour.tracking import GazeSelector
from reachy_mini_ha_satellite.config import (
    ENV_PREFIX,
    configuration_report,
    load_settings,
)
from reachy_mini_ha_satellite.daemon_link import (
    COUNTER_LIMIT,
    DaemonLink,
    DaemonLinkState,
    attempt_daemon_call,
    report_daemon_call,
)
from reachy_mini_ha_satellite.main import (
    SatelliteApplication,
    build_application,
    run,
)
from reachy_mini_ha_satellite.motor_control import (
    MotionGatingMode,
    MotorGroup,
    MotorGroupCoordinator,
    TorqueConfirmationSupport,
)
from reachy_mini_ha_satellite.ports import (
    AntennaPose,
    CalibrationStatus,
    Detections,
    DetectionSource,
    GazeDirective,
    GazeSample,
    HeadPose,
    MotionCommandStatus,
    MotionFault,
)
from reachy_mini_ha_satellite.web.render import render_settings_page

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

# The RFC 5737 documentation range. This repository is public.
_GROUNDSTATION: Final = "ws://192.0.2.10:8080/v1/session"

_ENVIRONMENT: Final[dict[str, str]] = {
    f"{ENV_PREFIX}DEVICE_NAME": "reachy-mini-1",
    f"{ENV_PREFIX}GROUNDSTATION_URL": _GROUNDSTATION,
    f"{ENV_PREFIX}GROUNDSTATION_CREDENTIAL": "example-credential",
    f"{ENV_PREFIX}STATE_DIR": "/reachy-satellite-link",
    f"{ENV_PREFIX}ADVERTISE": "false",
    f"{ENV_PREFIX}WEB_ENABLED": "false",
}

# How long the one test that could hang is given. Nothing here is expected to
# spend it: the drain it bounds is over producers that have already returned, so
# reaching this number is the failure rather than the wait.
_DRAIN_TIMEOUT_SECONDS: Final = 5.0


def _identity() -> NetworkIdentity:
    """Build the announced identity from documentation ranges.

    Returns:
        The identity, supplied rather than discovered: discovery reads this
        machine's interfaces and no test here is about this machine.
    """
    return NetworkIdentity(
        interface="eth0",
        ip_address="192.0.2.20",
        mac_address="02:00:5e:10:00:00",
    )


def _sample() -> GazeSample:
    """Build one in-envelope head-only command with no derivatives.

    Returns:
        A sample the adapter's own validation accepts, so that what a test sees
        is the link's answer and not the sample's.
    """
    return GazeSample(
        world_yaw=0.1,
        elevation=0.05,
        body_yaw=0.0,
        head_yaw=0.1,
        body_enabled=False,
    )


def _directive() -> GazeDirective:
    """Build one selected source-qualified directive over a face in view.

    Returns:
        The directive, whose captured timestamp sits inside the pose history
        the tests below fill, so what a calibration reports is the daemon's
        answer rather than a stale window.
    """
    detections = Detections(
        faces=(face(0.2, 0.0),),
        fresh=True,
        source=DetectionSource.REMOTE,
        age_seconds=0.1,
        generation=0,
        sequence=1,
        captured_at=1.0,
        received_at=1.1,
    )
    return GazeSelector().select(detections)


def _patch_startup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    build: Callable[..., Awaitable[object]],
) -> None:
    """Replace startup's configuration edges, leaving the wake order real.

    The same shape `test_satellite_main.py` uses, restated here rather than
    imported: a test module reaching into another test module for a helper
    would make either one unreadable on its own.

    Args:
        monkeypatch: Installs the inert edges.
        build: What stands in for composition, awaited the way the real one is.
    """

    class _Store:
        def load(self) -> dict[str, str]:
            """Return no persisted overrides without reading a file."""
            return {}

    async def _offload(work: Callable[[], object]) -> object:
        """Run an SDK call inline rather than on a worker thread."""
        return work()

    resolution = load_settings(_ENVIRONMENT)
    monkeypatch.setattr(satellite_main, "OverrideStore", lambda _path: _Store())
    monkeypatch.setattr(satellite_main, "load_settings", lambda **_kwargs: resolution)
    monkeypatch.setattr(satellite_main, "configure_logging", lambda _settings: None)
    monkeypatch.setattr(
        satellite_main,
        "log_resolved_configuration",
        lambda _resolution: None,
    )
    monkeypatch.setattr(satellite_main, "build_application", build)
    monkeypatch.setattr(satellite_main, "in_thread", _offload)


def _acquired(robot: FakeRobot, link: DaemonLink) -> ReachyMotion:
    """Build an ungated motion adapter that has taken gaze ownership.

    Args:
        robot: The daemon handle to command.
        link: The record to report the daemon on.

    Returns:
        The adapter, acquired at time zero.
    """
    motion = ReachyMotion(robot, link=link)
    motion.acquire(0.0)
    return motion


class TestTheLinkRecordKeepsTheTwoConditionsApart:
    """A link that is down is not a command that was refused, and says so."""

    def test_nothing_refused_yet_reads_up(self) -> None:
        """The first observation is the controlled wake, before any surface exists."""
        link = DaemonLink()

        assert link.state is DaemonLinkState.UP
        assert not link.down

    def test_one_refusal_takes_the_link_down_and_counts_the_outage(self) -> None:
        """The transition is what the caller is told about, so it can log once."""
        link = DaemonLink()

        assert link.record_failure() is True
        assert link.down
        assert link.status() == {
            "state": "down",
            "outages": 1,
            "refused_calls": 1,
        }

    def test_a_second_refusal_is_the_same_outage(self) -> None:
        """A loop commanding at twenty hertz is one outage, not twenty."""
        link = DaemonLink()
        link.record_failure()

        assert link.record_failure() is False
        assert link.status() == {
            "state": "down",
            "outages": 1,
            "refused_calls": 2,
        }

    def test_a_carried_command_brings_it_back(self) -> None:
        """Recovery needs no reconnect and no operator: one command that lands."""
        link = DaemonLink()
        link.record_failure()

        assert link.record_success() is True
        assert link.state is DaemonLinkState.UP
        assert not link.down

    def test_a_second_outage_is_counted_separately(self) -> None:
        """So an operator can tell one blip from a daemon that keeps going."""
        link = DaemonLink()
        link.record_failure()
        link.record_success()
        link.record_failure()

        assert link.status() == {
            "state": "down",
            "outages": 2,
            "refused_calls": 2,
        }

    def test_a_carried_command_on_a_healthy_link_reports_no_recovery(self) -> None:
        """Which is what stops the log line firing on every tick.

        The assertion is on the answer rather than on the log, because the
        answer is the contract: `record_success` returns whether *this* call was
        the one that brought the link back.
        """
        link = DaemonLink()

        assert link.record_success() is False

    def test_the_report_carries_nothing_identifying(self) -> None:
        """Bounded diagnostics: a state, two counts, and no address or name."""
        link = DaemonLink()
        link.record_failure()

        report = link.status()

        assert set(report) == {"state", "outages", "refused_calls"}
        assert report["state"] in {state.value for state in DaemonLinkState}
        assert all(isinstance(report[key], int) for key in ("outages", "refused_calls"))

    def test_both_counts_saturate_rather_than_growing_with_uptime(self) -> None:
        """A robot commanding at twenty hertz must not grow the `/status` payload.

        Driven past the limit rather than to it, because what is pinned is that
        the number *stops* — a counter tested only at its cap passes just as
        well while still rising underneath.
        """
        link = DaemonLink()

        for _refusal in range(COUNTER_LIMIT + 5):
            link.record_failure()
            link.record_success()

        assert link.status() == {
            "state": "up",
            "outages": COUNTER_LIMIT,
            "refused_calls": COUNTER_LIMIT,
        }


class TestOnlyALinkFaultIsSwallowed:
    """Widening the catch to everything is the mistake this replaces."""

    def test_a_connection_error_is_reported_rather_than_raised(self) -> None:
        """What the SDK raises from every command while its liveness poll is false."""
        link = DaemonLink()

        def _refuse() -> None:
            raise ConnectionError(LOST_LINK_MESSAGE)

        assert attempt_daemon_call(link, _refuse) is False
        assert link.down

    def test_a_timeout_is_the_same_answer(self) -> None:
        """`wait_for_task_completion` says the daemon did not answer this way."""
        link = DaemonLink()

        def _hang() -> None:
            raise TimeoutError("Task did not complete in time.")

        assert attempt_daemon_call(link, _hang) is False
        assert link.down

    def test_a_bad_argument_still_propagates(self) -> None:
        """A `ValueError` says something about what was asked and must survive."""
        link = DaemonLink()

        def _reject() -> None:
            raise ValueError("head pose must be a 4x4 matrix")

        with pytest.raises(ValueError, match="4x4"):
            attempt_daemon_call(link, _reject)
        assert not link.down

    def test_the_reporting_helper_records_and_then_re_raises(self) -> None:
        """For a caller whose own failure path has to run — a lifecycle phase.

        The record is the point: without it the coordinator turns the refusal
        into `MotorConfirmation.failed()` and `/status` goes on saying the
        daemon is answering while it refuses everything.
        """
        link = DaemonLink()

        def _refuse() -> None:
            raise ConnectionError(LOST_LINK_MESSAGE)

        with pytest.raises(ConnectionError):
            report_daemon_call(link, _refuse)
        assert link.down

    def test_the_reporting_helper_returns_what_the_call_returned(self) -> None:
        """It stands in for the call rather than wrapping its result."""
        link = DaemonLink()

        assert report_daemon_call(link, lambda: 7) == 7
        assert not link.down


class TestTheUngatedPathSurvivesALostLink:
    """The stock robot's path: no coordinator, commands straight to the daemon."""

    def test_the_antenna_command_in_the_traceback_no_longer_raises(self) -> None:
        """`move_antennas` is the exact call that killed the process."""
        robot = FakeRobot(link_down=True)
        link = DaemonLink()
        motion = ReachyMotion(robot, link=link)

        motion.move_antennas(AntennaPose(right=0.1, left=0.2))

        assert link.down
        assert robot.refused_commands == 1
        assert robot.targets == []

    def test_the_head_command_does_not_raise_either(self) -> None:
        """`move_head` caught nothing at all before this."""
        robot = FakeRobot(link_down=True)
        link = DaemonLink()
        motion = ReachyMotion(robot, link=link)

        motion.move_head(HeadPose(yaw=0.1))

        assert link.down
        assert robot.targets == []

    def test_a_gaze_command_is_rejected_as_the_link_and_not_as_the_command(
        self,
    ) -> None:
        """The distinction the whole change is about, on the surface that carries it."""
        robot = FakeRobot(link_down=True)
        link = DaemonLink()
        motion = _acquired(robot, link)

        result = motion.command_gaze(_sample())

        # The second assertion is the released behaviour this replaces, and it
        # is redundant to a type checker precisely because the distinction is
        # now in the type. It is spelled out anyway: `COMMAND` is what every
        # refused command reported before, and the whole point of the change is
        # that a robot nothing can reach no longer says the same word as a
        # sample the gate turned down.
        assert result.status is MotionCommandStatus.REJECTED
        assert result.fault is MotionFault.LINK
        assert result.fault.value != MotionFault.COMMAND.value

    def test_the_link_returning_resumes_motion_with_nobody_asked(self) -> None:
        """No reconnect and no restart: the next command that lands is the recovery."""
        robot = FakeRobot(link_down=True)
        link = DaemonLink()
        motion = _acquired(robot, link)
        assert motion.command_gaze(_sample()).fault is MotionFault.LINK

        robot.link_down = False
        result = motion.command_gaze(_sample())

        assert result.status is MotionCommandStatus.ACCEPTED
        assert result.fault is MotionFault.NONE
        assert not link.down
        assert len(robot.targets) == 1

    def test_the_antenna_path_recovers_too(self) -> None:
        """The pipeline animation resumes on its own, which is what a person sees."""
        robot = FakeRobot(link_down=True)
        link = DaemonLink()
        motion = ReachyMotion(robot, link=link)
        motion.move_antennas(AntennaPose(right=0.1, left=0.2))

        robot.link_down = False
        motion.move_antennas(AntennaPose(right=0.3, left=0.4))

        assert not link.down
        assert robot.antennas == [[0.3, 0.4]]


class TestTheMeasurementPathsAreDefendedToo:
    """The reads cannot refuse on the released SDK, and are covered regardless.

    `get_current_head_pose`, `get_current_joint_positions` and the non-moving
    image query all answer out of the cache the SDK's receive loop fills, so on
    `reachy-mini` 1.9 none of them raises when the socket dies. The branches
    exist because the cost of being wrong about that is the process, and they
    are driven here rather than left as unexercised prose — a defence nothing
    runs is a defence nobody knows is broken.
    """

    def test_a_refused_pose_read_is_the_link_and_not_the_pose(self) -> None:
        """`POSE` says the daemon answered with something unusable."""
        robot = FakeRobot(measured_head_poses=(ConnectionError(LOST_LINK_MESSAGE),))
        link = DaemonLink()
        motion = ReachyMotion(robot, link=link)

        measurement = motion.observe(1.0)

        assert measurement.head_fault is MotionFault.LINK
        assert link.down

    def test_a_refused_joint_read_is_the_link_on_the_body_channel(self) -> None:
        """The body's own measurement channel makes the same distinction."""
        robot = FakeRobot(measured_joints=(ConnectionError(LOST_LINK_MESSAGE),))
        link = DaemonLink()
        motion = ReachyMotion(robot, link=link, body_enabled=True)

        measurement = motion.observe(1.0)

        assert measurement.body_fault is MotionFault.LINK
        assert link.down

    def test_a_refused_calibration_query_is_not_cached_against_the_face(self) -> None:
        """A transient outage must not pin a rejection to a face for good.

        Every other rejection in `calibrate` is final for that identity, which
        is right when the daemon answered and the answer was unusable. A link
        that was down says nothing about the face, so the same identity is
        calibrated again once the daemon is back.
        """
        target = head_pose_matrix(HeadPose(yaw=0.3))
        pose = head_pose_matrix(HeadPose(yaw=0.1))
        robot = FakeRobot(
            measured_head_poses=(pose, pose, pose, pose, pose),
            image_gaze_poses=(ConnectionError(LOST_LINK_MESSAGE), target),
        )
        link = DaemonLink()
        motion = ReachyMotion(robot, link=link)
        motion.acquire(0.0)
        motion.observe(1.0)

        refused = motion.calibrate(_directive(), 1.0)
        assert refused.state is CalibrationStatus.REJECTED
        assert refused.fault is MotionFault.LINK
        assert link.down

        motion.observe(2.0)
        retried = motion.calibrate(_directive(), 2.0)

        assert link.state is DaemonLinkState.UP
        assert retried.state is CalibrationStatus.ACCEPTED


class TestAcquisitionAndReleaseSurviveIt:
    """The two daemon-ownership writes, at each end of the application's life."""

    def test_acquiring_over_a_dead_link_does_not_raise(self) -> None:
        """Startup calls this, uncaught, before the settings interface exists."""
        robot = FakeRobot(link_down=True)
        link = DaemonLink()
        motion = ReachyMotion(robot, link=link)

        motion.acquire(0.0)

        assert link.down
        assert robot.automatic_body_yaw == []

    def test_the_ownership_write_is_made_again_once_the_link_returns(self) -> None:
        """Otherwise the daemon goes on moving the body under a head this owns.

        On the next `observe`, which is what a tick calls: nothing else in the
        adapter would notice, because every other daemon call it makes is
        either a cached read or a command the behaviour layer only issues when
        it wants the robot to move.
        """
        robot = FakeRobot(link_down=True)
        link = DaemonLink()
        motion = _acquired(robot, link)
        assert robot.automatic_body_yaw == []

        robot.link_down = False
        motion.observe(1.0)

        assert robot.automatic_body_yaw == [False]
        assert not link.down

    def test_an_idle_robot_still_notices_the_link_come_back(self) -> None:
        """The probe and the repair are one call, so neither needs a face in view.

        Without it a robot alone in a room commands nothing, discovers nothing,
        and goes on reporting an outage that ended hours ago.
        """
        robot = FakeRobot(link_down=True)
        link = DaemonLink()
        motion = _acquired(robot, link)
        motion.observe(1.0)
        assert link.down

        robot.link_down = False
        motion.observe(2.0)

        assert link.state is DaemonLinkState.UP

    def test_the_write_is_made_once_and_not_at_every_tick(self) -> None:
        """It is a repair and a probe, not a heartbeat on a healthy robot."""
        robot = FakeRobot(link_down=True)
        link = DaemonLink()
        motion = _acquired(robot, link)
        robot.link_down = False

        for tick in range(5):
            motion.observe(float(tick) + 1.0)

        assert robot.automatic_body_yaw == [False]

    def test_a_healthy_robot_is_never_probed_at_all(self) -> None:
        """A daemon that has refused nothing is asked nothing extra."""
        robot = FakeRobot()
        link = DaemonLink()
        motion = _acquired(robot, link)
        assert robot.automatic_body_yaw == [False]

        for tick in range(5):
            motion.observe(float(tick) + 1.0)

        assert robot.automatic_body_yaw == [False]

    def test_a_refused_ownership_write_that_is_not_the_link_still_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A daemon that answered and refused is one this adapter does not own."""
        robot = FakeRobot()
        link = DaemonLink()
        motion = ReachyMotion(robot, link=link)

        def _refuse(enabled: bool) -> None:
            del enabled
            raise RuntimeError("the daemon refused ownership")

        monkeypatch.setattr(robot, "set_automatic_body_yaw", _refuse, raising=False)

        with pytest.raises(RuntimeError, match="refused ownership"):
            motion.acquire(0.0)
        assert not link.down

    def test_a_released_adapter_probes_nothing(self) -> None:
        """The probe sits inside `observe`, and release short-circuits it.

        REQ-050 says a released port stops commanding movement, and a liveness
        write is a command: a tick still in flight when the daemon asks for
        shutdown must not reach the robot with one.
        """
        robot = FakeRobot(link_down=True)
        link = DaemonLink()
        motion = _acquired(robot, link)
        motion.release()
        robot.link_down = False
        before = len(robot.automatic_body_yaw)

        measurement = motion.observe(1.0)

        assert measurement.head_fault is MotionFault.RELEASED
        assert len(robot.automatic_body_yaw) == before

    def test_releasing_over_a_dead_link_does_not_raise(self) -> None:
        """Shutdown hands the policy back, and cannot when nothing is listening."""
        robot = FakeRobot()
        link = DaemonLink()
        motion = _acquired(robot, link)
        robot.link_down = True

        motion.release()

        assert motion.released
        assert link.down


@pytest.mark.filesystem
class TestTheGatedPathSurvivesALostLink:
    """The forked daemon's path, where the same `set_target` sits behind a gate.

    `@pytest.mark.filesystem` for the reason the other assembly tests carry it:
    `build_application` loads the wake-word models and sounds the wheel ships,
    and a fake asset directory would pin whatever the fake was told to contain.
    Composed rather than hand-built because what is under test is that the gate
    the composition opened is still open afterwards.
    """

    @staticmethod
    async def _gated(robot: FakeRobot) -> SatelliteApplication:
        """Assemble production wiring over a confirming fake daemon.

        Args:
            robot: The daemon handle to compose against.

        Returns:
            The assembled application, with all three gates open.
        """
        application = await build_application(
            load_settings(_ENVIRONMENT),
            robot,
            identity=_identity(),
        )
        assert application.motion_gating.mode is MotionGatingMode.CONFIRMED
        return application

    @pytest.mark.asyncio
    async def test_a_gaze_command_reports_the_link_rather_than_the_gate(self) -> None:
        """A confirmed robot reaches the same raising call through the coordinator."""
        robot = FakeRobot()
        application = await self._gated(robot)
        motion = cast("ReachyMotion", application._motion)
        motion.acquire(0.0)
        robot.link_down = True

        result = motion.command_gaze(_sample())

        assert result.fault is MotionFault.LINK

    @pytest.mark.asyncio
    async def test_the_reservation_is_released_and_the_gate_stays_open(self) -> None:
        """A swallowed fault must not leave the coordinator holding a producer.

        The gate is not closed by an outage either: torque did not change, the
        daemon did not restart, and shutting the gate would need a confirmed
        transition to reopen — one the same dead link could not carry out.

        `aclose` is the assertion that the reservation was released. It drains
        every producer before it returns, so a coordinator still counting one
        that never finished would wait here for ever; the timeout turns that
        into a failure rather than a hung suite.
        """
        robot = FakeRobot()
        application = await self._gated(robot)
        groups = application.motor_groups
        assert groups is not None
        motion = cast("ReachyMotion", application._motion)
        motion.acquire(0.0)
        robot.link_down = True

        motion.command_gaze(_sample())
        motion.move_antennas(AntennaPose(right=0.1, left=0.2))

        assert all(groups.gate_open(group) for group in MotorGroup)
        transitions = cast("dict[str, dict[str, object]]", groups.status()["groups"])
        assert all(state["transition"] == "idle" for state in transitions.values())
        await asyncio.wait_for(groups.aclose(), timeout=_DRAIN_TIMEOUT_SECONDS)

    @pytest.mark.asyncio
    async def test_it_recovers_through_the_gate_it_never_closed(self) -> None:
        """Which is what makes the gated robot's recovery the same as the stock one's."""
        robot = FakeRobot()
        application = await self._gated(robot)
        motion = cast("ReachyMotion", application._motion)
        motion.acquire(0.0)
        robot.link_down = True
        assert motion.command_gaze(_sample()).fault is MotionFault.LINK

        robot.link_down = False
        result = motion.command_gaze(_sample())

        assert result.status is MotionCommandStatus.ACCEPTED

    @pytest.mark.asyncio
    async def test_a_refused_torque_read_is_a_failed_group_and_a_down_link(
        self,
    ) -> None:
        """The gated path's own daemon calls, which the motion adapter never sees.

        `MotorGroupCoordinator._set`/`_read` turn every exception into
        `MotorConfirmation.failed()`, and that must keep happening — a gate
        opened over torque nobody confirmed is the safety contract gone. What
        would otherwise be lost is *why*: the group closes, the application
        lives, and nothing anywhere says the daemon refused to answer.
        """
        robot = FakeRobot(link_down=True)
        link = DaemonLink()
        coordinator = MotorGroupCoordinator(robot, clock=ManualClock(), link=link)
        try:
            registered = await coordinator.initialize()

            assert registered == ()
            assert not any(coordinator.gate_open(group) for group in MotorGroup)
            assert link.down
        finally:
            await asyncio.wait_for(
                coordinator.aclose(),
                timeout=_DRAIN_TIMEOUT_SECONDS,
            )

    @pytest.mark.asyncio
    async def test_a_confirming_daemon_reports_the_link_as_up(self) -> None:
        """The same calls are what mark it back up on the gated path."""
        robot = FakeRobot()
        link = DaemonLink()
        link.record_failure()
        coordinator = MotorGroupCoordinator(robot, clock=ManualClock(), link=link)
        try:
            await coordinator.initialize()

            assert link.state is DaemonLinkState.UP
        finally:
            await asyncio.wait_for(
                coordinator.aclose(),
                timeout=_DRAIN_TIMEOUT_SECONDS,
            )

    @pytest.mark.asyncio
    async def test_a_closed_gate_is_still_reported_as_the_command(self) -> None:
        """The other half of the distinction: a live gate has not become a link."""
        robot = FakeRobot()
        application = await self._gated(robot)
        groups = application.motor_groups
        assert groups is not None
        motion = cast("ReachyMotion", application._motion)
        motion.acquire(0.0)
        groups.terminal()

        result = motion.command_gaze(_sample())

        assert result.fault is MotionFault.COMMAND


class TestTheWakeSequenceIsNotADeathSentence:
    """Every restart on the robot died here, because waking moves the antennas."""

    @pytest.mark.asyncio
    async def test_a_link_down_at_startup_still_assembles_and_runs(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The whole point: a running, diagnosable robot rather than an exit.

        Startup, the acquisition and one whole behaviour tick, every one of
        them over a daemon refusing every command, and at the end of it an
        application that ran and a `/status` that says why nothing moved.
        """
        robot = FakeRobot(link_down=True)
        assembled: list[SatelliteApplication] = []
        stop = asyncio.Event()

        async def _one_tick_then_stop(_seconds: float) -> None:
            """Stand in for the inter-tick wait and end the loop after one."""
            stop.set()

        async def _build(
            resolution: object,
            handle: object,
            **kwargs: object,
        ) -> SatelliteApplication:
            del resolution, handle
            link = kwargs["link"]
            assert isinstance(link, DaemonLink)
            application = SatelliteApplication(
                settings=load_settings(_ENVIRONMENT).settings,
                audio=FakeAudio(),
                motion=ReachyMotion(robot, link=link),
                perception=FakePerception(),
                behaviour=SatelliteBehaviour(now=0.0),
                daemon_link=link,
                clock=ManualClock(),
                sleep=_one_tick_then_stop,
            )
            assembled.append(application)
            return application

        _patch_startup(monkeypatch, build=_build)

        await run(robot, stop)

        assert len(assembled) == 1
        link_report = cast("dict[str, object]", assembled[0].status()["daemon_link"])
        assert link_report["state"] == "down"
        # One outage, however many calls it refused: the wake, the ownership
        # write and whatever the tick asked for are one thing being wrong.
        assert link_report["outages"] == 1
        assert robot.motor_enables == 0
        assert robot.wake_ups == 0
        assert robot.targets == []

    def test_the_running_application_recovers_without_being_restarted(self) -> None:
        """A tick against a dead daemon is survivable, and the next one commands.

        Driven a tick at a time rather than through `run`, because what is
        under test is that the loop body neither raises nor latches: the same
        application object that reported the outage is the one that moves the
        robot afterwards.
        """
        robot = FakeRobot(
            torque_confirmation_support=TorqueConfirmationSupport.ABSENT,
            link_down=True,
        )
        link = DaemonLink()
        motion = ReachyMotion(robot, link=link)
        motion.acquire(0.0)
        application = SatelliteApplication(
            settings=load_settings(_ENVIRONMENT).settings,
            audio=FakeAudio(),
            motion=motion,
            perception=FakePerception(),
            behaviour=SatelliteBehaviour(now=0.0),
            daemon_link=link,
            clock=ManualClock(),
        )
        for _tick in range(5):
            application.tick()
        assert (
            cast("dict[str, object]", application.status()["daemon_link"])["state"]
            == "down"
        )

        robot.link_down = False
        for _tick in range(5):
            application.tick()

        assert (
            cast("dict[str, object]", application.status()["daemon_link"])["state"]
            == "up"
        )

    @pytest.mark.asyncio
    async def test_neither_wake_call_reaches_the_robot_and_neither_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Both are refused, both are stepped over, and startup carries on."""
        robot = FakeRobot(link_down=True)

        async def _build(
            resolution: object,
            handle: object,
            **kwargs: object,
        ) -> SatelliteApplication:
            del resolution, handle, kwargs
            raise AssertionError("composition ran after a stop was requested")

        _patch_startup(monkeypatch, build=_build)
        stop = asyncio.Event()

        def _enable() -> None:
            robot.motor_enables += 1
            stop.set()
            raise ConnectionError(LOST_LINK_MESSAGE)

        monkeypatch.setattr(robot, "enable_motors", _enable, raising=False)

        await run(robot, stop)

        assert robot.motor_enables == 1
        assert robot.wake_ups == 0


class TestTheSurfacesReportIt:
    """An operator who opens the page is told, rather than left to infer it."""

    @staticmethod
    def _application(link: DaemonLink) -> SatelliteApplication:
        """Build a bare application reporting one link.

        Args:
            link: The record to report.

        Returns:
            The application.
        """
        robot = FakeRobot(
            torque_confirmation_support=TorqueConfirmationSupport.ABSENT,
        )
        return SatelliteApplication(
            settings=load_settings(_ENVIRONMENT).settings,
            audio=FakeAudio(),
            motion=ReachyMotion(robot, link=link),
            perception=FakePerception(),
            behaviour=SatelliteBehaviour(now=0.0),
            daemon_link=link,
            clock=ManualClock(),
        )

    def test_status_carries_the_link_beside_the_gating_mode(self) -> None:
        """Both are process-level facts about why the robot is not moving."""
        link = DaemonLink()
        link.record_failure()

        status = self._application(link).status()

        assert status["daemon_link"] == {
            "state": "down",
            "outages": 1,
            "refused_calls": 1,
        }
        assert "motion_gating" in status

    def test_a_healthy_link_is_reported_too(self) -> None:
        """A surface that only appeared on failure would be one nobody trusts."""
        status = self._application(DaemonLink()).status()

        assert status["daemon_link"] == {
            "state": "up",
            "outages": 0,
            "refused_calls": 0,
        }

    def test_the_settings_page_leads_with_the_outage(self) -> None:
        """Sorted into the summary line is not being told."""
        link = DaemonLink()
        link.record_failure()
        resolution = load_settings(_ENVIRONMENT)

        page = render_settings_page(
            resolution,
            configuration_report(resolution),
            status=self._application(link).status(),
            overrides_path="/reachy-satellite-link/settings.json",
        )

        assert "The link to the robot daemon is <strong>down</strong>" in page
        assert "<strong>The application is still running</strong>" in page
        assert "restart the daemon" in page
        # The qualification is the part an operator acts on: without it the page
        # promises a state that refreshes by itself, and on a robot with face
        # tracking off it does not until something moves the robot.
        assert "with face tracking on it re-checks every tick" in page
        assert "can stay showing an outage that has already ended" in page

    def test_the_page_says_nothing_while_the_daemon_answers(self) -> None:
        """A standing warning is a warning an operator stops reading."""
        resolution = load_settings(_ENVIRONMENT)

        page = render_settings_page(
            resolution,
            configuration_report(resolution),
            status=self._application(DaemonLink()).status(),
            overrides_path="/reachy-satellite-link/settings.json",
        )

        assert "The link to the robot daemon is" not in page

    def test_a_page_with_no_application_behind_it_says_nothing_either(self) -> None:
        """There is no link to report when nothing is commanding a daemon."""
        resolution = load_settings(_ENVIRONMENT)

        page = render_settings_page(
            resolution,
            configuration_report(resolution),
            status={"running": False},
            overrides_path="/reachy-satellite-link/settings.json",
        )

        assert "The link to the robot daemon is" not in page
