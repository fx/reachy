"""Deterministic acceptance tests for confirmed motor-group coordination.

Every operation below is reserved the way the composition root reserves one —
`initialize`, `reserve_transition` and `reserve_refresh`, over the split
loop/worker lifecycle `main.build_application` installs. There is no second,
synchronous coordinator path to exercise instead: an acceptance matrix driving
one is a matrix that says nothing about the machine the robot runs.
"""

from __future__ import annotations

import asyncio
import functools
import threading
from typing import TYPE_CHECKING, cast

import pytest
from satellite_support import (
    YIELD_TURNS,
    FakeRobot,
    ManualClock,
    face,
    motor_worker_threads,
    until,
)

from reachy_mini_ha_satellite.adapters.daemon import RobotHandle
from reachy_mini_ha_satellite.adapters.motion_reachy import (
    ReachyMotion,
    head_pose_matrix,
)
from reachy_mini_ha_satellite.behaviour.gaze_controller import (
    BodyMeasurement,
    ControllerConfig,
    ControllerFault,
    HeadMeasurement,
)
from reachy_mini_ha_satellite.behaviour.pipeline import PipelineEvent
from reachy_mini_ha_satellite.behaviour.satellite import SatelliteBehaviour
from reachy_mini_ha_satellite.behaviour.tracking import GazeSelector
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
    MotorGroupLifecycle,
)
from reachy_mini_ha_satellite.ports import (
    AntennaPose,
    Detections,
    DetectionSource,
    GazeDirective,
    GazeSample,
    HeadPose,
    MotionCommandStatus,
)

if TYPE_CHECKING:
    from collections.abc import Callable


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


def install_lifecycles(
    groups: MotorGroupCoordinator,
    motion: ReachyMotion,
    behaviour: SatelliteBehaviour,
    clock: Callable[[], float],
) -> None:
    """Install exactly the hooks `main.build_application` installs.

    Copied rather than imported because assembling the application reads the
    wheel's own assets off a real disk. What matters is that the shape is the
    same one: a lifecycle per generation, its reseed generation captured on the
    loop before any blocking phase begins.
    """

    def _lifecycle(group: MotorGroup) -> MotorGroupLifecycle:
        expected = behaviour.reseed_generation

        def _finalize(
            head: HeadMeasurement | None,
            body: BodyMeasurement | None,
            antennas: AntennaPose | None,
        ) -> None:
            if not behaviour.reseed_motion(
                head=head,
                body=body,
                antennas=antennas,
                expected_generation=expected,
            ):
                raise RuntimeError("newer measured reseed superseded this one")

        return motion.motor_lifecycle(group, clock, _finalize)

    for group in MotorGroup:
        groups.set_hooks(group, lifecycle=functools.partial(_lifecycle, group))


async def coordinator(
    robot: RobotHandle,
    clock: ManualClock | None = None,
) -> MotorGroupCoordinator:
    """Build and confirm one bare coordinator over deterministic time."""
    result = MotorGroupCoordinator(robot, clock=clock or ManualClock())
    await result.initialize()
    return result


async def wired(
    robot: FakeRobot | None = None,
    *,
    body_enabled: bool = False,
) -> tuple[FakeRobot, MotorGroupCoordinator, ReachyMotion, SatelliteBehaviour]:
    """Assemble and confirm the coordinator, motion adapter and behavior layer."""
    handle = robot if robot is not None else FakeRobot()
    clock = ManualClock()
    groups = MotorGroupCoordinator(handle, clock=clock)
    config = ControllerConfig(body_enabled=body_enabled)
    motion = ReachyMotion(handle, controller_config=config, coordinator=groups)
    behaviour = SatelliteBehaviour(controller_config=config)
    install_lifecycles(groups, motion, behaviour, clock)
    await groups.initialize()
    return handle, groups, motion, behaviour


async def driven(
    groups: MotorGroupCoordinator,
    group: MotorGroup,
    requested: bool | None,
) -> list[bool]:
    """Reserve one operation the way an entity does and await its one worker."""
    published: list[bool] = []

    def _publish() -> None:
        published.append(True)

    reserved = (
        groups.reserve_refresh(group, _publish)
        if requested is None
        else groups.reserve_transition(group, requested, _publish)
    )
    if reserved:
        await groups.wait_idle()
    return published


def group_status(
    groups: MotorGroupCoordinator,
    group: MotorGroup,
) -> dict[str, object]:
    """Read one group's bounded public coordinator status."""
    status_groups = cast("dict[str, object]", groups.status()["groups"])
    return cast("dict[str, object]", status_groups[group.value])


def directive_for(captured_at: float) -> GazeDirective:
    """Select one actionable qualified directive for a visible face."""
    return GazeSelector().select(
        Detections(
            faces=(face(0.2, 0.0),),
            fresh=True,
            source=DetectionSource.REMOTE,
            generation=1,
            sequence=1,
            captured_at=captured_at,
            received_at=captured_at + 0.1,
        )
    )


class _PausedRobot(FakeRobot):
    """Park one named daemon call on the worker while the loop keeps running."""

    def __init__(self, call: str, **kwargs: object) -> None:
        """Take the call to park on, plus whatever `FakeRobot` scripts."""
        super().__init__(**kwargs)  # type: ignore[arg-type]  # forwarded verbatim to the fake's own keyword script
        self.parked_call = call
        self.started = threading.Event()
        self.release = threading.Event()

    def _park(self, call: str) -> None:
        if call != self.parked_call:
            return
        self.started.set()
        self.release.wait()

    def read_motor_torque(self, ids: list[str]) -> MotorConfirmation:
        """Park an independent read, then answer as the fake normally would."""
        self._park("read")
        return super().read_motor_torque(ids)

    def enable_motors_confirmed(self, ids: list[str]) -> MotorConfirmation:
        """Park a confirmed enable, then answer as the fake normally would."""
        self._park("enable")
        return super().enable_motors_confirmed(ids)


