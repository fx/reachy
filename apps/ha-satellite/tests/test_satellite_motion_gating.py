"""Named deterministic acceptance matrix for stock-robot REQ-099 and REQ-100.

Three daemons and one process-lifetime decision. The first offers no correlated
grouped-torque confirmation, which every released `reachy-mini` is, and gets the
ungated command path with no motor switch. The second offers it and is gated
exactly as change 0020 left it. The third offers it and answers badly, and is
the reason the degradation cannot be a per-call fallback: it stays gated.

The measured symptom this fixes is here as an assertion rather than as prose. On
a stock robot the confirmed path reported `controller.fault == "command"` with
`safe_hold` engaged and every gate shut, while the robot tracked a face and
never moved; `TestSafeHoldFollowsTheGate` drives both halves of that through the
real motion adapter over a fake daemon.

The wrapper's own probe — the one that must answer for the object it wraps
rather than for itself — is tested where the wrapper is, in
`test_satellite_daemon_app.py`, because that is the module with the stubbed SDK.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast

import pytest
from satellite_support import (
    FakeAudio,
    FakePerception,
    FakeRobot,
    ManualClock,
    face,
)

from reachy_mini_ha_satellite.adapters.motion_reachy import ReachyMotion
from reachy_mini_ha_satellite.behaviour import SatelliteBehaviour
from reachy_mini_ha_satellite.config import ENV_PREFIX, load_settings
from reachy_mini_ha_satellite.main import (
    EsphomeService,
    SatelliteApplication,
    build_application,
)
from reachy_mini_ha_satellite.motor_control import (
    HEAD_MOTOR_IDS,
    MotionGating,
    MotionGatingMode,
    MotionGatingReason,
    MotorConfirmation,
    MotorConfirmationOutcome,
    MotorEvidence,
    MotorGroup,
    MotorGroupCoordinator,
    TorqueConfirmationSupport,
)
from reachy_mini_ha_satellite.motor_entities import MotorSwitchEntity
from reachy_mini_ha_satellite.ports import (
    DetectionSource,
    GazeSample,
    MotionCommandStatus,
    MotionFault,
)

if TYPE_CHECKING:
    from reachy_mini_ha_satellite.adapters.network import NetworkIdentity

# The RFC 5737 documentation range. This repository is public.
_GROUNDSTATION: Final = "ws://192.0.2.10:8080/v1/session"

_ENVIRONMENT: Final[dict[str, str]] = {
    f"{ENV_PREFIX}DEVICE_NAME": "reachy-mini-1",
    f"{ENV_PREFIX}GROUNDSTATION_URL": _GROUNDSTATION,
    f"{ENV_PREFIX}GROUNDSTATION_CREDENTIAL": "example-credential",
    f"{ENV_PREFIX}STATE_DIR": "/reachy-satellite-gating",
    f"{ENV_PREFIX}ADVERTISE": "false",
    f"{ENV_PREFIX}WEB_ENABLED": "false",
}


def _identity() -> NetworkIdentity:
    """Build the announced identity from documentation ranges.

    Supplied rather than discovered: discovery reads the machine's own
    interfaces, and no test here is about this machine.

    Returns:
        The identity.
    """
    from reachy_mini_ha_satellite.adapters.network import NetworkIdentity

    return NetworkIdentity(
        interface="eth0",
        ip_address="192.0.2.20",
        mac_address="02:00:5e:10:00:00",
    )


def _sample() -> GazeSample:
    """Build one in-envelope head-only command with no derivatives.

    Returns:
        A sample the adapter's own validation accepts, so that what a test sees
        is the gate's answer and not the sample's.
    """
    return GazeSample(
        world_yaw=0.1,
        elevation=0.05,
        body_yaw=0.0,
        head_yaw=0.1,
        body_enabled=False,
    )


def _incomplete() -> MotorConfirmation:
    """Build a confirmation from a daemon that answered for one motor only.

    Returns:
        A partial result: acknowledged, terminal, and short of the evidence a
        group needs, which is what an unconfirmed group looks like.
    """
    return MotorConfirmation(
        True,
        MotorConfirmationOutcome.PARTIAL,
        (MotorEvidence(name=HEAD_MOTOR_IDS[0], enabled=True),),
    )


def _switches(application: SatelliteApplication) -> list[MotorSwitchEntity]:
    """List the motor switches this assembly announced.

    Args:
        application: The assembled application.

    Returns:
        Every registered motor switch, which is none of them on a robot whose
        groups were never confirmed.
    """
    esphome = next(
        service
        for service in application.services
        if isinstance(service, EsphomeService)
    )
    return [
        entity
        for entity in esphome._state.entities
        if isinstance(entity, MotorSwitchEntity)
    ]


class TestTheModeIsDecidedFromWhatTheDaemonOffers:
    """One probe, one decision, and a reason bounded enough to publish."""

    def test_an_absent_surface_is_ungated(self) -> None:
        """There is no torque state to protect, so there is no gate to hold."""
        gating = MotionGating.decide(TorqueConfirmationSupport.ABSENT)

        assert gating.mode is MotionGatingMode.UNGATED
        assert gating.reason is MotionGatingReason.CONFIRMATION_ABSENT
        assert not gating.gated

    def test_the_whole_surface_is_confirmed(self) -> None:
        """The path change 0020 shipped, chosen for the daemon that supports it."""
        gating = MotionGating.decide(TorqueConfirmationSupport.AVAILABLE)

        assert gating.mode is MotionGatingMode.CONFIRMED
        assert gating.reason is MotionGatingReason.CONFIRMATION_AVAILABLE
        assert gating.gated

    def test_part_of_the_surface_is_confirmed_and_says_so(self) -> None:
        """Conservative, and reported precisely enough to diagnose.

        A half-implemented daemon may already correlate torque, so it keeps the
        gate. The reason is its own value rather than the absent one, because an
        operator looking at a robot that is not moving needs to be able to tell
        the two apart.
        """
        gating = MotionGating.decide(TorqueConfirmationSupport.PARTIAL)

        assert gating.mode is MotionGatingMode.CONFIRMED
        assert gating.reason is MotionGatingReason.CONFIRMATION_PARTIAL
        assert gating.gated

    def test_the_report_is_two_bounded_strings(self) -> None:
        """REQ-100 wants a mode and a reason, and nothing that identifies anybody."""
        report = MotionGating.decide(TorqueConfirmationSupport.ABSENT).status()

        assert report == {
            "mode": "ungated",
            "reason": "daemon_confirmation_absent",
        }


class TestTheApplicationCannotMisreportItsOwnMode:
    """The report and the command path are one decision, so they cannot differ."""

    @staticmethod
    def _application(
        *,
        coordinator: MotorGroupCoordinator | None,
        gating: MotionGating | None,
    ) -> SatelliteApplication:
        """Build a bare application with the two under test.

        Args:
            coordinator: The coordinator to hand over, or `None`.
            gating: The mode to report, or `None` to have it derived.

        Returns:
            The application.
        """
        return SatelliteApplication(
            settings=load_settings(_ENVIRONMENT).settings,
            audio=FakeAudio(),
            motion=ReachyMotion(FakeRobot(), coordinator=coordinator),
            perception=FakePerception(),
            behaviour=SatelliteBehaviour(now=0.0),
            motor_groups=coordinator,
            motion_gating=gating,
        )

    def test_no_coordinator_reports_the_ungated_mode(self) -> None:
        """The derived answer for an application nobody probed a daemon for."""
        application = self._application(coordinator=None, gating=None)

        assert application.motion_gating.mode is MotionGatingMode.UNGATED

    @pytest.mark.asyncio
    async def test_a_coordinator_reports_the_confirmed_mode(self) -> None:
        """The same derivation, the other way round."""
        coordinator = MotorGroupCoordinator(FakeRobot(), clock=ManualClock())
        try:
            application = self._application(coordinator=coordinator, gating=None)

            assert application.motion_gating.mode is MotionGatingMode.CONFIRMED
        finally:
            await coordinator.aclose()

    def test_a_confirmed_report_over_no_coordinator_is_refused(self) -> None:
        """A report of a gate this process does not have is refused.

        Saying "gated" over a process with no gate is worse than saying nothing.
        """
        with pytest.raises(ValueError, match="must match the coordinator"):
            self._application(
                coordinator=None,
                gating=MotionGating.decide(TorqueConfirmationSupport.AVAILABLE),
            )

    @pytest.mark.asyncio
    async def test_an_ungated_report_over_a_coordinator_is_refused(self) -> None:
        """And the mistake in the other direction, which would hide a live gate."""
        coordinator = MotorGroupCoordinator(FakeRobot(), clock=ManualClock())
        try:
            with pytest.raises(ValueError, match="must match the coordinator"):
                self._application(
                    coordinator=coordinator,
                    gating=MotionGating.decide(TorqueConfirmationSupport.ABSENT),
                )
        finally:
            await coordinator.aclose()


@pytest.mark.filesystem
class TestAssemblyOverEachDaemon:
    """Composition against the wheel's own assets, once per kind of daemon.

    `@pytest.mark.filesystem` for the reason the other assembly tests carry it:
    `build_application` loads the wake-word models and sounds the wheel ships,
    and a fake asset directory would pin whatever the fake was told to contain.
    """

    @staticmethod
    async def _assembled(robot: FakeRobot) -> SatelliteApplication:
        """Assemble production wiring over one fake daemon.

        Args:
            robot: The daemon handle to compose against.

        Returns:
            The assembled application.
        """
        return await build_application(
            load_settings(_ENVIRONMENT),
            robot,
            identity=_identity(),
        )

    #:= docs/specs/stock-robot-installation/index.md#req-099-motion-survives-a-daemon-without-torque-confirmation
    #:% The satellite MUST command motion on a robot whose daemon offers no correlated
    #:% grouped-torque confirmation capability, treating that absence as nothing to gate
    #:% rather than as a motor group whose torque state could not be confirmed.
    @pytest.mark.asyncio
    async def test_a_stock_daemon_builds_no_coordinator_and_confirms_nothing(
        self,
    ) -> None:
        """The absent capability is decided once, and nothing is asked of it."""
        robot = FakeRobot(
            torque_confirmation_support=TorqueConfirmationSupport.ABSENT,
        )

        application = await self._assembled(robot)

        assert application.motor_groups is None
        assert application.motion_gating.mode is MotionGatingMode.UNGATED
        assert robot.torque_probes == 1
        assert robot.motor_requests == []
        assert "motors.read" not in robot.events

    @pytest.mark.asyncio
    async def test_a_stock_daemon_announces_no_motor_switch(self) -> None:
        """Which is the unconfirmed-group contract's outcome, not this one's gap."""
        robot = FakeRobot(
            torque_confirmation_support=TorqueConfirmationSupport.ABSENT,
        )

        application = await self._assembled(robot)

        assert _switches(application) == []
        assert "motors" not in application.status()

    @pytest.mark.asyncio
    async def test_a_stock_daemon_commands_the_head_with_no_gate(self) -> None:
        """The head follows, which on real hardware it did not.

        The composed adapter rather than a new one: what is under test is that
        the assembly handed it no coordinator, so its existing ungated branch is
        the one a command takes.
        """
        robot = FakeRobot(
            torque_confirmation_support=TorqueConfirmationSupport.ABSENT,
        )
        application = await self._assembled(robot)
        motion = cast("ReachyMotion", application._motion)
        motion.acquire(0.0)
        before = len(robot.targets)

        result = motion.command_gaze(_sample())

        assert result.status is MotionCommandStatus.ACCEPTED
        assert result.fault is MotionFault.NONE
        assert len(robot.targets) == before + 1

    @pytest.mark.asyncio
    async def test_a_confirming_daemon_is_gated_exactly_as_before(self) -> None:
        """Every group confirmed, every gate open, all three switches announced."""
        robot = FakeRobot()

        application = await self._assembled(robot)

        groups = application.motor_groups
        assert groups is not None
        assert application.motion_gating.reason is (
            MotionGatingReason.CONFIRMATION_AVAILABLE
        )
        assert all(groups.gate_open(group) for group in MotorGroup)
        assert len(_switches(application)) == len(MotorGroup)
        assert "motors" in application.status()

    #:= docs/specs/stock-robot-installation/index.md#req-099-motion-survives-a-daemon-without-torque-confirmation
    #:% The satellite MUST command motion on a robot whose daemon offers no correlated
    #:% grouped-torque confirmation capability, treating that absence as nothing to gate
    #:% rather than as a motor group whose torque state could not be confirmed.
    @pytest.mark.asyncio
    async def test_a_capability_that_fails_stays_gated(self) -> None:
        """The safety boundary: a refused confirmation is not licence to ungate.

        This daemon offers the surface and every read raises, which is how a
        broken confirming daemon looks. Treating that as an absent capability
        would make breaking the confirmation a way to switch the safety contract
        off, so it stays an unconfirmed group with its gate shut.
        """
        robot = FakeRobot(
            motor_reads=[RuntimeError("no"), RuntimeError("no"), RuntimeError("no")],
        )

        application = await self._assembled(robot)

        groups = application.motor_groups
        assert groups is not None
        assert application.motion_gating.mode is MotionGatingMode.CONFIRMED
        assert not any(groups.gate_open(group) for group in MotorGroup)
        assert _switches(application) == []

    @pytest.mark.asyncio
    async def test_a_capability_that_answers_partially_stays_gated(self) -> None:
        """A confirmation short of its group's evidence is unconfirmed, not absent."""
        robot = FakeRobot(motor_reads=[_incomplete(), _incomplete(), _incomplete()])

        application = await self._assembled(robot)

        groups = application.motor_groups
        assert groups is not None
        assert not any(groups.gate_open(group) for group in MotorGroup)
        assert all(groups.last_confirmed(group) is None for group in MotorGroup)
        assert _switches(application) == []

    @pytest.mark.asyncio
    async def test_a_daemon_offering_part_of_the_surface_stays_gated(self) -> None:
        """The other partial: some methods rather than some evidence."""
        robot = FakeRobot(
            torque_confirmation_support=TorqueConfirmationSupport.PARTIAL,
        )

        application = await self._assembled(robot)

        assert application.motor_groups is not None
        assert application.motion_gating.mode is MotionGatingMode.CONFIRMED
        assert application.motion_gating.reason is (
            MotionGatingReason.CONFIRMATION_PARTIAL
        )

    #:= docs/specs/stock-robot-installation/index.md#req-100-the-motion-gating-mode-in-force-is-reported
    #:% The satellite MUST report which motion-gating mode is in force and why, so that
    #:% an operator can tell an ungated stock robot from a confirmed one without
    #:% inferring it from whether the robot moved.
    @pytest.mark.asyncio
    async def test_the_two_robots_are_distinguishable_from_the_report_alone(
        self,
    ) -> None:
        """One read of the health surface separates a stock robot from a fork.

        The stock robot is the one with no `motors` key at all, which is exactly
        why the mode does not live under it.
        """
        stock = await self._assembled(
            FakeRobot(torque_confirmation_support=TorqueConfirmationSupport.ABSENT),
        )
        confirming = await self._assembled(FakeRobot())

        assert stock.status()["motion_gating"] == {
            "mode": "ungated",
            "reason": "daemon_confirmation_absent",
        }
        assert confirming.status()["motion_gating"] == {
            "mode": "confirmed",
            "reason": "daemon_confirmation_available",
        }

    @pytest.mark.asyncio
    async def test_the_report_names_no_credential_and_no_installation(self) -> None:
        """REQ-100 is a bounded report, beside bounded motor diagnostics."""
        application = await self._assembled(
            FakeRobot(torque_confirmation_support=TorqueConfirmationSupport.ABSENT),
        )

        rendered = repr(application.status()["motion_gating"])

        assert "example-credential" not in rendered
        assert "192.0.2.10" not in rendered
        assert "reachy-mini-1" not in rendered


