"""Home Assistant Boolean entity tests for confirmed physical motor state."""

from __future__ import annotations

import asyncio
import threading
from typing import cast

import pytest

# pylint: disable=no-name-in-module
from aioesphomeapi.api_pb2 import (  # type: ignore[attr-defined]  # generated protobuf module, which mypy cannot see the message classes inside
    ListEntitiesRequest,
    ListEntitiesSwitchResponse,
    SubscribeHomeAssistantStatesRequest,
    SwitchCommandRequest,
    SwitchStateResponse,
)
from aioesphomeapi.model import EntityCategory
from satellite_support import (
    FakeRobot,
    ManualClock,
    connected,
    motor_worker_threads,
    vendored_server_state,
)

from reachy_mini_ha_satellite.motor_control import (
    HEAD_MOTOR_IDS,
    MotorConfirmation,
    MotorConfirmationOutcome,
    MotorEvidence,
    MotorEvidenceError,
    MotorGroup,
    MotorGroupCoordinator,
    MotorGroupLifecycle,
)
from reachy_mini_ha_satellite.motor_entities import MotorSwitchEntity


def complete(enabled: bool, *, contradicted: bool = False) -> MotorConfirmation:
    """Build complete head-group evidence."""
    return MotorConfirmation(
        True,
        (
            MotorConfirmationOutcome.CONTRADICTED
            if contradicted
            else MotorConfirmationOutcome.CONFIRMED
        ),
        tuple(MotorEvidence(name=name, enabled=enabled) for name in HEAD_MOTOR_IDS),
    )


class _PausedMotorRobot(FakeRobot):
    """Block one confirmed disable until its test releases the sole worker."""

    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release_confirmation = threading.Event()
        self.active_confirmations = 0
        self.maximum_active_confirmations = 0

    def disable_motors_confirmed(self, ids: list[str]) -> MotorConfirmation:
        self.active_confirmations += 1
        self.maximum_active_confirmations = max(
            self.maximum_active_confirmations,
            self.active_confirmations,
        )
        self.started.set()
        self.release_confirmation.wait()
        try:
            return super().disable_motors_confirmed(ids)
        finally:
            self.active_confirmations -= 1


async def entity(
    robot: FakeRobot | None = None,
) -> tuple[MotorSwitchEntity, MotorGroupCoordinator, FakeRobot]:
    """Build a registered head switch over deterministic daemon evidence."""
    handle = robot or FakeRobot()
    groups = MotorGroupCoordinator(handle, clock=ManualClock())
    assert MotorGroup.HEAD in await groups.initialize()
    return (
        MotorSwitchEntity(
            state=vendored_server_state(),
            coordinator=groups,
            group=MotorGroup.HEAD,
            key=7,
        ),
        groups,
        handle,
    )


#:= docs/specs/home-assistant-configuration-and-camera-feed/index.md#req-093-home-assistant-configuration-reports-effective-state
#:% The satellite MUST expose stable Home Assistant Configuration entities for the
#:% head motors, body motor, antenna motors and groundstation session URL that report
#:% only confirmed effective state, announce each Boolean motor switch only after an
#:% initial agreeing correlated daemon acknowledgement and physical grouped-torque
#:% read-back, publish a new Boolean only from a later successful read-back including
#:% the actual value when it contradicts a request, and otherwise reject the request,
#:% retain the last-confirmed Boolean without publishing the requested value, keep the
#:% group's command gate closed and surface bounded identifier-free confirmation
#:% diagnostics.
@pytest.mark.asyncio
async def test_entity_has_stable_configuration_identity_and_reconnect_state() -> None:
    """Listing is inert; reconnect returns retained state before one worker read."""
    control, groups, robot = await entity()
    client = connected(control._state)[0]
    before = len(robot.motor_requests)

    first = list(control.handle_message(ListEntitiesRequest()))
    second = list(control.handle_message(ListEntitiesRequest()))
    reconnect = list(control.handle_message(SubscribeHomeAssistantStatesRequest()))

    assert first == second
    listing = cast("ListEntitiesSwitchResponse", first[0])
    assert listing.object_id == "head_motors"
    assert listing.name == "Head Motors"
    assert listing.entity_category == EntityCategory.CONFIG
    assert reconnect == [SwitchStateResponse(key=7, state=True)]
    assert len(robot.motor_requests) == before
    await groups.wait_idle()
    assert robot.motor_requests[before:] == [("read", HEAD_MOTOR_IDS)]
    assert client.sent == [SwitchStateResponse(key=7, state=True)]
    await groups.aclose()


