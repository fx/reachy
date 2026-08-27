"""Stable Home Assistant Configuration switches for confirmed motor groups."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast

# pylint: disable=no-name-in-module
from aioesphomeapi.api_pb2 import (  # type: ignore[attr-defined]  # generated protobuf module, which mypy cannot see the message classes inside
    ListEntitiesRequest,
    ListEntitiesSwitchResponse,
    SubscribeHomeAssistantStatesRequest,
    SwitchCommandRequest,
    SwitchStateResponse,
)
from aioesphomeapi.model import EntityCategory

from reachy_mini_ha_satellite.esphome.entity import ESPHomeEntity
from reachy_mini_ha_satellite.motor_control import MotorGroup, MotorGroupCoordinator

if TYPE_CHECKING:
    from collections.abc import Iterable

    from google.protobuf import message

    from reachy_mini_ha_satellite.esphome.api_server import APIServer
    from reachy_mini_ha_satellite.esphome.models import ServerState

__all__ = ["MOTOR_ENTITY_METADATA", "MotorSwitchEntity"]

# Object IDs are compatibility identifiers. Names and icons are presentation only.
MOTOR_ENTITY_METADATA: Final[dict[MotorGroup, tuple[str, str, str]]] = {
    MotorGroup.HEAD: ("head_motors", "Head Motors", "mdi:robot-industrial"),
    MotorGroup.BODY: ("body_motor", "Body Motor", "mdi:rotate-360"),
    MotorGroup.ANTENNAS: ("antenna_motors", "Antenna Motors", "mdi:antenna"),
}


class MotorSwitchEntity(ESPHomeEntity):
    """One Boolean switch backed only by correlated physical confirmation."""

    def __init__(
        self,
        *,
        state: ServerState,
        coordinator: MotorGroupCoordinator,
        group: MotorGroup,
        key: int,
        server: APIServer | None = None,
    ) -> None:
        """Bind a registered group to the existing Boolean switch messages."""
        ESPHomeEntity.__init__(self, cast("APIServer", server))
        if coordinator.last_confirmed(group) is None:
            raise ValueError("an unconfirmed motor group cannot have an entity")
        self._state = state
        self._coordinator = coordinator
        self._group = group
        self.key = key

    def state_message(self) -> SwitchStateResponse:
        """Return the retained last-confirmed Boolean for this registered group."""
        confirmed = self._coordinator.last_confirmed(self._group)
        if confirmed is None:
            raise RuntimeError("a registered motor group lost its confirmed value")
        return SwitchStateResponse(key=self.key, state=confirmed)

    def publish(self) -> None:
        """Broadcast only the retained value confirmed by completed worker I/O."""
        self._state.broadcast([self.state_message()])

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        """Reserve bounded work and immediately return retained state."""
        if isinstance(msg, ListEntitiesRequest):
            object_id, name, icon = MOTOR_ENTITY_METADATA[self._group]
            yield ListEntitiesSwitchResponse(
                object_id=object_id,
                key=self.key,
                name=name,
                entity_category=EntityCategory.CONFIG,
                icon=icon,
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            self._coordinator.reserve_refresh(self._group, self.publish)
            yield self.state_message()
        elif isinstance(msg, SwitchCommandRequest) and msg.key == self.key:
            self._coordinator.reserve_transition(
                self._group,
                bool(msg.state),
                self.publish,
            )
            yield self.state_message()
