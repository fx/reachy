"""What the groundstation link reports when there is no groundstation.

The session that works is exercised against a real groundstation over a real
transport, in `cli/reachyctl/tests/test_reachyctl_doctor_integration.py`,
because a session is the one thing a fake cannot be evidence about. What is
tested here is the other half: the shapes this class produces when the far end
refuses, hangs, or was never asked twice.

No test here waits for a timeout to elapse. The wedged case configures a bound
that is already spent by the time the event loop looks at it, so what is
exercised is the branch rather than the clock — see `REVIEW.md`.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final

import pytest
from checks_support import CREDENTIAL, ENDPOINT

from reachy_checks import PROBE_FRAME, SessionLink
from reachy_contracts import FACE_CAPABILITY, Capability, SessionAgreement
from reachy_session_client import (
    ClientTransport,
    ConnectionFailedError,
    Credential,
    MessageKind,
    TransportFactory,
    encode_control,
)

# A bound that has already run out by the time the event loop is asked about
# it. Nothing waits, so no outcome here turns on how loaded the runner is.
_ALREADY_ELAPSED: Final = 0.0

FACE: Final = Capability(name=FACE_CAPABILITY, version=1)

if TYPE_CHECKING:
    from collections.abc import Callable


class _SilentTransport(ClientTransport):
    """A connection that is accepted and then answers nothing.

    This is what a wedged service looks like from outside: the socket accepts,
    the handshake completes, and the offer is never answered. The real
    groundstation cannot be made to behave this way without breaking it, and it
    is the client's bound rather than the server's behaviour under test.
    """

    def __init__(self) -> None:
        """Create a transport that has been closed zero times."""
        self.closed = False

    async def send_text(self, text: str) -> None:
        """Accept a control message and do nothing with it.

        Args:
            text: The already-framed message.
        """

    async def send_bytes(self, data: bytes) -> None:
        """Accept a frame and do nothing with it.

        Args:
            data: The already-framed binary message.
        """

    async def receive(self) -> str:
        """Never answer.

        Returns:
            Never. Waits on an event nothing sets, until cancelled.
        """
        await asyncio.Event().wait()
        raise AssertionError  # pragma: no cover - the wait above never returns

    async def close(self) -> None:
        """Record that the connection was closed."""
        self.closed = True


class _RefusingFactory:
    """A transport factory that never connects.

    Attributes:
        attempts: How many connections were tried, which is what a test asserts
            when it wants to know that a report came from one session.
    """

    def __init__(self, detail: str = "the connection was refused") -> None:
        """Describe how connecting fails.

        Args:
            detail: What the failure says.
        """
        self._detail = detail
        self.attempts = 0

    async def __call__(self, url: str) -> ClientTransport:
        """Fail to connect.

        Args:
            url: Where it would have connected.

        Returns:
            Never.

        Raises:
            ConnectionFailedError: Always.
        """
        del url
        self.attempts += 1
        raise ConnectionFailedError(self._detail)


class _SilentFactory:
    """A transport factory handing out connections that answer nothing."""

    def __init__(self) -> None:
        """Create a factory that has handed out nothing yet."""
        self.transports: list[_SilentTransport] = []

    async def __call__(self, url: str) -> ClientTransport:
        """Hand out a connection that will never answer.

        Args:
            url: Where it connected.

        Returns:
            The transport.
        """
        del url
        transport = _SilentTransport()
        self.transports.append(transport)
        return transport


class _StalledWriteTransport(ClientTransport):
    """A connection that negotiates and then stops draining what is written.

    This is the shape of an unhealthy link that a bound on the handshake alone
    would not catch: the session opens, and the write of the frame is what
    never completes. The agreement it answers with is built from the contracts
    package rather than written out by hand, so a change to the wire type is a
    change this transport gets for free.

    Attributes:
        spent: Called once the offer has been answered, so a test can declare
            the run's budget used up before anything is written.
    """

    def __init__(self, spent: Callable[[], None]) -> None:
        """Create a transport that will answer one offer.

        Args:
            spent: Called when the offer is answered.
        """
        self._answered = False
        self._spent = spent
        self.writes = 0

    async def send_text(self, text: str) -> None:
        """Accept the offer.

        Args:
            text: The already-framed message.
        """

    async def send_bytes(self, data: bytes) -> None:
        """Accept a frame and never finish writing it.

        Args:
            data: The already-framed binary message, deliberately unused.
        """
        del data
        self.writes += 1
        await asyncio.Event().wait()

    async def receive(self) -> str:
        """Answer the offer once, and nothing after that.

        Returns:
            The agreement.
        """
        if self._answered:
            await asyncio.Event().wait()
        self._answered = True
        self._spent()
        return _agreement()

    async def close(self) -> None:
        """End the connection."""


class _QuietTransport(ClientTransport):
    """A connection that negotiates, takes the frame, and then says nothing.

    The failure the round-trip check exists to report: the link is up, the
    frame went out, and whatever was supposed to answer it never did.

    Attributes:
        writes: How many frames were accepted.
    """

    def __init__(self, spent: Callable[[], None]) -> None:
        """Create a transport that will answer one offer and no frame.

        Args:
            spent: Called once the frame has been accepted, so a test can
                declare the run's budget used up before the wait for a result.
        """
        self._answered = False
        self._spent = spent
        self.writes = 0

    async def send_text(self, text: str) -> None:
        """Accept the offer.

        Args:
            text: The already-framed message.
        """

    async def send_bytes(self, data: bytes) -> None:
        """Accept a frame and answer nothing.

        Args:
            data: The already-framed binary message, deliberately unused.
        """
        del data
        self.writes += 1
        self._spent()

    async def receive(self) -> str:
        """Answer the offer once, and nothing after that.

        Returns:
            The agreement.
        """
        if self._answered:
            await asyncio.Event().wait()
        self._answered = True
        return _agreement()

    async def close(self) -> None:
        """End the connection."""


def _agreement() -> str:
    """Build the answer a groundstation sends to an offer.

    Built from the contracts package rather than written out by hand, so a
    change to the wire type is one these transports get for free.

    Returns:
        The control message agreeing to the face capability.
    """
    return encode_control(
        MessageKind.AGREEMENT,
        SessionAgreement(capabilities=(FACE,)),
    )


def _link(open_transport: TransportFactory, timeout: float = 5.0) -> SessionLink:
    """Build a link pointed at a reserved address.

    Args:
        open_transport: How to open the connection.
        timeout: The bound on opening the session and on waiting for a result.

    Returns:
        The link.
    """
    return SessionLink(
        url=ENDPOINT,
        credential=Credential(CREDENTIAL),
        capabilities=(FACE,),
        timeout=timeout,
        open_transport=open_transport,
    )


@pytest.mark.asyncio
async def test_a_refused_connection_is_reported_rather_than_raised() -> None:
    """A groundstation that is not there is a diagnosis, not an exception."""
    link = _link(_RefusingFactory())

    report = await link.inspect()

    assert not report.established
    assert "ConnectionFailedError" in report.complaint
    assert "the connection was refused" in report.complaint
    assert report.endpoint == ENDPOINT
    assert report.offered == (FACE_CAPABILITY,)
    assert report.round_trip_ms is None


@pytest.mark.asyncio
async def test_a_wedged_service_is_told_apart_from_a_slow_one() -> None:
    """Accepting a connection and never answering the offer is bounded, not waited out."""
    link = _link(_SilentFactory(), timeout=_ALREADY_ELAPSED)

    report = await link.inspect()

    assert not report.established
    assert "no session was opened within" in report.complaint


@pytest.mark.asyncio
async def test_the_session_is_opened_once_however_often_it_is_asked() -> None:
    """Three checks share one session; opening three would measure three moments."""
    factory = _RefusingFactory()
    link = _link(factory)

    first = await link.inspect()
    second = await link.inspect()

    assert first is second
    # `connect` makes exactly one attempt, so one attempt here means `inspect`
    # did not open a second session for the second caller.
    assert factory.attempts == 1


@pytest.mark.asyncio
async def test_closing_a_link_that_never_opened_a_session_does_nothing() -> None:
    """`aclose` is called on every path out of a run, including the ones that failed early."""
    link = _link(_RefusingFactory())

    await link.aclose()

    assert await link.inspect() is not None


@pytest.mark.asyncio
async def test_closing_a_link_after_a_failed_session_is_safe() -> None:
    """The client exists even when it never connected, and is said goodbye to."""
    link = _link(_RefusingFactory())

    await link.inspect()
    await link.aclose()
    await link.aclose()


def test_the_probe_frame_is_a_real_compressed_image() -> None:
    """A payload the far end cannot decode would measure a decode failure, not a link."""
    assert PROBE_FRAME.startswith(b"\xff\xd8\xff")
    assert PROBE_FRAME.endswith(b"\xff\xd9")
    assert len(PROBE_FRAME) < 2048


@pytest.mark.asyncio
async def test_writing_the_frame_is_bounded_by_the_run_s_budget() -> None:
    """A peer that stops reading blocks the write, and a diagnostic must not block.

    The clock is driven rather than waited on: the transport declares the run's
    budget spent as it answers the offer, so what is left when the frame is
    about to be written is zero and the bound is applied without any wall time
    passing. What is asserted is that the write is bounded at all — with no
    budget left the send is abandoned before it starts, which is the same
    branch a real stalled write reaches.
    """
    elapsed = 0.0

    def clock() -> float:
        """Read a clock the transport moves.

        Returns:
            The current reading.
        """
        return elapsed

    def spend() -> None:
        """Declare the run's whole budget used up."""
        nonlocal elapsed
        elapsed += 60.0

    transports: list[_StalledWriteTransport] = []

    async def open_transport(url: str) -> ClientTransport:
        """Hand out a connection that negotiates and then stalls.

        Args:
            url: Ignored.

        Returns:
            The transport.
        """
        del url
        transport = _StalledWriteTransport(spend)
        transports.append(transport)
        return transport

    link = SessionLink(
        url=ENDPOINT,
        credential=Credential(CREDENTIAL),
        capabilities=(FACE,),
        timeout=30.0,
        open_transport=open_transport,
        clock=clock,
    )
    try:
        report = await link.inspect()
    finally:
        await link.aclose()

    assert report.established
    assert report.agreed == (FACE_CAPABILITY,)
    assert report.round_trip_ms is None
    assert "could not be written within the run's bound" in report.result_complaint


@pytest.mark.asyncio
async def test_a_link_that_takes_the_frame_and_answers_nothing_is_named() -> None:
    """A session up and a capability silent is what this check exists to report.

    The budget is spent by the transport as it accepts the frame, so the wait
    for a result begins with nothing left and ends without any wall time
    passing.
    """
    elapsed = 0.0

    def clock() -> float:
        """Read a clock the transport moves.

        Returns:
            The current reading.
        """
        return elapsed

    def spend() -> None:
        """Declare the run's whole budget used up."""
        nonlocal elapsed
        elapsed += 60.0

    async def open_transport(url: str) -> ClientTransport:
        """Hand out a connection that negotiates and then goes quiet.

        Args:
            url: Ignored.

        Returns:
            The transport.
        """
        del url
        return _QuietTransport(spend)

    link = SessionLink(
        url=ENDPOINT,
        credential=Credential(CREDENTIAL),
        capabilities=(FACE,),
        timeout=30.0,
        open_transport=open_transport,
        clock=clock,
    )
    try:
        report = await link.inspect()
    finally:
        await link.aclose()

    assert report.established
    assert report.round_trip_ms is None
    assert "no result came back within the run's bound" in report.result_complaint
