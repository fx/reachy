"""One real session to the groundstation, opened once and reported three ways.

This is the only place in the package that opens a connection, and what it
opens is a `SessionClient` — the same protocol implementation the robot uses,
not a lightweight client written to be easy to diagnose with. A diagnostic that
spoke its own dialect would report on the dialect.

It measures two things and reports both. **Establishment** is how long the
connection and the negotiation took together, which is what an operator is
actually waiting through. **Round trip** is one frame out and its result back,
measured on the clock that stamped the frame, which is the number that says
whether the link or the far end is the problem. Establishment alone would
conflate a slow handshake with a slow pipeline; a round trip alone would say
nothing when the session never opened.

The frame is a solid mid-grey image, generated once and committed below rather
than assembled at run time, because sending one means having a real compressed
image and this package will not depend on an encoder to make one. It carries no
likeness, no metadata and no licence: it is 32 by 24 pixels of one value. What
comes back from it is legitimately zero detections — the timing is the point,
and a capability that answers "nothing here" has proved the whole pipeline.
"""

from __future__ import annotations

import asyncio
import base64
import time
from typing import TYPE_CHECKING, Final

from reachy_checks.ports import LinkReport
from reachy_session_client import (
    SessionClient,
    SessionClientError,
    open_websocket,
    redact_url,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from reachy_contracts import Capability
    from reachy_session_client import Credential, TransportFactory

__all__ = ["PROBE_FRAME", "SessionLink"]

# A 32x24 solid mid-grey JPEG, encoded once by OpenCV and committed here. See
# the module documentation for why it is a literal rather than something this
# package generates.
PROBE_FRAME: Final[bytes] = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA0JCgsKCA0LCgsODg0PEyAVExISEyccHhcg"
    "LikxMC4pLSwzOko+MzZGNywtQFdBRkxOUlNSMj5aYVpQYEpRUk//2wBDAQ4ODhMREyYV"
    "FSZPNS01T09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09P"
    "T09PT0//wAARCAAYACADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQF"
    "BgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEI"
    "I0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNk"
    "ZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLD"
    "xMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEB"
    "AQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJB"
    "UQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZH"
    "SElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaan"
    "qKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oA"
    "DAMBAAIRAxEAPwAooooAKKKKACiiigAooooA/9k=",
)


