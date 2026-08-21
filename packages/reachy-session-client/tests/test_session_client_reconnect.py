"""Reconnection, induced rather than configured.

Every test here kills a session and watches the client recover from it. None of
them asserts that a retry setting exists — a setting can be right while the loop
around it is wrong, and the loop is the part that has to work at three in the
morning when a groundstation restarts.

Nothing sleeps. The delays are recorded by an injected sleep which advances an
injected clock, so a test can drive several minutes of outage and assert on the
exact sequence of delays it produced.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final

import pytest
from session_client_support import (
    FACE,
    GESTURE,
    ManualClock,
    RecordedSleep,
    ScriptedTransports,
    StubTransport,
    agreement,
    capability_names,
    credential,
    face_result,
    hand_control_to_the_event_loop,
    session_close,
)

from reachy_contracts import CloseReason
from reachy_session_client import (
    Backoff,
    ProtocolError,
    SessionClient,
    SessionRefusedError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from reachy_contracts import Capability
    from reachy_session_client import FrameResult

# RFC 5737 TEST-NET-2 — see the root AGENTS.md on what may enter a tracked file.
URL: Final = "ws://198.51.100.10:8080/v1/session"

JPEG: Final = b"\xff\xd8\xff\xe0 opaque compressed bytes"

# Small numbers with an obvious sequence, so the assertion reads as the rule it
# is checking rather than as arithmetic.
BACKOFF: Final = Backoff(initial_seconds=1.0, multiplier=2.0, maximum_seconds=8.0)


def build(
    *steps: StubTransport | None,
    capabilities: Sequence[Capability] = (FACE,),
    backoff: Backoff = BACKOFF,
) -> tuple[SessionClient, RecordedSleep, ScriptedTransports]:
    """Build a client over a scripted sequence of connections.

    Args:
        steps: A transport for each attempt that should succeed, `None` for
            each that should fail.
        capabilities: What the client offers.
        backoff: The delay policy to exercise.

    Returns:
        The client, the sleep its delays are recorded in, and the factory.
    """
    clock = ManualClock()
    sleep = RecordedSleep(clock)
    factory = ScriptedTransports(*steps)
    client = SessionClient(
        url=URL,
        credential=credential(),
        capabilities=capabilities,
        open_transport=factory,
        backoff=backoff,
        clock=clock,
        sleep=sleep,
    )
    return client, sleep, factory


async def collect(client: SessionClient, count: int) -> list[FrameResult]:
    """Take a fixed number of results and stop.

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


#:= docs/specs/robot-link/index.md#req-018-reconnection-is-automatic-and-rate-limited
#:% A client MUST re-establish a dropped session automatically, and MUST increase
#:% the delay between successive failed attempts up to a bound.
@pytest.mark.asyncio
async def test_a_dropped_session_is_re_established_without_anybody_asking() -> None:
    """The groundstation restarts and results resume, with no operator action."""
    first = StubTransport()
    first.push(agreement(FACE), face_result(0))
    second = StubTransport()
    second.push(agreement(FACE), face_result(0))
    client, sleep, factory = build(first, second)
    await client.connect()

    results = client.results()
    try:
        before = await anext(results)
        first.drop("the groundstation restarted")
        after = await anext(results)
    finally:
        await results.aclose()

    assert before.sequence == after.sequence == 0
    assert client.stats.reconnections == 1
    assert factory.attempts == 2
    assert sleep.delays == [1.0]
    assert first.closed


#:= docs/specs/robot-link/index.md#req-018-reconnection-is-automatic-and-rate-limited
#:% A client MUST re-establish a dropped session automatically, and MUST increase
#:% the delay between successive failed attempts up to a bound.
@pytest.mark.asyncio
async def test_the_delay_stops_growing_at_its_bound_over_a_long_outage() -> None:
    """Attempts continue for minutes; the delay does not increase without limit."""
    first = StubTransport()
    first.push(agreement(FACE))
    recovered = StubTransport()
    recovered.push(agreement(FACE), face_result(0))
    # Six failures then a success: enough for the doubling to reach the bound
    # twice over, so the flat tail is a property rather than a coincidence.
    client, sleep, _factory = build(first, *([None] * 6), recovered)
    await client.connect()
    first.drop("the address stopped resolving")

    (result,) = await collect(client, 1)

    assert result.sequence == 0
    assert sleep.delays == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0, 8.0]
    assert max(sleep.delays) == BACKOFF.maximum_seconds
    assert client.stats.connection_attempts == 8


#:= docs/specs/robot-link/index.md#req-012-capabilities-are-negotiated-at-session-start
#:% Both sides MUST exchange the set of capabilities they support, each with a
#:% version, before any capability-specific message is sent.
@pytest.mark.asyncio
async def test_a_reconnection_negotiates_again_rather_than_resuming() -> None:
    """A groundstation that restarted with a different set is the normal case."""
    first = StubTransport()
    first.push(agreement(FACE, GESTURE))
    second = StubTransport()
    second.push(agreement(FACE), face_result(0))
    client, _sleep, _factory = build(first, second, capabilities=(FACE, GESTURE))
    await client.connect()
    assert client.agreement is not None
    assert capability_names(client.agreement.capabilities) == ["face", "gesture"]

    first.drop()
    await collect(client, 1)

    assert client.agreement is not None
    assert capability_names(client.agreement.capabilities) == ["face"]
    assert client.agreed("gesture") is None


