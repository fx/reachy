"""Where the vendored protocol layer's events become the behaviour layer's.

The vendored satellite already broadcasts everything the voice pipeline does. It
does it through one slot on `ServerState` — `peripheral_api` — which is typed as
`Optional[Any]`, is called with exactly one method, `emit_event_sync(event,
data)`, and exists precisely so that something outside the protocol layer can
watch the pipeline without the protocol layer knowing what is watching. That is
the seam this module fills, and filling it is why the vendored files need no
edit to drive the robot's movement.

Upstream's own occupant of that slot is a WebSocket server for LED and button
peripherals. This robot has neither — its antennas are motors on the daemon's
control handle, not a peripheral board — so the slot is free, and a tap that
implements the one method it is called with is a complete implementation of what
the vendored code asks for.

The translation is deliberate rather than a pass-through. `PipelineEvent` is the
behaviour layer's own vocabulary: ten things a voice pipeline can do, none of
them named after a protobuf message. Nineteen upstream events map onto them or
are explicitly ignored, and a test asserts the two sets together cover every
member of `LVAEvent` — so an upstream event added by a later re-vendoring is a
red run rather than a silent nothing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Final

from reachy_mini_ha_satellite.behaviour import PipelineEvent
from reachy_mini_ha_satellite.esphome.peripheral_api import LVAEvent

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

__all__ = [
    "IGNORED_EVENTS",
    "PipelineEventTap",
    "pipeline_event_for",
]

_LOGGER: Final = logging.getLogger(__name__)

# What each upstream event means to the robot's movement.
#
# `MUTED` is absent because it carries the answer in its payload rather than in
# its name — it is emitted both for muting and for unmuting — and it is handled
# below. `IDLE` is what the protocol emits when a connection is established as
# well as when an exchange ends, which is why it is what clears the disconnected
# state.
_TRANSLATION: Final[dict[LVAEvent, PipelineEvent]] = {
    LVAEvent.WAKE_WORD_DETECTED: PipelineEvent.WAKE_WORD_DETECTED,
    LVAEvent.LISTENING: PipelineEvent.LISTENING,
    LVAEvent.THINKING: PipelineEvent.PROCESSING,
    LVAEvent.TTS_SPEAKING: PipelineEvent.RESPONDING,
    LVAEvent.TTS_FINISHED: PipelineEvent.RESPONSE_FINISHED,
    LVAEvent.PIPELINE_ERROR: PipelineEvent.ERROR,
    LVAEvent.IDLE: PipelineEvent.IDLE,
    LVAEvent.DISCONNECTED: PipelineEvent.DISCONNECTED,
    # A ringing timer is the robot demanding attention out loud, which is the
    # same thing to a person in the room as an answer being spoken.
    LVAEvent.TIMER_RINGING: PipelineEvent.RESPONDING,
}

#: Upstream events the robot's movement deliberately says nothing about.
#:
#: Two kinds. The transcripts — `STT_TEXT` and `TTS_TEXT` — arrive during a state
#: the robot is already expressing, and a second movement per sentence would be
#: noise. The rest are bookkeeping: a volume change, a timer counting down, music
#: starting, an mDNS registration and a light command are not things the head or
#: the antennas have an opinion about. Music in particular is deliberate — Home
#: Assistant driving the media player is not the robot being spoken to, and a
#: robot that acted out a conversation whenever an album started would be
#: reporting something that is not happening.
#:
#: This set exists so that the mapping is *total*. A test asserts that every
#: `LVAEvent` is either translated or listed here, which turns "upstream added an
#: event and nothing noticed" from a silence into a failure.
IGNORED_EVENTS: Final[frozenset[LVAEvent]] = frozenset(
    {
        LVAEvent.STT_TEXT,
        LVAEvent.TTS_TEXT,
        LVAEvent.TIMER_TICKING,
        LVAEvent.TIMER_UPDATED,
        LVAEvent.MEDIA_PLAYER_PLAYING,
        LVAEvent.VOLUME_CHANGED,
        LVAEvent.VOLUME_MUTED,
        LVAEvent.ZEROCONF,
        LVAEvent.LIGHT_COMMAND,
    }
)


def pipeline_event_for(
    event: LVAEvent,
    data: Mapping[str, Any] | None = None,
) -> PipelineEvent | None:
    """Say what one upstream event means to the robot's movement.

    Args:
        event: What the vendored protocol layer broadcast.
        data: What it broadcast with it. Only the mute event uses it, and only
            for the one field that says which way the switch went.

    Returns:
        The behaviour layer's event, or `None` when this one changes nothing
        the robot expresses.
    """
    if event is LVAEvent.MUTED:
        muted = bool((data or {}).get("muted", True))
        return PipelineEvent.MUTED if muted else PipelineEvent.UNMUTED
    return _TRANSLATION.get(event)


class PipelineEventTap:
    """Watches the voice pipeline on the behaviour layer's behalf.

    Satisfies every method the vendored code calls on
    `ServerState.peripheral_api`, which is one — `emit_event_sync`. Installing
    one is the whole of connecting the protocol to the robot's movement.

    `bind` is not part of that surface and the vendored code never calls it: it
    is how the composition root names the loop events are delivered on, once,
    when the service starts.
    """

    def __init__(
        self,
        deliver: Callable[[PipelineEvent], None],
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Say where translated events go.

        Args:
            deliver: What to hand each translated event to. Called on the event
                loop's own thread whenever a loop is known, because the
                behaviour layer is single-threaded by construction and the
                vendored code emits from whichever thread playback or audio
                capture happened to finish on.
            loop: The loop to hop to. `None` means deliver inline, which is
                what a test wants and what happens before a loop exists.
        """
        self._deliver = deliver
        self._loop = loop

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """Name the loop events are delivered on.

        Args:
            loop: The running loop.
        """
        self._loop = loop

    def emit_event_sync(
        self,
        event: LVAEvent,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        """Receive one event from the vendored protocol layer.

        The signature is upstream's, including the argument that is usually
        `None`; this is the method the carried code calls and its shape is not
        this repository's to choose.

        Args:
            event: What the pipeline did.
            data: What it did it with.
        """
        translated = pipeline_event_for(event, data)
        if translated is None:
            return

        loop = self._loop
        if loop is None:
            self._deliver(translated)
            return

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._deliver(translated)
            return

        try:
            loop.call_soon_threadsafe(self._deliver, translated)
        except RuntimeError:
            # The loop closed between the emit and the hop, which happens on
            # the way out of a shutdown. A movement nobody will make is not
            # worth an exception raised on an audio thread.
            _LOGGER.debug("dropped %s: the event loop has closed", translated.value)