class SessionLink:
    """Opens one session to the groundstation and remembers what happened.

    Three checks ask about the same session, so the work happens on the first
    `inspect` and every later call gets the same report. That is what keeps the
    checks independent without making them expensive: each reports its own
    outcome about one shared piece of evidence.
    """

    def __init__(
        self,
        *,
        url: str,
        credential: Credential,
        capabilities: Sequence[Capability] = (),
        timeout: float = 10.0,
        staleness: float = 2.0,
        open_transport: TransportFactory = open_websocket,
        frame: bytes = PROBE_FRAME,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Describe a session without opening one.

        Args:
            url: Where the groundstation serves its session endpoint.
            credential: What to present. Held in a type that will not print
                itself.
            capabilities: What to offer during negotiation.
            timeout: The bound on the whole measurement — opening the session,
                sending the frame, and waiting for the result that answers it.
                One bound rather than one per step, because a bound that
                restarted at each step would describe a part of the run rather
                than the run, and every step here is one a wedged service can
                stop dead.
            staleness: How long a result stays worth acting on, passed through
                to the client.
            open_transport: How to open the connection. Injected so the
                failure paths can be exercised without a network; the
                transport is otherwise always the real one.
            frame: What to send to measure the round trip.
            clock: The monotonic source establishment and the run's remaining
                budget are measured against.
        """
        self._url = url
        self._credential = credential
        self._capabilities = tuple(capabilities)
        self._timeout = timeout
        self._staleness = staleness
        self._open_transport = open_transport
        self._frame = frame
        self._clock = clock
        self._client: SessionClient | None = None
        self._report: LinkReport | None = None

    async def inspect(self) -> LinkReport:
        """Open the session if it is not open, and report what happened.

        Returns:
            The evidence, the same on every call.
        """
        if self._report is None:
            self._report = await self._measure()
        return self._report

    async def aclose(self) -> None:
        """Say goodbye to the session, if one was ever opened."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _measure(self) -> LinkReport:
        """Open a session, send one frame, and time both.

        Returns:
            What happened, with a complaint in place of whichever measurement
            could not be taken.
        """
        offered = tuple(capability.name for capability in self._capabilities)
        endpoint = redact_url(self._url)
        client = SessionClient(
            url=self._url,
            credential=self._credential,
            capabilities=self._capabilities,
            open_transport=self._open_transport,
            staleness_seconds=self._staleness,
        )
        self._client = client
        started = self._clock()
        # The whole measurement's bound, started before the connection rather
        # than after it. Every step below is one a wedged peer can stop dead —
        # opening the session, writing the frame, waiting for the answer — and
        # a diagnostic that hangs on any of them is not a diagnostic.
        deadline = started + self._timeout
        try:
            await asyncio.wait_for(client.connect(), timeout=self._timeout)
        except TimeoutError:
            return LinkReport(
                endpoint=endpoint,
                established=False,
                offered=offered,
                complaint=(
                    # Not "the connection was accepted": this bound covers
                    # opening the transport as well as the negotiation, so an
                    # unreachable host that never completes its handshake ends
                    # up here too, and a message naming only the wedged case
                    # would assert more than was observed.
                    f"no session was opened within {self._timeout}s; the "
                    f"connection or the negotiation never completed, which is "
                    f"what an unreachable or a wedged service looks like from "
                    f"outside"
                ),
            )
        except SessionClientError as error:
            return LinkReport(
                endpoint=endpoint,
                established=False,
                offered=offered,
                complaint=f"{type(error).__name__}: {error}",
            )
        establishment_ms = (self._clock() - started) * 1000.0
        agreement = client.agreement
        agreed = (
            ()
            if agreement is None
            else tuple(capability.name for capability in agreement.capabilities)
        )
        round_trip_ms, complaint = await self._time_one_frame(client, agreed, deadline)
        return LinkReport(
            endpoint=endpoint,
            established=True,
            offered=offered,
            agreed=agreed,
            establishment_ms=establishment_ms,
            round_trip_ms=round_trip_ms,
            result_complaint=complaint,
        )

    def _remaining(self, deadline: float) -> float:
        """Say how much of the run's bound is left.

        Args:
            deadline: When the measurement is out of time, on this link's
                clock.

        Returns:
            The seconds left, clamped at zero. A zero budget asks "is it ready
            now?", which is exactly the question once the bound has run out.
        """
        return max(0.0, deadline - self._clock())

    async def _time_one_frame(
        self,
        client: SessionClient,
        agreed: tuple[str, ...],
        deadline: float,
    ) -> tuple[float | None, str]:
        """Send one frame and time the first result that comes back.

        Both halves are bounded by what is left of the run, and the send is
        bounded for the same reason the wait is. Writing to a peer that has
        stopped reading fills the socket's buffer and then blocks — which is
        what an unhealthy link does, so an unbounded write would hang the
        command in exactly the case it was run to diagnose.

        Args:
            client: The connected client.
            agreed: What the two sides settled on.
            deadline: When the whole measurement is out of time.

        Returns:
            The round trip in milliseconds and an empty complaint, or `None`
            and the reason there is no measurement.
        """
        if not agreed:
            return None, "no capability was agreed, so nothing would answer a frame"
        try:
            header = await asyncio.wait_for(
                client.submit_frame(self._frame),
                timeout=self._remaining(deadline),
            )
        except TimeoutError:
            return None, (
                f"the frame could not be written within the run's bound of "
                f"{self._timeout}s, which is what writing to a peer that has "
                f"stopped reading looks like"
            )
        except SessionClientError as error:
            return None, f"{type(error).__name__}: {error}"
        if header is None:
            return None, "the session dropped the frame before it went out"
        results = client.results()
        try:
            result = await asyncio.wait_for(
                anext(results),
                timeout=self._remaining(deadline),
            )
        except TimeoutError:
            errors = client.stats.errors_received
            if errors:
                return None, (
                    f"the groundstation answered with {errors} error(s) rather "
                    f"than a result, so nothing could be timed"
                )
            return (
                None,
                f"no result came back within the run's bound of {self._timeout}s",
            )
        except SessionClientError as error:
            return None, f"{type(error).__name__}: {error}"
        finally:
            # Closed whichever way the wait ended. A `wait_for` that timed out
            # has already cancelled the pending `anext`, which closes the
            # generator underneath it; closing again is what makes the path
            # where a result *did* arrive leave nothing behind either.
            await results.aclose()
        if result.round_trip_seconds is None:
            return None, (
                "the result came back carrying a timing this run did not stamp, "
                "so it cannot be attributed to the frame that was sent"
            )
        return result.round_trip_seconds * 1000.0, ""
