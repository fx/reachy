"""The two speaker controls: what they declare, what they report, what they set.

Every test here drives the real entity classes, and the ones about ordering drive
them through the real `VoiceSatelliteProtocol` fan-out rather than by calling
`handle_message` twice by hand — because what those tests are about is the
fan-out, and a hand-rolled loop would be a test of the loop the test wrote.

Nothing sleeps and no socket is opened. Two tests do read a file: the volume
control persists through the vendored `ServerState.persist_volume`, so what is on
disk afterwards is the assertion that the persistence happened rather than a
description of it. That file is in the fake filesystem the `tmp_path` fixture
installs, so no unit-test rule is bent.

The state and the protocol are built with `vendored_server_state` and the real
`VoiceSatelliteProtocol` rather than with `make_satellite`, which the carried
tests use. `make_satellite` builds its own state, and half of the tests here need
entities appended to a state *before* the protocol is constructed over it — which
is what the composition root does. Constructing the protocol directly is the only
way to stand at that point.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import pytest
from aioesphomeapi.api_pb2 import (  # type: ignore[attr-defined]  # generated protobuf module, which mypy cannot see the message classes inside
    ListEntitiesNumberResponse,
    ListEntitiesRequest,
    MediaPlayerCommandRequest,
    MediaPlayerStateResponse,
    NumberCommandRequest,
    NumberStateResponse,
    SubscribeHomeAssistantStatesRequest,
)
from aioesphomeapi.model import EntityCategory, MediaPlayerCommand, NumberMode
from satellite_support import FakePlayback, vendored_server_state

from reachy_mini_ha_satellite.adapters.output_gain import (
    MAX_BOOST_PERCENT,
    MIN_BOOST_PERCENT,
)
from reachy_mini_ha_satellite.audio_entities import (
    BOOST_OBJECT_ID,
    VOLUME_OBJECT_ID,
    SpeakerBoostNumberEntity,
    SpeakerVolumeNumberEntity,
)
from reachy_mini_ha_satellite.esphome.entity import MediaPlayerEntity
from reachy_mini_ha_satellite.esphome.satellite import VoiceSatelliteProtocol

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from google.protobuf import message

    from reachy_mini_ha_satellite.esphome.models import ServerState

# What the boost setter is handed when a test does not care about the value.
_A_BOOST: Final = 300.0


def _handled(
    entity: SpeakerVolumeNumberEntity | SpeakerBoostNumberEntity,
    msg: message.Message,
) -> list[message.Message]:
    """Hand one message to one entity and collect what it answers with.

    Args:
        entity: The control under test.
        msg: What arrived.

    Returns:
        Every response, in order.
    """
    return list(entity.handle_message(msg))


def _only[Message](messages: list[message.Message], kind: type[Message]) -> Message:
    """Pick the one response of a given type out of a batch.

    Args:
        messages: What was answered.
        kind: The response type wanted.

    Returns:
        The single response of that type.
    """
    matching = [msg for msg in messages if isinstance(msg, kind)]
    assert len(matching) == 1, f"expected one {kind.__name__}, got {len(matching)}"
    return matching[0]


def _boost(
    *,
    key: int = 0,
    initial: float = _A_BOOST,
) -> tuple[SpeakerBoostNumberEntity, list[float]]:
    """Build a boost control over a setter that records and a getter that reads it.

    Args:
        key: The identifier Home Assistant would address it by.
        initial: What the boost is before anything sets it.

    Returns:
        The control, and the list every set appends to — whose last entry is
        also what the getter reports, so the read-back is real rather than a
        constant.
    """
    written = [initial]
    return (
        SpeakerBoostNumberEntity(
            key=key,
            get_percent=lambda: written[-1],
            set_percent=written.append,
        ),
        written,
    )


class TestWhatTheControlsDeclareToHomeAssistant:
    """They are Numbers in the Configuration group, which is the whole point."""

    def test_the_volume_declares_a_percentage_slider(self) -> None:
        """Beside `Mic Volume`, where an operator looks for a speaker volume."""
        entity = SpeakerVolumeNumberEntity(state=vendored_server_state(), key=3)

        declared = _only(
            _handled(entity, ListEntitiesRequest()),
            ListEntitiesNumberResponse,
        )

        assert declared.object_id == VOLUME_OBJECT_ID
        assert declared.key == 3
        assert declared.name == "Speaker Volume"
        assert declared.min_value == pytest.approx(0.0)
        assert declared.max_value == pytest.approx(100.0)
        assert declared.step == pytest.approx(1.0)
        assert declared.unit_of_measurement == "%"
        assert declared.mode == NumberMode.SLIDER
        assert declared.entity_category == EntityCategory.CONFIG
        assert declared.icon == "mdi:volume-high"

    def test_the_boost_declares_the_range_the_gain_module_bounds(self) -> None:
        """Imported rather than restated, so the two cannot drift apart."""
        entity, _written = _boost(key=4)

        declared = _only(
            _handled(entity, ListEntitiesRequest()),
            ListEntitiesNumberResponse,
        )

        assert declared.object_id == BOOST_OBJECT_ID
        assert declared.key == 4
        assert declared.name == "Speaker Boost"
        assert declared.min_value == pytest.approx(MIN_BOOST_PERCENT)
        assert declared.max_value == pytest.approx(MAX_BOOST_PERCENT)
        assert declared.step == pytest.approx(10.0)
        assert declared.unit_of_measurement == "%"
        assert declared.mode == NumberMode.SLIDER
        assert declared.entity_category == EntityCategory.CONFIG
        assert declared.icon == "mdi:volume-vibrate"


class TestWhatTheControlsReportOnSubscribing:
    """The value in effect now, not the one they were built with."""

    @pytest.mark.parametrize("volume", [0.0, 0.5, 1.0])
    def test_the_volume_tracks_the_one_number_the_media_player_reads(
        self,
        volume: float,
    ) -> None:
        """`ServerState.volume` is the single source, so the two always agree.

        Args:
            volume: The level in effect, from 0.0 to 1.0.
        """
        state = vendored_server_state(volume=volume)
        entity = SpeakerVolumeNumberEntity(state=state, key=0)

        reported = _only(
            _handled(entity, SubscribeHomeAssistantStatesRequest()),
            NumberStateResponse,
        )

        assert reported.state == pytest.approx(volume * 100.0)

    def test_the_boost_reports_what_the_getter_says_now(self) -> None:
        """A snapshot taken at construction would report a stale value for ever."""
        entity, written = _boost(initial=200.0)
        written.append(650.0)

        reported = _only(
            _handled(entity, SubscribeHomeAssistantStatesRequest()),
            NumberStateResponse,
        )

        assert reported.state == pytest.approx(650.0)


class TestSettingTheVolumeFromTheControl:
    """It reaches the speaker, it is persisted, and both views are told."""

    def test_it_sets_both_outputs_and_answers_with_both_states(
        self,
        control: _Control,
        tmp_path: Path,
    ) -> None:
        """Home Assistant's two views of one level must not fall out of step.

        Args:
            control: The control, wired as the robot wires it.
            tmp_path: The directory the preferences file is written into.
        """
        answered = _handled(
            control.entity,
            NumberCommandRequest(key=control.entity.key, state=40.0),
        )

        assert control.music.volume == pytest.approx(40.0)
        assert control.speech.volume == pytest.approx(40.0)
        assert control.state.preferences.volume == pytest.approx(0.4)
        stored = json.loads((tmp_path / "preferences.json").read_text(encoding="utf-8"))
        assert stored["volume"] == pytest.approx(0.4)
        assert _only(answered, NumberStateResponse).state == pytest.approx(40.0)
        player_state = _only(answered, MediaPlayerStateResponse)
        assert player_state.key == control.player.key
        assert player_state.volume == pytest.approx(0.4)
        assert not player_state.muted

    @pytest.mark.parametrize(
        ("asked", "expected"),
        [(-30.0, 0.0), (140.0, 100.0)],
    )
    def test_a_level_outside_the_slider_is_brought_back_inside_it(
        self,
        control: _Control,
        asked: float,
        expected: float,
    ) -> None:
        """A client is not the place a range is trusted from.

        Args:
            control: The control, wired as the robot wires it.
            asked: What arrived.
            expected: What should be in effect afterwards, in percent.
        """
        answered = _handled(
            control.entity,
            NumberCommandRequest(key=control.entity.key, state=asked),
        )

        assert _only(answered, NumberStateResponse).state == pytest.approx(expected)
        assert control.state.volume == pytest.approx(expected / 100.0)

    def test_a_command_for_another_entitys_key_is_ignored(
        self,
        control: _Control,
    ) -> None:
        """Every entity is handed every command, so most of them are not ours.

        Args:
            control: The control, wired as the robot wires it.
        """
        assert _handled(control.entity, NumberCommandRequest(key=9, state=10.0)) == []
        assert control.state.volume == pytest.approx(1.0)


class TestTheMediaPlayersOwnCommandsMoveTheControl:
    """A level set from the media-player card has to move the slider too."""

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            (MediaPlayerCommand.MUTE, 0.0),
            (MediaPlayerCommand.UNMUTE, 100.0),
        ],
    )
    def test_muting_and_unmuting_are_reported(
        self,
        control: _Control,
        command: MediaPlayerCommand,
        expected: float,
    ) -> None:
        """Muted is a level of zero, and unmuting restores what was remembered.

        Args:
            control: The control, wired as the robot wires it.
            command: What Home Assistant sent the media player.
            expected: The level the control should report, in percent.
        """
        answered = _handled(
            control.entity,
            MediaPlayerCommandRequest(
                key=control.player.key,
                has_command=True,
                command=command,
            ),
        )

        assert _only(answered, NumberStateResponse).state == pytest.approx(expected)

    def test_a_volume_set_on_the_media_player_is_reported(
        self,
        control: _Control,
    ) -> None:
        """Derived from the request, which is what makes the order not matter.

        Args:
            control: The control, wired as the robot wires it.
        """
        answered = _handled(
            control.entity,
            MediaPlayerCommandRequest(
                key=control.player.key,
                has_volume=True,
                volume=0.25,
            ),
        )

        assert _only(answered, NumberStateResponse).state == pytest.approx(25.0)

    @pytest.mark.parametrize(
        "build",
        [
            # A pause is not a volume change, and reporting one would be noise.
            lambda key: MediaPlayerCommandRequest(
                key=key,
                has_command=True,
                command=MediaPlayerCommand.PAUSE,
            ),
            # Playing something does not move the slider, so nothing is
            # reported.
            lambda key: MediaPlayerCommandRequest(
                key=key,
                has_media_url=True,
                media_url="http://192.0.2.10/tts.mp3",
            ),
            # A client may send a command carrying nothing at all; guessing a
            # level from it would be inventing one.
            lambda key: MediaPlayerCommandRequest(key=key),
            # Every entity is handed every command, most of them somebody
            # else's — so one addressed to a different media player is not this
            # control's business, even though it does carry a volume.
            lambda key: MediaPlayerCommandRequest(
                key=key + 10,
                has_volume=True,
                volume=0.5,
            ),
        ],
        ids=("a pause", "a media url", "nothing at all", "another media player"),
    )
    def test_a_command_that_moves_no_level_is_answered_with_nothing(
        self,
        control: _Control,
        build: Callable[[int], MediaPlayerCommandRequest],
    ) -> None:
        """Each of these reaches the control and none of them moves the slider.

        Args:
            control: The control, wired as the robot wires it.
            build: Makes the command, from the media player's own key — which
                is not known until the protocol has built one.
        """
        assert _handled(control.entity, build(control.player.key)) == []

    @pytest.mark.parametrize(
        "msg",
        [
            NumberCommandRequest(key=0, state=40.0),
            MediaPlayerCommandRequest(key=0, has_volume=True, volume=0.5),
        ],
    )
    def test_nothing_happens_before_a_connection_has_built_a_media_player(
        self,
        msg: message.Message,
    ) -> None:
        """The control is registered in the composition root, which is earlier.

        A command cannot actually arrive then — nothing is connected — but the
        window exists, and the alternative to answering it with nothing is an
        attribute error inside the protocol's message loop.

        Args:
            msg: A command addressed to this control's key.
        """
        entity = SpeakerVolumeNumberEntity(state=vendored_server_state(), key=0)

        assert _handled(entity, msg) == []

    @pytest.mark.parametrize("ours_first", [True, False])
    def test_the_answer_does_not_depend_on_where_in_the_fan_out_we_sit(
        self,
        tmp_path: Path,
        ours_first: bool,
    ) -> None:
        """The vendored layer hands every entity every message, in list order.

        Ours is appended in the composition root, before the protocol exists,
        so it sits *before* the media player — which the protocol appends. A
        reader should not have to work out whether that matters, so this drives
        the real fan-out both ways round and requires the same answer.

        It builds its own state rather than taking the `control` fixture,
        because the registration order is the thing under test.

        Args:
            tmp_path: An empty directory in a fake filesystem.
            ours_first: Whether the control is registered before the protocol
                builds the media player, as the composition root does.
        """
        state = vendored_server_state(tmp_path=tmp_path)
        if ours_first:
            state.entities.append(
                SpeakerVolumeNumberEntity(state=state, key=len(state.entities)),
            )
            VoiceSatelliteProtocol(state)
        else:
            VoiceSatelliteProtocol(state)
            state.entities.append(
                SpeakerVolumeNumberEntity(state=state, key=len(state.entities)),
            )
        player = state.media_player_entity
        assert player is not None

        levels = [
            _levels_from(state, command)
            for command in (
                MediaPlayerCommandRequest(
                    key=player.key,
                    has_volume=True,
                    volume=0.6,
                ),
                MediaPlayerCommandRequest(
                    key=player.key,
                    has_command=True,
                    command=MediaPlayerCommand.MUTE,
                ),
                MediaPlayerCommandRequest(
                    key=player.key,
                    has_command=True,
                    command=MediaPlayerCommand.UNMUTE,
                ),
            )
        ]

        assert levels == [
            pytest.approx(60.0),
            pytest.approx(0.0),
            pytest.approx(60.0),
        ]


class TestTheMutedRule:
    """Muted, the control reads zero and a set is remembered rather than applied."""

    def test_it_reads_zero_while_muted(self, control: _Control) -> None:
        """Which is the level in effect: the device is silent.

        Args:
            control: The control, wired as the robot wires it.
        """
        _send_command(control, MediaPlayerCommand.MUTE)

        reported = _only(
            _handled(control.entity, SubscribeHomeAssistantStatesRequest()),
            NumberStateResponse,
        )

        assert reported.state == pytest.approx(0.0)

    def test_a_set_while_muted_is_remembered_and_not_persisted(
        self,
        control: _Control,
        tmp_path: Path,
    ) -> None:
        """It lands in `previous_volume`, which is what unmuting restores.

        Args:
            control: The control, wired as the robot wires it.
            tmp_path: The directory the preferences file is written into.
        """
        _send_command(control, MediaPlayerCommand.MUTE)
        written = json.loads(
            (tmp_path / "preferences.json").read_text(encoding="utf-8"),
        )

        answered = _handled(
            control.entity,
            NumberCommandRequest(key=control.entity.key, state=30.0),
        )

        assert control.player.previous_volume == pytest.approx(0.3)
        assert _only(answered, NumberStateResponse).state == pytest.approx(0.0)
        assert (
            json.loads((tmp_path / "preferences.json").read_text(encoding="utf-8"))
            == written
        )

    def test_unmuting_restores_what_was_set_while_muted(
        self,
        control: _Control,
    ) -> None:
        """Home Assistant's slider snapping back to zero is honest, not lost.

        Args:
            control: The control, wired as the robot wires it.
        """
        _send_command(control, MediaPlayerCommand.MUTE)
        _handled(
            control.entity,
            NumberCommandRequest(key=control.entity.key, state=30.0),
        )

        _send_command(control, MediaPlayerCommand.UNMUTE)

        assert _only(
            _handled(control.entity, SubscribeHomeAssistantStatesRequest()),
            NumberStateResponse,
        ).state == pytest.approx(30.0)

    def test_the_two_controls_agree_after_every_step_of_a_sweep(
        self,
        control: _Control,
    ) -> None:
        """One number underneath both, in every state including muted.

        Args:
            control: The control, wired as the robot wires it.
        """
        steps: list[tuple[float, float]] = []

        for step in range(5):
            if step == 2:
                _send_command(control, MediaPlayerCommand.MUTE)
            elif step == 4:
                _send_command(control, MediaPlayerCommand.UNMUTE)
            else:
                _handled(
                    control.entity,
                    NumberCommandRequest(
                        key=control.entity.key,
                        state=20.0 * (step + 1),
                    ),
                )
            reported = _only(
                _handled(control.entity, SubscribeHomeAssistantStatesRequest()),
                NumberStateResponse,
            )
            steps.append((reported.state, control.player.volume * 100.0))

        assert all(reported == pytest.approx(media) for reported, media in steps), (
            f"the two controls disagreed somewhere in {steps}"
        )


class TestSettingTheBoostFromTheControl:
    """Clamped to the gain module's own bounds, and reported by reading back."""

    @pytest.mark.parametrize(
        ("asked", "expected"),
        [
            (MIN_BOOST_PERCENT - 50.0, MIN_BOOST_PERCENT),
            (MAX_BOOST_PERCENT + 200.0, MAX_BOOST_PERCENT),
            (450.0, 450.0),
        ],
    )
    def test_the_setter_receives_a_value_inside_the_bounds(
        self,
        asked: float,
        expected: float,
    ) -> None:
        """A boost of 5000% would be a limiter squashing everything flat.

        Args:
            asked: What arrived.
            expected: What the setter should be handed.
        """
        entity, written = _boost()

        answered = _handled(entity, NumberCommandRequest(key=0, state=asked))

        assert written[-1] == pytest.approx(expected)
        assert _only(answered, NumberStateResponse).state == pytest.approx(expected)

    def test_the_answer_is_read_back_rather_than_echoed(self) -> None:
        """So a refused write reports the value actually in effect."""
        refused: list[float] = [200.0]

        def _refuse(percent: float) -> None:
            """Take the value and do nothing with it.

            Args:
                percent: What was asked for, and dropped.
            """
            del percent

        entity = SpeakerBoostNumberEntity(
            key=0,
            get_percent=lambda: refused[-1],
            set_percent=_refuse,
        )

        answered = _handled(entity, NumberCommandRequest(key=0, state=700.0))

        assert _only(answered, NumberStateResponse).state == pytest.approx(200.0)

    def test_a_command_for_another_entitys_key_is_ignored(self) -> None:
        """Every entity is handed every command, so most of them are not ours."""
        entity, written = _boost(key=2)

        assert _handled(entity, NumberCommandRequest(key=7, state=700.0)) == []
        assert written == [_A_BOOST]


