"""Detections from the groundstation, over the one robot-link session client.

This is the robot's end of the link, and it is a **second consumer** of
`reachy_session_client` rather than a second implementation of the protocol.
`reachyctl probe` is the first, and reachyctl REQ-057 exists to keep it that
way: a probe that negotiated, sequenced, stamped, superseded and reconnected
through its own code would pass its own tests and prove nothing about the robot.
Everything this module adds is what to do with a session, not how to hold one.

What it adds is three things.

**Frames go up on a fixed schedule.** The camera is read every
`DEFAULT_FRAME_INTERVAL` seconds and the bytes are submitted as they came off
the capture hardware — already JPEG, never decoded and re-encoded, because doing
that on the robot would spend a scarce core to arrive at the same bytes. The
rate is fixed rather than adapting to the observed round trip; see the change
document's open questions, and change 0014 for where the number gets measured.

**Results become the port's answer.** A result names the capability that
produced it and carries the payload for that capability; this keeps the latest
*face* payload and the moment it arrived, which is what `latest()` reports
freshness against. Filtering by capability rather than taking whatever arrived
last matters as soon as a second capability is agreed: a gesture result is not
an absence of faces.

**The session is kept up.** `SessionClient.results()` re-establishes a session
that dropped, so the loop below stays inside one `async for` across a
groundstation restart. What it does not do is retry a session that was
*refused*, and neither does this: a credential the groundstation will not accept
is not a thing a delay fixes, so the adapter stops trying and reports itself
disconnected — which is exactly the state a local-fallback source is watching
for.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from typing import TYPE_CHECKING, Final

from reachy_contracts import FACE_CAPABILITY, FaceDetections
from reachy_mini_ha_satellite.adapters.daemon import in_thread
from reachy_mini_ha_satellite.ports import Detections, DetectionSource
from reachy_session_client import (
    DEFAULT_BACKOFF,
    ConnectionFailedError,
    SessionClientError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from reachy_contracts import CapabilityName, FaceDetection
    from reachy_mini_ha_satellite.adapters.daemon import MediaInterface, Offload
    from reachy_session_client import Backoff, FrameResult, SessionClient, SessionStats

__all__ = [
    "DEFAULT_FRAME_INTERVAL",
    "DEFAULT_STALENESS_SECONDS",
    "RemotePerception",
]

_LOGGER: Final = logging.getLogger(__name__)

# How often a frame goes up, in seconds. Ten per second, which is the rate the
# robot-link spec's steady-state scenario is written against and what the
# predecessor arrangement ran at when the robot was measured at 1.52 of its four
# cores.
#
# **Fixed, not adaptive.** The link spikes to 700 ms and an adaptive rate would
# be a control loop with its own failure modes, tuned against a network nobody
# has instrumented yet. Change 0014 measures this; until then a constant is
# honest about being a guess and an adaptive rate would not be.
DEFAULT_FRAME_INTERVAL: Final = 0.1

# How long a result stays worth acting on. Two seconds is several of the link's
# 700 ms spikes' worth of grace and still well inside the time a person notices
# a robot watching where they used to be. It matches the session client's own
# default, deliberately: two different windows would make "stale" mean two
# things depending on which object was asked.
DEFAULT_STALENESS_SECONDS: Final = 2.0


class RemotePerception:
    """Face detections from the groundstation, over one long-lived session."""

    def __init__(
        self,
        media: MediaInterface,
        client: SessionClient,
        *,
        capability: CapabilityName = FACE_CAPABILITY,
        frame_interval: float = DEFAULT_FRAME_INTERVAL,
        staleness_seconds: float = DEFAULT_STALENESS_SECONDS,
        backoff: Backoff = DEFAULT_BACKOFF,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        offload: Offload = in_thread,
    ) -> None:
        """Describe the source without opening anything.

        Args:
            media: The daemon's media interface, which the frames come off.
            client: The session client. Built by the composition root, because
                it is where the groundstation's address and the credential are,
                and neither belongs in here.
            capability: Which capability's results to read.
            frame_interval: How long to wait between frames, in seconds.
            staleness_seconds: How long a result stays worth acting on.
            backoff: How long to wait between attempts at the *first*
                connection. Reconnection after a session drops is the client's
                own, and uses the client's own.
            clock: The monotonic source freshness is measured against.
            sleep: How to wait. Injected so the test suite drives an outage
                without spending one.
            offload: How to read the camera without stalling the event loop.

        Raises:
            ValueError: If the frame interval or the staleness window is not a
                positive number of seconds.
        """
        if frame_interval <= 0:
            message = f"the frame interval must be positive, not {frame_interval}"
            raise ValueError(message)
        if staleness_seconds <= 0:
            message = f"the staleness window must be positive, not {staleness_seconds}"
            raise ValueError(message)
        self._media = media
        self._client = client
        self._capability = capability
        self._frame_interval = frame_interval
        self._staleness_seconds = staleness_seconds
        self._backoff = backoff
        self._clock = clock
        self._sleep = sleep
        self._offload = offload

        self._faces: tuple[FaceDetection, ...] = ()
        self._generation = 0
        self._sequence: int | None = None
        self._captured_at: float | None = None
        self._received_at: float | None = None
        self._refused = False
        self._closed = False
        self._session: asyncio.Task[None] | None = None

    @property
    def connected(self) -> bool:
        """Whether a session is up right now.

        This is the signal a local-fallback source watches, and it is
        deliberately not the same question as whether the results are fresh. A
        session that is up but has gone quiet is a groundstation that stopped
        answering, and the honest response to that is a neutral head — not a
        second detector started on the strength of a stall that may last one
        frame.

        Returns:
            True while a negotiated session is held and this adapter has not
            given up on it.
        """
        return self._client.connected and not self._refused

    @property
    def stats(self) -> SessionStats:
        """What the session has done so far.

        Returns:
            The client's running counters, for whatever reports on the link.
        """
        return self._client.stats

    async def start(self) -> None:
        """Open the session and begin exchanging frames for results.

        Returns without waiting for the first connection to succeed: an
        unreachable groundstation is a normal state for a robot that was turned
        on before the rest of the house, and a start that blocked on it would
        hold up the voice pipeline, which does not need the groundstation at
        all.
        """
        if self._session is not None or self._closed:
            return
        self._session = asyncio.create_task(self._run(), name="robot-link")

    def latest(self) -> Detections:
        """Say what the groundstation last reported, if it is still current.

        Returns:
            The faces from the most recent result, or an empty, not-fresh
            answer once the staleness window has elapsed — which is robot-link
            REQ-017, and is what makes the head go back to neutral rather than
            keep watching an empty chair.
        """
        if self._received_at is None:
            # Nothing has produced anything yet, so no field describes a
            # detection — `source` included. Naming this source here would say
            # the groundstation produced a view of an empty room, which is
            # robot-link REQ-013's ordinary success and a different fact from
            # "the session has not answered yet". They are least
            # distinguishable at start-up, which is when a robot is most likely
            # to do something odd and least likely to be watched.
            return Detections()
        age = self._clock() - self._received_at
        fresh = age < self._staleness_seconds
        return Detections(
            faces=self._faces if fresh else (),
            fresh=fresh,
            source=DetectionSource.REMOTE,
            age_seconds=age,
            generation=self._generation,
            sequence=self._sequence,
            captured_at=self._captured_at,
            received_at=self._received_at,
        )

    async def aclose(self) -> None:
        """Say goodbye to the groundstation and stop sending frames."""
        self._closed = True
        session, self._session = self._session, None
        if session is not None:
            session.cancel()
            # Every exception, not only the cancellation. A task that had
            # already failed is not cancelled by `cancel`, so awaiting it here
            # re-raises what it failed with — out of a shutdown path, where it
            # would replace the orderly close below with a failure about
            # something that happened long before.
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await session
        await self._client.aclose()

    async def _run(self) -> None:
        """Hold a session for as long as the adapter is open.

        The inner `async for` survives a session that drops, because the client
        re-establishes it. This loop exists for the case the client does not
        handle — the very first connection, which has no session to
        re-establish — and for deciding what to do when the client gives up.
        """
        try:
            await self._hold_a_session()
        except Exception:
            # Anything this loop did not anticipate. It runs in a task nobody
            # awaits until shutdown, so an exception escaping here would be
            # re-raised out of `aclose` — reporting a failure that happened
            # minutes earlier as though the shutdown had caused it.
            _LOGGER.exception("the robot link supervisor stopped")
            self._refused = True

    async def _hold_a_session(self) -> None:
        """Open a session and keep one, until the adapter gives up or closes.

        Raises:
            SessionClientError: Only from something this loop does not handle;
                the refusals and faults it does handle are recorded rather than
                raised. `_run` reports whatever reaches it.
        """
        attempt = 0
        # `_refused` is in the condition as well as `_closed`, and it has to be:
        # `_exchange` sets it when the groundstation ends an established session
        # for a reason a delay will not fix, and a loop that only tested
        # `_closed` would go straight back to `connect` — which returns the
        # agreement it is already holding, and lands back in `_exchange`
        # against a session this adapter has already given up on.
        while not self._closed and not self._refused:
            try:
                await self._client.connect()
            except ConnectionFailedError:
                attempt += 1
                await self._sleep(self._backoff.delay(attempt))
                continue
            except SessionClientError:
                # A refusal or a protocol fault: neither is answered by
                # waiting, and looping on one would hide the single failure
                # that needs a person. Reported as disconnected, which is the
                # state a fallback source acts on.
                _LOGGER.exception("the groundstation would not open a session")
                self._refused = True
                return
            attempt = 0
            await self._exchange()

    async def _exchange(self) -> None:
        """Send frames and apply results until the session ends for good."""
        frames = asyncio.create_task(self._submit_frames(), name="robot-link-frames")
        results = self._client.results()
        try:
            async for result in results:
                self._apply(result)
        except ConnectionFailedError:
            # The client gave up mid-iteration. The outer loop reconnects.
            _LOGGER.info("the robot link dropped; reopening")
        except SessionClientError:
            _LOGGER.exception("the robot link failed in a way retrying will not fix")
            self._refused = True
        finally:
            frames.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await frames
            await results.aclose()

    async def _submit_frames(self) -> None:
        """Put one already-compressed frame on the session at a fixed rate.

        The frame is read on a worker thread. The daemon's camera read is a
        pipeline pull that can take a few milliseconds, and doing it inline
        would stall the ESPHome protocol handling that shares this event loop
        for that long, ten times a second.

        A turn that fails is logged and the next one is taken. Letting the
        exception end this task would be the worst of the available outcomes:
        `_exchange` only awaits it in a `finally`, so nothing would report the
        failure until the session ended, and in the meantime the link would be
        up with no frames on it — results would stop, `latest()` would go
        stale, and `connected` would stay true, which is precisely the state
        this adapter promises is *not* a reason to fall back.
        """
        while True:
            await self._sleep(self._frame_interval)
            try:
                await self._submit_one()
            except Exception:
                _LOGGER.exception("a frame could not be put on the robot link")

    async def _submit_one(self) -> None:
        """Take one frame off the camera and put it on the session."""
        if not self._client.connected:
            # Nothing to send it on. The client would count a drop, which is
            # true but says the link is losing frames rather than that there is
            # no link; and reading the camera for a frame with nowhere to go is
            # work for nothing.
            return
        payload = await self._offload(self._media.get_frame_jpeg)
        if not payload:
            return
        await self._client.submit_frame(payload)

    def _apply(self, result: FrameResult) -> None:
        """Take the faces out of one result, if it is one this source reads.

        Args:
            result: What came back, already checked for supersession by the
                client — a result for a frame older than one already applied
                never reaches here.
        """
        if result.capability != self._capability:
            return
        payload = result.payload
        if not isinstance(payload, FaceDetections):
            # The named capability produced something other than the payload
            # this source knows how to read. The contract makes that a parse
            # failure at the client, so reaching here would mean the registry
            # and this adapter disagree about what `face` carries.
            _LOGGER.warning(
                "ignoring a %s result carrying %s",
                result.capability,
                type(payload).__name__,
            )
            return
        round_trip = result.round_trip_seconds
        if (
            round_trip is None
            or not math.isfinite(round_trip)
            or round_trip < 0.0
            or not math.isfinite(result.received_at)
        ):
            _LOGGER.warning("ignoring a face result without valid robot capture timing")
            return
        received_at = self._clock()
        captured_at = received_at - round_trip
        if not math.isfinite(received_at) or not math.isfinite(captured_at):
            _LOGGER.warning("ignoring a face result with non-finite adapter timing")
            return
        sequence = int(result.sequence)
        # The shared client owns the session lifecycle and counts every session
        # established after the first. Read that explicit boundary only when a
        # result arrives: a reconnect must not relabel the cached result from the
        # session that just ended while the new session has answered nothing.
        self._generation = self._client.stats.reconnections
        self._faces = payload.faces
        self._sequence = sequence
        self._received_at = received_at
        self._captured_at = captured_at
