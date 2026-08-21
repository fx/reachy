"""One session, from the offer that opens it to the close that ends it.

The lifecycle is the one the robot link spec describes, and the order matters:
the client presents a credential, the two sides agree on a capability set, and
only then does capability-specific traffic flow. A client that fails
authentication is closed without any negotiation taking place.

Negotiation happens exactly once per session and is never cached across
reconnections. That is not an optimisation left on the table — a reconnection is
most often caused by a restart, and a restart is the likeliest moment for the
capability set to have changed, so caching would make the one case that matters
the one case that breaks. A session holds nothing that outlives its connection.

Routing is by capability name against a registry this module cannot name the
members of. `just lint-capability-boundary` proves that: nothing under
`session/` may import `reachy_groundstation.capabilities` at all.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from reachy_contracts import (
    CloseReason,
    ErrorCode,
    SessionClose,
    SessionError,
    SessionOffer,
    negotiate,
)
from reachy_groundstation.faults import validation_summary
from reachy_groundstation.obs import frame_exemplar, get_logger, session_context
from reachy_groundstation.pipeline.queue import FrameQueue, QueuedFrame
from reachy_groundstation.pipeline.runner import FramePipeline
from reachy_groundstation.ports import AgreedCapability
from reachy_groundstation.session.auth import credential_is_valid
from reachy_groundstation.session.framing import (
    FramingError,
    MessageKind,
    decode_control,
    decode_frame,
    encode_control,
)
from reachy_groundstation.session.transport import (
    CLOSE_NORMAL,
    CLOSE_POLICY_VIOLATION,
    CLOSE_PROTOCOL_ERROR,
    TransportClosedError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from reachy_contracts import Capability, WireModel
    from reachy_groundstation.config import Settings
    from reachy_groundstation.obs import Observability
    from reachy_groundstation.ports import AgreedCapability, CapabilityRegistryPort
    from reachy_groundstation.session.transport import SessionTransport

__all__ = ["SessionOutcome", "SessionRunner", "new_session_id"]

_logger = get_logger(__name__)

# What a refused client is told. It says the credential was not accepted and
# nothing about which part of it was wrong.
_UNAUTHENTICATED_DETAIL = "the credential presented is not the configured one"


def new_session_id() -> str:
    """Mint an identifier for one session.

    Returns:
        An opaque identifier short enough to travel as a metric exemplar.
    """
    return uuid4().hex


@dataclass(frozen=True, slots=True)
class SessionOutcome:
    """What a finished session did, for the caller and for the tests.

    Attributes:
        session_id: The identifier every log line and exemplar carried.
        reason: Why the session ended.
        agreed: The capability set both sides settled on, possibly empty.
        frames_received: Frames accepted from the client.
        frames_dropped: Frames discarded because the queue was at its bound.
    """

    session_id: str
    reason: CloseReason
    agreed: tuple[Capability, ...]
    frames_received: int
    frames_dropped: int


#:= docs/specs/robot-link/index.md#req-011-one-session-carries-every-exchange
#:% All frames, results, and control messages for a running app MUST travel over a
#:% single session, established once and reused for the lifetime of that session.
class SessionRunner:
    """Drives one connection through its whole life."""

    def __init__(
        self,
        *,
        transport: SessionTransport,
        registry: CapabilityRegistryPort,
        settings: Settings,
        obs: Observability,
        session_id: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create a runner for one connection.

        Args:
            transport: The connection, already accepted.
            registry: What to negotiate against and route by name into.
            settings: The settings in effect.
            obs: Where timings, spans and log lines go.
            session_id: The identifier to use, minted when not supplied.
            clock: A monotonic source, injected so the arrival ordering the
                queue depends on is testable without sleeping.
        """
        self._transport = transport
        self._registry = registry
        self._settings = settings
        self._obs = obs
        self._session_id = session_id or new_session_id()
        self._clock = clock
        self._frames_received = 0
        self._frames_dropped = 0

    @property
    def session_id(self) -> str:
        """The identifier this session reports itself by.

        Returns:
            The session identifier.
        """
        return self._session_id

    async def run(self) -> SessionOutcome:
        """Carry the session from its offer to its close.

        Returns:
            What the session did.
        """
        with session_context(self._session_id):
            self._obs.metrics.sessions_active.inc()
            try:
                return await self._run()
            finally:
                self._obs.metrics.sessions_active.dec()

    async def _run(self) -> SessionOutcome:
        """Run the session, having bound its logging context.

        Returns:
            What the session did.
        """
        agreed: tuple[Capability, ...] = ()
        try:
            offer = await self._await_offer()
            if offer is None:
                return self._finish(CloseReason.PROTOCOL_ERROR, agreed)
            if not await self._authenticate(offer):
                return self._finish(CloseReason.UNAUTHENTICATED, agreed)
            capabilities, agreed = await self._agree(offer)
            reason = await self._pump(capabilities)
        except TransportClosedError:
            # The client vanished. That is ordinary: it reconnects and
            # negotiates again, against whatever this service offers by then.
            _logger.info("session.disconnected", frames=self._frames_received)
            return self._finish(CloseReason.GOING_AWAY, agreed)
        return self._finish(reason, agreed)

    def _finish(
        self,
        reason: CloseReason,
        agreed: tuple[Capability, ...],
    ) -> SessionOutcome:
        """Record how the session ended.

        Args:
            reason: Why it ended.
            agreed: What had been agreed, if anything.

        Returns:
            The outcome.
        """
        self._obs.metrics.sessions_total.labels(outcome=reason.value).inc()
        return SessionOutcome(
            session_id=self._session_id,
            reason=reason,
            agreed=agreed,
            frames_received=self._frames_received,
            frames_dropped=self._frames_dropped,
        )

    async def _send(self, kind: MessageKind, message: WireModel) -> None:
        """Frame a message and put it on the wire.

        Args:
            kind: Which contract type `message` is.
            message: The message to send.
        """
        await self._transport.send(encode_control(kind, message))

    async def _await_offer(self) -> SessionOffer | None:
        """Read the client's opening message.

        Returns:
            The offer, or `None` when the client sent something else or nothing
            in time — in which case it has already been closed.

        Raises:
            TransportClosedError: If the connection ended while waiting.
        """
        try:
            first = await asyncio.wait_for(
                self._transport.receive(),
                timeout=self._settings.handshake_timeout_seconds,
            )
        except TimeoutError:
            await self._refuse(
                CloseReason.PROTOCOL_ERROR,
                CLOSE_PROTOCOL_ERROR,
                "no offer within the handshake timeout",
            )
            return None

        if not isinstance(first, str):
            await self._refuse(
                CloseReason.PROTOCOL_ERROR,
                CLOSE_PROTOCOL_ERROR,
                "a session opens with an offer, not a frame",
            )
            return None

        if len(first) > self._settings.max_message_bytes:
            await self._refuse(
                CloseReason.PROTOCOL_ERROR,
                CLOSE_PROTOCOL_ERROR,
                f"opening message of {len(first)} characters exceeds the "
                f"configured maximum of {self._settings.max_message_bytes}",
            )
            return None

        try:
            kind, payload = decode_control(first)
        except FramingError as error:
            await self._refuse(
                CloseReason.PROTOCOL_ERROR,
                CLOSE_PROTOCOL_ERROR,
                f"opening message did not parse: {error}",
            )
            return None

        if kind is not MessageKind.OFFER:
            await self._refuse(
                CloseReason.PROTOCOL_ERROR,
                CLOSE_PROTOCOL_ERROR,
                f"a session opens with an offer, not {kind.value!r}",
            )
            return None

        try:
            return SessionOffer.from_wire(payload)
        except ValueError as error:
            # The exception's message is deliberately not repeated. An offer is
            # the one message that carries the credential, and pydantic puts the
            # offending input value into a validation error's text — so
            # forwarding it would write the presented credential into a close
            # reason and into whatever reads one. What is reported instead is
            # which fields failed and how, which is what a client can act on.
            await self._refuse(
                CloseReason.PROTOCOL_ERROR,
                CLOSE_PROTOCOL_ERROR,
                f"offer did not parse: {validation_summary(error)}",
            )
            return None

    #:= docs/specs/robot-link/index.md#req-019-sessions-are-authenticated
    #:% The groundstation MUST reject a session whose client does not present a valid
    #:% credential.
    async def _authenticate(self, offer: SessionOffer) -> bool:
        """Check the credential, and refuse the session when it is wrong.

        The refusal happens here rather than after negotiation, because REQ-019
        requires that no capability negotiation takes place for a client that
        did not present a valid credential.

        Args:
            offer: The client's opening message.

        Returns:
            True when the credential was the configured one. When it was not,
            the session has already been closed.

        Raises:
            TransportClosedError: If the connection ended while refusing.
        """
        if credential_is_valid(
            offer.credential.get_secret_value(),
            self._settings.credential.get_secret_value(),
        ):
            return True
        _logger.warning("session.unauthenticated")
        # No exemplar: no frame has been handled, and none ever will be on this
        # session.
        self._obs.metrics.errors_total.labels(
            code=ErrorCode.UNAUTHENTICATED.value,
        ).inc()
        await self._refuse(
            CloseReason.UNAUTHENTICATED,
            CLOSE_POLICY_VIOLATION,
            _UNAUTHENTICATED_DETAIL,
        )
        return False

    #:= docs/specs/robot-link/index.md#req-012-capabilities-are-negotiated-at-session-start
    #:% Both sides MUST exchange the set of capabilities they support, each with a
    #:% version, before any capability-specific message is sent.
    async def _agree(
        self,
        offer: SessionOffer,
    ) -> tuple[tuple[AgreedCapability, ...], tuple[Capability, ...]]:
        """Reduce the offer against what this service can currently speak.

        `negotiate` is the contracts package's, so both sides of the link agree
        on what agreement means: a capability survives only when its name and
        its version match exactly, and one that does not survive is simply
        absent while the session continues.

        Args:
            offer: The client's opening message.

        Returns:
            The capabilities to route to, in the agreed order, and the agreed
            set as it was sent to the client.

        Raises:
            TransportClosedError: If the connection ended while answering.
        """
        agreement = negotiate(offer, self._registry.supported())
        await self._send(MessageKind.AGREEMENT, agreement)
        # The name comes from the agreement, which is the name the registry
        # already resolved. Nothing downstream asks a capability what it is
        # called: that is a property on third-party code, and reading it once
        # per frame would put it outside the registry's failure containment.
        routed = tuple(
            AgreedCapability(name=named.name, capability=capability)
            for named in agreement.capabilities
            if (capability := self._registry.get(named.name)) is not None
        )
        _logger.info(
            "session.negotiated",
            offered=[named.name for named in offer.capabilities],
            agreed=[named.name for named in agreement.capabilities],
        )
        return routed, agreement.capabilities

    async def _pump(self, capabilities: tuple[AgreedCapability, ...]) -> CloseReason:
        """Carry frames from the client into the pipeline until it stops.

        Args:
            capabilities: The agreed capabilities, in the agreed order.

        Returns:
            Why the session ended.

        Raises:
            TransportClosedError: If the connection ended.
        """
        queue = FrameQueue(self._settings.queue_bound)
        pipeline = FramePipeline(
            capabilities=capabilities,
            deliver=self._send,
            settings=self._settings,
            obs=self._obs,
            session_id=self._session_id,
            clock=self._clock,
        )
        worker = asyncio.create_task(pipeline.run(queue), name="pipeline")
        try:
            return await self._receive_frames(queue)
        finally:
            queue.close()
            await self._drain(worker)

    async def _drain(self, worker: asyncio.Task[None]) -> None:
        """Let the pipeline finish the frames it already accepted, then stop.

        Args:
            worker: The pipeline task for this session.

        Raises:
            asyncio.CancelledError: If the session itself is being cancelled, in
                which case the pipeline is cancelled with it rather than left
                running against a connection nobody holds.
        """
        try:
            # A client that went away while the pipeline was still answering
            # surfaces here as the pipeline's own send failing. The caller is
            # unwinding on that same event already, so raising a second copy of
            # it would replace the original — and the pipeline has already
            # logged it against the frame it was handling, which this far out
            # is no longer known.
            with suppress(TransportClosedError):
                await worker
        except asyncio.CancelledError:
            # Cancelling is not enough on its own: the task has to be awaited
            # for its cancellation to actually complete, or the session ends
            # with a pipeline still unwinding against a connection nobody
            # holds. Whatever it raises on the way out is its own end, not this
            # session's outcome. A capability that ignores cancellation is
            # already bounded by `capability_timeout_seconds`.
            worker.cancel()
            with suppress(asyncio.CancelledError, TransportClosedError):
                await worker
            raise

    async def _receive_frames(self, queue: FrameQueue) -> CloseReason:
        """Read frames off the connection and queue them.

        Args:
            queue: The session's queue.

        Returns:
            Why the session ended.

        Raises:
            TransportClosedError: If the connection ended.
        """
        while True:
            message = await self._transport.receive()
            # The bound is checked before anything reads the message, and it
            # covers text as well as frames: a control message is parsed as
            # JSON, so an unbounded one is an unbounded parse. `len` counts
            # bytes for a frame and characters for a control message, which is
            # the right order of magnitude for a guard and cheaper than
            # encoding a message in order to measure it.
            if len(message) > self._settings.max_message_bytes:
                await self._report_malformed(
                    f"message of {len(message)} bytes exceeds the configured "
                    f"maximum of {self._settings.max_message_bytes}",
                )
                continue
            if isinstance(message, str):
                reason = await self._handle_control(message)
                if reason is not None:
                    return reason
                continue
            try:
                header, payload = decode_frame(message)
            except FramingError as error:
                await self._report_malformed(str(error))
                continue
            self._frames_received += 1
            # Both counters carry the arriving frame's identity as an exemplar,
            # the same way the pipeline's timings do. A drop is attributed to
            # the frame whose arrival caused it, which is what makes "why was
            # frame N never answered?" answerable from the metrics alone.
            exemplar = frame_exemplar(self._session_id, header.sequence)
            self._obs.metrics.frames_received_total.inc(exemplar=exemplar)
            dropped = queue.put(
                QueuedFrame(
                    header=header,
                    payload=payload,
                    received_at=self._clock(),
                ),
            )
            if dropped:
                self._frames_dropped += dropped
                self._obs.metrics.frames_dropped_total.inc(dropped, exemplar=exemplar)

    async def _handle_control(self, text: str) -> CloseReason | None:
        """Deal with a control message arriving mid-session.

        Args:
            text: The message as it arrived.

        Returns:
            Why the session ended, or `None` to keep going.

        Raises:
            TransportClosedError: If the connection ended while answering.
        """
        try:
            kind, _ = decode_control(text)
        except FramingError as error:
            await self._report_malformed(str(error))
            return None
        if kind is MessageKind.CLOSE:
            await self._transport.close(CLOSE_NORMAL, "closing as asked")
            return CloseReason.GOING_AWAY
        await self._report_malformed(f"{kind.value!r} is not a client message")
        return None

    async def _report_malformed(self, detail: str) -> None:
        """Tell the client one of its messages was unusable, without closing.

        Args:
            detail: What was wrong with it.

        Raises:
            TransportClosedError: If the connection ended while answering.
        """
        # No exemplar: a message that did not parse as a frame has no sequence
        # number to attribute the error to. REQ-028 attaches a frame's identity
        # to what is emitted while handling a frame, and this is emitted while
        # declining to.
        self._obs.metrics.errors_total.labels(
            code=ErrorCode.MALFORMED_MESSAGE.value,
        ).inc()
        await self._send(
            MessageKind.ERROR,
            SessionError(code=ErrorCode.MALFORMED_MESSAGE, detail=detail[:500]),
        )

    async def _refuse(self, reason: CloseReason, code: int, detail: str) -> None:
        """Say why the session is ending and end it.

        Args:
            reason: The contract-level reason.
            code: The transport-level close code.
            detail: A short explanation, never a credential.

        Raises:
            TransportClosedError: If the connection ended first.
        """
        await self._send(
            MessageKind.CLOSE,
            SessionClose(reason=reason, detail=detail[:500]),
        )
        await self._transport.close(code, reason.value)