async def parked(robot: _PausedRobot) -> int:
    """Yield to the loop until the worker is inside the parked daemon call.

    The turns it takes are the evidence — the loop kept running while a daemon
    call blocked the worker — and `until` is what stops a worker that never
    arrives hanging the suite instead of failing it.

    Args:
        robot: The fake parked on one of its daemon calls.

    Returns:
        How many loop turns passed before the worker reached that call.
    """
    return await until(robot.started.is_set, "the parked daemon call")


@pytest.mark.asyncio
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
async def test_initial_absent_partial_or_contradictory_evidence_keeps_group_closed(
    failed: MotorConfirmation,
) -> None:
    """No optimistic switch/gate state can be inferred from incomplete evidence."""
    robot = FakeRobot(motor_reads=[failed])
    groups = MotorGroupCoordinator(robot, clock=ManualClock())

    registered = await groups.initialize()

    assert MotorGroup.HEAD not in registered
    assert groups.last_confirmed(MotorGroup.HEAD) is None
    assert not groups.gate_open(MotorGroup.HEAD)
    head = cast("dict[str, object]", groups.status()["groups"])["head"]
    assert cast("dict[str, object]", head)["last_confirmed"] is None


def test_sdk_neutral_local_evidence_may_omit_ids_for_unit_fakes() -> None:
    """Only local fakes use absent IDs; daemon translation tests require every ID."""
    local = confirmation(HEAD_MOTOR_IDS, True)

    assert all(item.motor_id is None for item in local.evidence)
    assert local.physical_value(HEAD_MOTOR_IDS) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("error", list(MotorEvidenceError))
