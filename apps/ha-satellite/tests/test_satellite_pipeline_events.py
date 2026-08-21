"""The seam where the vendored protocol's events become the behaviour layer's.

Two things are checked. The **translation is total**: every event upstream can
broadcast is either mapped onto something the robot expresses or listed as
deliberately ignored, so a re-vendoring that adds one is a red run rather than a
silence. And the **tap delivers on the right thread**: the vendored code emits
from whichever thread playback or capture finished on, and the behaviour layer is
single-threaded by construction.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import asyncio

import pytest

from reachy_mini_ha_satellite.adapters.pipeline_events import (
    IGNORED_EVENTS,
    PipelineEventTap,
    pipeline_event_for,
)
from reachy_mini_ha_satellite.behaviour import PipelineEvent
from reachy_mini_ha_satellite.esphome.peripheral_api import LVAEvent


class TestTheTranslationIsTotal:
    """Every upstream event is accounted for, one way or the other."""

    @pytest.mark.parametrize("event", list(LVAEvent))
    def test_each_event_is_translated_or_deliberately_ignored(
        self,
        event: LVAEvent,
    ) -> None:
        """A new upstream event has neither, and this is what says so.

        Args:
            event: One of the vendored protocol layer's broadcasts.
        """
        translated = pipeline_event_for(event, {})

        assert translated is not None or event in IGNORED_EVENTS

    def test_nothing_is_both_translated_and_ignored(self) -> None:
        """The two sets are a partition, not two opinions about the same event."""
        for event in IGNORED_EVENTS:
            assert pipeline_event_for(event, {}) is None


class TestWhatEachEventMeans:
    """The mappings that are not obvious from the names."""

    @pytest.mark.parametrize(
        ("event", "expected"),
        [
            (LVAEvent.WAKE_WORD_DETECTED, PipelineEvent.WAKE_WORD_DETECTED),
            (LVAEvent.LISTENING, PipelineEvent.LISTENING),
            (LVAEvent.THINKING, PipelineEvent.PROCESSING),
            (LVAEvent.TTS_SPEAKING, PipelineEvent.RESPONDING),
            (LVAEvent.TTS_FINISHED, PipelineEvent.RESPONSE_FINISHED),
            (LVAEvent.PIPELINE_ERROR, PipelineEvent.ERROR),
            (LVAEvent.IDLE, PipelineEvent.IDLE),
            (LVAEvent.DISCONNECTED, PipelineEvent.DISCONNECTED),
            (LVAEvent.TIMER_RINGING, PipelineEvent.RESPONDING),
        ],
    )
    def test_the_mapping(self, event: LVAEvent, expected: PipelineEvent) -> None:
        """One table, checked rather than described.

        Args:
            event: What upstream broadcast.
            expected: What the robot should express.
        """
        assert pipeline_event_for(event, {}) is expected

    def test_muting_and_unmuting_share_an_upstream_event(self) -> None:
        """It carries the answer in its payload rather than in its name."""
        assert pipeline_event_for(LVAEvent.MUTED, {"muted": True}) is (
            PipelineEvent.MUTED
        )
        assert pipeline_event_for(LVAEvent.MUTED, {"muted": False}) is (
            PipelineEvent.UNMUTED
        )

    def test_a_mute_event_with_no_payload_is_read_as_muting(self) -> None:
        """The safe reading, which is a robot that stops listening.

        The alternative would have it act out an exchange nobody is having.
        """
        assert pipeline_event_for(LVAEvent.MUTED, None) is PipelineEvent.MUTED

    def test_music_starting_is_not_a_conversation(self) -> None:
        """Music is not the robot being spoken to.

        One that performed an exchange whenever an album started would be
        reporting something that is not happening.
        """
        assert pipeline_event_for(LVAEvent.MEDIA_PLAYER_PLAYING, {}) is None


class TestTheTap:
    """What the vendored code is handed, and what it does with an event."""

    def test_it_delivers_a_translated_event(self) -> None:
        """The whole of connecting the protocol to the robot's movement."""
        received: list[PipelineEvent] = []
        tap = PipelineEventTap(received.append)

        tap.emit_event_sync(LVAEvent.THINKING, None)

        assert received == [PipelineEvent.PROCESSING]

    def test_it_drops_an_event_the_robot_says_nothing_about(self) -> None:
        """Rather than delivering a `None` for the behaviour layer to filter."""
        received: list[PipelineEvent] = []
        tap = PipelineEventTap(received.append)

        tap.emit_event_sync(LVAEvent.VOLUME_CHANGED, {"volume": 0.5})

        assert received == []

    @pytest.mark.asyncio
    async def test_an_event_raised_off_the_loop_is_delivered_on_it(self) -> None:
        """The vendored code emits from whichever thread playback finished on."""
        received: list[PipelineEvent] = []
        tap = PipelineEventTap(received.append)
        tap.bind(asyncio.get_running_loop())

        await asyncio.to_thread(tap.emit_event_sync, LVAEvent.LISTENING, None)
        # One pass of the loop is what a `call_soon_threadsafe` needs; this
        # yields rather than sleeping, so nothing waits for wall time.
        await asyncio.sleep(0)

        assert received == [PipelineEvent.LISTENING]

    @pytest.mark.asyncio
    async def test_an_event_raised_on_the_loop_is_delivered_inline(self) -> None:
        """Hopping to the loop from the loop would defer a movement a whole pass."""
        received: list[PipelineEvent] = []
        tap = PipelineEventTap(received.append)
        tap.bind(asyncio.get_running_loop())

        tap.emit_event_sync(LVAEvent.LISTENING, None)

        assert received == [PipelineEvent.LISTENING]

    def test_an_event_arriving_after_the_loop_closed_is_dropped(self) -> None:
        """Shutdown races playback, and the loser is dropped rather than raised.

        A movement nobody will make is not worth an exception on an audio
        thread.
        """
        received: list[PipelineEvent] = []
        tap = PipelineEventTap(received.append)
        loop = asyncio.new_event_loop()
        loop.close()
        tap.bind(loop)

        tap.emit_event_sync(LVAEvent.LISTENING, None)

        assert received == []