@pytest.mark.asyncio
async def test_frame_numbering_starts_over_on_the_new_session() -> None:
    """A sequence number is only meaningful against the session it was sent on."""
    first = StubTransport()
    first.push(agreement(FACE))
    second = StubTransport()
    second.push(agreement(FACE), face_result(0))
    client, _sleep, _factory = build(first, second)
    await client.connect()
    await client.submit_frame(JPEG)
    await client.submit_frame(JPEG)

    first.drop()
    await collect(client, 1)
    header = await client.submit_frame(JPEG)

    assert header is not None
    assert header.sequence == 0
    assert client.stats.frames_submitted == 3


@pytest.mark.asyncio
async def test_a_result_from_before_the_drop_does_not_supersede_one_after_it() -> None:
    """Supersession is scoped to a session, because numbering is."""
    first = StubTransport()
    first.push(agreement(FACE), face_result(9))
    second = StubTransport()
    second.push(agreement(FACE), face_result(0))
    client, _sleep, _factory = build(first, second)
    await client.connect()

    results = client.results()
    try:
        before = await anext(results)
        first.drop()
        after = await anext(results)
    finally:
        await results.aclose()

    assert before.sequence == 9
    assert after.sequence == 0
    assert client.stats.results_superseded == 0


@pytest.mark.asyncio
async def test_a_groundstation_going_away_mid_session_is_reconnected_to() -> None:
    """An orderly close is a restart, which is exactly what retrying is for."""
    first = StubTransport()
    first.push(agreement(FACE), session_close(CloseReason.GOING_AWAY, "shutting down"))
    second = StubTransport()
    second.push(agreement(FACE), face_result(0))
    client, _sleep, _factory = build(first, second)
    await client.connect()

    (result,) = await collect(client, 1)

    assert result.sequence == 0
    assert client.stats.reconnections == 1


@pytest.mark.asyncio
async def test_a_credential_the_groundstation_refuses_is_not_retried() -> None:
    """No delay makes a rejected credential true, and looping would hide it."""
    first = StubTransport()
    first.push(agreement(FACE))
    refusing = StubTransport()
    refusing.push(session_close())
    client, sleep, _factory = build(first, refusing)
    await client.connect()
    first.drop()

    with pytest.raises(SessionRefusedError, match="unauthenticated"):
        await collect(client, 1)

    assert sleep.delays == [1.0]
    assert client.stats.reconnections == 0


@pytest.mark.asyncio
async def test_a_groundstation_that_answers_nonsense_is_not_retried_either() -> None:
    """Retrying a defect would turn it into a quiet reconnection storm."""
    first = StubTransport()
    first.push(agreement(FACE))
    confused = StubTransport()
    confused.push(face_result(0))
    client, _sleep, _factory = build(first, confused)
    await client.connect()
    first.drop()

    with pytest.raises(ProtocolError, match="opens with an agreement"):
        await collect(client, 1)


@pytest.mark.asyncio
async def test_a_client_that_was_closed_does_not_reconnect() -> None:
    """A consumer that has finished is not a session that dropped."""
    transport = StubTransport()
    transport.push(agreement(FACE))
    # One transport in the script: a second attempt would run off the end and
    # fail this test rather than looping inside it.
    client, sleep, factory = build(transport)
    await client.connect()

    results = client.results()
    await client.aclose()
    transport.drop()
    collected = [result async for result in results]

    assert collected == []
    assert sleep.delays == []
    assert factory.attempts == 1


@pytest.mark.asyncio
async def test_closing_stops_a_reconnection_loop_that_is_already_running() -> None:
    """A retry loop with no end of its own is ended by the consumer leaving.

    Reconnection is unbounded by design — REQ-018 asks for a delay that stops
    growing, not for attempts that stop happening — so the only thing that ends
    it is the consumer. A reconnection holds the client's lock across its delay,
    which is why closing marks the client before it takes that lock: a close
    that queued for the lock would wait out however long the delay had grown to.
    """
    first = StubTransport()
    first.push(agreement(FACE))
    # A long script, because what matters is that closing stops the loop rather
    # than that the loop happens to run out. Running off the end would raise.
    client, sleep, factory = build(first, *([None] * 50))
    await client.connect()
    first.drop("the groundstation went away and stayed away")

    collected: list[FrameResult] = []

    async def drain() -> None:
        """Iterate until the client stops, which is what closing does."""
        async for result in client.results():
            collected.append(result)  # pragma: no cover - nothing arrives here

    task = asyncio.create_task(drain(), name="drain")
    await hand_control_to_the_event_loop(5)
    retries_before_closing = len(sleep.delays)

    await client.aclose()
    await asyncio.wait_for(task, timeout=5.0)
    attempts_when_it_stopped = factory.attempts
    await hand_control_to_the_event_loop(5)

    assert collected == []
    assert retries_before_closing > 0
    assert factory.attempts == attempts_when_it_stopped
