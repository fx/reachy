"""The groundstation address, as a Home Assistant Configuration control.

Beside `audio_entities.py` and `motor_entities.py` rather than inside the
vendored ESPHome directory, for the reason `audio_entities.py` records: nothing
vendored imports this, so a class here is not a line in a provenance record for
code upstream never wrote.

**It reports the address in effect, never the one that was asked for.** A
submission is refused before anything is built or written when it is too long,
and compensated back to the preceding address when a step of the replacement
fails — so the value this entity yields is read out of the owner after the fact
rather than echoed from the request. `publish` is what corrects a Home Assistant
control that optimistically moved, and the owner calls it after a commit and
after an asynchronous refusal alike.

**The declared maximum is the shared one.** An ESPHome text state carries at
most 255 characters, which is why that number is the bound everywhere; declaring
it here from `config.GROUNDSTATION_URL_MAX_LENGTH` is what stops the control
offering a field the settings model would refuse.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast

# pylint: disable=no-name-in-module
from aioesphomeapi.api_pb2 import (  # type: ignore[attr-defined]  # generated protobuf module, which mypy cannot see the message classes inside
    ListEntitiesRequest,
    ListEntitiesTextResponse,
    SubscribeHomeAssistantStatesRequest,
    TextCommandRequest,
    TextStateResponse,
)
from aioesphomeapi.model import EntityCategory, TextMode

from reachy_mini_ha_satellite.config import GROUNDSTATION_URL_MAX_LENGTH
from reachy_mini_ha_satellite.esphome.entity import ESPHomeEntity

if TYPE_CHECKING:
    from collections.abc import Iterable

    from google.protobuf import message

    from reachy_mini_ha_satellite.esphome.api_server import APIServer
    from reachy_mini_ha_satellite.esphome.models import ServerState
    from reachy_mini_ha_satellite.groundstation_url import GroundstationUrlOwner

__all__ = ["GROUNDSTATION_URL_OBJECT_ID", "GroundstationUrlTextEntity"]

# What Home Assistant keys this control on, after the device's own identity. A
# compatibility identifier rather than a display label: changing it detaches the
# entity's history exactly as changing the announced device name detaches the
# device's.
GROUNDSTATION_URL_OBJECT_ID: Final = "groundstation_url"

# No pattern is declared on the wire. The scheme and credential-exclusion half
# of the URL contract is `reachy_session_client.validate_session_url`, which
# `config._check_session_url` calls on the resolved candidate; a regular
# expression here would be a second copy of that rule, free to drift from the
# one the client actually applies when it connects.
_MINIMUM_LENGTH: Final = 0


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
class GroundstationUrlTextEntity(ESPHomeEntity):
    """One stable text control over the address the owner holds."""

    def __init__(
        self,
        *,
        state: ServerState,
        owner: GroundstationUrlOwner,
        key: int,
        server: APIServer | None = None,
    ) -> None:
        """Wire the control to the owner of the address and its source.

        Args:
            state: The vendored protocol layer's state, which is what a push
                goes out over: `broadcast` reaches every connected client, where
                `server` would reach one connection at best.
            owner: What holds the address in effect and performs a replacement.
            key: The identifier Home Assistant addresses this entity by.
            server: The connection this entity was built from, when it was built
                from one. `None` in the composition root, and never read — see
                `audio_entities.SpeakerVolumeNumberEntity.__init__`.
        """
        ESPHomeEntity.__init__(self, cast("APIServer", server))
        self._state = state
        self._owner = owner
        self.key = key

    def state_message(self) -> TextStateResponse:
        """Say what address is in effect, in the message it is read from.

        One definition, used by every branch below and by `publish`, so a pushed
        read-back and a polled one cannot come to differ.

        Returns:
            The state response carrying the durable, effective address.
        """
        return TextStateResponse(key=self.key, state=self._owner.effective_url)

    def publish(self) -> None:
        """Push the address in effect to every connected client.

        Registered with the owner by the composition root, and called by it
        after a commit and after a refusal that was decided asynchronously. A
        robot nobody is connected to broadcasts to nothing.
        """
        self._state.broadcast([self.state_message()])

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        """Answer one message from Home Assistant.

        There is no final `else` warning about an unrecognised message, for the
        reason `audio_entities` records: every entity is handed every message of
        several types, so warning about the ones addressed elsewhere would be
        log lines per connection saying nothing.

        Args:
            msg: What arrived.

        Yields:
            The responses that answer it, always carrying the effective address
            rather than a requested one.
        """
        if isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesTextResponse(
                object_id=GROUNDSTATION_URL_OBJECT_ID,
                key=self.key,
                name="Groundstation URL",
                min_length=_MINIMUM_LENGTH,
                max_length=GROUNDSTATION_URL_MAX_LENGTH,
                mode=TextMode.TEXT,
                entity_category=EntityCategory.CONFIG,
                icon="mdi:server-network",
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            yield self.state_message()
        elif isinstance(msg, TextCommandRequest) and msg.key == self.key:
            # Reserved rather than awaited: this runs in the protocol's message
            # loop, and retiring a session and opening another is not work to
            # hold it for. The reply is the address still in effect, which a
            # refusal leaves it at and a success replaces through `publish`.
            self._owner.reserve_submission(str(msg.state))
            yield self.state_message()