@pytest.mark.asyncio
async def test_agreeing_command_returns_retained_then_broadcasts_confirmation() -> None:
    """No requested Boolean is returned before worker evidence completes."""
    control, groups, robot = await entity()
    client = connected(control._state)[0]
    robot.motor_disables_confirmed.append(complete(False))

    responses = list(control.handle_message(SwitchCommandRequest(key=7, state=False)))

    assert responses == [SwitchStateResponse(key=7, state=True)]
    assert not groups.gate_open(MotorGroup.HEAD)
    await groups.wait_idle()
    assert groups.last_confirmed(MotorGroup.HEAD) is False
    assert client.sent == [SwitchStateResponse(key=7, state=False)]
    await groups.aclose()


@pytest.mark.asyncio
async def test_agreeing_enable_reseeds_before_it_opens_or_broadcasts() -> None:
    """Switching a group back on is the half an operator actually reaches for."""
    control, groups, robot = await entity()
    client = connected(control._state)[0]
    robot.motor_disables_confirmed.append(complete(False))
    list(control.handle_message(SwitchCommandRequest(key=7, state=False)))
    await groups.wait_idle()
    assert groups.last_confirmed(MotorGroup.HEAD) is False
    robot.motor_enables_confirmed.append(complete(True))

    responses = list(control.handle_message(SwitchCommandRequest(key=7, state=True)))

    assert responses == [SwitchStateResponse(key=7, state=False)]
    await groups.wait_idle()
    assert groups.last_confirmed(MotorGroup.HEAD) is True
    assert groups.gate_open(MotorGroup.HEAD)
    assert client.sent == [
        SwitchStateResponse(key=7, state=False),
        SwitchStateResponse(key=7, state=True),
    ]
    await groups.aclose()


@pytest.mark.asyncio
async def test_a_broken_reseed_after_a_confirmed_enable_never_opens_the_gate() -> None:
    """Torque that came on behind a failed reseed is retained and stays shut."""
    control, groups, robot = await entity()
    client = connected(control._state)[0]
    robot.motor_disables_confirmed.append(complete(False))
    list(control.handle_message(SwitchCommandRequest(key=7, state=False)))
    await groups.wait_idle()
    robot.motor_enables_confirmed.append(complete(True))

    class _BrokenReseed(MotorGroupLifecycle):
        def sample_loop(self, sample: object) -> None:
            del sample
            raise RuntimeError("the loop could not adopt the measured sample")

    groups.set_hooks(MotorGroup.HEAD, lifecycle=_BrokenReseed)

    list(control.handle_message(SwitchCommandRequest(key=7, state=True)))
    await groups.wait_idle()

    assert groups.last_confirmed(MotorGroup.HEAD) is True
    assert not groups.gate_open(MotorGroup.HEAD)
    assert client.sent == [SwitchStateResponse(key=7, state=False)]
    await groups.aclose()


@pytest.mark.asyncio
async def test_failed_command_and_refresh_retain_without_broadcast() -> None:
    """Two incomplete worker phases produce no optimistic or duplicate push."""
    control, groups, robot = await entity()
    client = connected(control._state)[0]
    robot.motor_disables_confirmed.append(MotorConfirmation.failed())
    robot.motor_reads.append(MotorConfirmation.failed())
    before = len(robot.motor_requests)

    responses = list(control.handle_message(SwitchCommandRequest(key=7, state=False)))

    assert responses == [SwitchStateResponse(key=7, state=True)]
    await groups.wait_idle()
    assert groups.last_confirmed(MotorGroup.HEAD) is True
    assert not groups.gate_open(MotorGroup.HEAD)
    assert robot.motor_requests[before:] == [
        ("disable", HEAD_MOTOR_IDS),
        ("read", HEAD_MOTOR_IDS),
    ]
    assert client.sent == []
    await groups.aclose()