async def test_any_per_motor_error_makes_initial_group_incomplete(
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

    assert MotorGroup.HEAD not in await groups.initialize()
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evidence",
    [case[1] for case in _INCOMPLETE_EVIDENCE],
    ids=[case[0] for case in _INCOMPLETE_EVIDENCE],
)
async def test_incomplete_evidence_fails_initial_transition_and_refresh_closed(
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
    assert MotorGroup.HEAD not in await initial.initialize()
    assert initial.last_confirmed(MotorGroup.HEAD) is None
    assert not initial.gate_open(MotorGroup.HEAD)

    transition_robot = FakeRobot(
        motor_disables_confirmed=[incomplete],
        motor_reads=[
            confirmation(HEAD_MOTOR_IDS, True),
            confirmation(BODY_MOTOR_IDS, True),
            confirmation(ANTENNA_MOTOR_IDS, True),
            incomplete,
        ],
    )
    transition = await coordinator(transition_robot)
    await driven(transition, MotorGroup.HEAD, False)
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
    refreshed = await coordinator(refresh_robot)
    await driven(refreshed, MotorGroup.HEAD, None)
    assert refreshed.last_confirmed(MotorGroup.HEAD) is True
    assert not refreshed.gate_open(MotorGroup.HEAD)


@pytest.mark.asyncio
async def test_missing_confirmed_api_gates_every_group_closed() -> None:
    """A released SDK without the canary surface cannot expose motor controls."""

    class LegacyHandle:
        """Deliberately has none of the three confirmed methods."""

    groups = MotorGroupCoordinator(
        cast("RobotHandle", LegacyHandle()), clock=ManualClock()
    )

    assert await groups.initialize() == ()
    assert all(not groups.gate_open(group) for group in MotorGroup)


@pytest.mark.asyncio
async def test_initial_reads_use_the_three_exact_independent_motor_sets() -> None:
    """Group boundaries are fixed and neither antenna can become its own switch."""
    robot = FakeRobot()
    groups = MotorGroupCoordinator(robot, clock=ManualClock())

    assert await groups.initialize() == tuple(MotorGroup)
    assert robot.motor_requests == [
        ("read", HEAD_MOTOR_IDS),
        ("read", BODY_MOTOR_IDS),
        ("read", ANTENNA_MOTOR_IDS),
    ]


_LIFECYCLE_PHASES = (
    "prepare_worker",
    "prepare_loop",
    "sample_worker",
    "sample_loop",
    "restore_worker",
    "restore_loop",
)


class _ScriptedLifecycle(MotorGroupLifecycle):
    """Inject one failure, cancellation or terminal request at an exact phase."""

    def __init__(
        self,
        groups: MotorGroupCoordinator,
        phase: str,
        outcome: str,
        events: list[str],
    ) -> None:
        """Take what to do, where to do it, and where to record every phase."""
        self._groups = groups
        self._phase = phase
        self._outcome = outcome
        self._events = events

    def _run(self, phase: str) -> None:
        self._events.append(phase)
        if self._phase != phase:
            return
        if self._outcome == "failure":
            raise RuntimeError("bounded startup failure")
        if self._outcome == "cancel":
            raise asyncio.CancelledError
        self._groups.terminal()

    def prepare_worker(self) -> object:
        """Record and optionally break the worker-side quiesce."""
        self._run("prepare_worker")
        return None

    def prepare_loop(self, prepared: object) -> None:
        """Record and optionally break the loop-side quiesce commit."""
        del prepared
        self._run("prepare_loop")

    def sample_worker(self) -> object:
        """Record and optionally break the worker-side measured sample."""
        self._run("sample_worker")
        return None

    def sample_loop(self, sample: object) -> None:
        """Record and optionally break the loop-side reseed commit."""
        del sample
        self._run("sample_loop")

    def restore_worker(self, policy: bool | None) -> object:
        """Record and optionally break the worker-side policy restore."""
        del policy
        self._run("restore_worker")
        return None

    def restore_loop(self, restored: object) -> None:
        """Record and optionally break the loop-side restore commit."""
        del restored
        self._run("restore_loop")


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", _LIFECYCLE_PHASES)
@pytest.mark.parametrize("outcome", ["failure", "cancel", "terminal"])
async def test_each_enabled_startup_phase_fails_closed(
    phase: str,
    outcome: str,
) -> None:
    """No failed, cancelled or terminal reseed generation registers or opens."""
    robot = FakeRobot()
    groups = MotorGroupCoordinator(robot, clock=ManualClock())
    events: list[str] = []
    groups.set_hooks(
        MotorGroup.HEAD,
        lifecycle=lambda: _ScriptedLifecycle(groups, phase, outcome, events),
    )

    if outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await groups.initialize()
    else:
        assert MotorGroup.HEAD not in await groups.initialize()

    assert not groups.gate_open(MotorGroup.HEAD)
    assert groups.last_confirmed(MotorGroup.HEAD) is None
    assert phase in events
    if outcome == "terminal":
        assert all(not groups.gate_open(group) for group in MotorGroup)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", _LIFECYCLE_PHASES)
@pytest.mark.parametrize("outcome", ["failure", "cancel", "terminal"])
async def test_each_enabled_transition_phase_fails_closed(
    phase: str,
    outcome: str,
) -> None:
    """The reserved enable path retains truth and never opens on a broken phase."""
    robot = FakeRobot()
    groups = await coordinator(robot)
    events: list[str] = []
    await driven(groups, MotorGroup.HEAD, False)
    assert groups.last_confirmed(MotorGroup.HEAD) is False
    groups.set_hooks(
        MotorGroup.HEAD,
        lifecycle=lambda: _ScriptedLifecycle(groups, phase, outcome, events),
    )

    if outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await driven(groups, MotorGroup.HEAD, True)
    else:
        await driven(groups, MotorGroup.HEAD, True)

    assert phase in events
    assert not groups.gate_open(MotorGroup.HEAD)
    # Once a loop phase has failed *after* the confirmed enable, the physical
    # truth it read is still truth and is retained; the gate is what must not
    # advance with it. Terminal retains nothing, because terminal outranks every
    # promotion.
    promoted = outcome != "terminal" and phase in {
        "sample_loop",
        "restore_worker",
        "restore_loop",
    }
    assert groups.last_confirmed(MotorGroup.HEAD) is promoted


@pytest.mark.asyncio
async def test_initial_enabled_body_is_read_under_exclusive_ownership_then_restored() -> (
    None
):
    """Initial registration does not race daemon automatic yaw either."""
    robot, groups, _motion, _behaviour = await wired()

    assert groups.gate_open(MotorGroup.BODY)
    disabled = robot.events.index("motion.auto_yaw.false")
    read = robot.events.index("motors.read", disabled)
    restored = robot.events.index("motion.auto_yaw.true", read)
    assert disabled < read < restored


@pytest.mark.asyncio
async def test_initial_disabled_body_is_quiesced_before_registration() -> None:
    """A confirmed-off body cannot leave daemon automatic yaw targeting it."""
    robot = FakeRobot(
        motor_reads=[
            confirmation(HEAD_MOTOR_IDS, True),
            confirmation(BODY_MOTOR_IDS, False),
            confirmation(ANTENNA_MOTOR_IDS, True),
        ]
    )
    _robot, groups, _motion, _behaviour = await wired(robot)

    assert groups.last_confirmed(MotorGroup.BODY) is False
    assert not groups.gate_open(MotorGroup.BODY)
    assert robot.automatic_body_yaw == [False]


#:= docs/specs/home-assistant-configuration-and-camera-feed/index.md#req-094-motor-groups-change-safely-at-run-time
#:% The satellite MUST apply independent head, body and antenna motor-group switches
#:% immediately by quiescing every application- and daemon-owned command producer for
#:% the group before torque-off, establishing exclusive body-command ownership when
#:% needed, confirming each physical grouped-torque transition, and reacquiring and
#:% seeding measured state before movement or the preceding ownership policy resumes,
#:% without weakening existing trajectory, workspace, safe-hold or terminal-release
#:% guarantees.
@pytest.mark.asyncio
async def test_disable_closes_gate_before_daemon_call_and_isolates_unrelated_group() -> (
    None
):
    """An in-flight producer cannot pass the gate at the torque-off call edge."""
    robot = FakeRobot()
    groups = await coordinator(robot)
    observed: list[bool] = []
    original = robot.disable_motors_confirmed

    def _disable(ids: list[str]) -> MotorConfirmation:
        observed.append(groups.command((MotorGroup.HEAD,), lambda: None))
        observed.append(groups.command((MotorGroup.ANTENNAS,), lambda: None))
        return original(ids)

    robot.disable_motors_confirmed = _disable  # type: ignore[method-assign]  # test instruments the exact daemon-call edge

    await driven(groups, MotorGroup.HEAD, False)

    assert observed == [False, True]
    assert groups.last_confirmed(MotorGroup.HEAD) is False
    assert not groups.gate_open(MotorGroup.HEAD)
    assert groups.gate_open(MotorGroup.ANTENNAS)


@pytest.mark.asyncio
async def test_agreeing_transition_uses_physical_result_and_reseed_before_open() -> (
    None
):
    """Torque-on is not commandable until fresh-state reseeding completes."""
    robot = FakeRobot()
    groups = await coordinator(robot)
    await driven(groups, MotorGroup.HEAD, False)
    phases: list[bool] = []

    class _Reseeding(MotorGroupLifecycle):
        def sample_worker(self) -> object:
            phases.append(groups.command((MotorGroup.HEAD,), lambda: None))
            return None

    groups.set_hooks(MotorGroup.HEAD, lifecycle=_Reseeding)

    published = await driven(groups, MotorGroup.HEAD, True)

    assert phases == [False]
    assert published == [True]
    assert groups.last_confirmed(MotorGroup.HEAD) is True
    assert groups.gate_open(MotorGroup.HEAD)


@pytest.mark.asyncio
async def test_contradiction_publishes_actual_and_keeps_gate_closed() -> None:
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
    groups = await coordinator(robot)

    published = await driven(groups, MotorGroup.HEAD, False)

    assert published == [True]
    assert groups.last_confirmed(MotorGroup.HEAD) is True
    assert not groups.gate_open(MotorGroup.HEAD)


@pytest.mark.asyncio
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
async def test_missing_late_failed_or_partial_transition_retains_boolean(
    failure: MotorConfirmation | BaseException,
) -> None:
    """Fire-and-forget/no-exception behavior would fail this regression proof."""
    robot = FakeRobot(
        motor_disables_confirmed=[failure],
        motor_reads=[
            confirmation(HEAD_MOTOR_IDS, True),
            confirmation(BODY_MOTOR_IDS, True),
            confirmation(ANTENNA_MOTOR_IDS, True),
            confirmation(HEAD_MOTOR_IDS, True),
        ],
    )
    groups = await coordinator(robot)

    await driven(groups, MotorGroup.HEAD, False)

    assert groups.last_confirmed(MotorGroup.HEAD) is True
    assert not groups.gate_open(MotorGroup.HEAD)


@pytest.mark.asyncio
async def test_later_independent_read_advances_retained_state() -> None:
    """A failed request does not poison a later complete physical sample."""
    robot = FakeRobot(
        motor_disables_confirmed=[MotorConfirmation.failed()],
        motor_reads=[
            confirmation(HEAD_MOTOR_IDS, True),
            confirmation(BODY_MOTOR_IDS, True),
            confirmation(ANTENNA_MOTOR_IDS, True),
            confirmation(HEAD_MOTOR_IDS, True),
            confirmation(HEAD_MOTOR_IDS, False),
        ],
    )
    groups = await coordinator(robot)
    await driven(groups, MotorGroup.HEAD, False)

    await driven(groups, MotorGroup.HEAD, None)

    assert groups.last_confirmed(MotorGroup.HEAD) is False
    assert not groups.gate_open(MotorGroup.HEAD)


@pytest.mark.asyncio
async def test_motion_adapter_gates_gaze_pipeline_head_and_antennas_independently() -> (
    None
):
    """Every current application producer enters through the shared coordinator."""
    robot, groups, motion, _behaviour = await wired()
    motion.acquire(0.0)
    sample = GazeSample(0.0, 0.0, 0.0, 0.0, False)

    assert motion.command_gaze(sample).status is MotionCommandStatus.ACCEPTED
    motion.move_head(HeadPose(pitch=0.1))
    motion.move_antennas(AntennaPose(left=0.2, right=-0.2))
    before = len(robot.targets)

    await driven(groups, MotorGroup.HEAD, False)
    motion.command_gaze(sample)
    motion.move_head(HeadPose(pitch=0.2))
    motion.move_antennas(AntennaPose(left=0.3, right=-0.3))

    assert len(robot.targets) == before + 1
    assert robot.targets[-1][1] == [-0.3, 0.3]


#:= docs/specs/home-assistant-configuration-and-camera-feed/index.md#req-094-motor-groups-change-safely-at-run-time
#:% The satellite MUST apply independent head, body and antenna motor-group switches
#:% immediately by quiescing every application- and daemon-owned command producer for
#:% the group before torque-off, establishing exclusive body-command ownership when
#:% needed, confirming each physical grouped-torque transition, and reacquiring and
#:% seeding measured state before movement or the preceding ownership policy resumes,
#:% without weakening existing trajectory, workspace, safe-hold or terminal-release
#:% guarantees.
@pytest.mark.asyncio
@pytest.mark.parametrize("face_tracking", [False, True])
@pytest.mark.parametrize("body_motion", [False, True])
async def test_body_transition_captures_quiesces_and_restores_all_setting_combinations(
    face_tracking: bool,
    body_motion: bool,
) -> None:
    """Automatic yaw is owned even when either restart-bound setting is false."""
    robot, groups, motion, _behaviour = await wired(body_enabled=body_motion)
    if face_tracking:
        motion.acquire(0.0)
    captured = len(robot.automatic_body_yaw)

    await driven(groups, MotorGroup.BODY, False)

    quiesced = robot.events.index("motion.auto_yaw.false", captured)
    assert quiesced < robot.events.index("motors.disable.confirmed", quiesced)
    assert not groups.gate_open(MotorGroup.BODY)

    await driven(groups, MotorGroup.BODY, True)

    assert robot.automatic_body_yaw[-1] is (not face_tracking)
    assert groups.last_confirmed(MotorGroup.BODY) is True
    assert groups.gate_open(MotorGroup.BODY)


@pytest.mark.asyncio
async def test_failed_body_transition_never_restores_automatic_yaw() -> None:
    """Unknown torque leaves exclusive ownership and the body gate closed."""
    robot = FakeRobot(
        motor_disables_confirmed=[MotorConfirmation.failed()],
        motor_reads=[
            confirmation(HEAD_MOTOR_IDS, True),
            confirmation(BODY_MOTOR_IDS, True),
            confirmation(ANTENNA_MOTOR_IDS, True),
            confirmation(BODY_MOTOR_IDS, True),
        ],
    )
    _robot, groups, motion, _behaviour = await wired(robot)
    restored = robot.automatic_body_yaw.count(True)

    await driven(groups, MotorGroup.BODY, False)

    assert robot.automatic_body_yaw.count(True) == restored
    assert robot.automatic_body_yaw[-1] is False
    motion.release()
    assert robot.automatic_body_yaw[-1] is False


@pytest.mark.asyncio
async def test_terminal_during_a_policy_restore_leaves_daemon_yaw_disabled() -> None:
    """The daemon's body producer does not come back behind terminal's back.

    A restore that reports success while the coordinator is already terminal
    hands the daemon its body back and then abandons the loop commit, so the
    retained capture stops `release` undoing it. Nothing else would.
    """
    robot = FakeRobot()
    _robot, groups, motion, _behaviour = await wired(robot)
    await driven(groups, MotorGroup.BODY, False)
    original = robot.set_automatic_body_yaw

    def _set_automatic_body_yaw(enabled: bool) -> None:
        if enabled:
            groups.terminal()
        original(enabled)

    robot.set_automatic_body_yaw = _set_automatic_body_yaw  # type: ignore[method-assign]  # terminal is requested inside the blocking policy write

    await driven(groups, MotorGroup.BODY, True)

    assert robot.automatic_body_yaw[-1] is False
    assert not groups.gate_open(MotorGroup.BODY)
    assert not groups.safe_to_restore_body_policy()
    motion.release()
    assert robot.automatic_body_yaw[-1] is False


@pytest.mark.asyncio
async def test_a_raising_policy_restore_still_re_asserts_the_safe_state() -> None:
    """A write may be adopted before it raises, and this is its only witness."""
    robot = FakeRobot()
    _robot, groups, motion, _behaviour = await wired(robot)
    await driven(groups, MotorGroup.BODY, False)
    original = robot.set_automatic_body_yaw

    def _set_automatic_body_yaw(enabled: bool) -> None:
        original(enabled)
        if enabled:
            raise RuntimeError("the daemon adopted the policy and then failed")

    robot.set_automatic_body_yaw = _set_automatic_body_yaw  # type: ignore[method-assign]  # the policy write raises after the daemon took it

    await driven(groups, MotorGroup.BODY, True)

    assert robot.automatic_body_yaw[-2:] == [True, False]
    assert not groups.gate_open(MotorGroup.BODY)
    motion.release()
    assert robot.automatic_body_yaw[-1] is False


@pytest.mark.asyncio
async def test_head_and_body_reenable_reset_faults_derivatives_and_hidden_target() -> (
    None
):
    """Fresh measurement replaces every controller target before a gate can reopen."""
    robot = FakeRobot()
    robot.measured_head_pose = head_pose_matrix(HeadPose(yaw=0.4, pitch=0.2))
    robot.measured_body_yaw = 0.1
    _robot, groups, _motion, behaviour = await wired(robot, body_enabled=True)
    prepared = behaviour.prepare(Detections(), 1.0)
    behaviour.finish(
        prepared,
        calibrated=None,
        body_measurement=None,
        dt=0.05,
        input_fault=ControllerFault.COMMAND,
    )
    assert behaviour.controller_state.safe_hold

    await driven(groups, MotorGroup.BODY, False)
    await driven(groups, MotorGroup.BODY, True)

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


@pytest.mark.asyncio
async def test_antenna_reenable_seeds_both_measured_joints_as_one_group() -> None:
    """Neither antenna inherits a pre-disable expression target."""
    robot = FakeRobot()
    _robot, groups, _motion, behaviour = await wired(robot)
    await driven(groups, MotorGroup.ANTENNAS, False)
    robot.measured_joints.append(([0.0] * 7, [0.3, -0.4]))

    await driven(groups, MotorGroup.ANTENNAS, True)

    assert groups.gate_open(MotorGroup.ANTENNAS)
    assert behaviour._last_antennas == AntennaPose(right=0.3, left=-0.4)


@pytest.mark.asyncio
async def test_first_face_acquire_supersedes_inflight_body_policy_restore() -> None:
    """Temporary transition ownership cannot overwrite newer gaze ownership."""
    robot = FakeRobot()
    config = ControllerConfig(body_enabled=True)
    motion = ReachyMotion(robot, controller_config=config)
    behaviour = SatelliteBehaviour(controller_config=config)
    generation = behaviour.reseed_generation

    def _finalize(
        head: HeadMeasurement | None,
        body: BodyMeasurement | None,
        antennas: AntennaPose | None,
    ) -> None:
        assert behaviour.reseed_motion(
            head=head,
            body=body,
            antennas=antennas,
            expected_generation=generation,
        )

    lifecycle = motion.motor_lifecycle(MotorGroup.BODY, lambda: 2.0, _finalize)
    prepared = lifecycle.prepare_worker()
    lifecycle.prepare_loop(prepared)
    sample = lifecycle.sample_worker()
    lifecycle.sample_loop(sample)

    motion.acquire(3.0)
    restored = lifecycle.restore_worker(lifecycle.captured_policy(prepared))

    with pytest.raises(RuntimeError, match="superseded"):
        lifecycle.restore_loop(restored)
    assert robot.automatic_body_yaw == [False]
    assert motion._acquired
    assert not motion._temporary_ownership


@pytest.mark.asyncio
async def test_calibrating_a_visible_face_cannot_strand_a_confirmed_reenable() -> None:
    """Tracking a face while a motor switch comes back on must not close it forever.

    Calibration repopulates a per-source cache. It changes no ownership and no
    measured state, so it has no business invalidating the measured sample a
    torque re-enable is waiting to commit — and if it does, the group ends
    confirmed on, gated shut, and unreachable by any later refresh.
    """
    robot = _PausedRobot("enable")
    _robot, groups, motion, _behaviour = await wired(robot)
    motion.acquire(1000.0)
    motion.observe(1000.5)
    await driven(groups, MotorGroup.HEAD, False)

    reserved = groups.reserve_transition(MotorGroup.HEAD, True, lambda: None)
    assert reserved
    assert await parked(robot) > 0
    assert motion.calibrate(directive_for(1000.2), 1000.6).state.value == "accepted"
    robot.release.set()
    await groups.wait_idle()

    assert groups.last_confirmed(MotorGroup.HEAD) is True
    assert groups.gate_open(MotorGroup.HEAD)
    motion.move_head(HeadPose(pitch=0.2))
    assert robot.targets


@pytest.mark.asyncio
async def test_calibrating_without_ownership_takes_it_and_advances_the_generation() -> (
    None
):
    """Calibrating never advances the counter; the fallback it can reach does.

    The comment beside that read depends on this being the *only* way through
    `calibrate` that touches the generation, and on production never taking it.
    Pinned rather than argued, because the argument lives in two other files:
    if the fallback ever became reachable, a tick with a face in view would go
    back to invalidating the measured reseed of any group coming back on, and
    nothing in this file would notice.
    """
    robot = FakeRobot()
    motion = ReachyMotion(robot)
    motion.observe(1000.0)
    before = motion._generation
    # Read into locals: asserting on the attribute both before and after would
    # narrow it to the first reading and make the second assertion vacuous.
    owned_before = motion._acquired

    accepted = motion.calibrate(directive_for(1000.0), 1000.5)
    owned_after = motion._acquired

    assert accepted.state.value == "accepted"
    assert not owned_before
    assert owned_after
    assert motion._generation > before


@pytest.mark.asyncio
async def test_a_gate_refused_expression_cannot_strand_a_confirmed_reenable() -> None:
    """The intents a closed gate refused never reached the robot, so they lose."""
    robot = _PausedRobot("enable")
    _robot, groups, motion, behaviour = await wired(robot)
    await driven(groups, MotorGroup.ANTENNAS, False)

    reserved = groups.reserve_transition(MotorGroup.ANTENNAS, True, lambda: None)
    assert reserved
    assert await parked(robot) > 0
    intents = behaviour.handle(PipelineEvent.WAKE_WORD_DETECTED, 1.0)
    assert intents
    motion.move_antennas(AntennaPose(left=0.9, right=0.9))
    robot.release.set()
    await groups.wait_idle()

    assert groups.last_confirmed(MotorGroup.ANTENNAS) is True
    assert groups.gate_open(MotorGroup.ANTENNAS)
    assert behaviour._last_antennas == AntennaPose(right=0.0, left=0.0)


@pytest.mark.asyncio
async def test_a_newer_measured_reseed_still_supersedes_an_older_one() -> None:
    """Dropping the expression guard does not let two reseeds commit out of order."""
    robot = FakeRobot()
    motion = ReachyMotion(robot)
    behaviour = SatelliteBehaviour()
    generation = behaviour.reseed_generation

    def _finalize(
        head: HeadMeasurement | None,
        body: BodyMeasurement | None,
        antennas: AntennaPose | None,
    ) -> None:
        if not behaviour.reseed_motion(
            head=head,
            body=body,
            antennas=antennas,
            expected_generation=generation,
        ):
            raise RuntimeError("newer measured reseed superseded this one")

    lifecycle = motion.motor_lifecycle(MotorGroup.HEAD, lambda: 2.0, _finalize)
    prepared = lifecycle.prepare_worker()
    lifecycle.prepare_loop(prepared)
    sample = lifecycle.sample_worker()
    assert behaviour.reseed_motion(head=HeadMeasurement(0.1, 0.1, 2.0))

    with pytest.raises(RuntimeError, match="superseded"):
        lifecycle.sample_loop(sample)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["prepare", "read", "restore"])
