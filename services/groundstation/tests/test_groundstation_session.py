"""The session: offer, credential, agreement, frames, and the end of it.

These drive the real session layer over an in-memory transport. The transport is
the only thing faked, and only because a server would be input and output for
nothing here — the integration tests drive the real one.

Backpressure is induced rather than configured. The producer in
`test_frames_beyond_the_bound_drop_the_oldest` fills the queue faster than the
pipeline is scheduled to drain it, which is the actual overload condition, and
the assertions are about which frames survived.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`. Nothing here opens a socket or reads a file. The one test that
configures a handshake timeout does wait on a clock, bounded at ten
milliseconds, because a timeout elapsing is the behaviour it is about.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from groundstation_support import (
    CREDENTIAL,
    ECHO,
    TALLY,
    BlockingCapability,
    EchoCapability,
    MemoryTransport,
    StaticRegistry,
    TallyCapability,
    build_observability,
    captured_logs,
    frame_message,
    hand_control_to_the_event_loop,
    make_settings,
)

from reachy_contracts import (
    Capability,
    CloseReason,
    ErrorCode,
    SessionAgreement,
    SessionClose,
    SessionError,
)
from reachy_groundstation.obs import Observability
from reachy_groundstation.session.framing import MessageKind, decode_control
from reachy_groundstation.session.runner import SessionRunner
from reachy_groundstation.session.transport import (
    CLOSE_POLICY_VIOLATION,
    CLOSE_PROTOCOL_ERROR,
)

ECHO_V2 = Capability(name="echo", version=2)
UNKNOWN = Capability(name="lidar", version=1)


def _sent(transport: MemoryTransport) -> list[tuple[MessageKind, bytes]]:
    """Unpack everything the session sent.

    Args:
        transport: The transport it sent on.

    Returns:
        The kind and canonical bytes of each message, in order.
    """
    return [decode_control(text) for text in transport.sent]


def _agreement(transport: MemoryTransport) -> SessionAgreement:
    """Pick the agreement out of what the session sent.

    Args:
        transport: The transport it sent on.

    Returns:
        The agreement.
    """
    for kind, payload in _sent(transport):
        if kind is MessageKind.AGREEMENT:
            return SessionAgreement.from_wire(payload)
    message = "the session sent no agreement"
    raise AssertionError(message)


def _results(transport: MemoryTransport) -> list[dict[str, Any]]:
    """Pick the results out of what the session sent, as they went on the wire.

    Results are read as JSON rather than parsed into one envelope type, because
    a session carries results from several capabilities and each carries its own
    payload type. What these tests assert about is the envelope — which frame it
    answers and which capability produced it — and that is the same for all of
    them.

    Args:
        transport: The transport it sent on.

    Returns:
        The results, in the order they were delivered.
    """
    return [
        json.loads(payload)
        for kind, payload in _sent(transport)
        if kind is MessageKind.RESULT
    ]


def _errors(transport: MemoryTransport) -> list[SessionError]:
    """Pick the errors out of what the session sent.

    Args:
        transport: The transport it sent on.

    Returns:
        The errors, in order.
    """
    return [
        SessionError.from_wire(payload)
        for kind, payload in _sent(transport)
        if kind is MessageKind.ERROR
    ]


def _runner(
    transport: MemoryTransport,
    registry: StaticRegistry,
    **overrides: object,
) -> tuple[SessionRunner, Observability]:
    """Build a session runner over a transport and a registry.

    Args:
        transport: The connection to drive.
        registry: What to negotiate against.
        overrides: Settings to change from their defaults.

    Returns:
        The runner and the reporting bundle it writes to.
    """
    obs, _exporter = build_observability()
    return (
        SessionRunner(
            transport=transport,
            registry=registry,
            settings=make_settings(**overrides),
            obs=obs,
            session_id="feedfacefeedface",
            clock=iter(range(10_000)).__next__,
        ),
        obs,
    )


#:= docs/specs/robot-link/index.md#req-012-capabilities-are-negotiated-at-session-start
#:% Both sides MUST exchange the set of capabilities they support, each with a
#:% version, before any capability-specific message is sent.
@pytest.mark.asyncio
async def test_the_agreed_set_is_the_intersection() -> None:
    """A groundstation capability the app cannot read is not agreed."""
    transport = MemoryTransport()
    transport.offer(ECHO)
    transport.disconnect()
    runner, _ = _runner(transport, StaticRegistry(EchoCapability(), TallyCapability()))
    outcome = await runner.run()
    assert _agreement(transport).capabilities == (ECHO,)
    assert outcome.agreed == (ECHO,)


@pytest.mark.asyncio
async def test_a_version_the_groundstation_does_not_offer_is_absent() -> None:
    """The session continues with whatever else was agreed."""
    transport = MemoryTransport()
    transport.offer(ECHO_V2, TALLY)
    transport.disconnect()
    runner, _ = _runner(transport, StaticRegistry(EchoCapability(), TallyCapability()))
    await runner.run()
    assert _agreement(transport).capabilities == (TALLY,)


@pytest.mark.asyncio
async def test_a_capability_this_build_never_heard_of_is_absent() -> None:
    """An app ahead of its groundstation still gets a working session."""
    transport = MemoryTransport()
    transport.offer(UNKNOWN)
    transport.disconnect()
    runner, _ = _runner(transport, StaticRegistry(EchoCapability()))
    await runner.run()
    assert _agreement(transport).capabilities == ()


@pytest.mark.asyncio
async def test_an_offer_of_nothing_still_opens_a_session() -> None:
    """A client that speaks nothing is authenticated and answered."""
    transport = MemoryTransport()
    transport.offer()
    transport.disconnect()
    runner, _ = _runner(transport, StaticRegistry(EchoCapability()))
    outcome = await runner.run()
    assert outcome.reason is CloseReason.GOING_AWAY
    assert _agreement(transport).capabilities == ()


#:= docs/specs/robot-link/index.md#req-019-sessions-are-authenticated
#:% The groundstation MUST reject a session whose client does not present a valid
#:% credential.
@pytest.mark.asyncio
async def test_a_wrong_credential_is_refused_before_any_negotiation() -> None:
    """No capability negotiation takes place for a client that failed."""
    transport = MemoryTransport()
    transport.offer(ECHO, credential="not-the-configured-one")
    runner, _ = _runner(transport, StaticRegistry(EchoCapability()))
    outcome = await runner.run()
    assert outcome.reason is CloseReason.UNAUTHENTICATED
    assert [kind for kind, _ in _sent(transport)] == [MessageKind.CLOSE]
    assert transport.closed == (CLOSE_POLICY_VIOLATION, "unauthenticated")


@pytest.mark.asyncio
async def test_the_refusal_does_not_repeat_the_credential() -> None:
    """A close reason is read by whoever can read the link."""
    transport = MemoryTransport()
    transport.offer(credential="a-wrong-credential")
    runner, _ = _runner(transport, StaticRegistry())
    await runner.run()
    _, payload = _sent(transport)[0]
    assert "a-wrong-credential" not in SessionClose.from_wire(payload).detail


@pytest.mark.asyncio
async def test_a_credential_that_differs_in_one_character_is_refused() -> None:
    """The comparison is exact, and constant-time about being exact."""
    transport = MemoryTransport()
    transport.offer(credential=CREDENTIAL + "x")
    runner, _ = _runner(transport, StaticRegistry())
    assert (await runner.run()).reason is CloseReason.UNAUTHENTICATED


@pytest.mark.asyncio
async def test_a_frame_before_the_offer_is_a_protocol_error() -> None:
    """A session opens with an offer; anything else is not a session."""
    transport = MemoryTransport()
    transport.push(frame_message(0))
    runner, _ = _runner(transport, StaticRegistry(EchoCapability()))
    outcome = await runner.run()
    assert outcome.reason is CloseReason.PROTOCOL_ERROR
    assert transport.closed == (CLOSE_PROTOCOL_ERROR, "protocol_error")


@pytest.mark.asyncio
async def test_an_opening_message_that_is_not_an_offer_is_refused() -> None:
    """The first message's kind is checked, not merely its shape."""
    transport = MemoryTransport()
    transport.push(json.dumps({"kind": "close", "message": {"reason": "going_away"}}))
    runner, _ = _runner(transport, StaticRegistry())
    assert (await runner.run()).reason is CloseReason.PROTOCOL_ERROR


