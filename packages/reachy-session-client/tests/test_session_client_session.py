"""The session's own behaviour, driven without a server.

Every test here uses a queue-backed transport, because what is under test is
what the client does with the messages rather than how they travel; the
integration tests drive the real WebSocket against the real groundstation. Time
is injected in both directions — a clock the test advances and a sleep that
costs nothing — so nothing here waits for anything.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import pytest
from session_client_support import (
    CREDENTIAL,
    FACE,
    GESTURE,
    STAMP,
    ManualClock,
    RecordedSleep,
    ScriptedTransports,
    StubTransport,
    agreement,
    capability_names,
    credential,
    empty_face_result,
    face_result,
    gesture_result,
    session_close,
    session_error,
)

from reachy_contracts import CloseReason, ErrorCode
from reachy_session_client import (
    ConnectionFailedError,
    Credential,
    NotConnectedError,
    ProtocolError,
    SessionClient,
    SessionClientError,
    SessionRefusedError,
    decode_control,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from reachy_contracts import Capability
    from reachy_session_client import FrameResult

# RFC 5737 TEST-NET-2. No address belonging to anybody's network enters a
# tracked file in this repository — see the root AGENTS.md.
URL: Final = "ws://198.51.100.10:8080/v1/session"

JPEG: Final = b"\xff\xd8\xff\xe0 opaque compressed bytes"


def build(
    *steps: StubTransport | None,
    capabilities: Sequence[Capability] = (FACE,),
    clock: ManualClock | None = None,
    staleness_seconds: float = 2.0,
) -> tuple[SessionClient, RecordedSleep]:
    """Build a client over a scripted sequence of connections.

    Args:
        steps: A transport for each attempt that should succeed, `None` for
            each that should fail.
        capabilities: What the client offers.
        clock: The clock to stamp frames and measure staleness against.
        staleness_seconds: How long a result stays worth acting on.

    Returns:
        The client, and the sleep its reconnection delays are recorded in.
    """
    ticking = ManualClock() if clock is None else clock
    sleep = RecordedSleep(ticking)
    client = SessionClient(
        url=URL,
        credential=credential(),
        capabilities=capabilities,
        open_transport=ScriptedTransports(*steps),
        staleness_seconds=staleness_seconds,
        clock=ticking,
        sleep=sleep,
    )
    return client, sleep


async def collect(client: SessionClient, count: int) -> list[FrameResult]:
    """Take a fixed number of results and stop.

    A fixed number rather than draining, so a client that yields nothing fails
    the test on its assertion instead of hanging the suite on an empty queue.

    Args:
        client: The client to read from.
        count: How many results to wait for.

    Returns:
        The results, in the order they arrived.
    """
    results = client.results()
    try:
        return [await anext(results) for _ in range(count)]
    finally:
        await results.aclose()


@pytest.mark.asyncio
async def test_a_session_opens_with_an_offer_carrying_the_credential() -> None:
    """The credential is presented, which is the one place it is revealed."""
    transport = StubTransport()
    transport.push(agreement(FACE))
    client, _sleep = build(transport)

    agreed = await client.connect()

    kind, payload = decode_control(transport.sent_text[0])
    assert kind.value == "offer"
    assert CREDENTIAL.encode() in payload
    assert capability_names(agreed.capabilities) == ["face"]
    assert client.connected


#:= docs/specs/robot-link/index.md#req-012-capabilities-are-negotiated-at-session-start
#:% Both sides MUST exchange the set of capabilities they support, each with a
#:% version, before any capability-specific message is sent.
@pytest.mark.asyncio
async def test_a_capability_that_was_not_agreed_is_simply_absent() -> None:
    """The session continues with whatever else was agreed."""
    transport = StubTransport()
    transport.push(agreement(FACE))
    client, _sleep = build(transport, capabilities=(FACE, GESTURE))

    await client.connect()

    assert client.agreed("face") == FACE
    assert client.agreed("gesture") is None
    assert client.connected


@pytest.mark.asyncio
async def test_asking_a_client_that_has_no_session_what_was_agreed() -> None:
    """Before a session exists, nothing has been agreed."""
    client, _sleep = build(StubTransport())

    assert client.agreement is None
    assert client.agreed("face") is None
    assert client.url == URL


#:= docs/specs/reachyctl/index.md#req-059-secrets-are-never-written-to-output
#:% The tool MUST NOT write credentials to its output, its logs, or its error
#:% messages.
@pytest.mark.asyncio
async def test_a_refused_session_reports_why_without_the_credential() -> None:
    """A rejected credential is the case where one most easily escapes."""
    transport = StubTransport()
    transport.push(session_close())
    client, _sleep = build(transport)

    with pytest.raises(SessionRefusedError) as raised:
        await client.connect()

    assert raised.value.reason == "unauthenticated"
    assert CREDENTIAL not in str(raised.value)
    assert CREDENTIAL not in repr(raised.value)
    assert transport.closed
    assert not client.connected


@pytest.mark.asyncio
async def test_an_orderly_goodbye_during_the_handshake_is_worth_retrying() -> None:
    """A groundstation shutting down is not a groundstation refusing anybody."""
    transport = StubTransport()
    transport.push(session_close(CloseReason.GOING_AWAY, "restarting"))
    client, _sleep = build(transport)

    with pytest.raises(ConnectionFailedError, match="going away"):
        await client.connect()

    assert transport.closed


@pytest.mark.asyncio
async def test_an_answer_that_is_not_an_agreement_ends_the_attempt() -> None:
    """A groundstation answering an offer with a result is not one to retry."""
    transport = StubTransport()
    transport.push(face_result(0))
    client, _sleep = build(transport)

    with pytest.raises(ProtocolError, match="opens with an agreement"):
        await client.connect()

    assert transport.closed


@pytest.mark.asyncio
async def test_connecting_twice_reuses_the_session_already_held() -> None:
    """One session carries every exchange; a second connect is not a second one."""
    transport = StubTransport()
    transport.push(agreement(FACE))
    client, _sleep = build(transport)

    first = await client.connect()
    second = await client.connect()

    assert first == second
    assert len(transport.sent_text) == 1


#:= docs/specs/robot-link/index.md#req-014-results-are-keyed-to-the-frame-that-produced-them
#:% Every frame MUST carry a monotonically increasing sequence number, and every
#:% result MUST identify the sequence number of the frame it derives from.
@pytest.mark.asyncio
async def test_frames_are_numbered_upwards_and_stamped_forwards() -> None:
    """Both the sequence and the capture token only ever increase."""
    transport = StubTransport()
    transport.push(agreement(FACE))
    clock = ManualClock(start=500.0)
    client, _sleep = build(transport, clock=clock)
    await client.connect()

    first = await client.submit_frame(JPEG)
    clock.advance(0.1)
    second = await client.submit_frame(JPEG)

    assert first is not None
    assert second is not None
    assert [first.sequence, second.sequence] == [0, 1]
    assert float(first.captured_at.root) < float(second.captured_at.root)
    assert len(transport.sent_bytes) == 2
    assert client.stats.frames_submitted == 2


#:= docs/specs/robot-link/index.md#req-015-overload-drops-frames-rather-than-queueing-them
#:% When frames arrive faster than they can be processed, the oldest unprocessed
#:% frame MUST be discarded in preference to growing the queue or blocking the
#:% producer.
@pytest.mark.asyncio
async def test_a_frame_produced_with_no_session_is_dropped_rather_than_queued() -> None:
    """The producer is never blocked and nothing accumulates behind a dead link."""
    client, _sleep = build(StubTransport())

    dropped = await client.submit_frame(JPEG)

    assert dropped is None
    assert client.stats.frames_dropped == 1
    assert client.stats.frames_submitted == 0


@pytest.mark.asyncio
async def test_a_frame_lost_to_a_connection_that_went_away_mid_send() -> None:
    """The results loop reconnects; this frame is simply gone."""
    transport = StubTransport()
    transport.push(agreement(FACE))
    client, _sleep = build(transport)
    await client.connect()
    transport.sends_fail = True

    assert await client.submit_frame(JPEG) is None
    assert client.stats.frames_dropped == 1


@pytest.mark.asyncio
async def test_a_result_comes_back_with_the_round_trip_it_took() -> None:
    """The token round-tripped, and the age is a single-clock subtraction."""
    transport = StubTransport()
    transport.push(agreement(FACE))
    clock = ManualClock(start=500.0)
    client, _sleep = build(transport, clock=clock)
    await client.connect()
    header = await client.submit_frame(JPEG)
    assert header is not None
    clock.advance(0.2)
    transport.push(face_result(header.sequence, header.captured_at.root))

    (result,) = await collect(client, 1)

    assert result.sequence == 0
    assert result.capability == "face"
    assert result.captured_at == header.captured_at
    assert result.round_trip_seconds == pytest.approx(0.2)
    assert result.detections == 1


@pytest.mark.asyncio
async def test_a_result_stamped_by_somebody_else_has_no_round_trip() -> None:
    """There is no clock to measure it against, so no number is invented."""
    transport = StubTransport()
    transport.push(agreement(FACE), face_result(0, "2026-08-21T00:00:00Z"))
    client, _sleep = build(transport)
    await client.connect()

    (result,) = await collect(client, 1)

    assert result.round_trip_seconds is None


#:= docs/specs/robot-link/index.md#req-014-results-are-keyed-to-the-frame-that-produced-them
#:% Every frame MUST carry a monotonically increasing sequence number, and every
#:% result MUST identify the sequence number of the frame it derives from.
@pytest.mark.asyncio
async def test_a_result_for_an_older_frame_is_discarded_as_superseded() -> None:
    """Results for frames 7 and 8 arriving as 8 then 7: 7 is history."""
    transport = StubTransport()
    transport.push(
        agreement(FACE),
        face_result(8),
        face_result(7),
        face_result(9),
    )
    client, _sleep = build(transport)
    await client.connect()

    applied = await collect(client, 2)

    assert [result.sequence for result in applied] == [8, 9]
    assert client.stats.results_superseded == 1
    assert client.stats.results_applied == 2


@pytest.mark.asyncio
async def test_two_capabilities_answering_one_frame_are_both_applied() -> None:
    """Equal is not older: the second answer is not superseded by the first."""
    transport = StubTransport()
    transport.push(agreement(FACE, GESTURE), face_result(4), gesture_result(4))
    client, _sleep = build(transport, capabilities=(FACE, GESTURE))
    await client.connect()

    applied = await collect(client, 2)

    assert [result.capability for result in applied] == ["face", "gesture"]
    assert client.stats.results_superseded == 0


#:= docs/specs/robot-link/index.md#req-013-an-empty-result-is-a-valid-result
#:% A result message carrying no detections MUST be treated as a successful result
#:% for that frame.
@pytest.mark.asyncio
async def test_a_result_with_no_detections_is_an_ordinary_success() -> None:
    """It is delivered, it is counted as applied, and no error counter moves."""
    transport = StubTransport()
    transport.push(agreement(FACE), empty_face_result(3))
    client, _sleep = build(transport)
    await client.connect()

    (result,) = await collect(client, 1)

    assert result.detections == 0
    assert result.sequence == 3
    assert client.stats.results_applied == 1
    assert client.stats.errors_received == 0


@pytest.mark.asyncio
async def test_a_failure_report_is_counted_and_does_not_end_the_session() -> None:
    """An error is not an empty result, and neither of them closes anything."""
    transport = StubTransport()
    transport.push(
        agreement(FACE),
        session_error(ErrorCode.CAPABILITY_FAILED, sequence=1),
        face_result(2),
    )
    client, _sleep = build(transport)
    await client.connect()

    (result,) = await collect(client, 1)

    assert result.sequence == 2
    assert client.stats.errors_received == 1
    assert client.connected


@pytest.mark.asyncio
async def test_a_result_from_a_capability_this_build_cannot_parse_is_ignored() -> None:
    """An older robot goes on holding a session with a newer groundstation."""
    transport = StubTransport()
    transport.push(agreement(FACE), _unknown_capability_result(), face_result(1))
    client, _sleep = build(transport)
    await client.connect()

    (result,) = await collect(client, 1)

    assert result.capability == "face"
    assert client.stats.results_ignored == 1


def _unknown_capability_result() -> str:
    """Build a result naming a capability no registry in this build carries.

    It is assembled as text rather than as a contract type, because the contract
    types are the ones this build knows about — which is exactly what this
    message is not.

    Returns:
        The control message.
    """
    inner = (
        f'{{"sequence":0,"captured_at":"{STAMP}",'
        '"capability":"telepathy","payload":{"thoughts":[]}}'
    )
    return f'{{"kind":"result","message":{inner}}}'


#:= docs/specs/robot-link/index.md#req-017-stale-results-stop-being-acted-on
#:% A consumer MUST stop acting on results once none has arrived within a configured
#:% staleness window.
@pytest.mark.asyncio
async def test_the_consumer_stops_being_offered_a_result_that_went_stale() -> None:
    """The groundstation disappeared; the last known face is no longer current."""
    transport = StubTransport()
    transport.push(agreement(FACE), face_result(0))
    clock = ManualClock(start=100.0)
    client, _sleep = build(transport, clock=clock, staleness_seconds=2.0)
    await client.connect()
    await collect(client, 1)

    assert client.latest() is not None
    assert not client.stale

    clock.advance(2.0)

    assert client.latest() is None
    assert client.stale


@pytest.mark.asyncio
async def test_a_client_that_has_received_nothing_has_nothing_to_act_on() -> None:
    """Staleness before the first result is not a special case."""
    transport = StubTransport()
    transport.push(agreement(FACE))
    client, _sleep = build(transport)
    await client.connect()

    assert client.latest() is None
    assert client.stale


@pytest.mark.asyncio
async def test_a_message_that_is_not_of_this_protocol_ends_the_iteration() -> None:
    """Retrying a defect would turn it into a quiet reconnection storm."""
    transport = StubTransport()
    transport.push(agreement(FACE), "{not json at all")
    client, _sleep = build(transport)
    await client.connect()

    with pytest.raises(ProtocolError, match="unusable message"):
        await collect(client, 1)


@pytest.mark.asyncio
async def test_a_message_the_groundstation_never_sends_ends_the_iteration() -> None:
    """An offer arriving from the groundstation is not a message to act on."""
    transport = StubTransport()
    transport.push(agreement(FACE), transport_offer())
    client, _sleep = build(transport)
    await client.connect()

    with pytest.raises(ProtocolError, match="not a groundstation message"):
        await collect(client, 1)


def transport_offer() -> str:
    """Build an offer, which is a message that only travels the other way.

    Returns:
        The control message.
    """
    return f'{{"kind":"offer","message":{{"credential":"{CREDENTIAL}"}}}}'


@pytest.mark.asyncio
async def test_iterating_results_before_connecting_says_so() -> None:
    """A caller that forgot to connect is a caller, not a dropped session."""
    client, _sleep = build(StubTransport())

    with pytest.raises(NotConnectedError, match="call connect first"):
        await collect(client, 1)


@pytest.mark.asyncio
async def test_closing_says_goodbye_and_is_idempotent() -> None:
    """A consumer that has finished is not a session that dropped."""
    transport = StubTransport()
    transport.push(agreement(FACE))
    client, _sleep = build(transport)
    await client.connect()

    await client.aclose()
    await client.aclose()

    kind, _payload = decode_control(transport.sent_text[-1])
    assert kind.value == "close"
    assert transport.closed
    assert not client.connected


@pytest.mark.asyncio
async def test_closing_a_connection_that_has_already_gone_says_nothing() -> None:
    """There is nothing left to tell anybody, and leaving is not conditional."""
    transport = StubTransport()
    transport.push(agreement(FACE))
    client, _sleep = build(transport)
    await client.connect()
    transport.sends_fail = True

    await client.aclose()

    assert transport.closed


@pytest.mark.asyncio
async def test_the_client_works_as_an_asynchronous_context_manager() -> None:
    """Which is how a command holds a session for the length of a command."""
    transport = StubTransport()
    transport.push(agreement(FACE))
    client, _sleep = build(transport)

    async with client as session:
        assert session.connected

    assert not client.connected


@pytest.mark.parametrize(
    "url",
    ["http://198.51.100.10/v1/session", "198.51.100.10:8080", ""],
)
def test_a_url_that_is_not_a_session_url_is_refused_when_the_client_is_built(
    url: str,
) -> None:
    """Retrying a configuration mistake is a way to never be told about it.

    Args:
        url: The address to refuse.
    """
    with pytest.raises(ValueError, match="ws://"):
        SessionClient(url=url, credential=credential())


def test_a_staleness_window_that_never_opens_is_refused() -> None:
    """A window of zero would make every result stale on arrival."""
    with pytest.raises(ValueError, match="must be positive"):
        SessionClient(url=URL, credential=credential(), staleness_seconds=0.0)


@pytest.mark.asyncio
async def test_an_agreement_that_does_not_parse_ends_the_attempt() -> None:
    """A groundstation whose agreement is not one is not a groundstation to retry."""
    transport = StubTransport()
    transport.push('{"kind":"agreement","message":{"capabilities":"all of them"}}')
    client, _sleep = build(transport)

    with pytest.raises(ProtocolError, match="agreement did not parse"):
        await client.connect()


@pytest.mark.asyncio
async def test_a_close_message_that_does_not_parse_ends_the_attempt() -> None:
    """Nothing can be concluded from it, least of all that retrying would help."""
    transport = StubTransport()
    transport.push('{"kind":"close","message":{"reason":"who knows"}}')
    client, _sleep = build(transport)

    with pytest.raises(ProtocolError, match="close message did not parse"):
        await client.connect()


@pytest.mark.asyncio
async def test_a_result_whose_payload_is_not_its_capabilitys_ends_the_iteration() -> (
    None
):
    """A message that would route as face and carry something else is a defect."""
    transport = StubTransport()
    transport.push(
        agreement(FACE),
        '{"kind":"result","message":{"sequence":0,"captured_at":"1000.000000",'
        '"capability":"face","payload":{"faces":"quite a lot"}}}',
    )
    client, _sleep = build(transport)
    await client.connect()

    with pytest.raises(ProtocolError, match="result did not parse"):
        await collect(client, 1)


@pytest.mark.asyncio
async def test_a_result_that_is_not_even_an_object_is_ignored() -> None:
    """No registry holds a capability it does not name, so there is nothing to do."""
    transport = StubTransport()
    transport.push(
        agreement(FACE), '{"kind":"result","message":[1,2,3]}', face_result(0)
    )
    client, _sleep = build(transport)
    await client.connect()

    (result,) = await collect(client, 1)

    assert result.capability == "face"
    assert result.payload is not None
    assert client.stats.results_ignored == 1


#:= docs/specs/reachyctl/index.md#req-059-secrets-are-never-written-to-output
#:% The tool MUST NOT write credentials to its output, its logs, or its error
#:% messages.
@pytest.mark.asyncio
async def test_an_offer_that_will_not_validate_is_reported_without_the_credential() -> (
    None
):
    """The offer is the one message carrying a credential, so it is the one to check.

    A `pydantic.ValidationError` renders the value it rejected into its own
    text, and it is raised while building a message the credential is part of.
    What comes out names the field and the kind of fault and nothing else, and
    the cause is deliberately not chained — a handler that printed one would
    print what this exists to withhold.
    """
    # Longer than the contract allows a credential to be, which is a whole
    # credential file pasted into a variable by mistake. It is the credential
    # field itself that fails, so the rejected value pydantic renders into its
    # own text is the credential.
    too_long = "example-credential-" + "x" * 300
    transport = StubTransport()
    transport.push(agreement(FACE))
    _client, _sleep = build(transport)
    client = SessionClient(
        url=URL,
        credential=Credential(too_long),
        capabilities=(FACE,),
        open_transport=ScriptedTransports(transport),
    )

    with pytest.raises(SessionClientError) as raised:
        await client.connect()

    assert "offer is not valid" in str(raised.value)
    assert "credential" in str(raised.value)
    assert too_long not in str(raised.value)
    assert raised.value.__cause__ is None
