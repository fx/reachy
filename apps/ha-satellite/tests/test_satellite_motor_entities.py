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


def entity(
    robot: FakeRobot | None = None,
) -> tuple[MotorSwitchEntity, MotorGroupCoordinator, FakeRobot]:
    """Build a registered head switch over deterministic daemon evidence."""
    handle = robot or FakeRobot()
    groups = MotorGroupCoordinator(handle, clock=ManualClock())
    assert MotorGroup.HEAD in groups.initialize()
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


@pytest.mark.asyncio
async def test_entity_has_stable_configuration_identity_and_reconnect_state() -> None:
    """Listing is inert; reconnect returns retained state before one worker read."""
    control, groups, robot = entity()
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
    control, groups, robot = entity()
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
async def test_failed_command_and_refresh_retain_without_broadcast() -> None:
    """Two incomplete worker phases produce no optimistic or duplicate push."""
    control, groups, robot = entity()
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
    control, groups, robot = entity()
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


@pytest.mark.asyncio
async def test_contradiction_broadcasts_actual_and_keeps_gate_closed() -> None:
    """A physical contradiction pushes actual state only after worker completion."""
    control, groups, robot = entity()
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
    control, groups, robot = entity()
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
    """Give the finite worker bounded loop turns to reach its pause."""
    for _ in range(20):
        if robot.started.is_set():
            return
        await asyncio.sleep(0)
    raise AssertionError("motor worker did not start within bounded loop turns")


@pytest.mark.asyncio
async def test_paused_confirmation_keeps_loop_and_producers_non_blocking() -> None:
    """One paused worker stays finite while handlers and producers fail closed."""
    robot = _PausedMotorRobot()
    control, groups, _robot = entity(robot)
    client = connected(control._state)[0]
    threads_before = {thread.name for thread in threading.enumerate()}

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
    assert {thread.name for thread in threading.enumerate()} == threads_before


@pytest.mark.asyncio
async def test_shutdown_refuses_reserved_operation_before_worker_start() -> None:
    """Terminal cancels queued-but-not-started intent without touching the SDK."""
    robot = _PausedMotorRobot()
    control, groups, _robot = entity(robot)
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
    robot = _PausedMotorRobot()
    control, groups, _robot = entity(robot)
    client = connected(control._state)[0]
    threads_before = {thread.name for thread in threading.enumerate()}
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
    assert {thread.name for thread in threading.enumerate()} == threads_before


def test_wrong_key_does_not_touch_torque_or_reply() -> None:
    """Fan-out messages addressed to another switch remain inert."""
    control, _groups, robot = entity()
    before = list(robot.motor_requests)

    assert list(control.handle_message(SwitchCommandRequest(key=99, state=False))) == []
    assert robot.motor_requests == before


def test_unconfirmed_group_cannot_construct_an_operable_entity() -> None:
    """Registration is structurally impossible without an initial Boolean."""
    robot = FakeRobot(motor_reads=[MotorConfirmation.unavailable()])
    groups = MotorGroupCoordinator(robot, clock=ManualClock())
    assert MotorGroup.HEAD not in groups.initialize()

    with pytest.raises(ValueError, match="unconfirmed"):
        MotorSwitchEntity(
            state=vendored_server_state(),
            coordinator=groups,
            group=MotorGroup.HEAD,
            key=7,
        )
