"""Home Assistant Boolean entity tests for confirmed physical motor state."""

from __future__ import annotations

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
from satellite_support import FakeRobot, ManualClock, connected, vendored_server_state

from reachy_mini_ha_satellite.motor_control import (
    HEAD_MOTOR_IDS,
    MotorConfirmation,
    MotorConfirmationOutcome,
    MotorEvidence,
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


def entity(
    robot: FakeRobot | None = None,
) -> tuple[MotorSwitchEntity, MotorGroupCoordinator, FakeRobot]:
    """Build a registered head switch over a real vendored server state."""
    handle = robot or FakeRobot()
    groups = MotorGroupCoordinator(handle, clock=ManualClock())
    assert MotorGroup.HEAD in groups.initialize()
    state = vendored_server_state()
    return (
        MotorSwitchEntity(
            state=state,
            coordinator=groups,
            group=MotorGroup.HEAD,
            key=7,
        ),
        groups,
        handle,
    )


def test_entity_has_stable_configuration_identity_and_reconnect_state() -> None:
    """The same object ID and physical Boolean survive every protocol listing."""
    control, _groups, _robot = entity()

    first = list(control.handle_message(ListEntitiesRequest()))
    second = list(control.handle_message(ListEntitiesRequest()))
    reconnect = list(control.handle_message(SubscribeHomeAssistantStatesRequest()))

    assert first == second
    listing = cast("ListEntitiesSwitchResponse", first[0])
    assert listing.object_id == "head_motors"
    assert listing.name == "Head Motors"
    assert listing.entity_category == EntityCategory.CONFIG
    assert reconnect == [SwitchStateResponse(key=7, state=True)]


def test_agreeing_command_replies_with_confirmed_physical_boolean() -> None:
    """The response is daemon read-back, not the request or no-exception optimism."""
    control, groups, robot = entity()
    robot.motor_disables_confirmed.append(complete(False))

    responses = list(control.handle_message(SwitchCommandRequest(key=7, state=False)))

    assert responses == [SwitchStateResponse(key=7, state=False)]
    assert groups.last_confirmed(MotorGroup.HEAD) is False


def test_failed_command_replies_only_with_retained_boolean() -> None:
    """Missing confirmation snaps Home Assistant back to the last known value."""
    control, groups, robot = entity()
    robot.motor_disables_confirmed.append(MotorConfirmation.failed())

    responses = list(control.handle_message(SwitchCommandRequest(key=7, state=False)))

    assert responses == [SwitchStateResponse(key=7, state=True)]
    assert groups.last_confirmed(MotorGroup.HEAD) is True
    assert not groups.gate_open(MotorGroup.HEAD)


def test_contradiction_replies_with_actual_readback_and_closes_gate() -> None:
    """A physical contradiction updates the Boolean while refusing the outcome."""
    control, groups, robot = entity()
    robot.motor_disables_confirmed.append(complete(True, contradicted=True))

    responses = list(control.handle_message(SwitchCommandRequest(key=7, state=False)))

    assert responses == [SwitchStateResponse(key=7, state=True)]
    assert not groups.gate_open(MotorGroup.HEAD)


def test_later_independent_read_publishes_to_every_connection() -> None:
    """Fresh evidence after a failure can advance state without replaying a request."""
    control, groups, robot = entity()
    clients = connected(
        control._state, 2
    )  # exercise the entity's actual vendored broadcast owner
    robot.motor_disables_confirmed.append(MotorConfirmation.failed())
    list(control.handle_message(SwitchCommandRequest(key=7, state=False)))
    robot.motor_reads.append(complete(False))

    assert control.refresh()
    assert groups.last_confirmed(MotorGroup.HEAD) is False
    for client in clients:
        assert client.sent == [SwitchStateResponse(key=7, state=False)]


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
    state = vendored_server_state()

    with pytest.raises(ValueError, match="unconfirmed"):
        MotorSwitchEntity(
            state=state,
            coordinator=groups,
            group=MotorGroup.HEAD,
            key=7,
        )