async def test_reentrant_terminal_wins_every_initialize_callback_phase(
    phase: str,
) -> None:
    """Initialization stops at the callback that requested terminal state."""
    robot = FakeRobot()
    clock = ManualClock()
    groups = MotorGroupCoordinator(robot, clock=clock)
    motion = ReachyMotion(robot, coordinator=groups)
    behaviour = SatelliteBehaviour()
    install_lifecycles(groups, motion, behaviour, clock)
    original_yaw = robot.set_automatic_body_yaw
    original_read = robot.read_motor_torque

    def _set_automatic_body_yaw(enabled: bool) -> None:
        original_yaw(enabled)
        if not enabled and phase == "prepare":
            groups.terminal()
        if enabled and phase == "restore":
            groups.terminal()

    def _read(ids: list[str]) -> MotorConfirmation:
        result = original_read(ids)
        if phase == "read" and tuple(ids) == HEAD_MOTOR_IDS:
            groups.terminal()
        return result

    robot.set_automatic_body_yaw = _set_automatic_body_yaw  # type: ignore[method-assign]  # inject terminal at the exact daemon callback boundary
    robot.read_motor_torque = _read  # type: ignore[method-assign]  # inject terminal at the exact daemon callback boundary

    registered = await groups.initialize()

    assert registered == ()
    assert all(not groups.gate_open(group) for group in MotorGroup)
    assert all(
        group_status(groups, group)["transition"] == "terminal" for group in MotorGroup
    )
    if phase != "restore":
        assert robot.automatic_body_yaw.count(True) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["prepare", "set", "reseed", "restore"])
