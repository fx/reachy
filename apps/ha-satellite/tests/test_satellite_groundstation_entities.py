"""The Home Assistant read-back of the groundstation address.

The entity half of REQ-095's "either configuration surface" and "read-back"
claims: a stable Configuration text control declaring the shared 255-character
maximum, reporting only the address in effect, and routed to by the vendored
dispatcher — which upstream never had a reason to route, since it declares no
text entity.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

# pylint: disable=no-name-in-module
from aioesphomeapi.api_pb2 import (  # type: ignore[attr-defined]  # generated protobuf module, which mypy cannot see the message classes inside
    ListEntitiesRequest,
    ListEntitiesTextResponse,
    SubscribeHomeAssistantStatesRequest,
    TextCommandRequest,
    TextStateResponse,
)
from aioesphomeapi.model import EntityCategory, TextMode
from satellite_support import address_of_length, connected, vendored_server_state
from test_satellite_groundstation_url import (
    ENVIRONMENT,
    FIRST_URL,
    SECOND_URL,
    FakeFactory,
    FakeSource,
    build_owner,
    stored,
)

from reachy_mini_ha_satellite.config import (
    GROUNDSTATION_URL_MAX_LENGTH,
    GROUNDSTATION_URL_SETTING,
    OverrideStore,
    load_settings,
)
from reachy_mini_ha_satellite.esphome.satellite import VoiceSatelliteProtocol
from reachy_mini_ha_satellite.groundstation_entities import (
    GROUNDSTATION_URL_OBJECT_ID,
    GroundstationUrlTextEntity,
)

if TYPE_CHECKING:
    from pyfakefs.fake_filesystem import FakeFilesystem

    from reachy_mini_ha_satellite.esphome.models import ServerState
    from reachy_mini_ha_satellite.groundstation_url import GroundstationUrlOwner

_KEY: Final = 11


def build_entity(
    *,
    factory: FakeFactory,
    initial: FakeSource | None,
) -> tuple[GroundstationUrlTextEntity, GroundstationUrlOwner, ServerState]:
    """Build the control over a real owner and a vendored server state.

    Args:
        factory: What builds replacements.
        initial: The source the composition root built.

    Returns:
        The entity, the owner behind it and the state a push goes out over.
    """
    owner, _source, _store = build_owner(factory=factory, initial=initial)
    state = vendored_server_state()
    entity = GroundstationUrlTextEntity(state=state, owner=owner, key=_KEY)
    owner.publish_changes(entity.publish)
    return entity, owner, state


async def settle() -> None:
    """Let a reserved submission run to completion."""
    for _ in range(8):
        await asyncio.sleep(0)


class TestTheAnnouncedControl:
    """What Home Assistant is told this control is."""

    def test_it_is_a_configuration_text_control_at_the_shared_maximum(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """Object ID, category and maximum are the whole of the announcement.

        Args:
            fs: The in-memory filesystem the overrides file lives in.
        """
        del fs
        entity, _owner, _state = build_entity(
            factory=FakeFactory(FakeSource(SECOND_URL)),
            initial=FakeSource(FIRST_URL),
        )

        (announced,) = list(entity.handle_message(ListEntitiesRequest()))

        assert isinstance(announced, ListEntitiesTextResponse)
        assert announced.object_id == GROUNDSTATION_URL_OBJECT_ID
        assert announced.key == _KEY
        assert announced.max_length == GROUNDSTATION_URL_MAX_LENGTH
        assert announced.mode == TextMode.TEXT
        assert announced.entity_category == EntityCategory.CONFIG

    def test_the_object_id_is_the_one_the_specification_fixes(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """It is a compatibility identifier, so it is pinned rather than derived.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        assert GROUNDSTATION_URL_OBJECT_ID == "groundstation_url"

    def test_a_subscription_reports_the_address_in_effect(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """Read out of the owner at message time, never captured at build time.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        entity, _owner, _state = build_entity(
            factory=FakeFactory(FakeSource(SECOND_URL)),
            initial=FakeSource(FIRST_URL),
        )

        (reported,) = list(
            entity.handle_message(SubscribeHomeAssistantStatesRequest()),
        )

        assert isinstance(reported, TextStateResponse)
        assert reported.state == FIRST_URL

    def test_a_message_addressed_elsewhere_is_ignored(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """Every entity is handed every message; only one of them owns this key.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        entity, owner, _state = build_entity(
            factory=FakeFactory(FakeSource(SECOND_URL)),
            initial=FakeSource(FIRST_URL),
        )

        assert (
            list(
                entity.handle_message(
                    TextCommandRequest(key=_KEY + 1, state=SECOND_URL)
                )
            )
            == []
        )
        assert owner.effective_url == FIRST_URL


class TestTheReadBack:
    """REQ-095: what the control reports is what is in effect."""

    @pytest.mark.asyncio
    async def test_an_accepted_address_is_pushed_once_it_is_durable(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """The reply carries the preceding value; the push carries the new one.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        entity, owner, state = build_entity(
            factory=FakeFactory(FakeSource(SECOND_URL)),
            initial=FakeSource(FIRST_URL),
        )
        clients = connected(state, 2)

        (replied,) = list(
            entity.handle_message(TextCommandRequest(key=_KEY, state=SECOND_URL)),
        )
        assert isinstance(replied, TextStateResponse)
        assert replied.state == FIRST_URL

        await settle()

        assert owner.effective_url == SECOND_URL
        pushed = [
            message.state
            for client in clients
            for message in client.sent
            if isinstance(message, TextStateResponse)
        ]
        assert pushed == [SECOND_URL, SECOND_URL]

    @pytest.mark.asyncio
    async def test_an_overlong_address_leaves_the_preceding_value_reported(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """Refused before any source construction or durable write.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        factory = FakeFactory(FakeSource(SECOND_URL))
        entity, owner, state = build_entity(
            factory=factory,
            initial=FakeSource(FIRST_URL),
        )
        clients = connected(state)

        (replied,) = list(
            entity.handle_message(
                TextCommandRequest(key=_KEY, state=address_of_length(256)),
            ),
        )
        await settle()

        assert isinstance(replied, TextStateResponse)
        assert replied.state == FIRST_URL
        assert owner.effective_url == FIRST_URL
        assert factory.asked == []
        assert clients[0].sent == []

    @pytest.mark.asyncio
    async def test_a_refused_replacement_corrects_the_control(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """A control that optimistically moved is told the effective value.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        entity, owner, state = build_entity(
            factory=FakeFactory(
                FakeSource(SECOND_URL, start_error=RuntimeError("refused")),
                FakeSource(FIRST_URL),
            ),
            initial=FakeSource(FIRST_URL),
        )
        clients = connected(state)

        list(entity.handle_message(TextCommandRequest(key=_KEY, state=SECOND_URL)))
        await settle()

        assert owner.effective_url == FIRST_URL
        pushed = [
            message.state
            for message in clients[0].sent
            if isinstance(message, TextStateResponse)
        ]
        assert pushed == [FIRST_URL]

    @pytest.mark.asyncio
    async def test_an_accepted_address_survives_a_restart(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """REQ-095's restart scenario, from the Home Assistant surface.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        entity, _owner, _state = build_entity(
            factory=FakeFactory(FakeSource(SECOND_URL)),
            initial=FakeSource(FIRST_URL),
        )

        list(entity.handle_message(TextCommandRequest(key=_KEY, state=SECOND_URL)))
        await settle()

        store = OverrideStore(Path("/reachy-satellite-url/settings.json"))
        assert stored(store)[GROUNDSTATION_URL_SETTING] == SECOND_URL
        restarted = load_settings(ENVIRONMENT, store.load())
        assert restarted.settings.groundstation_url == SECOND_URL


class TestVendoredRouting:
    """The one-name delta the `esphome/NOTICE` records.

    Without it the control is announced, subscribed to and inert: the dispatcher
    would drop every `TextCommandRequest` before any entity saw it.
    """

    @pytest.mark.asyncio
    async def test_a_text_command_reaches_the_entities(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """The routed branch is the existing entity fan-out, unchanged.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        entity, _owner, state = build_entity(
            factory=FakeFactory(FakeSource(SECOND_URL)),
            initial=FakeSource(FIRST_URL),
        )
        state.entities.append(entity)
        protocol = VoiceSatelliteProtocol(state)

        replies = list(
            protocol.handle_message(TextCommandRequest(key=_KEY, state=SECOND_URL)),
        )

        assert [
            message.state
            for message in replies
            if isinstance(message, TextStateResponse)
        ] == [FIRST_URL]
        await settle()
