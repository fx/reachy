"""Deterministic acceptance tests for confirmed motor-group coordination."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from satellite_support import FakeRobot, ManualClock

from reachy_mini_ha_satellite.adapters.daemon import RobotHandle
from reachy_mini_ha_satellite.adapters.motion_reachy import (
    ReachyMotion,
    head_pose_matrix,
)
from reachy_mini_ha_satellite.behaviour.gaze_controller import (
    ControllerConfig,
    ControllerFault,
)
from reachy_mini_ha_satellite.behaviour.satellite import SatelliteBehaviour
from reachy_mini_ha_satellite.motor_control import (
    ANTENNA_MOTOR_IDS,
    BODY_MOTOR_IDS,
    HEAD_MOTOR_IDS,
    MOTOR_IDENTIFIERS,
    MotorConfirmation,
    MotorConfirmationOutcome,
    MotorEvidence,
    MotorEvidenceError,
    MotorGroup,
    MotorGroupCoordinator,
)
from reachy_mini_ha_satellite.ports import (
    AntennaPose,
    Detections,
    GazeSample,
    HeadPose,
    MotionCommandStatus,
)


def confirmation(
    names: tuple[str, ...],
    enabled: bool,
    *,
    outcome: MotorConfirmationOutcome = MotorConfirmationOutcome.CONFIRMED,
    acknowledged: bool = True,
) -> MotorConfirmation:
    """Build complete correlated physical evidence for one exact group."""
    return MotorConfirmation(
        acknowledged,
        outcome,
        tuple(MotorEvidence(name=name, enabled=enabled) for name in names),
    )


def coordinator(
    robot: RobotHandle,
    clock: ManualClock | None = None,
) -> MotorGroupCoordinator:
    """Build and initialize one coordinator over deterministic time."""
    result = MotorGroupCoordinator(robot, clock=clock or ManualClock())
    result.initialize()
    return result


@pytest.mark.parametrize(
    "failed",
    [
        MotorConfirmation.unavailable(),
        MotorConfirmation(False, MotorConfirmationOutcome.FAILED),
        MotorConfirmation(
            True,
            MotorConfirmationOutcome.PARTIAL,
            (MotorEvidence(name=HEAD_MOTOR_IDS[0], enabled=True),),
        ),
        MotorConfirmation(
            True,
            MotorConfirmationOutcome.CONFIRMED,
            tuple(
                MotorEvidence(name=name, enabled=index != 0)
                for index, name in enumerate(HEAD_MOTOR_IDS)
            ),
        ),
        confirmation(
            HEAD_MOTOR_IDS,
            True,
            outcome=MotorConfirmationOutcome.CONTRADICTED,
        ),
    ],
)
def test_initial_absent_partial_or_contradictory_evidence_keeps_group_closed(
    failed: MotorConfirmation,
) -> None:
    """No optimistic switch/gate state can be inferred from incomplete evidence."""
    robot = FakeRobot(motor_reads=[failed])
    groups = MotorGroupCoordinator(robot, clock=ManualClock())

    registered = groups.initialize()

    assert MotorGroup.HEAD not in registered
    assert groups.last_confirmed(MotorGroup.HEAD) is None
    assert not groups.gate_open(MotorGroup.HEAD)
    head = cast("dict[str, object]", groups.status()["groups"])["head"]
    assert cast("dict[str, object]", head)["last_confirmed"] is None


@pytest.mark.parametrize("error", list(MotorEvidenceError))
def test_any_per_motor_error_makes_initial_group_incomplete(
    error: MotorEvidenceError,
) -> None:
    """No bounded per-motor error may be filtered out before agreement."""
    evidence = (
        *(MotorEvidence(name=name, enabled=True) for name in HEAD_MOTOR_IDS[:-1]),
        MotorEvidence(name=HEAD_MOTOR_IDS[-1], error=error),
    )
    robot = FakeRobot(
        motor_reads=[
            MotorConfirmation(True, MotorConfirmationOutcome.CONFIRMED, evidence)
        ]
    )
    groups = MotorGroupCoordinator(robot, clock=ManualClock())

    assert MotorGroup.HEAD not in groups.initialize()
    assert groups.last_confirmed(MotorGroup.HEAD) is None
    assert not groups.gate_open(MotorGroup.HEAD)


_INCOMPLETE_EVIDENCE = [
    (
        "mixed-success-error",
        (
            *(MotorEvidence(name=name, enabled=True) for name in HEAD_MOTOR_IDS[:-1]),
            MotorEvidence(
                name=HEAD_MOTOR_IDS[-1],
                error=MotorEvidenceError.READ_FAILED,
            ),
        ),
    ),
    (
        "duplicate-missing-substitution",
        (
            *(MotorEvidence(name=name, enabled=True) for name in HEAD_MOTOR_IDS[:-1]),
            MotorEvidence(name=HEAD_MOTOR_IDS[0], enabled=True),
        ),
    ),
    (
        "extra-evidence",
        (
            *(MotorEvidence(name=name, enabled=True) for name in HEAD_MOTOR_IDS),
            MotorEvidence(name=BODY_MOTOR_IDS[0], enabled=True),
        ),
    ),
    (
        "wrong-group-member",
        (
            *(MotorEvidence(name=name, enabled=True) for name in HEAD_MOTOR_IDS[:-1]),
            MotorEvidence(name=BODY_MOTOR_IDS[0], enabled=True),
        ),
    ),
    (
        "mismatched-name-id",
        (
            MotorEvidence(
                name=HEAD_MOTOR_IDS[0],
                motor_id=MOTOR_IDENTIFIERS[HEAD_MOTOR_IDS[1]],
                enabled=True,
            ),
            *(
                MotorEvidence(
                    name=name,
                    motor_id=MOTOR_IDENTIFIERS[name],
                    enabled=True,
                )
                for name in HEAD_MOTOR_IDS[1:]
            ),
        ),
    ),
    (
        "partial-ids",
        (
            MotorEvidence(
                name=HEAD_MOTOR_IDS[0],
                motor_id=MOTOR_IDENTIFIERS[HEAD_MOTOR_IDS[0]],
                enabled=True,
            ),
            *(MotorEvidence(name=name, enabled=True) for name in HEAD_MOTOR_IDS[1:]),
        ),
    ),
]


@pytest.mark.parametrize(
    "evidence",
    [case[1] for case in _INCOMPLETE_EVIDENCE],
    ids=[case[0] for case in _INCOMPLETE_EVIDENCE],
)
def test_incomplete_evidence_fails_initial_transition_and_refresh_closed(
    evidence: tuple[MotorEvidence, ...],
) -> None:
    """Every confirmation path requires exact one-to-one complete agreement."""
    incomplete = MotorConfirmation(
        True,
        MotorConfirmationOutcome.CONFIRMED,
        evidence,
    )

    initial_robot = FakeRobot(motor_reads=[incomplete])
    initial = MotorGroupCoordinator(initial_robot, clock=ManualClock())
    assert MotorGroup.HEAD not in initial.initialize()
    assert initial.last_confirmed(MotorGroup.HEAD) is None
    assert not initial.gate_open(MotorGroup.HEAD)

    transition_robot = FakeRobot(motor_disables_confirmed=[incomplete])
    transition = coordinator(transition_robot)
    assert transition.transition(MotorGroup.HEAD, False) is None
    assert transition.last_confirmed(MotorGroup.HEAD) is True
    assert not transition.gate_open(MotorGroup.HEAD)

    refresh_robot = FakeRobot(
        motor_reads=[
            confirmation(HEAD_MOTOR_IDS, True),
            confirmation(BODY_MOTOR_IDS, True),
            confirmation(ANTENNA_MOTOR_IDS, True),
            incomplete,
        ]
    )
    refreshed = coordinator(refresh_robot)
    assert refreshed.refresh(MotorGroup.HEAD) is None
    assert refreshed.last_confirmed(MotorGroup.HEAD) is True
    assert not refreshed.gate_open(MotorGroup.HEAD)


def test_missing_confirmed_api_gates_every_group_closed() -> None:
    """A released SDK without the canary surface cannot expose motor controls."""

    class LegacyHandle:
        """Deliberately has none of the three confirmed methods."""

    groups = MotorGroupCoordinator(
        cast("RobotHandle", LegacyHandle()), clock=ManualClock()
    )

    assert groups.initialize() == ()
    assert all(not groups.gate_open(group) for group in MotorGroup)


def test_initial_reads_use_the_three_exact_independent_motor_sets() -> None:
    """Group boundaries are fixed and neither antenna can become its own switch."""
    robot = FakeRobot()
    groups = MotorGroupCoordinator(robot, clock=ManualClock())

    assert groups.initialize() == tuple(MotorGroup)
    assert robot.motor_requests == [
        ("read", HEAD_MOTOR_IDS),
        ("read", BODY_MOTOR_IDS),
        ("read", ANTENNA_MOTOR_IDS),
    ]


def test_initial_enabled_body_is_read_under_exclusive_ownership_then_restored() -> None:
    """Initial registration does not race daemon automatic yaw either."""
    robot = FakeRobot()
    groups = MotorGroupCoordinator(robot, clock=ManualClock())
    motion = ReachyMotion(robot, coordinator=groups)
    groups.set_hooks(
        MotorGroup.BODY,
        prepare=motion.quiesce_body,
        restore=motion.restore_body_policy,
    )

    assert MotorGroup.BODY in groups.initialize()
    disabled = robot.events.index("motion.auto_yaw.false")
    read = robot.events.index("motors.read", disabled)
    restored = robot.events.index("motion.auto_yaw.true", read)
    assert disabled < read < restored
    assert groups.gate_open(MotorGroup.BODY)


def test_initial_disabled_body_is_quiesced_before_registration() -> None:
    """A confirmed-off body cannot leave daemon automatic yaw targeting it."""
    robot = FakeRobot(
        motor_reads=[
            confirmation(HEAD_MOTOR_IDS, True),
            confirmation(BODY_MOTOR_IDS, False),
            confirmation(ANTENNA_MOTOR_IDS, True),
        ]
    )
    groups = MotorGroupCoordinator(robot, clock=ManualClock())
    motion = ReachyMotion(robot, coordinator=groups)
    groups.set_hooks(
        MotorGroup.BODY,
        prepare=motion.quiesce_body,
        reseed=lambda: None,
        restore=motion.restore_body_policy,
    )

    registered = groups.initialize()

    assert MotorGroup.BODY in registered
    assert groups.last_confirmed(MotorGroup.BODY) is False
    assert not groups.gate_open(MotorGroup.BODY)
    assert robot.automatic_body_yaw == [False]


def test_disable_closes_gate_before_daemon_call_and_isolates_unrelated_group() -> None:
    """An in-flight producer cannot pass the gate at the torque-off call edge."""
    robot = FakeRobot()
    groups = coordinator(robot)
    observed: list[bool] = []
    original = robot.disable_motors_confirmed

    def _disable(ids: list[str]) -> MotorConfirmation:
        observed.append(groups.command((MotorGroup.HEAD,), lambda: None))
        observed.append(groups.command((MotorGroup.ANTENNAS,), lambda: None))
        return original(ids)

    robot.disable_motors_confirmed = _disable  # type: ignore[method-assign]  # test instruments the exact daemon-call edge

    assert groups.transition(MotorGroup.HEAD, False) is False
    assert observed == [False, True]
    assert not groups.gate_open(MotorGroup.HEAD)
    assert groups.gate_open(MotorGroup.ANTENNAS)


def test_agreeing_transition_uses_physical_result_and_reseed_before_open() -> None:
    """Torque-on is not commandable until fresh-state reseeding completes."""
    robot = FakeRobot()
    groups = coordinator(robot)
    assert groups.transition(MotorGroup.HEAD, False) is False
    phases: list[bool] = []

    def _reseed() -> None:
        phases.append(groups.command((MotorGroup.HEAD,), lambda: None))

    groups.set_hooks(MotorGroup.HEAD, reseed=_reseed)

    assert groups.transition(MotorGroup.HEAD, True) is True
    assert phases == [False]
    assert groups.gate_open(MotorGroup.HEAD)


def test_contradiction_publishes_actual_and_keeps_gate_closed() -> None:
    """A successful physical contradiction is evidence, never requested success."""
    robot = FakeRobot(
        motor_disables_confirmed=[
            confirmation(
                HEAD_MOTOR_IDS,
                True,
                outcome=MotorConfirmationOutcome.CONTRADICTED,
            )
        ]
    )
    groups = coordinator(robot)

    assert groups.transition(MotorGroup.HEAD, False) is True
    assert groups.last_confirmed(MotorGroup.HEAD) is True
    assert not groups.gate_open(MotorGroup.HEAD)


@pytest.mark.parametrize(
    "failure",
    [
        MotorConfirmation(False, MotorConfirmationOutcome.FAILED),
        MotorConfirmation(True, MotorConfirmationOutcome.PARTIAL),
        MotorConfirmation(
            True,
            MotorConfirmationOutcome.CONFIRMED,
            tuple(
                MotorEvidence(name=name, enabled=False) for name in HEAD_MOTOR_IDS[:-1]
            ),
        ),
        RuntimeError("unbounded daemon detail must not escape"),
    ],
)
def test_missing_late_failed_or_partial_transition_retains_boolean(
    failure: MotorConfirmation | BaseException,
) -> None:
    """Fire-and-forget/no-exception behavior would fail this regression proof."""
    robot = FakeRobot(motor_disables_confirmed=[failure])
    groups = coordinator(robot)

    assert groups.transition(MotorGroup.HEAD, False) is None
    assert groups.last_confirmed(MotorGroup.HEAD) is True
    assert not groups.gate_open(MotorGroup.HEAD)


def test_later_independent_read_advances_retained_state() -> None:
    """A failed request does not poison a later complete physical sample."""
    robot = FakeRobot(
        motor_disables_confirmed=[MotorConfirmation.failed()],
        motor_reads=[
            confirmation(HEAD_MOTOR_IDS, True),
            confirmation(BODY_MOTOR_IDS, True),
            confirmation(ANTENNA_MOTOR_IDS, True),
            confirmation(HEAD_MOTOR_IDS, False),
        ],
    )
    groups = coordinator(robot)
    assert groups.transition(MotorGroup.HEAD, False) is None

    assert groups.refresh(MotorGroup.HEAD) is False
    assert groups.last_confirmed(MotorGroup.HEAD) is False
    assert not groups.gate_open(MotorGroup.HEAD)


def test_motion_adapter_gates_gaze_pipeline_head_and_antennas_independently() -> None:
    """Every current application producer enters through the shared coordinator."""
    robot = FakeRobot()
    groups = coordinator(robot)
    motion = ReachyMotion(robot, coordinator=groups)
    motion.acquire(0.0)
    sample = GazeSample(0.0, 0.0, 0.0, 0.0, False)

    assert motion.command_gaze(sample).status is MotionCommandStatus.ACCEPTED
    motion.move_head(HeadPose(pitch=0.1))
    motion.move_antennas(AntennaPose(left=0.2, right=-0.2))
    assert len(robot.targets) == 3

    groups.transition(MotorGroup.HEAD, False)
    motion.command_gaze(sample)
    motion.move_head(HeadPose(pitch=0.2))
    motion.move_antennas(AntennaPose(left=0.3, right=-0.3))

    assert len(robot.targets) == 4
    assert robot.targets[-1][1] == [-0.3, 0.3]


@pytest.mark.parametrize("face_tracking", [False, True])
@pytest.mark.parametrize("body_motion", [False, True])
def test_body_transition_captures_quiesces_and_restores_all_setting_combinations(
    face_tracking: bool,
    body_motion: bool,
) -> None:
    """Automatic yaw is owned even when either restart-bound setting is false."""
    robot = FakeRobot()
    groups = coordinator(robot)
    motion = ReachyMotion(
        robot,
        coordinator=groups,
        controller_config=ControllerConfig(body_enabled=body_motion),
    )
    if face_tracking:
        motion.acquire(0.0)

    def _reseed_body() -> None:
        motion.reseed(MotorGroup.BODY, 1.0)

    groups.set_hooks(
        MotorGroup.BODY,
        prepare=motion.quiesce_body,
        reseed=_reseed_body,
        restore=motion.restore_body_policy,
    )

    assert groups.transition(MotorGroup.BODY, False) is False
    assert robot.events.index("motion.auto_yaw.false") < robot.events.index(
        "motors.disable.confirmed"
    )
    assert groups.transition(MotorGroup.BODY, True) is True

    expected_restored = not face_tracking
    assert robot.automatic_body_yaw[-1] is expected_restored
    assert groups.gate_open(MotorGroup.BODY)


def test_failed_body_transition_never_restores_automatic_yaw() -> None:
    """Unknown torque leaves exclusive ownership and the body gate closed."""
    robot = FakeRobot(motor_disables_confirmed=[MotorConfirmation.failed()])
    groups = coordinator(robot)
    motion = ReachyMotion(robot, coordinator=groups)

    def _reseed_body() -> None:
        motion.reseed(MotorGroup.BODY, 1.0)

    groups.set_hooks(
        MotorGroup.BODY,
        prepare=motion.quiesce_body,
        reseed=_reseed_body,
        restore=motion.restore_body_policy,
    )

    assert groups.transition(MotorGroup.BODY, False) is None
    assert robot.automatic_body_yaw == [False]
    motion.release()
    assert robot.automatic_body_yaw == [False]


@pytest.mark.parametrize("phase", ["prepare", "confirm", "reseed", "restore"])
def test_cancellation_in_each_transition_phase_leaves_gate_closed(phase: str) -> None:
    """Cancellation propagates only after the group has entered its safe path."""
    robot = FakeRobot()
    groups = coordinator(robot)
    groups.transition(MotorGroup.HEAD, False)

    def _cancel() -> bool:
        raise asyncio.CancelledError

    def _cancel_void() -> None:
        raise asyncio.CancelledError

    def _cancel_restore(_policy: bool) -> None:
        raise asyncio.CancelledError

    if phase == "confirm":
        robot.motor_enables_confirmed.append(asyncio.CancelledError())
    groups.set_hooks(
        MotorGroup.HEAD,
        prepare=_cancel if phase == "prepare" else lambda: False,
        reseed=_cancel_void if phase == "reseed" else lambda: None,
        restore=_cancel_restore if phase == "restore" else lambda _policy: None,
    )

    with pytest.raises(asyncio.CancelledError):
        groups.transition(MotorGroup.HEAD, True)
    assert not groups.gate_open(MotorGroup.HEAD)


def test_head_and_body_reenable_reset_faults_derivatives_and_hidden_target() -> None:
    """Fresh measurement replaces every controller target before a gate can reopen."""
    robot = FakeRobot()
    robot.measured_head_pose = head_pose_matrix(HeadPose(yaw=0.4, pitch=0.2))
    robot.measured_body_yaw = 0.1
    config = ControllerConfig(body_enabled=True)
    motion = ReachyMotion(robot, controller_config=config)
    behaviour = SatelliteBehaviour(controller_config=config)
    prepared = behaviour.prepare(Detections(), 1.0)
    behaviour.finish(
        prepared,
        calibrated=None,
        body_measurement=None,
        dt=0.05,
        input_fault=ControllerFault.COMMAND,
    )
    assert behaviour.controller_state.safe_hold

    head, body, antennas = motion.reseed(MotorGroup.BODY, 2.0)
    behaviour.reseed_motion(head=head, body=body, antennas=antennas)

    state = behaviour.controller_state
    assert not state.safe_hold
    assert state.head_initialized
    assert state.body_feedback.initialized
    assert state.world_yaw.position == pytest.approx(0.4)
    assert state.elevation.position == pytest.approx(0.2)
    assert state.body_yaw.position == pytest.approx(0.1)
    assert state.last_safe_sample.head_yaw == pytest.approx(0.3)
    for axis in (state.world_yaw, state.elevation, state.body_yaw):
        assert axis.velocity == 0.0
        assert axis.acceleration == 0.0


def test_antenna_reenable_seeds_both_measured_joints_as_one_group() -> None:
    """Neither antenna inherits a pre-disable expression target."""
    robot = FakeRobot(
        measured_joints=[([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], [0.3, -0.4])]
    )
    motion = ReachyMotion(robot)
    behaviour = SatelliteBehaviour()

    head, body, antennas = motion.reseed(MotorGroup.ANTENNAS, 2.0)
    behaviour.reseed_motion(head=head, body=body, antennas=antennas)

    assert head is None
    assert body is None
    assert antennas == AntennaPose(right=0.3, left=-0.4)


def test_terminal_release_is_idempotent_and_blocks_all_later_producers() -> None:
    """Shutdown and racing commands share one irreversible closed-gate path."""
    robot = FakeRobot()
    groups = coordinator(robot)
    motion = ReachyMotion(robot, coordinator=groups)

    groups.terminal()
    groups.terminal()
    motion.release()
    motion.release()
    motion.move_head(HeadPose(pitch=0.5))
    motion.move_antennas(AntennaPose(left=0.5, right=0.5))

    assert robot.targets == []
    assert all(not groups.gate_open(group) for group in MotorGroup)


def test_diagnostics_are_fixed_bounded_and_identifier_free() -> None:
    """Repeated failures retain only stable scalar categories and group names."""
    robot = FakeRobot()
    groups = coordinator(robot)
    for _ in range(40):
        robot.motor_reads.append(RuntimeError("private request and hardware details"))
        assert groups.refresh(MotorGroup.HEAD) is None

    status = groups.status()
    events = cast("list[dict[str, object]]", status["events"])
    head_events = [event for event in events if event["group"] == "head"]
    assert len(head_events) == 32
    assert all(
        frozenset(event)
        == {
            "group",
            "requested",
            "acknowledgement",
            "readback",
            "fresh",
            "changed",
            "confirmation_age",
        }
        for event in events
    )
    serialized = repr(status)
    assert "stewart" not in serialized
    assert "private request" not in serialized