@pytest.mark.asyncio
async def test_an_offer_that_does_not_parse_is_refused() -> None:
    """An offer without a credential never reaches the credential check."""
    transport = MemoryTransport()
    transport.push(json.dumps({"kind": "offer", "message": {"capabilities": []}}))
    runner, _ = _runner(transport, StaticRegistry())
    assert (await runner.run()).reason is CloseReason.PROTOCOL_ERROR


@pytest.mark.asyncio
async def test_a_client_that_never_speaks_is_closed_on_the_handshake_timeout() -> None:
    """A connection that presents nothing must not be held open forever."""
    transport = MemoryTransport()
    runner, _ = _runner(
        transport,
        StaticRegistry(),
        handshake_timeout_seconds=0.01,
    )
    assert (await runner.run()).reason is CloseReason.PROTOCOL_ERROR


@pytest.mark.asyncio
async def test_a_frame_is_answered_by_every_agreed_capability() -> None:
    """The whole point of the session: frames in, results out, one connection."""
    transport = MemoryTransport()
    transport.offer(ECHO, TALLY)
    transport.push(frame_message(0))
    transport.disconnect()
    runner, _ = _runner(transport, StaticRegistry(EchoCapability(), TallyCapability()))
    outcome = await runner.run()
    assert outcome.frames_received == 1
    assert [result["capability"] for result in _results(transport)] == [
        "echo",
        "tally",
    ]