class TestSafeHoldFollowsTheGate:
    """The measured stock-robot symptom, and its absence once the gate is gone.

    Driven through the real motion adapter over a fake daemon, because the fault
    the controller derives safe hold from is the one a refused command produces
    and nothing else here would produce it.
    """

    @staticmethod
    def _motion(coordinator: MotorGroupCoordinator | None) -> ReachyMotion:
        """Build an acquired adapter over a fake daemon.

        Args:
            coordinator: The gate, or `None` for the ungated path.

        Returns:
            The adapter, acquired and ready to be commanded.
        """
        motion = ReachyMotion(FakeRobot(), coordinator=coordinator)
        motion.acquire(0.0)
        return motion

    def test_the_ungated_path_raises_no_command_fault(self) -> None:
        """Nothing refuses, so nothing puts the controller into safe hold."""
        motion = self._motion(None)

        result = motion.command_gaze(_sample())

        assert result.status is MotionCommandStatus.ACCEPTED
        assert result.fault is MotionFault.NONE

    @pytest.mark.asyncio
    async def test_a_closed_gate_is_the_command_fault_that_was_measured(self) -> None:
        """`controller.fault == "command"` with every group shut, as observed."""
        coordinator = MotorGroupCoordinator(FakeRobot(), clock=ManualClock())
        try:
            motion = self._motion(coordinator)

            result = motion.command_gaze(_sample())

            assert result.status is MotionCommandStatus.REJECTED
            assert result.fault is MotionFault.COMMAND
        finally:
            await coordinator.aclose()

    @staticmethod
    def _tracking(
        coordinator: MotorGroupCoordinator | None,
    ) -> tuple[FakeRobot, SatelliteApplication]:
        """Build the whole loop over one face and one fake daemon.

        Args:
            coordinator: The gate, or `None` for the ungated path.

        Returns:
            The daemon handle and the application driving it.
        """
        robot = FakeRobot()
        motion = ReachyMotion(robot, coordinator=coordinator)
        motion.acquire(0.0)
        perception = FakePerception()
        perception.see(face(0.5, 0.0), source=DetectionSource.REMOTE)
        return robot, SatelliteApplication(
            settings=load_settings(_ENVIRONMENT).settings,
            audio=FakeAudio(),
            motion=motion,
            perception=perception,
            behaviour=SatelliteBehaviour(now=0.0),
            motor_groups=coordinator,
            clock=ManualClock(0.1),
        )

    def test_a_tracked_face_moves_the_head_under_the_ungated_mode(self) -> None:
        """A face in view, a head that moves, no command fault and no safe hold."""
        robot, application = self._tracking(None)

        for _ in range(3):
            application.tick()

        controller = cast("dict[str, object]", application.status()["controller"])
        assert robot.targets
        assert controller["fault"] == "none"
        assert controller["safe_hold"] is False

    @pytest.mark.asyncio
    async def test_the_same_loop_over_a_shut_gate_is_the_frozen_robot(self) -> None:
        """The measured stock-robot session, reproduced.

        Every command refused, the command fault raised, safe hold engaged, and
        the robot standing still while it tracks.
        """
        coordinator = MotorGroupCoordinator(FakeRobot(), clock=ManualClock())
        try:
            robot, application = self._tracking(coordinator)

            for _ in range(3):
                application.tick()

            controller = cast("dict[str, object]", application.status()["controller"])
            assert robot.targets == []
            assert controller["fault"] == "command"
            assert controller["safe_hold"] is True
        finally:
            await coordinator.aclose()


