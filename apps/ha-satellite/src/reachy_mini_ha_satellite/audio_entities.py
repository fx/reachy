"""The two speaker controls Home Assistant shows beside the microphone's.

Home Assistant already had a working volume: the media-player entity advertises
`VOLUME_SET` and `VOLUME_MUTE` and its `volume_level` round-trips to the speaker.
What it did not have was a **control in the device page's Configuration group**,
beside `Mic Volume` — where an operator looks for one — and it had no control at
all over the software boost that change 0016 made the robot's loudness come from.
This module is those two controls.

**Why here and not in `esphome/entity.py`.** That file is derived from the
upstream Linux voice assistant and its every departure from upstream is
enumerated in the `NOTICE` beside it, so a new class there is a new line in a
provenance record for code upstream never wrote. The directory holds exactly two
original files and both exist because *vendored code imports them*. Nothing
vendored imports these — `main.build_application` does — so they belong at the
package's top level, beside `wake_word.py`, which is the same arrangement for the
same reason.

**Neither entity ever sends anything unprompted.** Each one answers the message
it was handed and yields the responses that answer goes back in; the vendored
protocol layer's own fan-out is what delivers a message to every entity, and what
writes the replies. A future change that wants an asynchronous push should use
`ServerState.broadcast`, which reaches every connected client rather than
whichever connection an entity happens to hold.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast

# pylint: disable=no-name-in-module
from aioesphomeapi.api_pb2 import (  # type: ignore[attr-defined]  # generated protobuf module, which mypy cannot see the message classes inside
    ListEntitiesNumberResponse,
    ListEntitiesRequest,
    MediaPlayerCommandRequest,
    NumberCommandRequest,
    NumberStateResponse,
    SubscribeHomeAssistantStatesRequest,
)
from aioesphomeapi.model import EntityCategory, MediaPlayerCommand, NumberMode

from reachy_mini_ha_satellite.adapters.output_gain import (
    MAX_BOOST_PERCENT,
    MIN_BOOST_PERCENT,
)
from reachy_mini_ha_satellite.esphome.entity import ESPHomeEntity

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from google.protobuf import message

    from reachy_mini_ha_satellite.esphome.api_server import APIServer
    from reachy_mini_ha_satellite.esphome.entity import MediaPlayerEntity
    from reachy_mini_ha_satellite.esphome.models import ServerState

__all__ = ["SpeakerBoostNumberEntity", "SpeakerVolumeNumberEntity"]

# What Home Assistant keys each control on, after the device's own identity. The
# unique identifier it registers is `{mac}-{entity_type}-{object_id}`, so these
# two strings are the part of it this repository decides and the part that must
# never change: changing one detaches the entity's history exactly as changing
# the announced device name detaches the device's.
VOLUME_OBJECT_ID: Final = "speaker_volume"
BOOST_OBJECT_ID: Final = "speaker_boost"

# The slider's granularity. One percent for the volume, which is the resolution
# Home Assistant's own media-player control has; ten for the boost, whose useful
# range is 100 to 800 and which nobody tunes to the percent.
_VOLUME_STEP: Final = 1.0
_BOOST_STEP: Final = 10.0

_VOLUME_MINIMUM: Final = 0.0
_VOLUME_MAXIMUM: Final = 100.0

# What converts between the two units in play: the media player holds the level
# as a fraction of full scale, and this control offers it in percent. It is the
# same number as `_VOLUME_MAXIMUM` and it is not the same thing — that one is
# where the slider stops, and a slider that stopped at 50 would not change how a
# fraction becomes a percentage.
_PERCENT_PER_UNIT: Final = 100.0


def _clamp(value: float, low: float, high: float) -> float:
    """Bring a number inside a range.

    Args:
        value: What was asked for.
        low: The smallest permitted value.
        high: The largest permitted value.

    Returns:
        The value, or whichever bound it went past.
    """
    return max(low, min(high, value))


class SpeakerVolumeNumberEntity(ESPHomeEntity):
    """The speaker's volume, as a control rather than as a media-player attribute.

    It is the *same* volume: this reports and sets what the media-player entity
    reports and sets, and the two cannot disagree because both read one number —
    `ServerState.volume` — and both write it through the media player's own
    public methods. Nothing here is a second store of the level.

    **The guarantee is that the two controls always report the same level**, in
    every state including muted — not that this one reads 0 whenever the device
    is muted. `ServerState.volume * 100` is the one answer both of them give, so
    there is nothing to keep in step.

    **What muting does to it, which is what a reviewer will ask about.** The
    vendored MUTE branch sets `ServerState.volume` to 0, so this control reads 0
    too. A value then set *through this control* is **remembered rather than
    applied**: `MediaPlayerEntity.apply_volume_from_state` stores it into
    `previous_volume`, the guard below persists nothing, and unmuting restores
    it — so the control goes on reading 0 and Home Assistant's slider snaps back
    to it, which is honest, because 0 is the level in effect.

    A media-player `VOLUME_SET` arriving while muted is a **different path with a
    different outcome**, and this control does not pretend otherwise. The
    vendored `has_volume` branch applies that level to both outputs and persists
    it, leaving the mute flag set, so `ServerState.volume` is non-zero while
    `muted` is true. This control mirrors that level rather than contradicting
    the media player and the state both. What it leaves the vendored player in is
    recorded as a known limitation in change 0017; it is upstream behaviour in a
    file this repository does not edit, and this control neither causes nor
    worsens it.

    This never broadcasts. See the module docstring.
    """

    def __init__(
        self,
        *,
        state: ServerState,
        key: int,
        server: APIServer | None = None,
    ) -> None:
        """Wire the control to the state the media player already reads.

        Args:
            state: The vendored protocol layer's state. The media-player entity
                is resolved out of it at message time rather than held, because
                it does not exist until a `VoiceSatelliteProtocol` is
                constructed — which is when a client connects, long after this.
            key: The identifier Home Assistant addresses this entity by.
            server: The connection this entity was built from, when it was built
                from one. `None` in the composition root, which is where both of
                these are built; the base class annotates it as required because
                upstream builds every entity from inside a live connection, and
                nothing here ever reads it.
        """
        ESPHomeEntity.__init__(self, cast("APIServer", server))
        self.key = key
        self._state = state

    def _level(self) -> float:
        """Say the level in effect, in the percent Home Assistant asked for.

        Returns:
            `ServerState.volume` as a percentage, which is the same number the
            media-player entity reports in every state.
        """
        return float(self._state.volume) * _PERCENT_PER_UNIT

    def _level_after(
        self,
        msg: MediaPlayerCommandRequest,
        player: MediaPlayerEntity,
    ) -> float | None:
        """Work out what a media-player command leaves the level at.

        Derived from the request rather than read back from the media player,
        which is what makes this independent of whether the fan-out reached that
        entity before or after this one. The one thing read from post-command
        state below is `previous_volume`, and only in the UNMUTE branch — which
        is the one command that does not itself change it, so that read gives
        the same answer whichever of the two entities ran first.

        The branch order mirrors `MediaPlayerEntity.handle_message`: a media URL
        first, then a command, then a volume.

        Args:
            msg: The command Home Assistant sent to the media player.
            player: The media player it was addressed to.

        Returns:
            The level in percent, or `None` for a command that does not change
            it — a play, a pause, a stop, or anything else.
        """
        if msg.has_media_url:
            return None
        if msg.has_command:
            command = MediaPlayerCommand(msg.command)
            if command == MediaPlayerCommand.MUTE:
                return 0.0
            if command == MediaPlayerCommand.UNMUTE:
                return float(player.previous_volume) * _PERCENT_PER_UNIT
            return None
        if msg.has_volume:
            return float(msg.volume) * _PERCENT_PER_UNIT
        return None

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        """Answer one message from Home Assistant.

        There is no final `else` warning about an unrecognised message, and
        deliberately: the vendored protocol layer hands *every* entity every
        message of six types, so warning about the ones addressed elsewhere
        would be several log lines per connection saying nothing.

        Args:
            msg: What arrived.

        Yields:
            The responses that answer it.
        """
        if isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesNumberResponse(
                object_id=VOLUME_OBJECT_ID,
                key=self.key,
                name="Speaker Volume",
                min_value=_VOLUME_MINIMUM,
                max_value=_VOLUME_MAXIMUM,
                step=_VOLUME_STEP,
                unit_of_measurement="%",
                mode=NumberMode.SLIDER,
                entity_category=EntityCategory.CONFIG,
                icon="mdi:volume-high",
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            yield NumberStateResponse(key=self.key, state=self._level())
        elif isinstance(msg, NumberCommandRequest) and msg.key == self.key:
            player = self._state.media_player_entity
            if player is None:
                return
            fraction = (
                _clamp(float(msg.state), _VOLUME_MINIMUM, _VOLUME_MAXIMUM)
                / _PERCENT_PER_UNIT
            )
            player.apply_volume_from_state(fraction)
            # Guarded, because while muted this level belongs in
            # `previous_volume` and nowhere else: `apply_volume_from_state`
            # leaves the media player's own level where it was, so persisting
            # this one would move `ServerState.volume` away from it and the two
            # controls would then report different numbers.
            if not player.muted:
                self._state.persist_volume(fraction)
            yield NumberStateResponse(key=self.key, state=self._level())
            # The media player's own state, so Home Assistant's two views of one
            # level move together rather than one of them lagging until the next
            # subscription — and asked *of* the media player rather than
            # assembled on its behalf, so a field upstream adds to that message
            # arrives here too. A subscription request is the one branch of its
            # `handle_message` that yields its state message and nothing else.
            yield from player.handle_message(SubscribeHomeAssistantStatesRequest())
        elif isinstance(msg, MediaPlayerCommandRequest):
            player = self._state.media_player_entity
            if player is None or msg.key != player.key:
                return
            after = self._level_after(msg, player)
            if after is not None:
                yield NumberStateResponse(key=self.key, state=after)


class SpeakerBoostNumberEntity(ESPHomeEntity):
    """The software boost, as a control an operator can reach.

    Change 0016 measured the robot's one hardware playback control already at
    `0.00dB`, so the loudness comes from multiplying the samples — and until now
    the multiplier was configuration and nothing else. This is the control its
    own non-goals promised as the follow-up.

    Injected callables rather than the application itself, exactly as
    `MicSettingEntity` takes its pair: `main` imports this module, so this module
    importing `main` would be a cycle, and two closures keep the class free of
    input and output so its tests need no filesystem.

    **The response carries a read-back, not an echo.** What is yielded after a
    set is what the getter says afterwards, so a value the setter refused — an
    overrides file that cannot be written is the case that exists — is reported
    as the value actually in effect rather than as the one that was asked for.

    This never broadcasts. See the module docstring.
    """

    def __init__(
        self,
        *,
        key: int,
        get_percent: Callable[[], float],
        set_percent: Callable[[float], None],
        server: APIServer | None = None,
    ) -> None:
        """Wire the control to whatever reads and writes the boost.

        Args:
            key: The identifier Home Assistant addresses this entity by.
            get_percent: What the boost is now. Read at message time rather than
                once, so a boost changed from the settings page is what Home
                Assistant is told.
            set_percent: How to change it. Whatever this does with the value is
                the setter's business; this class only clamps and reads back.
            server: The connection this entity was built from, when it was built
                from one. `None` in the composition root, and never read — see
                `SpeakerVolumeNumberEntity.__init__`.
        """
        ESPHomeEntity.__init__(self, cast("APIServer", server))
        self.key = key
        self._get_percent = get_percent
        self._set_percent = set_percent

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        """Answer one message from Home Assistant.

        Args:
            msg: What arrived.

        Yields:
            The responses that answer it.
        """
        if isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesNumberResponse(
                object_id=BOOST_OBJECT_ID,
                key=self.key,
                name="Speaker Boost",
                min_value=MIN_BOOST_PERCENT,
                max_value=MAX_BOOST_PERCENT,
                step=_BOOST_STEP,
                unit_of_measurement="%",
                mode=NumberMode.SLIDER,
                entity_category=EntityCategory.CONFIG,
                icon="mdi:volume-vibrate",
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            yield NumberStateResponse(key=self.key, state=self._get_percent())
        elif isinstance(msg, NumberCommandRequest) and msg.key == self.key:
            self._set_percent(
                _clamp(float(msg.state), MIN_BOOST_PERCENT, MAX_BOOST_PERCENT),
            )
            yield NumberStateResponse(key=self.key, state=self._get_percent())