@pytest.mark.asyncio
async def test_a_capability_that_was_not_agreed_is_not_run() -> None:
    """Routing is by the agreed set, not by everything the service has."""
    tally = TallyCapability()
    transport = MemoryTransport()
    transport.offer(ECHO)
    transport.push(frame_message(0))
    transport.disconnect()
    runner, _ = _runner(transport, StaticRegistry(EchoCapability(), tally))
    await runner.run()
    assert tally.seen == []


#:= docs/specs/robot-link/index.md#req-015-overload-drops-frames-rather-than-queueing-them
#:% When frames arrive faster than they can be processed, the oldest unprocessed
#:% frame MUST be discarded in preference to growing the queue or blocking the
#:% producer.
@pytest.mark.asyncio
async def test_frames_beyond_the_bound_drop_the_oldest() -> None:
    """The producer outruns the pipeline, so the queue actually overflows."""
    transport = MemoryTransport()
    transport.offer(ECHO)
    for sequence in range(8):
        transport.push(frame_message(sequence))
    transport.disconnect()
    runner, _obs = _runner(
        transport,
        StaticRegistry(EchoCapability()),
        queue_bound=2,
    )
    outcome = await runner.run()

    assert outcome.frames_received == 8
    assert outcome.frames_dropped == 6
    # The two most recent frames are the ones still answered.
    assert [result["sequence"] for result in _results(transport)] == [6, 7]


@pytest.mark.asyncio
async def test_a_drop_is_counted_rather_than_logged() -> None:
    """Per-occurrence logging would add load exactly when there is none spare."""
    transport = MemoryTransport()
    transport.offer(ECHO)
    for sequence in range(5):
        transport.push(frame_message(sequence))
    transport.disconnect()
    runner, obs = _runner(transport, StaticRegistry(EchoCapability()), queue_bound=1)
    with captured_logs() as logs:
        await runner.run()

    dropped = obs.metrics.registry.get_sample_value(
        "groundstation_frames_dropped_total",
    )
    assert dropped == 4
    assert not [line for line in logs if "drop" in line["event"]]