@pytest.mark.asyncio
async def test_mixed_success_and_error_never_broadcasts_requested_state() -> None:
    """One errored motor invalidates command and recovery evidence."""
    control, groups, robot = await entity()
    client = connected(control._state)[0]
    evidence = (
        *(MotorEvidence(name=name, enabled=False) for name in HEAD_MOTOR_IDS[:-1]),
        MotorEvidence(
            name=HEAD_MOTOR_IDS[-1],
            error=MotorEvidenceError.READ_FAILED,
        ),
    )
    incomplete = MotorConfirmation(
        True,
        MotorConfirmationOutcome.CONFIRMED,
        evidence,
    )
    robot.motor_disables_confirmed.append(incomplete)
    robot.motor_reads.append(incomplete)

    responses = list(control.handle_message(SwitchCommandRequest(key=7, state=False)))

    assert responses == [SwitchStateResponse(key=7, state=True)]
    await groups.wait_idle()
    assert groups.last_confirmed(MotorGroup.HEAD) is True
    assert not groups.gate_open(MotorGroup.HEAD)
    assert client.sent == []
    await groups.aclose()


#:= docs/specs/home-assistant-configuration-and-camera-feed/index.md#req-093-home-assistant-configuration-reports-effective-state
#:% The satellite MUST expose stable Home Assistant Configuration entities for the
#:% head motors, body motor, antenna motors and groundstation session URL that report
#:% only confirmed effective state, announce each Boolean motor switch only after an
#:% initial agreeing correlated daemon acknowledgement and physical grouped-torque
#:% read-back, publish a new Boolean only from a later successful read-back including
#:% the actual value when it contradicts a request, and otherwise reject the request,
#:% retain the last-confirmed Boolean without publishing the requested value, keep the
#:% group's command gate closed and surface bounded identifier-free confirmation
#:% diagnostics.
@pytest.mark.asyncio
async def test_contradiction_broadcasts_actual_and_keeps_gate_closed() -> None:
    """A physical contradiction pushes actual state only after worker completion."""
    control, groups, robot = await entity()
    client = connected(control._state)[0]
    robot.motor_disables_confirmed.append(complete(True, contradicted=True))

    responses = list(control.handle_message(SwitchCommandRequest(key=7, state=False)))

    assert responses == [SwitchStateResponse(key=7, state=True)]
    await groups.wait_idle()
    assert not groups.gate_open(MotorGroup.HEAD)
    assert client.sent == [SwitchStateResponse(key=7, state=True)]
    await groups.aclose()


@pytest.mark.asyncio
async def test_failed_command_refresh_broadcasts_one_fresh_read() -> None:
    """One recovery read advances state with exactly one completion push."""
    control, groups, robot = await entity()
    client = connected(control._state)[0]
    robot.motor_disables_confirmed.append(MotorConfirmation.failed())
    robot.motor_reads.append(complete(False))
    before = len(robot.motor_requests)

    responses = list(control.handle_message(SwitchCommandRequest(key=7, state=False)))

    assert responses == [SwitchStateResponse(key=7, state=True)]
    await groups.wait_idle()
    assert groups.last_confirmed(MotorGroup.HEAD) is False
    assert robot.motor_requests[before:] == [
        ("disable", HEAD_MOTOR_IDS),
        ("read", HEAD_MOTOR_IDS),
    ]
    assert client.sent == [SwitchStateResponse(key=7, state=False)]
    await groups.aclose()


async def _wait_for_worker_start(robot: _PausedMotorRobot) -> None:
    """Wait for the sole worker to reach its pause without polling for it.

    On a helper thread rather than by counting loop turns: the operation needs
    two executor round trips to get there, and how many turns those take is a
    property of how loaded the machine is. The loop stays free throughout, which
    is what lets those round trips complete at all.
    """
    await asyncio.get_running_loop().run_in_executor(None, robot.started.wait)