async def test_reentrant_terminal_wins_every_transition_callback_phase(
    phase: str,
) -> None:
    """A stale transition never advances state or restores policy after terminal."""
    robot = FakeRobot()
    _robot, groups, _motion, _behaviour = await wired(robot)
    requested = phase in {"reseed", "restore"}
    if requested:
        await driven(groups, MotorGroup.BODY, False)
    original_yaw = robot.set_automatic_body_yaw
    original_set = (
        robot.enable_motors_confirmed if requested else robot.disable_motors_confirmed
    )
    original_joints = robot.get_current_joint_positions

    def _set_automatic_body_yaw(enabled: bool) -> None:
        original_yaw(enabled)
        if not enabled and phase == "prepare":
            groups.terminal()
        if enabled and phase == "restore":
            groups.terminal()

    def _set(ids: list[str]) -> MotorConfirmation:
        result = original_set(ids)
        if phase == "set":
            groups.terminal()
        return result

    def _joints() -> tuple[list[float], list[float]]:
        result = original_joints()
        if phase == "reseed":
            groups.terminal()
        return result

    robot.set_automatic_body_yaw = _set_automatic_body_yaw  # type: ignore[method-assign]  # inject terminal at the exact daemon callback boundary
    robot.get_current_joint_positions = _joints  # type: ignore[method-assign]  # inject terminal at the exact daemon callback boundary
    if requested:
        robot.enable_motors_confirmed = _set  # type: ignore[method-assign]  # inject terminal at the exact daemon callback boundary
    else:
        robot.disable_motors_confirmed = _set  # type: ignore[method-assign]  # inject terminal at the exact daemon callback boundary
    before = groups.last_confirmed(MotorGroup.BODY)

    published = await driven(groups, MotorGroup.BODY, requested)

    assert published == []
    assert groups.last_confirmed(MotorGroup.BODY) is before
    assert not groups.gate_open(MotorGroup.BODY)
    assert group_status(groups, MotorGroup.BODY)["transition"] == "terminal"
    assert robot.automatic_body_yaw[-1] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["prepare", "read", "restore"])