@pytest.mark.asyncio
async def test_the_producer_is_never_blocked_by_the_queue() -> None:
    """Every frame the client sent was accepted, whatever happened next."""
    transport = MemoryTransport()
    transport.offer(ECHO)
    for sequence in range(20):
        transport.push(frame_message(sequence))
    transport.disconnect()
    runner, obs = _runner(transport, StaticRegistry(EchoCapability()), queue_bound=2)
    outcome = await runner.run()
    received = obs.metrics.registry.get_sample_value(
        "groundstation_frames_received_total",
    )
    assert outcome.frames_received == 20
    assert received == 20


@pytest.mark.asyncio
async def test_a_malformed_frame_is_reported_without_ending_the_session() -> None:
    """One bad message is one error, not a dropped connection."""
    transport = MemoryTransport()
    transport.offer(ECHO)
    transport.push(b"\x00\x00\x00\x05not a frame")
    transport.push(frame_message(1))
    transport.disconnect()
    runner, _ = _runner(transport, StaticRegistry(EchoCapability()))
    outcome = await runner.run()
    assert _errors(transport)[0].code is ErrorCode.MALFORMED_MESSAGE
    assert outcome.frames_received == 1
    assert outcome.reason is CloseReason.GOING_AWAY


@pytest.mark.asyncio
async def test_an_oversized_frame_is_refused_before_it_is_parsed() -> None:
    """A length a client asserts is not a length this service reserves."""
    transport = MemoryTransport()
    transport.offer(ECHO)
    transport.push(frame_message(0, payload=b"x" * 4096))
    transport.disconnect()
    runner, _ = _runner(
        transport,
        StaticRegistry(EchoCapability()),
        max_message_bytes=1024,
    )
    outcome = await runner.run()
    assert outcome.frames_received == 0
    assert "exceeds the configured maximum" in _errors(transport)[0].detail


@pytest.mark.asyncio
async def test_a_control_message_the_client_should_not_send_is_reported() -> None:
    """Results travel one way; a client sending one is a protocol mistake."""
    transport = MemoryTransport()
    transport.offer(ECHO)
    transport.push(json.dumps({"kind": "agreement", "message": {}}))
    transport.disconnect()
    runner, _ = _runner(transport, StaticRegistry(EchoCapability()))
    await runner.run()
    assert "not a client message" in _errors(transport)[0].detail


@pytest.mark.asyncio
async def test_unparseable_text_mid_session_is_reported() -> None:
    """The session survives a message it cannot read."""
    transport = MemoryTransport()
    transport.offer(ECHO)
    transport.push("{not json")
    transport.push(frame_message(0))
    transport.disconnect()
    runner, _ = _runner(transport, StaticRegistry(EchoCapability()))
    outcome = await runner.run()
    assert _errors(transport)[0].code is ErrorCode.MALFORMED_MESSAGE
    assert outcome.frames_received == 1


@pytest.mark.asyncio
async def test_a_client_asking_to_close_is_closed() -> None:
    """An orderly shutdown from the other side is an ordinary ending."""
    transport = MemoryTransport()
    transport.offer(ECHO)
    transport.push(json.dumps({"kind": "close", "message": {"reason": "going_away"}}))
    runner, _ = _runner(transport, StaticRegistry(EchoCapability()))
    outcome = await runner.run()
    assert outcome.reason is CloseReason.GOING_AWAY
    assert transport.closed is not None


#:= docs/specs/robot-link/index.md#req-011-one-session-carries-every-exchange
#:% All frames, results, and control messages for a running app MUST travel over a
#:% single session, established once and reused for the lifetime of that session.
@pytest.mark.asyncio
async def test_many_frames_travel_on_the_one_session() -> None:
    """Nothing reopens anything: one offer, one agreement, many frames."""
    transport = MemoryTransport()
    transport.offer(ECHO)
    for sequence in range(5):
        transport.push(frame_message(sequence))
    transport.disconnect()
    runner, _ = _runner(transport, StaticRegistry(EchoCapability()), queue_bound=16)
    outcome = await runner.run()
    kinds = [kind for kind, _ in _sent(transport)]
    assert kinds.count(MessageKind.AGREEMENT) == 1
    assert outcome.frames_received == 5
    assert len(_results(transport)) == 5