class TestShutdownInBothModes:
    """REQ-050's order holds either way, and neither mode leaves a worker behind."""

    @staticmethod
    def _application(
        robot: FakeRobot,
        coordinator: MotorGroupCoordinator | None,
    ) -> SatelliteApplication:
        """Build an application whose motion is acquired over a fake daemon.

        Args:
            robot: The daemon handle.
            coordinator: The gate, or `None` for the ungated path.

        Returns:
            The application.
        """
        motion = ReachyMotion(robot, coordinator=coordinator)
        motion.acquire(0.0)
        return SatelliteApplication(
            settings=load_settings(_ENVIRONMENT).settings,
            audio=FakeAudio(),
            motion=motion,
            perception=FakePerception(),
            behaviour=SatelliteBehaviour(now=0.0),
            motor_groups=coordinator,
        )

    @pytest.mark.asyncio
    async def test_the_ungated_mode_releases_motion_and_the_daemon_policy(
        self,
    ) -> None:
        """Nothing to drain, and the daemon gets its own body producer back."""
        robot = FakeRobot()
        application = self._application(robot, None)

        await application.aclose()

        assert robot.automatic_body_yaw[-1] is True
        assert application._motion.released
        assert application._motion.command_gaze(_sample()).fault is MotionFault.RELEASED

    @pytest.mark.asyncio
    async def test_the_confirmed_mode_still_becomes_terminal_and_drains(self) -> None:
        """Unchanged, which is the half of this change that must stay unchanged."""
        robot = FakeRobot()
        coordinator = MotorGroupCoordinator(robot, clock=ManualClock())
        application = self._application(robot, coordinator)

        await application.aclose()

        assert coordinator.terminal_requested
        assert not any(coordinator.gate_open(group) for group in MotorGroup)
        assert not coordinator.command(
            (MotorGroup.HEAD, MotorGroup.BODY, MotorGroup.ANTENNAS),
            lambda: None,
        )
        assert application._motion.released