async def test_reentrant_terminal_wins_every_refresh_callback_phase(phase: str) -> None:
    """A stale independent read cannot advance or restore after terminal."""
    robot = FakeRobot()
    _robot, groups, _motion, _behaviour = await wired(robot)
    original_yaw = robot.set_automatic_body_yaw
    original_read = robot.read_motor_torque

    def _set_automatic_body_yaw(enabled: bool) -> None:
        original_yaw(enabled)
        if not enabled and phase == "prepare":
            groups.terminal()
        if enabled and phase == "restore":
            groups.terminal()

    def _read(ids: list[str]) -> MotorConfirmation:
        result = original_read(ids)
        if phase == "read":
            groups.terminal()
        return result

    robot.set_automatic_body_yaw = _set_automatic_body_yaw  # type: ignore[method-assign]  # inject terminal at the exact daemon callback boundary
    robot.read_motor_torque = _read  # type: ignore[method-assign]  # inject terminal at the exact daemon callback boundary
    before = groups.last_confirmed(MotorGroup.BODY)

    published = await driven(groups, MotorGroup.BODY, None)

    assert published == []
    assert groups.last_confirmed(MotorGroup.BODY) is before
    assert not groups.gate_open(MotorGroup.BODY)
    assert group_status(groups, MotorGroup.BODY)["transition"] == "terminal"
    assert robot.automatic_body_yaw[-1] is False