@pytest.mark.asyncio
async def test_a_reconnection_negotiates_again_against_what_is_offered_now() -> None:
    """A groundstation that restarted with a different set is the normal case."""
    registry = StaticRegistry(EchoCapability(), TallyCapability())

    first = MemoryTransport()
    first.offer(ECHO, TALLY)
    first.disconnect()
    runner, _ = _runner(first, registry)
    assert (await runner.run()).agreed == (ECHO, TALLY)

    # The service loses a capability between the two connections — a restart, a
    # model that would not load, an operator switching one off.
    registry.capabilities = registry.capabilities[:1]

    second = MemoryTransport()
    second.offer(ECHO, TALLY)
    second.disconnect()
    runner, _ = _runner(second, registry)
    assert (await runner.run()).agreed == (ECHO,)


@pytest.mark.asyncio
async def test_a_session_that_ends_mid_frame_ends_cleanly() -> None:
    """A dropped connection is ordinary: the client reconnects and re-offers."""
    transport = MemoryTransport()
    transport.offer(ECHO)
    transport.push(frame_message(0))
    transport.disconnect()
    runner, obs = _runner(transport, StaticRegistry(EchoCapability()))
    outcome = await runner.run()
    assert outcome.reason is CloseReason.GOING_AWAY
    assert (
        obs.metrics.registry.get_sample_value(
            "groundstation_sessions_total",
            {"outcome": "going_away"},
        )
        == 1
    )
    assert obs.metrics.registry.get_sample_value("groundstation_sessions_active") == 0


#:= docs/specs/groundstation/index.md#req-028-work-is-attributable-end-to-end
#:% Every log line and metric emitted while handling a frame MUST carry the session
#:% identifier and the frame's sequence number.
@pytest.mark.asyncio
async def test_every_line_logged_while_handling_a_frame_is_attributable() -> None:
    """Searching by sequence number retrieves the session it belonged to."""
    transport = MemoryTransport()
    transport.offer(ECHO)
    transport.push(frame_message(0, payload=b"not a jpeg at all"))
    transport.disconnect()
    runner, _ = _runner(transport, StaticRegistry(EchoCapability()))
    with captured_logs() as logs:
        await runner.run()
    assert logs
    assert all(line["session"] == "feedfacefeedface" for line in logs)


@pytest.mark.asyncio
async def test_a_session_reports_the_identifier_it_was_given() -> None:
    """Every line and every exemplar has to name the same session."""
    transport = MemoryTransport()
    transport.disconnect()
    runner, _ = _runner(transport, StaticRegistry())
    assert runner.session_id == "feedfacefeedface"


@pytest.mark.asyncio
async def test_a_session_given_no_identifier_mints_one() -> None:
    """Two sessions are told apart even when nobody named them."""
    obs, _exporter = build_observability()
    first = SessionRunner(
        transport=MemoryTransport(),
        registry=StaticRegistry(),
        settings=make_settings(),
        obs=obs,
    )
    second = SessionRunner(
        transport=MemoryTransport(),
        registry=StaticRegistry(),
        settings=make_settings(),
        obs=obs,
    )
    assert first.session_id != second.session_id


@pytest.mark.asyncio
async def test_an_opening_message_that_is_not_control_framing_is_refused() -> None:
    """The first message is read as framing before it is read as an offer."""
    transport = MemoryTransport()
    transport.push("not json at all")
    runner, _ = _runner(transport, StaticRegistry())
    outcome = await runner.run()
    assert outcome.reason is CloseReason.PROTOCOL_ERROR
    _, payload = _sent(transport)[0]
    assert "opening message did not parse" in SessionClose.from_wire(payload).detail


