"""One live JPEG, held for the sole authenticated robot session and nobody else.

The robot already spends the capture and the compression, and the session layer
already accepted the bytes. This is what makes the same bytes visible to an
operator without opening a second connection to the robot, decoding twice or
re-encoding anything: the pipeline hands over the payload it just decoded, and
`/stream.mjpg` sends it on.

Three properties are the whole design, and each of them is a thing this module
deliberately does not have.

**One value, not a mapping.** There is a single optional payload for the whole
process, never a payload per session, because a feed that kept one image per
session would have to choose between them — and connection order, frame recency
and an opaque session identifier are none of them operator intent. Cardinality is
therefore a count and not a set of identifiers: with more than one authenticated
session the feed refuses rather than selects, so it never needs to know which
session anything came from.

**A revision, not a queue.** Publication replaces. A viewer reads the newest
revision each time round, so one that falls behind skips to the current frame
instead of draining a backlog it would then serve late. There is no per-viewer
storage and no history to retain.

**A bound, not a promise.** The viewer count is capped, and the cap is taken and
released synchronously — a counting semaphore in the plain sense — so the same
step that decides whether to serve a stream is the step that reserves the slot.

What may enter the value is decided outside this module, by the one caller in
`pipeline.runner`: a payload is published only when it carries a real JPEG
signature *and* decoded successfully. The signature test lives beside the decoder
in `pipeline.decode`, which is where this service's knowledge that frames are
JPEG belongs; this module holds bytes and never inspects them.

Nothing here logs, measures or traces a payload. `feed.py` sits beside `ports.py`
rather than inside `api/`, `session/` or `pipeline/` because all three hold one
of these, handed to them by `service.py`, and it names no capability — the
boundary `just lint-capability-boundary` enforces is untouched.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "MAX_VIEWERS",
    "FeedAvailability",
    "FeedFrame",
    "FeedRegistry",
]

# How many viewers may hold a stream at once. Fixed rather than configured: the
# spec settles on four and says a different viewer count is a separate proposal,
# so a setting here would offer an operator a decision nobody has made.
MAX_VIEWERS: Final = 4


class FeedAvailability(StrEnum):
    """Why a request for the feed is or is not given a stream.

    The two refusals are separate values because they are separate operator
    situations: nothing is connected, or too much is. Collapsing them would send
    somebody looking for an unplugged robot when what they have is two.

    Attributes:
        AVAILABLE: Exactly one authenticated session is active and has supplied
            a validated JPEG that is still retained.
        NO_ELIGIBLE_SESSION: No authenticated session is active, or the one that
            is has not supplied a validated JPEG since it became the only one.
        AMBIGUOUS_SESSIONS: More than one authenticated session is active, so
            there is no one robot the feed could be showing.
    """

    AVAILABLE = "available"
    NO_ELIGIBLE_SESSION = "no_eligible_session"
    AMBIGUOUS_SESSIONS = "ambiguous_sessions"


@dataclass(frozen=True, slots=True)
class FeedFrame:
    """The retained payload, with the revision it was retained at.

    Attributes:
        payload: The original compressed bytes, exactly as the robot sent them.
        revision: A count that only ever increases, so a viewer can ask for
            anything newer than what it last sent without comparing payloads.
    """

    payload: bytes
    revision: int


class FeedRegistry:
    """The one live frame, its eligibility, and the viewers reading it."""

    def __init__(self, *, max_viewers: int = MAX_VIEWERS) -> None:
        """Create an empty registry with no sessions and no viewers.

        Args:
            max_viewers: How many viewers may hold a stream at once. It is an
                argument so a test can reach the bound without opening four
                connections, and it has one production value.
        """
        self._max_viewers = max_viewers
        self._sessions = 0
        self._viewers = 0
        self._payload: bytes | None = None
        self._revision = 0
        self._closed = False
        # Replaced rather than cleared on every wake-up. Clearing an event that
        # several viewers are waiting on races — whichever of them runs first
        # would clear it out from under the rest — while handing each round of
        # waiters its own event cannot.
        self._changed = asyncio.Event()

    @property
    def viewers(self) -> int:
        """How many viewers currently hold a slot.

        Returns:
            The number of reservations outstanding.
        """
        return self._viewers

    @property
    def revision(self) -> int:
        """How many payloads have been retained since the process started.

        Returns:
            The current revision. It survives a payload being cleared, so a
            viewer that saw revision N is never handed an older image under a
            number it has already passed.
        """
        return self._revision

    @contextmanager
    def authenticated_session(self) -> Iterator[None]:
        """Count one authenticated session for as long as it lasts.

        Entered once a client's credential has been accepted and left in a
        `finally`, so a session that ends by disconnection, by cancellation or
        by a fault is counted out exactly as one that closes cleanly is. A client
        that failed authentication never reaches here and therefore never makes
        the feed ambiguous.

        Yields:
            Nothing; the counting is the point.
        """
        self._sessions += 1
        self._settle()
        try:
            yield
        finally:
            self._sessions -= 1
            self._settle()

    #:= docs/specs/home-assistant-configuration-and-camera-feed/index.md#req-097-feed-eligibility-is-deterministic
    #:% The groundstation MUST serve `/stream.mjpg` only after exactly one active
    #:% authenticated robot session has supplied a fresh validated JPEG while it is the
    #:% sole session, clear all feed frame state and end viewers whenever authenticated
    #:% session cardinality is zero or greater than one, and require another fresh
    #:% validated JPEG after cardinality returns to one.
    def _settle(self) -> None:
        """Apply the cardinality rule after the count changed, and wake viewers.

        Any count that is not exactly one discards the payload: with none there
        is nothing to show, and with several there is nothing to choose. The
        discard is what stops ambiguity resurrecting an image — coming back down
        to one session leaves the value empty until that session supplies its own
        fresh frame.
        """
        if self._sessions != 1:
            self._payload = None
        self._wake()

    def _wake(self) -> None:
        """Let every waiting viewer look at the state again."""
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    #:= docs/specs/home-assistant-configuration-and-camera-feed/index.md#req-096-mjpeg-is-a-bounded-latest-frame-view
    #:% The groundstation MUST retain at most one original payload globally for a
    #:% standards-compatible MJPEG stream only after both explicit JPEG-format signature
    #:% validation and successful image decode, replace rather than queue that payload
    #:% for slow viewers, and add no robot connection, stream-only decode or re-encode,
    #:% or capability-processing blockage.
    def publish(self, payload: bytes) -> bool:
        """Retain one payload, replacing whatever was retained before.

        The caller has already proved the bytes carry a JPEG signature and
        decoded — see `pipeline.runner`, which is the only caller. Nothing is
        copied and nothing is re-encoded: what is retained is the object the
        session read off the wire.

        Args:
            payload: The original compressed frame.

        Returns:
            Whether it was retained. It is not when the process is shutting down
            or when the number of authenticated sessions is anything but one,
            and a caller that ignores the answer is still correct — a refused
            publication changes nothing.
        """
        if self._closed or self._sessions != 1:
            return False
        self._revision += 1
        self._payload = payload
        self._wake()
        return True

    #:= docs/specs/home-assistant-configuration-and-camera-feed/index.md#req-097-feed-eligibility-is-deterministic
    #:% The groundstation MUST serve `/stream.mjpg` only after exactly one active
    #:% authenticated robot session has supplied a fresh validated JPEG while it is the
    #:% sole session, clear all feed frame state and end viewers whenever authenticated
    #:% session cardinality is zero or greater than one, and require another fresh
    #:% validated JPEG after cardinality returns to one.
    def availability(self) -> FeedAvailability:
        """Say whether a stream may be opened, and why not when it may not.

        Returns:
            What a request for the feed should be answered with.
        """
        if self._sessions > 1:
            return FeedAvailability.AMBIGUOUS_SESSIONS
        if self._closed or self._sessions != 1 or self._payload is None:
            return FeedAvailability.NO_ELIGIBLE_SESSION
        return FeedAvailability.AVAILABLE

    @property
    def at_capacity(self) -> bool:
        """Whether every viewer slot is currently taken.

        Asking is not taking, which is the whole reason this exists beside
        `reserve_viewer`: a `HEAD` has to report the answer a `GET` would have
        been given without holding a slot in order to find it out.

        Returns:
            Whether a `reserve_viewer` made now would be refused.
        """
        return self._viewers >= self._max_viewers

    def reserve_viewer(self) -> bool:
        """Take one of the viewer slots, if there is one free.

        Synchronous on purpose: the decision to serve and the reservation have
        to be one step, or two requests arriving together both see a free slot.

        Returns:
            Whether a slot was taken. A caller that got `True` owes exactly one
            `release_viewer`.
        """
        if self.at_capacity:
            return False
        self._viewers += 1
        return True

    def release_viewer(self) -> None:
        """Give a viewer slot back."""
        self._viewers -= 1

    async def next_frame(self, after: int) -> FeedFrame | None:
        """Wait for a payload newer than the one a viewer last sent.

        The newest revision is read each time round rather than a queued one, so
        a viewer that spent a long time sending its last part comes back to the
        current frame and the ones it missed are simply gone.

        Args:
            after: The revision the caller last sent, or zero before its first.

        Returns:
            The current payload once it is newer than `after`, or `None` when
            this viewer is finished — the process is shutting down, or the
            number of authenticated sessions stopped being one.
        """
        while True:
            if self._closed or self._sessions != 1:
                return None
            payload = self._payload
            if payload is not None and self._revision > after:
                return FeedFrame(payload=payload, revision=self._revision)
            # Nothing above awaited, so the event read here is the one any
            # change after these checks will replace. That holds for every
            # caller because every caller runs between whole steps of the event
            # loop — the shutdown signal included, which is why `service.py`
            # schedules its `close` rather than running it in the handler.
            await self._changed.wait()

    def close(self) -> None:
        """Discard the payload and finish every viewer.

        Called twice over on the way out, and deliberately. `service.py`'s
        server schedules it on the event loop the moment a shutdown signal
        arrives, which is before uvicorn starts waiting for open responses to
        finish and therefore the only point at which waking a parked viewer
        still makes that wait end; the application's lifespan calls it again for
        every other way a process can stop. Idempotent, so neither has to know
        whether the other ran.

        Every caller reaches this between steps of the event loop and never
        inside one — see `next_frame`, whose correctness depends on it.
        """
        self._closed = True
        self._payload = None
        self._wake()
