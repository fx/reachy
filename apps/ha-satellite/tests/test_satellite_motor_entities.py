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
from satellite_support import FakeRobot, ManualClock

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


def entity(
    robot: FakeRobot | None = None,
) -> tuple[MotorSwitchEntity, MotorGroupCoordinator, FakeRobot]:
    """Build a registered head switch over deterministic daemon evidence."""
    handle = robot or FakeRobot()
    groups = MotorGroupCoordinator(handle, clock=ManualClock())
    assert MotorGroup.HEAD in groups.initialize()
    return (
        MotorSwitchEntity(
            coordinator=groups,
            group=MotorGroup.HEAD,
            key=7,
        ),
        groups,
        handle,
    )


def test_entity_has_stable_configuration_identity_and_reconnect_state() -> None:
    """The same object ID and physical Boolean survive every protocol listing."""
    control, _groups, robot = entity()
    before = len(robot.motor_requests)

    first = list(control.handle_message(ListEntitiesRequest()))
    second = list(control.handle_message(ListEntitiesRequest()))
    assert len(robot.motor_requests) == before
    reconnect = list(control.handle_message(SubscribeHomeAssistantStatesRequest()))

    assert first == second
    listing = cast("ListEntitiesSwitchResponse", first[0])
    assert listing.object_id == "head_motors"
    assert listing.name == "Head Motors"
    assert listing.entity_category == EntityCategory.CONFIG
    assert reconnect == [SwitchStateResponse(key=7, state=True)]
    assert robot.motor_requests[before:] == [("read", HEAD_MOTOR_IDS)]


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
    robot.motor_reads.append(MotorConfirmation.failed())
    before = len(robot.motor_requests)

    responses = list(control.handle_message(SwitchCommandRequest(key=7, state=False)))

    assert responses == [SwitchStateResponse(key=7, state=True)]
    assert groups.last_confirmed(MotorGroup.HEAD) is True
    assert not groups.gate_open(MotorGroup.HEAD)
    assert robot.motor_requests[before:] == [
        ("disable", HEAD_MOTOR_IDS),
        ("read", HEAD_MOTOR_IDS),
    ]


def test_mixed_success_and_error_never_publishes_requested_state() -> None:
    """One errored motor invalidates otherwise agreeing physical evidence."""
    control, groups, robot = entity()
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
    assert groups.last_confirmed(MotorGroup.HEAD) is True
    assert not groups.gate_open(MotorGroup.HEAD)


def test_contradiction_replies_with_actual_readback_and_closes_gate() -> None:
    """A physical contradiction updates the Boolean while refusing the outcome."""
    control, groups, robot = entity()
    robot.motor_disables_confirmed.append(complete(True, contradicted=True))

    responses = list(control.handle_message(SwitchCommandRequest(key=7, state=False)))

    assert responses == [SwitchStateResponse(key=7, state=True)]
    assert not groups.gate_open(MotorGroup.HEAD)


def test_failed_command_performs_one_refresh_and_yields_fresh_state() -> None:
    """The bounded recovery read advances state without an optimistic response."""
    control, groups, robot = entity()
    robot.motor_disables_confirmed.append(MotorConfirmation.failed())
    robot.motor_reads.append(complete(False))
    before = len(robot.motor_requests)

    responses = list(control.handle_message(SwitchCommandRequest(key=7, state=False)))

    assert responses == [SwitchStateResponse(key=7, state=False)]
    assert groups.last_confirmed(MotorGroup.HEAD) is False
    assert robot.motor_requests[before:] == [
        ("disable", HEAD_MOTOR_IDS),
        ("read", HEAD_MOTOR_IDS),
    ]


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
            coordinator=groups,
            group=MotorGroup.HEAD,
            key=7,
        )