@pytest.mark.asyncio
async def test_reentrant_terminal_from_clock_wins_before_state_promotion() -> None:
    """The injected clock is another callback boundary before publication."""
    robot = FakeRobot()
    groups = await coordinator(robot)
    before = groups.last_confirmed(MotorGroup.HEAD)

    def _clock() -> float:
        groups.terminal()
        return 1001.0

    groups._clock = _clock

    published = await driven(groups, MotorGroup.HEAD, False)

    assert published == []
    assert groups.last_confirmed(MotorGroup.HEAD) is before
    assert not groups.gate_open(MotorGroup.HEAD)
    assert group_status(groups, MotorGroup.HEAD)["transition"] == "terminal"


@pytest.mark.asyncio
async def test_concurrent_terminal_request_wins_before_transition_publication() -> None:
    """The lock defers terminal's caller while its request still stops stale work."""
    robot = FakeRobot()
    groups = await coordinator(robot)
    begin_terminal = threading.Event()
    terminal_requested = threading.Event()
    terminal_completed = threading.Event()
    original_disable = robot.disable_motors_confirmed

    def _terminal() -> None:
        begin_terminal.wait()
        groups._terminal_requested.set()  # exercise the signal visible while the coordinator lock is held
        terminal_requested.set()
        groups.terminal()
        terminal_completed.set()

    worker = threading.Thread(target=_terminal, name="racing-terminal")
    worker.start()

    def _disable(ids: list[str]) -> MotorConfirmation:
        result = original_disable(ids)
        begin_terminal.set()
        terminal_requested.wait()
        return result

    robot.disable_motors_confirmed = _disable  # type: ignore[method-assign]  # hold the exact in-flight daemon callback boundary
    before = groups.last_confirmed(MotorGroup.HEAD)

    published = await driven(groups, MotorGroup.HEAD, False)
    worker.join()

    assert published == []
    assert terminal_completed.is_set()
    assert groups.last_confirmed(MotorGroup.HEAD) is before
    assert not groups.gate_open(MotorGroup.HEAD)
    assert group_status(groups, MotorGroup.HEAD)["transition"] == "terminal"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["prepare", "read", "restore"])