@pytest.mark.asyncio
async def test_paused_confirmation_keeps_loop_and_producers_non_blocking() -> None:
    """One paused worker stays finite while handlers and producers fail closed."""
    workers_before = motor_worker_threads()
    robot = _PausedMotorRobot()
    control, groups, _robot = await entity(robot)
    client = connected(control._state)[0]

    response = list(control.handle_message(SwitchCommandRequest(key=7, state=False)))

    assert response == [SwitchStateResponse(key=7, state=True)]
    assert not robot.started.is_set()
    assert not groups.gate_open(MotorGroup.HEAD)
    assert groups.last_confirmed(MotorGroup.HEAD) is True
    assert not groups.command((MotorGroup.HEAD,), lambda: None)
    await _wait_for_worker_start(robot)

    for _ in range(40):
        assert list(
            control.handle_message(SwitchCommandRequest(key=7, state=True))
        ) == [SwitchStateResponse(key=7, state=True)]
    heartbeat = 0
    for _ in range(10):
        await asyncio.sleep(0)
        heartbeat += 1

    assert heartbeat == 10
    assert robot.maximum_active_confirmations == 1
    assert robot.motor_requests.count(("disable", HEAD_MOTOR_IDS)) == 0
    assert len(cast("list[object]", groups.status()["events"])) <= 96
    robot.motor_disables_confirmed.append(complete(False))
    robot.release_confirmation.set()
    await groups.wait_idle()

    assert robot.maximum_active_confirmations == 1
    assert groups.last_confirmed(MotorGroup.HEAD) is False
    assert client.sent == [SwitchStateResponse(key=7, state=False)]
    await groups.aclose()
    assert motor_worker_threads() <= workers_before


@pytest.mark.asyncio
async def test_shutdown_refuses_reserved_operation_before_worker_start() -> None:
    """Terminal cancels queued-but-not-started intent without touching the SDK."""
    robot = _PausedMotorRobot()
    control, groups, _robot = await entity(robot)
    client = connected(control._state)[0]
    before = len(robot.motor_requests)

    response = list(control.handle_message(SwitchCommandRequest(key=7, state=False)))
    await groups.aclose()

    assert response == [SwitchStateResponse(key=7, state=True)]
    assert len(robot.motor_requests) == before
    assert not robot.started.is_set()
    assert groups.last_confirmed(MotorGroup.HEAD) is True
    assert not groups.gate_open(MotorGroup.HEAD)
    assert client.sent == []


@pytest.mark.asyncio
async def test_shutdown_drains_paused_worker_and_discards_late_result() -> None:
    """Terminal shutdown stays loop-responsive and joins the sole active worker."""
    workers_before = motor_worker_threads()
    robot = _PausedMotorRobot()
    control, groups, _robot = await entity(robot)
    client = connected(control._state)[0]
    robot.motor_disables_confirmed.append(complete(False))
    list(control.handle_message(SwitchCommandRequest(key=7, state=False)))
    await _wait_for_worker_start(robot)

    shutdown = asyncio.create_task(groups.aclose())
    for _ in range(10):
        await asyncio.sleep(0)
    assert not shutdown.done()
    assert not groups.command((MotorGroup.HEAD,), lambda: None)

    robot.release_confirmation.set()
    await shutdown

    assert groups.last_confirmed(MotorGroup.HEAD) is True
    assert not groups.gate_open(MotorGroup.HEAD)
    assert client.sent == []
    assert motor_worker_threads() <= workers_before


@pytest.mark.asyncio
async def test_wrong_key_does_not_touch_torque_or_reply() -> None:
    """Fan-out messages addressed to another switch remain inert."""
    control, _groups, robot = await entity()
    before = list(robot.motor_requests)

    assert list(control.handle_message(SwitchCommandRequest(key=99, state=False))) == []
    assert robot.motor_requests == before


@pytest.mark.asyncio
async def test_unconfirmed_group_cannot_construct_an_operable_entity() -> None:
    """Registration is structurally impossible without an initial Boolean."""
    robot = FakeRobot(motor_reads=[MotorConfirmation.unavailable()])
    groups = MotorGroupCoordinator(robot, clock=ManualClock())
    assert MotorGroup.HEAD not in await groups.initialize()

    with pytest.raises(ValueError, match="unconfirmed"):
        MotorSwitchEntity(
            state=vendored_server_state(),
            coordinator=groups,
            group=MotorGroup.HEAD,
            key=7,
        )