class TestRegisteringBothBeforeTheProtocolExists:
    """Which is what the composition root does, so it has to survive it."""

    def test_both_survive_and_every_key_stays_unique(self, tmp_path: Path) -> None:
        """The vendored de-duplication matches its own classes, never these.

        Args:
            tmp_path: An empty directory in a fake filesystem.
        """
        state = vendored_server_state(tmp_path=tmp_path)
        volume = SpeakerVolumeNumberEntity(state=state, key=len(state.entities))
        state.entities.append(volume)
        boost, _written = _boost(key=len(state.entities))
        state.entities.append(boost)

        VoiceSatelliteProtocol(state)

        assert volume in state.entities
        assert boost in state.entities
        # `ESPHomeEntity` declares no `key`; every concrete entity upstream
        # writes carries one, and Home Assistant addresses each entity by it.
        keys = [entity.key for entity in state.entities]  # type: ignore[attr-defined]  # the vendored base class declares no `key`, and every subclass has one
        assert len(set(keys)) == len(keys), f"a key is used twice in {keys}"


@dataclass(frozen=True, slots=True)
class _Control:
    """Everything one of these tests reaches for, wired as the robot wires it.

    Attributes:
        state: The vendored protocol layer's state.
        entity: The speaker-volume control.
        player: The media player the vendored layer built beside it.
        music: The output Home Assistant drives.
        speech: The output announcements go to.
    """

    state: ServerState
    entity: SpeakerVolumeNumberEntity
    player: MediaPlayerEntity
    music: FakePlayback
    speech: FakePlayback


