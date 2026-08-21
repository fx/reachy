"""Fakes and builders shared by the session client's tests.

Everything here fakes something the client talks to — a connection, a clock, a
delay — and nothing here fakes the client. The session's own behaviour is always
the real one, and the integration tests drive the real transport against the real
groundstation.

Time is the reason most of this exists. The staleness window and the
reconnection delay are the two pieces of behaviour a test would otherwise have
to wait for, so the client takes both a clock and a sleep as parameters:
`ManualClock` advances when a test says so, and `RecordedSleep` advances it
without spending any wall time and keeps the delays for the test to assert
against. A suite that slept instead would be slower and less certain about
exactly the property it was checking.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final

from reachy_contracts import (
    FACE_CAPABILITY,
    GESTURE_CAPABILITY,
    Capability,
    CaptureTimestamp,
    CloseReason,
    ErrorCode,
    FaceDetection,
    FaceDetections,
    FrameHeader,
    GestureDetection,
    GestureDetections,
    NormalisedPoint,
    ResultEnvelope,
    SessionAgreement,
    SessionClose,
    SessionError,
)
from reachy_session_client import (
    ClientTransport,
    ConnectionFailedError,
    Credential,
    MessageKind,
    encode_control,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from reachy_contracts import WireModel

__all__ = [
    "CREDENTIAL",
    "FACE",
    "GESTURE",
    "STAMP",
    "ManualClock",
    "RecordedSleep",
    "ScriptedTransports",
    "StubTransport",
    "agreement",
    "capability_names",
    "credential",
    "empty_face_result",
    "face_result",
    "gesture_result",
    "hand_control_to_the_event_loop",
    "session_close",
    "session_error",
]

# A placeholder credential. Not anybody's, and never a real one — see the root
# AGENTS.md on what may enter a tracked file in a public repository.
CREDENTIAL: Final = "example-credential"

FACE: Final = Capability(name=FACE_CAPABILITY, version=1)
GESTURE: Final = Capability(name=GESTURE_CAPABILITY, version=1)

# A capture token in the shape `MonotonicStamps` mints, for the tests that feed
# a result in from outside rather than round-tripping one the client sent.
STAMP: Final = "1000.000000"


def credential(value: str = CREDENTIAL) -> Credential:
    """Wrap a placeholder credential.

    Args:
        value: The secret to hold.

    Returns:
        The credential.
    """
    return Credential(value)


def agreement(*capabilities: Capability) -> str:
    """Build the answer a groundstation sends to an offer.

    Args:
        capabilities: What it agreed to, in the agreed order.

    Returns:
        The control message.
    """
    return encode_control(
        MessageKind.AGREEMENT,
        SessionAgreement(capabilities=capabilities),
    )


def _result(
    sequence: int,
    stamp: str,
    capability: str,
    payload: WireModel,
) -> str:
    """Build a result message for one frame.

    Args:
        sequence: The frame it answers.
        stamp: The capture token that frame carried.
        capability: Which capability produced it.
        payload: What that capability found.

    Returns:
        The control message.
    """
    return encode_control(
        MessageKind.RESULT,
        ResultEnvelope.for_frame(
            FrameHeader(sequence=sequence, captured_at=CaptureTimestamp(stamp)),
            capability,
            payload,
        ),
    )


def face_result(sequence: int, stamp: str = STAMP, faces: int = 1) -> str:
    """Build a face result carrying some detections.

    Args:
        sequence: The frame it answers.
        stamp: The capture token that frame carried.
        faces: How many faces to report.

    Returns:
        The control message.
    """
    return _result(
        sequence,
        stamp,
        FACE_CAPABILITY,
        FaceDetections(
            faces=tuple(
                FaceDetection(
                    centre=NormalisedPoint(x=0.25, y=-0.25),
                    confidence=0.5,
                )
                for _ in range(faces)
            ),
        ),
    )


def empty_face_result(sequence: int, stamp: str = STAMP) -> str:
    """Build a face result for a frame that contained no face.

    Args:
        sequence: The frame it answers.
        stamp: The capture token that frame carried.

    Returns:
        The control message.
    """
    return face_result(sequence, stamp, faces=0)


def gesture_result(sequence: int, stamp: str = STAMP) -> str:
    """Build a gesture result carrying one recognised signal.

    Args:
        sequence: The frame it answers.
        stamp: The capture token that frame carried.

    Returns:
        The control message.
    """
    return _result(
        sequence,
        stamp,
        GESTURE_CAPABILITY,
        GestureDetections(
            gestures=(GestureDetection(label="wave", confidence=0.75),),
        ),
    )


def session_error(
    code: ErrorCode = ErrorCode.CAPABILITY_FAILED,
    sequence: int | None = None,
) -> str:
    """Build a failure report, which does not end the session.

    Args:
        code: What kind of failure it is.
        sequence: The frame it concerns, when it concerns one.

    Returns:
        The control message.
    """
    return encode_control(
        MessageKind.ERROR,
        SessionError(code=code, detail="a capability declined", sequence=sequence),
    )


def session_close(
    reason: CloseReason = CloseReason.UNAUTHENTICATED,
    detail: str = "the credential presented is not the configured one",
) -> str:
    """Build the last message on a session.

    Args:
        reason: Why it is ending.
        detail: What to say about it, which is never a credential.

    Returns:
        The control message.
    """
    return encode_control(MessageKind.CLOSE, SessionClose(reason=reason, detail=detail))


async def hand_control_to_the_event_loop(turns: int = 100) -> None:
    """Let every other task run, without waiting on a clock.

    `asyncio.sleep(0)` yields to the event loop and resumes on its next pass: it
    reads no clock, schedules no timer and adds no wall time, so it is not the
    sleeping the no-input-or-output rule forbids. The number of turns is bounded
    so that a task which never reaches the state a test waits for fails that
    test rather than hanging the suite.

    Args:
        turns: How many times to yield.
    """
    for _ in range(turns):
        await asyncio.sleep(0)


class ManualClock:
    """A monotonic clock that only moves when a test moves it."""

    def __init__(self, start: float = 1000.0) -> None:
        """Start the clock somewhere plausible.

        Args:
            start: The first reading. Not zero, so that a test cannot pass by
                treating an unset value as the current time.
        """
        self._now = start

    def __call__(self) -> float:
        """Read the clock.

        Returns:
            The current reading, which is unchanged since the last advance.
        """
        return self._now

    def advance(self, seconds: float) -> None:
        """Move the clock forward.

        Args:
            seconds: How far. Never backwards: this stands in for a monotonic
                source, and one that went backwards would be testing something
                that cannot happen.

        Raises:
            ValueError: If asked to move backwards.
        """
        if seconds < 0:
            message = f"a monotonic clock does not go backwards: {seconds}"
            raise ValueError(message)
        self._now += seconds


class RecordedSleep:
    """A sleep that costs nothing and remembers what it was asked to wait."""

    def __init__(self, clock: ManualClock | None = None) -> None:
        """Create a sleep, optionally tied to a clock.

        Args:
            clock: A clock to advance by whatever is slept. Passing one is what
                makes "the delays elapsed" and "time passed" the same statement
                in a test.
        """
        self._clock = clock
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        """Record a delay and let the event loop run.

        Args:
            seconds: How long the caller wanted to wait.
        """
        self.delays.append(seconds)
        if self._clock is not None:
            self._clock.advance(seconds)
        await asyncio.sleep(0)


class StubTransport(ClientTransport):
    """A `ClientTransport` backed by a queue, for the tests that need no server.

    The integration tests use the real WebSocket. This exists for the unit tests
    of the client's own logic, where a server would be input and output for
    nothing.
    """

    def __init__(self) -> None:
        """Create a transport with nothing queued and nothing sent."""
        self.inbound: asyncio.Queue[str | BaseException] = asyncio.Queue()
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        self.closed = False
        self.sends_fail = False

    def push(self, *messages: str) -> None:
        """Queue messages from the groundstation.

        Args:
            messages: The control messages to deliver, in order.
        """
        for message in messages:
            self.inbound.put_nowait(message)

    def drop(self, detail: str = "the connection went away") -> None:
        """Make the next receive report that the connection ended.

        Args:
            detail: What the failure says.
        """
        self.inbound.put_nowait(ConnectionFailedError(detail))

    async def send_text(self, text: str) -> None:
        """Record an outgoing control message.

        Args:
            text: The already-framed message.

        Raises:
            ConnectionFailedError: When this transport was told to fail sends.
        """
        if self.sends_fail:
            message = "the connection went away mid-send"
            raise ConnectionFailedError(message)
        self.sent_text.append(text)

    async def send_bytes(self, data: bytes) -> None:
        """Record an outgoing frame.

        Args:
            data: The already-framed binary message.

        Raises:
            ConnectionFailedError: When this transport was told to fail sends.
        """
        if self.sends_fail:
            message = "the connection went away mid-send"
            raise ConnectionFailedError(message)
        self.sent_bytes.append(data)

    async def receive(self) -> str:
        """Take the next queued message.

        Returns:
            The message.

        Raises:
            BaseException: Whatever was queued in place of a message, which is
                how a dropped connection is delivered in order rather than as a
                flag a test has to time.
        """
        message = await self.inbound.get()
        if isinstance(message, BaseException):
            raise message
        return message

    async def close(self) -> None:
        """Record that the connection was closed."""
        self.closed = True


class ScriptedTransports:
    """A transport factory that hands out prepared connections in order.

    A `None` in the script is an attempt that fails to connect, which is how a
    test induces the outage that REQ-018's growing delay is about. Running off
    the end raises `AssertionError` rather than another connection failure, so a
    client that retried more often than the test expected fails the test instead
    of looping inside it.
    """

    def __init__(self, *steps: StubTransport | None) -> None:
        """Script a sequence of connection attempts.

        Args:
            steps: A transport for each attempt that should succeed, and `None`
                for each that should fail.
        """
        self._steps = list(steps)
        self.urls: list[str] = []

    @property
    def attempts(self) -> int:
        """How many times the client tried to connect.

        Returns:
            The number of attempts made so far.
        """
        return len(self.urls)

    async def __call__(self, url: str) -> ClientTransport:
        """Answer one connection attempt.

        Args:
            url: Where the client is trying to connect.

        Returns:
            The next scripted transport.

        Raises:
            ConnectionFailedError: When this attempt was scripted to fail.
            AssertionError: When the script has run out, which means the client
                retried more times than the test described.
        """
        self.urls.append(url)
        if not self._steps:
            message = f"the client made {self.attempts} attempts; the script had fewer"
            raise AssertionError(message)
        step = self._steps.pop(0)
        if step is None:
            detail = "scripted connection failure"
            raise ConnectionFailedError(detail)
        return step


def capability_names(capabilities: Sequence[Capability]) -> list[str]:
    """Name a capability set, for a readable assertion.

    Args:
        capabilities: What to name.

    Returns:
        The names, in order.
    """
    return [capability.name for capability in capabilities]
