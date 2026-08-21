"""One session, from the offer that opens it to the close that ends it.

This is the client half of the robot link, and there is exactly one of it. Both
`reachyctl probe` and the robot's groundstation adapter import this class, which
is what reachyctl REQ-057 asks for: a probe that fails the way the robot would,
because there is no separate protocol path for testing to succeed on.

The shape is deliberately task-free. The client owns no background coroutine; a
consumer submits frames from wherever it produces them and iterates `results()`
from wherever it applies them, and the reconnection loop lives inside that
iteration. A client that spawned its own task would have to decide where a
failure raised inside it surfaces, and the honest answer — at the consumer, on
its next await — is what iterating already does.

Two rules about time run through the whole module.

Capture tokens are minted from a monotonic clock and interpreted by nobody but
this client. The groundstation copies a token from a frame onto the result and
never reads it, so a round-trip measurement is a single-clock subtraction that
happens to have crossed two machines.

And every delay is injected. The staleness window is read from a clock the
caller supplies and the reconnection delay is awaited through a sleep the caller
supplies, so a test drives an hour of outage without waiting for one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

from reachy_contracts import (
    CloseReason,
    FrameHeader,
    SessionAgreement,
    SessionClose,
    SessionOffer,
)
from reachy_session_client.backoff import DEFAULT_BACKOFF
from reachy_session_client.clock import MonotonicStamps
from reachy_session_client.errors import (
    ConnectionFailedError,
    NotConnectedError,
    ProtocolError,
    SessionClientError,
    SessionRefusedError,
    describe_validation,
)
from reachy_session_client.framing import (
    FramingError,
    MessageKind,
    decode_control,
    encode_control,
    encode_frame,
)
from reachy_session_client.results import FrameResult, SessionStats, result_model_for
from reachy_session_client.transport import open_websocket

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
    from types import TracebackType

    from reachy_contracts import Capability, CapabilityName
    from reachy_session_client.backoff import Backoff
    from reachy_session_client.credential import Credential
    from reachy_session_client.transport import ClientTransport, TransportFactory

__all__ = ["SessionClient", "validate_session_url"]

# The schemes a session can be opened on. Checked when the client is built
# rather than when it connects, because a URL that does not name one is a
# configuration mistake, and retrying a configuration mistake with a growing
# delay is a way to never be told about it.
_SCHEMES: Final = frozenset({"ws", "wss"})

# How long a consumer keeps acting on the last result it received. Against a
# link whose idle round-trip is 100-170 ms with 700 ms spikes, two seconds is
# several spikes' worth of grace and still well inside the time a person notices
# a robot tracking something that has left the room.
_STALENESS_SECONDS: Final = 2.0

# What the client says on its way out. Never a credential.
_GOODBYE: Final = "the client is finished with this session"


def validate_session_url(url: str) -> str:
    """Refuse an address that is not one a session can be opened on.

    Public because a consumer wants to refuse a bad address *before* it has
    built anything around it — a CLI reporting "that is not a session URL"
    beside the option that carried it is a better answer than the same sentence
    arriving from a constructor three layers down. There is one rule and it
    lives here, so the check a caller runs early is the check the client runs.

    A URL carrying user information is refused, and that is a privacy rule
    rather than a syntax one. The address is repeated into verbose output, into
    a report, and into the text of a connection failure, and what redacts a
    credential knows the credential it was *given* — not one somebody embedded
    in an address. `wss://someone:secret@host/v1/session` would therefore reach
    output whole, which reachyctl REQ-059 forbids. The credential is presented
    in the offer, and this is what makes "the URL carries no credential" true
    rather than merely intended.

    Args:
        url: The address to check.

    Returns:
        The same address, so this can be used where the value is being passed.

    Raises:
        ValueError: If the address does not name a WebSocket scheme, or if it
            carries user information.
    """
    parts = urlsplit(url)
    if parts.scheme not in _SCHEMES:
        message = (
            f"a session URL is ws:// or wss://, not {parts.scheme or 'a bare address'}"
        )
        raise ValueError(message)
    if parts.username is not None or parts.password is not None:
        # Deliberately quoting nothing back: the value being refused is the one
        # thing that must not be repeated.
        message = (
            "a session URL carries no credential; remove the user information "
            "before the @ and present the credential the ordinary way"
        )
        raise ValueError(message)
    return url


#:= docs/specs/reachyctl/index.md#req-057-the-probe-exercises-the-real-session-protocol
#:% The probe command MUST establish a session using the same protocol
#:% implementation the robot application uses.
#
#:= docs/specs/robot-link/index.md#req-011-one-session-carries-every-exchange
#:% All frames, results, and control messages for a running app MUST travel over a
#:% single session, established once and reused for the lifetime of that session.
class SessionClient:
    """Holds one session at a time and re-establishes it when it drops."""

    def __init__(
        self,
        *,
        url: str,
        credential: Credential,
        capabilities: Sequence[Capability] = (),
        open_transport: TransportFactory = open_websocket,
        backoff: Backoff = DEFAULT_BACKOFF,
        staleness_seconds: float = _STALENESS_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Describe a session without opening one.

        Args:
            url: Where the groundstation serves its session endpoint.
            credential: What to present. Held in a type that will not print
                itself, and revealed at exactly one call site.
            capabilities: What this client can speak, each at one version.
                Negotiation matches on name and version exactly, and a
                capability that does not match is simply absent from the
                agreement.
            open_transport: How to open a connection. Injected so that a test
                induces a reconnection by handing over a factory that fails for
                a while and then does not.
            backoff: How long to wait between successive failed attempts.
            staleness_seconds: How long a result stays worth acting on.
            clock: The monotonic source frames are stamped from and staleness
                is measured against.
            sleep: How to wait out a reconnection delay. Injected so a test
                drives a long outage without spending one.

        Raises:
            ValueError: If the URL does not name a WebSocket scheme, or if the
                staleness window is not positive.
        """
        validate_session_url(url)
        if staleness_seconds <= 0:
            message = f"the staleness window must be positive, not {staleness_seconds}"
            raise ValueError(message)

        self._url = url
        self._credential = credential
        self._capabilities = tuple(capabilities)
        self._open_transport = open_transport
        self._backoff = backoff
        self._staleness_seconds = staleness_seconds
        self._stamps = MonotonicStamps(clock)
        self._sleep = sleep

        self._transport: ClientTransport | None = None
        self._agreement: SessionAgreement | None = None
        self._sequence = 0
        self._highest_applied: int | None = None
        self._latest: FrameResult | None = None
        self._closed = False
        self._stats = SessionStats()
        # Establishing and discarding a session touch every piece of state
        # above. `submit_frame` and `results()` run in different tasks, so one
        # of them can be reconnecting while the other is trying to send.
        self._lock = asyncio.Lock()

    @property
    def url(self) -> str:
        """Where this client connects.

        Returns:
            The session URL. It carries no credential — the credential travels
            in the offer, so it cannot reach a log line that records a URL.
        """
        return self._url

    @property
    def connected(self) -> bool:
        """Whether a session is up right now.

        Returns:
            True while a negotiated session is held.
        """
        return self._transport is not None

    @property
    def agreement(self) -> SessionAgreement | None:
        """What the two sides settled on for the session now held.

        Returns:
            The agreement, or `None` when no session is established.
        """
        return self._agreement

    @property
    def stats(self) -> SessionStats:
        """What this client has done so far.

        Returns:
            The running counters. They survive a reconnection; the sequence
            numbers do not, because those belong to one session.
        """
        return self._stats

    def agreed(self, name: CapabilityName) -> Capability | None:
        """Look up whether a capability survived negotiation.

        A capability that was offered and is not here is an ordinary outcome,
        not a failure: two components upgrade at different times, and the
        session continues with whatever else was agreed.

        Args:
            name: The capability's name.

        Returns:
            The agreed capability at its agreed version, or `None`.
        """
        if self._agreement is None:
            return None
        for capability in self._agreement.capabilities:
            if capability.name == name:
                return capability
        return None

    async def __aenter__(self) -> SessionClient:
        """Open the session.

        Returns:
            This client, with a session established.
        """
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the session.

        Args:
            exc_type: The exception type leaving the block, if any.
            exc: The exception leaving the block, if any.
            traceback: Its traceback, if any.
        """
        del exc_type, exc, traceback
        await self.aclose()

    async def connect(self) -> SessionAgreement:
        """Open a session, making exactly one attempt.

        One attempt rather than a retry loop, deliberately. The first connection
        is where a wrong address or a wrong credential shows up, and a caller
        that has just been given both should hear about it now rather than
        watching a growing delay. REQ-018's automatic re-establishment is about
        a session that dropped, which is what `results()` handles.

        Returns:
            What the two sides agreed to speak.

        Raises:
            ConnectionFailedError: If the connection could not be opened.
            ProtocolError: If the groundstation answered with something other
                than an agreement.
            SessionRefusedError: If the groundstation closed the session
                instead, which is what a rejected credential looks like.
        """
        async with self._lock:
            if self._agreement is not None:
                return self._agreement
            self._closed = False
            return await self._establish()

    async def aclose(self) -> None:
        """Say goodbye and end the session.

        Closing is idempotent, and it stops `results()` reconnecting: a
        consumer that has finished is not a session that dropped.
        """
        # Set before the lock is taken, not after. A reconnection in progress
        # holds the lock across its delay, and this is what it reads when the
        # delay is over — otherwise closing a client that happened to be
        # retrying would wait out the retry first.
        self._closed = True
        async with self._lock:
            transport = self._transport
            self._transport = None
            self._agreement = None
            if transport is None:
                return
            goodbye = encode_control(
                MessageKind.CLOSE,
                SessionClose(reason=CloseReason.GOING_AWAY, detail=_GOODBYE),
            )
            # A connection that has already gone cannot be told anything, and
            # this client is leaving in either case.
            with contextlib.suppress(ConnectionFailedError):
                await transport.send_text(goodbye)
            await transport.close()

    #:= docs/specs/robot-link/index.md#req-014-results-are-keyed-to-the-frame-that-produced-them
    #:% Every frame MUST carry a monotonically increasing sequence number, and every
    #:% result MUST identify the sequence number of the frame it derives from.
    #
    async def submit_frame(self, payload: bytes) -> FrameHeader | None:
        """Send one already-compressed frame.

        **This client holds no outbound queue, and that is the whole of what it
        offers about overload.** A frame produced while no session is up is
        dropped and counted rather than held against a connection that may
        never come back. A frame produced while one is up is handed to the
        link, and the await that follows is the link's own flow control: the
        producer is paced by how fast the frame can leave, which is
        backpressure rather than a queue growing behind it.

        Robot-link REQ-015 is deliberately **not** cited here, and the reason is
        worth writing down because the obvious alternative is worse. Returning
        before the frame is on the wire would mean holding it somewhere — a
        buffer of one — and dropping whatever arrived while it waited. That
        discards the *newest* frame and keeps the oldest, which is the exact
        inversion of what the requirement asks for, and the only way to do it
        the right way round would be to abandon a partly-sent frame, which
        corrupts the stream. The requirement is about the groundstation's
        queue, the groundstation implements it with a bounded one that drops
        the oldest, and this side has no queue for it to be about.

        Args:
            payload: The frame's bytes, already compressed by the capture
                hardware. The protocol never re-encodes them.

        Returns:
            The header the frame went out with, or `None` when there was no
            session to send it on and the frame was dropped.

        Raises:
            FramingError: If the frame cannot be packed — an empty payload, or
                a header that does not fit. Both are this caller's mistake
                rather than the link's, so neither is a dropped frame.
        """
        transport = self._transport
        if transport is None:
            self._stats.frames_dropped += 1
            return None
        header = FrameHeader(sequence=self._sequence, captured_at=self._stamps.stamp())
        message = encode_frame(header, payload)
        try:
            await transport.send_bytes(message)
        except ConnectionFailedError:
            # The session went away between the check and the send. The results
            # loop is already unwinding on the same event and is the one that
            # reconnects; this frame is simply gone.
            self._stats.frames_dropped += 1
            return None
        self._sequence += 1
        self._stats.frames_submitted += 1
        return header

    async def results(self) -> AsyncGenerator[FrameResult, None]:
        """Yield every result worth acting on, re-establishing the session as needed.

        This is the only caller of the transport's `receive`, and the only place
        a session is re-established, so a consumer's `async for` is also the
        loop that keeps the link up.

        The type is an async generator rather than merely an iterator because
        closing it is part of using it: a consumer that stops early closes the
        iteration, and that is what stops the client trying to reconnect on its
        behalf.

        What does not come out of here is as important as what does. A result
        for a frame older than one already applied is discarded as superseded, a
        result naming a capability this build cannot parse is ignored, and a
        `SessionError` is counted — none of the three is something a consumer
        can act on, and all three are visible in `stats`.

        Yields:
            Each result, in the order the groundstation sent it.

        Raises:
            ProtocolError: If the groundstation sent something this protocol
                does not describe. Retrying would turn a defect into a
                reconnection storm.
            SessionRefusedError: If the groundstation closed the session for a
                reason that will not resolve itself.
        """
        while True:
            # Checked at the top rather than in the handler below, because
            # `_reconnect` returns without a session once the client has been
            # closed, and this is where that answer is acted on.
            if self._closed:
                return
            try:
                text = await self._receive()
                outcome = self._interpret(text)
            except ConnectionFailedError:
                await self._reconnect()
                continue
            if outcome is not None:
                yield outcome

    #:= docs/specs/robot-link/index.md#req-017-stale-results-stop-being-acted-on
    #:% A consumer MUST stop acting on results once none has arrived within a configured
    #:% staleness window.
    def latest(self) -> FrameResult | None:
        """The most recent result, while it is still worth acting on.

        Staleness is enforced here rather than published as a flag, so that a
        consumer which simply asks for the latest result stops acting when the
        results stop arriving. A flag would have to be remembered, and the
        failure of remembering it is a robot going on tracking a face that left
        the room a minute ago.

        Returns:
            The last applied result, or `None` when none has arrived within the
            staleness window — including when none has arrived at all.
        """
        if self._latest is None:
            return None
        if self._stamps.now() - self._latest.received_at >= self._staleness_seconds:
            return None
        return self._latest

    @property
    def stale(self) -> bool:
        """Whether there is anything current to act on.

        Returns:
            True when the staleness window has elapsed with no result, and also
            before the first result arrives — in both cases there is nothing
            this client can honestly offer a consumer.
        """
        return self.latest() is None

    #:= docs/specs/robot-link/index.md#req-018-reconnection-is-automatic-and-rate-limited
    #:% A client MUST re-establish a dropped session automatically, and MUST increase
    #:% the delay between successive failed attempts up to a bound.
    async def _reconnect(self) -> None:
        """Wait out a growing delay and negotiate again, until it works.

        Negotiation happens again from scratch. It is never resumed from the
        agreement the previous session reached: a reconnection is most often
        caused by a restart, and a restart is the likeliest moment for the
        capability set to have changed, so caching would break exactly the case
        it was meant to speed up.

        Raises:
            ProtocolError: If the groundstation answers with something this
                protocol does not describe. Not retried.
            SessionRefusedError: If it refuses the session. Not retried either:
                a credential it does not accept is not a thing a delay fixes,
                and looping on it would hide the one failure needing a person.
        """
        async with self._lock:
            await self._discard()
            attempt = 0
            while not self._closed:
                attempt += 1
                await self._sleep(self._backoff.delay(attempt))
                try:
                    await self._establish()
                except ConnectionFailedError:
                    continue
                self._stats.reconnections += 1
                return

    async def _establish(self) -> SessionAgreement:
        """Open a connection, present the credential and negotiate.

        Returns:
            What the two sides agreed to speak.

        Raises:
            ConnectionFailedError: If the connection could not be opened or
                dropped during the handshake.
            ProtocolError: If the answer was not an agreement.
            SessionRefusedError: If the groundstation closed the session.
        """
        self._stats.connection_attempts += 1
        transport = await self._open_transport(self._url)
        try:
            agreement = await self._negotiate(transport)
        except SessionClientError:
            await transport.close()
            raise
        self._transport = transport
        self._agreement = agreement
        # Sequence numbers are meaningful only against the session they were
        # sent on, so a new session starts over — and nothing received on the
        # last one can supersede anything on this one.
        self._sequence = 0
        self._highest_applied = None
        return agreement

    #:= docs/specs/robot-link/index.md#req-012-capabilities-are-negotiated-at-session-start
    #:% Both sides MUST exchange the set of capabilities they support, each with a
    #:% version, before any capability-specific message is sent.
    async def _negotiate(self, transport: ClientTransport) -> SessionAgreement:
        """Send the offer and read the answer, before anything else is sent.

        Args:
            transport: The connection, freshly opened.

        Returns:
            The agreed set, possibly empty.

        Raises:
            ConnectionFailedError: If the connection dropped mid-handshake.
            ProtocolError: If the answer was not an agreement, or did not parse.
            SessionRefusedError: If the groundstation closed the session
                instead of answering.
        """
        await transport.send_text(encode_control(MessageKind.OFFER, self._offer()))
        kind, payload = self._decode(await transport.receive())
        if kind is MessageKind.CLOSE:
            raise self._refusal(payload)
        if kind is not MessageKind.AGREEMENT:
            message = f"a session opens with an agreement, not {kind.value!r}"
            raise ProtocolError(message)
        try:
            agreement = SessionAgreement.from_wire(payload)
        except ValueError as error:
            message = f"the agreement did not parse: {describe_validation(error)}"
            raise ProtocolError(message) from error
        # Negotiation reduces an offer; it does not add to one. A groundstation
        # that agreed to something nobody offered has not negotiated, and
        # accepting it would let a session carry capability traffic this client
        # never said it could speak — which is the thing REQ-012 exists to stop.
        # Reported rather than quietly trimmed: this is a defect in the other
        # side, and a client that silently corrected it would hide it.
        unoffered = set(agreement.capabilities) - set(self._capabilities)
        if unoffered:
            named = ", ".join(sorted(f"{one.name}:{one.version}" for one in unoffered))
            message = f"the agreement names capabilities that were not offered: {named}"
            raise ProtocolError(message)
        return agreement

    def _offer(self) -> SessionOffer:
        """Build the opening message, revealing the credential to do it.

        This is the one call site that reveals the credential. Everywhere else
        it is a `Credential`, which renders as a placeholder.

        Returns:
            The offer to send.

        Raises:
            SessionClientError: If the offer does not validate — a capability
                name the contract refuses, most likely.
        """
        try:
            return SessionOffer.model_validate(
                {
                    "credential": self._credential.reveal(),
                    "capabilities": self._capabilities,
                },
            )
        except ValueError as error:
            message = f"the session offer is not valid: {describe_validation(error)}"
            # `from None` rather than `from error`: a pydantic validation error
            # renders the value it rejected into its own text, and this is the
            # one message that carries a credential. Chaining it would print
            # that value out of any handler that reports a cause.
            raise SessionClientError(message) from None

    def _refusal(self, payload: bytes) -> SessionClientError:
        """Turn a close message into the error it means.

        Args:
            payload: The canonical bytes of the `SessionClose`.

        Returns:
            The error to raise: a refusal for anything that needs a person, and
            a connection failure for an orderly goodbye, which is a
            groundstation shutting down and is answered by reconnecting.
        """
        try:
            close = SessionClose.from_wire(payload)
        except ValueError as error:
            message = f"the close message did not parse: {describe_validation(error)}"
            return ProtocolError(message)
        if close.reason is CloseReason.GOING_AWAY:
            return ConnectionFailedError(
                f"the groundstation is going away: {close.detail}",
            )
        return SessionRefusedError(close.reason.value, close.detail)

    async def _receive(self) -> str:
        """Read the next control message off the session.

        Returns:
            The message as it arrived.

        Raises:
            NotConnectedError: If no session has ever been established, which
                is a caller iterating results before connecting.
            ConnectionFailedError: If the connection ended.
        """
        transport = self._transport
        if transport is None:
            message = "no session is established; call connect first"
            raise NotConnectedError(message)
        return await transport.receive()

    def _interpret(self, text: str) -> FrameResult | None:
        """Work out what one control message means.

        Args:
            text: The message as it arrived.

        Returns:
            The result to hand the consumer, or `None` when the message was
            handled and there is nothing to act on.

        Raises:
            ConnectionFailedError: If the groundstation said it is going away.
            ProtocolError: If the message is not one this protocol describes in
                this direction.
            SessionRefusedError: If the groundstation ended the session for a
                reason that will not resolve itself.
        """
        kind, payload = self._decode(text)
        if kind is MessageKind.RESULT:
            return self._apply(payload)
        if kind is MessageKind.ERROR:
            # A failure report does not end the session, and it is not an empty
            # result: REQ-013 makes that a success, and it arrives as a result.
            self._stats.errors_received += 1
            return None
        if kind is MessageKind.CLOSE:
            raise self._refusal(payload)
        message = f"{kind.value!r} is not a groundstation message"
        raise ProtocolError(message)

    #:= docs/specs/robot-link/index.md#req-013-an-empty-result-is-a-valid-result
    #:% A result message carrying no detections MUST be treated as a successful result
    #:% for that frame.
    def _apply(self, payload: bytes) -> FrameResult | None:
        """Parse a result and decide whether it is still worth applying.

        A result carrying no detections travels this path exactly like one
        carrying ten: it is parsed, applied, counted in `results_applied` and
        handed to the consumer, and no error counter moves. The predecessor
        made the opposite choice and answered an empty payload with a 400.

        Args:
            payload: The canonical bytes of the result envelope.

        Returns:
            The result, or `None` when it was superseded or names a capability
            this build cannot parse.

        Raises:
            ProtocolError: If the bytes do not parse as the result type the
                named capability produces.
        """
        capability = self._capability_of(payload)
        if self.agreed(capability) is None:
            # Not agreed for this session, so nothing asked for it and nothing
            # downstream is prepared to read it. Ignored rather than refused,
            # for the same reason an unfamiliar capability is: a session that
            # ended over traffic it could simply drop would be worse.
            self._stats.results_ignored += 1
            return None
        model = result_model_for(capability)
        if model is None:
            # A capability this build has never heard of. Negotiation means
            # nothing asked for it, and refusing to parse it would make an
            # older robot unable to hold a session with a newer groundstation
            # rather than simply ignoring traffic it cannot use.
            self._stats.results_ignored += 1
            return None
        try:
            envelope = model.from_wire(payload)
        except ValueError as error:
            message = f"a result did not parse: {describe_validation(error)}"
            raise ProtocolError(message) from error

        if self._highest_applied is not None and envelope.sequence < (
            self._highest_applied
        ):
            # REQ-014: a newer frame has already been applied, so this one is
            # history. Strictly older, not older-or-equal — one frame produces
            # one result per agreed capability, and the second of them is not
            # superseded by the first.
            self._stats.results_superseded += 1
            return None
        self._highest_applied = envelope.sequence

        now = self._stamps.now()
        result = FrameResult(
            envelope=envelope,
            received_at=now,
            round_trip_seconds=self._stamps.age_of(envelope.captured_at, now),
        )
        self._stats.results_applied += 1
        self._latest = result
        return result

    def _capability_of(self, payload: bytes) -> str:
        """Read which capability a result names, before parsing the rest of it.

        The payload type a result carries depends on the capability that
        produced it, so the name has to be read first. It is read from the JSON
        rather than by trying each type in turn, which would make an unfamiliar
        capability indistinguishable from a malformed message.

        Args:
            payload: The canonical bytes of the result envelope.

        Returns:
            The capability name, or an empty string when the message does not
            carry one — which no registry holds, so it is ignored.
        """
        document = json.loads(payload)
        if not isinstance(document, dict):
            return ""
        name = document.get("capability")
        return name if isinstance(name, str) else ""

    def _decode(self, text: str) -> tuple[MessageKind, bytes]:
        """Unpack a control message, reporting a bad one as a protocol failure.

        Args:
            text: The message as it arrived.

        Returns:
            Its kind and the canonical bytes it carries.

        Raises:
            ProtocolError: If it is not a control message of this framing.
        """
        try:
            return decode_control(text)
        except FramingError as error:
            message = f"the groundstation sent an unusable message: {error}"
            raise ProtocolError(message) from error

    async def _discard(self) -> None:
        """Forget the session that dropped, without saying goodbye on it."""
        transport = self._transport
        self._transport = None
        self._agreement = None
        if transport is not None:
            await transport.close()