async def test_cancelled_independent_refresh_closes_gate_and_retains_state(
    phase: str,
) -> None:
    """Cancellation at every refresh phase cannot strand an open producer gate."""
    robot = FakeRobot()
    groups = await coordinator(robot)
    events: list[str] = []
    mapped = {
        "prepare": "prepare_worker",
        "read": "sample_worker",
        "restore": "restore_worker",
    }[phase]
    groups.set_hooks(
        MotorGroup.BODY,
        lifecycle=lambda: _ScriptedLifecycle(groups, mapped, "cancel", events),
    )
    if phase == "read":
        robot.motor_reads.append(asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await driven(groups, MotorGroup.BODY, None)

    assert groups.last_confirmed(MotorGroup.BODY) is True
    assert not groups.gate_open(MotorGroup.BODY)


@pytest.mark.asyncio
async def test_initial_confirmation_leaves_the_event_loop_responsive() -> None:
    """A five-second daemon read is not five seconds of a deaf application.

    The daemon's stop watcher reaches the application through
    `loop.call_soon_threadsafe`, so a blocking confirmation on this loop is a
    stop nobody hears. What proves the loop is alive is that it kept running
    turns while the worker sat in the parked read.
    """
    workers_before = motor_worker_threads()
    robot = _PausedRobot("read")
    groups = MotorGroupCoordinator(robot, clock=ManualClock())
    beats = 0

    async def _heartbeat() -> None:
        """Count loop turns for as long as the parked confirmation is in flight."""
        nonlocal beats
        for _ in range(YIELD_TURNS):
            if robot.release.is_set():
                return
            beats += 1
            await asyncio.sleep(0)

    heart = asyncio.create_task(_heartbeat(), name="startup-heartbeat")
    startup = asyncio.create_task(groups.initialize(), name="startup")
    assert await parked(robot) > 0
    # An independent task, not this one: what has to keep running while a daemon
    # read blocks the worker is everything else on the loop.
    assert beats > 0

    groups.terminal()
    assert all(not groups.gate_open(group) for group in MotorGroup)

    robot.release.set()
    assert await startup == ()
    await heart
    await groups.aclose()

    assert all(groups.last_confirmed(group) is None for group in MotorGroup)
    assert motor_worker_threads() <= workers_before


@pytest.mark.asyncio
async def test_cancellation_during_initial_confirmation_leaks_nothing() -> None:
    """Startup and every later phase share one terminal path, cancellation too."""
    workers_before = motor_worker_threads()
    robot = _PausedRobot("read")
    groups = MotorGroupCoordinator(robot, clock=ManualClock())

    startup = asyncio.create_task(groups.initialize(), name="startup")
    assert await parked(robot) > 0
    startup.cancel()
    robot.release.set()
    with pytest.raises(asyncio.CancelledError):
        await startup
    await groups.aclose()

    assert all(not groups.gate_open(group) for group in MotorGroup)
    assert all(groups.last_confirmed(group) is None for group in MotorGroup)
    assert robot.automatic_body_yaw == []
    assert motor_worker_threads() <= workers_before


@pytest.mark.asyncio
async def test_closing_after_a_cancelled_phase_waits_off_the_loop() -> None:
    """Shutdown may not become the stall the whole split exists to prevent.

    Cancelling the task that awaits a blocking phase cancels the wrapper and not
    the thread, and startup has no task recorded in `_operation` for shutdown to
    drain. `ThreadPoolExecutor.shutdown(wait=True)` waits on its *calling*
    thread, so with nothing else tracking that worker this is where a
    five-second daemon read stops the event loop dead.
    """
    workers_before = motor_worker_threads()
    robot = _PausedRobot("read")
    groups = MotorGroupCoordinator(robot, clock=ManualClock())
    startup = asyncio.create_task(groups.initialize(), name="startup")
    assert await parked(robot) > 0
    startup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await startup

    closing = asyncio.create_task(groups.aclose(), name="closing")
    beats = 0
    for _ in range(50):
        beats += 1
        await asyncio.sleep(0)
    still_waiting = not closing.done()

    robot.release.set()
    await closing

    assert beats == 50
    assert still_waiting
    assert motor_worker_threads() <= workers_before


@pytest.mark.asyncio
async def test_shutdown_waits_for_every_outstanding_phase_not_only_the_last() -> None:
    """One record per phase, so a later one finishing cannot retire an earlier.

    Driven past the reserved paths deliberately, because this tree cannot reach
    the interleaving: it submits one phase at a time on one worker, so a second
    finishing does imply the first did. `aclose` was correct only for as long as
    that stayed true, and nothing states or checks it. A phase abandoned by its
    caller and a phase cancelled before it starts are exactly the pair that
    breaks a single slot — the second is finished the moment it is cancelled,
    and retires a record still owed to the first.
    """
    workers_before = motor_worker_threads()
    robot = FakeRobot()
    groups = MotorGroupCoordinator(robot, clock=ManualClock())
    started = threading.Event()
    release = threading.Event()

    def _park() -> None:
        started.set()
        release.wait()

    abandoned = asyncio.create_task(groups._offload(_park), name="abandoned-phase")
    await until(started.is_set, "the first phase reaching the worker")
    abandoned.cancel()
    with pytest.raises(asyncio.CancelledError):
        await abandoned

    # Queued behind the parked one, so cancelling it cancels work that never ran.
    queued = asyncio.create_task(groups._offload(lambda: None), name="queued-phase")
    await asyncio.sleep(0)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued

    closing = asyncio.create_task(groups.aclose(), name="closing")
    for _ in range(50):
        await asyncio.sleep(0)
    still_waiting = not closing.done()

    release.set()
    await closing

    assert still_waiting
    assert motor_worker_threads() <= workers_before


@pytest.mark.asyncio
async def test_confirmed_startup_registers_only_confirmed_groups_in_phase_order() -> (
    None
):
    """A responsive startup is still a strictly ordered one."""
    robot = FakeRobot(
        motor_reads=[
            confirmation(HEAD_MOTOR_IDS, True),
            confirmation(BODY_MOTOR_IDS, True),
            MotorConfirmation.failed(),
        ]
    )
    _robot, groups, _motion, _behaviour = await wired(robot)

    assert groups.last_confirmed(MotorGroup.ANTENNAS) is None
    assert groups.gate_open(MotorGroup.HEAD)
    assert groups.gate_open(MotorGroup.BODY)
    assert not groups.gate_open(MotorGroup.ANTENNAS)
    assert robot.events.index("motion.auto_yaw.false") < robot.events.index(
        "motion.auto_yaw.true"
    )


@pytest.mark.asyncio
async def test_terminal_drain_is_responsive_cancellation_safe_and_leak_free() -> None:
    """Shutdown closes now but awaits a reserved external-thread target to return."""
    workers_before = motor_worker_threads()
    robot = FakeRobot()
    groups = await coordinator(robot)
    started = threading.Event()
    release = threading.Event()
    completed: list[bool] = []

    def _producer() -> None:
        def _target() -> None:
            started.set()
            release.wait()

        completed.append(groups.command((MotorGroup.HEAD,), _target))

    worker = threading.Thread(target=_producer, name="paused-motor-producer")
    worker.start()
    started.wait()

    groups.terminal()
    assert not groups.gate_open(MotorGroup.HEAD)
    assert not groups.command((MotorGroup.HEAD,), lambda: None)
    assert not groups.safe_to_restore_body_policy()
    closing = asyncio.create_task(groups.aclose())
    await asyncio.sleep(0)
    assert not closing.done()

    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    worker.join()
    with pytest.raises(asyncio.CancelledError):
        await closing

    assert completed == [False]
    assert motor_worker_threads() <= workers_before


@pytest.mark.asyncio
async def test_terminal_release_is_idempotent_and_blocks_all_later_producers() -> None:
    """Shutdown and racing commands share one irreversible closed-gate path."""
    robot = FakeRobot()
    _robot, groups, motion, _behaviour = await wired(robot)
    before = len(robot.targets)

    groups.terminal()
    groups.terminal()
    motion.release()
    motion.release()
    motion.move_head(HeadPose(pitch=0.5))
    motion.move_antennas(AntennaPose(left=0.5, right=0.5))

    assert len(robot.targets) == before
    assert all(not groups.gate_open(group) for group in MotorGroup)


@pytest.mark.asyncio
async def test_diagnostics_are_fixed_bounded_and_identifier_free() -> None:
    """Repeated failures retain only stable scalar categories and group names."""
    robot = FakeRobot()
    groups = await coordinator(robot)
    for _ in range(40):
        robot.motor_reads.append(RuntimeError("private request and hardware details"))
        await driven(groups, MotorGroup.HEAD, None)

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