@pytest.fixture
def control(tmp_path: Path) -> _Control:
    """Register the control and then build the protocol, as the robot does.

    The order is the composition root's: the control is appended to a state
    holding nothing, and the vendored protocol layer then appends its own media
    player after it.

    A fixture rather than a helper each test calls, because every one of them
    wants the same wiring and none of them wants `tmp_path` for anything else —
    except the two that read `preferences.json`, which ask for both.

    Args:
        tmp_path: Where preferences are written.

    Returns:
        The state, the control, the media player, and the two outputs.
    """
    music, speech = FakePlayback(), FakePlayback()
    state = vendored_server_state(
        tmp_path=tmp_path,
        music_player=music,
        tts_player=speech,
    )
    entity = SpeakerVolumeNumberEntity(state=state, key=len(state.entities))
    state.entities.append(entity)
    VoiceSatelliteProtocol(state)
    player = state.media_player_entity
    assert player is not None
    return _Control(
        state=state,
        entity=entity,
        player=player,
        music=music,
        speech=speech,
    )


def _send_command(control: _Control, command: MediaPlayerCommand) -> None:
    """Send one media-player command the way Home Assistant does: to everything.

    Args:
        control: What the test is driving.
        command: What Home Assistant sent the media player.
    """
    _fan_out(
        control.state,
        MediaPlayerCommandRequest(
            key=control.player.key,
            has_command=True,
            command=command,
        ),
    )


def _fan_out(state: ServerState, msg: message.Message) -> list[message.Message]:
    """Hand one message to every entity, as the vendored protocol layer does.

    The one loop, because the ordering tests are *about* this loop: a second
    copy of it that filtered one entity's replies would be the thing under test
    written twice, and the two could then disagree about the order.

    Args:
        state: The state holding the entities.
        msg: What arrived.

    Returns:
        What the speaker-volume control answered with. Every other entity is
        handed the message and its replies are drained and dropped, which is
        what the protocol layer does with the ones a test is not asking about.
    """
    ours: list[message.Message] = []
    for entity in list(state.entities):
        answered = list(entity.handle_message(msg))
        if isinstance(entity, SpeakerVolumeNumberEntity):
            ours.extend(answered)
    return ours


def _levels_from(state: ServerState, msg: MediaPlayerCommandRequest) -> float:
    """Fan one command out and report what the speaker-volume control answered.

    Args:
        state: The state holding the entities.
        msg: The command Home Assistant sent.

    Returns:
        The level the control reported, in percent.
    """
    return float(_only(_fan_out(state, msg), NumberStateResponse).state)