@pytest.mark.asyncio
async def test_an_oversized_opening_message_is_refused_before_it_is_parsed() -> None:
    """A message nobody bounded is a parse nobody bounded."""
    transport = MemoryTransport()
    transport.push("x" * 2048)
    runner, _ = _runner(transport, StaticRegistry(), max_message_bytes=1024)
    outcome = await runner.run()
    assert outcome.reason is CloseReason.PROTOCOL_ERROR
    _, payload = _sent(transport)[0]
    assert "exceeds the configured maximum" in SessionClose.from_wire(payload).detail


@pytest.mark.asyncio
async def test_an_oversized_control_message_is_refused_before_it_is_parsed() -> None:
    """The same bound covers text mid-session, for the same reason."""
    transport = MemoryTransport()
    transport.offer(ECHO)
    transport.push("y" * 2048)
    transport.disconnect()
    runner, _ = _runner(
        transport,
        StaticRegistry(EchoCapability()),
        max_message_bytes=1024,
    )
    await runner.run()
    assert "exceeds the configured maximum" in _errors(transport)[0].detail


@pytest.mark.asyncio
async def test_an_offer_that_does_not_parse_never_echoes_what_it_carried() -> None:
    """An offer is the one message with a credential in it.

    Pydantic writes the offending input value into a validation error's text, so
    forwarding that text would put the presented credential into the close
    reason. What comes back names the field and the kind of fault instead.
    """
    transport = MemoryTransport()
    transport.push(
        json.dumps(
            {
                "kind": "offer",
                "message": {"credential": "x" * 500, "capabilities": []},
            },
        ),
    )
    runner, _ = _runner(transport, StaticRegistry())
    assert (await runner.run()).reason is CloseReason.PROTOCOL_ERROR
    detail = SessionClose.from_wire(_sent(transport)[0][1]).detail
    assert "credential: too_long" in detail
    assert "x" * 100 not in detail


@pytest.mark.asyncio
async def test_a_client_that_vanishes_mid_answer_ends_the_session_cleanly() -> None:
    """The pipeline's own send failing is the same event, not a second one."""
    transport = MemoryTransport(fail_send_after=1)
    transport.offer(ECHO)
    transport.push(frame_message(0))
    transport.disconnect()
    runner, _ = _runner(transport, StaticRegistry(EchoCapability()))
    outcome = await runner.run()
    assert outcome.reason is CloseReason.GOING_AWAY
    assert outcome.frames_received == 1


@pytest.mark.asyncio
async def test_a_cancelled_session_still_finishes_the_frame_it_had_in_hand() -> None:
    """Cancellation drains the pipeline rather than abandoning it mid-answer."""
    blocking = BlockingCapability()
    transport = MemoryTransport()
    transport.offer(ECHO)
    transport.push(frame_message(0))
    runner, _ = _runner(transport, StaticRegistry(blocking))

    session = asyncio.create_task(runner.run())
    await blocking.entered.wait()
    session.cancel()
    blocking.release.set()
    with pytest.raises(asyncio.CancelledError):
        await session

    assert blocking.processed == [0]
    assert not [task for task in asyncio.all_tasks() if task.get_name() == "pipeline"]


@pytest.mark.asyncio
async def test_a_second_cancellation_takes_the_pipeline_down_with_it() -> None:
    """A shutdown that escalates must not leave a task answering nobody.

    The first cancellation unwinds the receive loop into the drain, which waits
    for the pipeline to finish what it had already accepted. A second one says
    the shutdown will not wait, and the pipeline goes down with it.
    """
    blocking = BlockingCapability()
    transport = MemoryTransport()
    transport.offer(ECHO)
    transport.push(frame_message(0))
    runner, _ = _runner(transport, StaticRegistry(blocking))

    session = asyncio.create_task(runner.run())
    await blocking.entered.wait()
    session.cancel()
    await hand_control_to_the_event_loop()
    session.cancel()
    with pytest.raises(asyncio.CancelledError):
        await session

    assert blocking.processed == []
    assert not [task for task in asyncio.all_tasks() if task.get_name() == "pipeline"]
