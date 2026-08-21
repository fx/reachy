"""The golden corpus, run against the client that consumes and produces it.

Robot-link REQ-020 asks for both sides of every message type to be verified
against the same fixture. The groundstation's suite is one side; this is the
other. The bytes are read from the files committed in `reachy_contracts` and are
never re-serialised on the way in — a control message is built by embedding the
fixture verbatim, which is what a real groundstation does — so what travels
through the client here is the corpus itself rather than an equivalent of it.

Reading a file is input, so these are contract tests and say so with the
`filesystem` marker. See the root `AGENTS.md`: the marker declares that a test is
not a unit test, and the golden corpus is the case it exists for, because the
bytes on disk are the contract and a fake would pin whatever the fake was told
to return.

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
    ManualClock,
    RecordedSleep,
    ScriptedTransports,
    StubTransport,
    agreement,
    credential,
)

from reachy_contracts import FrameHeader, SessionOffer
from reachy_contracts.fixtures import fixture_bytes, golden_file_names, load_fixture
from reachy_session_client import (
    ConnectionFailedError,
    MessageKind,
    SessionClient,
    encode_control,
    encode_frame,
)

if TYPE_CHECKING:
    from reachy_session_client import FrameResult

# RFC 5737 TEST-NET-2 — see the root AGENTS.md on what may enter a tracked file.
URL: Final = "ws://198.51.100.10:8080/v1/session"

JPEG: Final = b"\xff\xd8\xff\xe0 opaque compressed bytes"

OFFER: Final = "session-offer.json"
AGREEMENT: Final = "session-agreement.json"
FRAME_HEADER: Final = "frame-header.json"
FACE_RESULT: Final = "face-result.json"
EMPTY_FACE_RESULT: Final = "empty-face-result.json"
GESTURE_RESULT: Final = "gesture-result.json"
ERROR: Final = "session-error.json"
CLOSE: Final = "session-close.json"

# Every fixture this module drives through the client. The last test compares
# this against the directory, so a fixture added to the corpus and not exercised
# here fails rather than being quietly untested on the consuming side.
EXERCISED: Final = frozenset(
    {
        OFFER,
        AGREEMENT,
        FRAME_HEADER,
        FACE_RESULT,
        EMPTY_FACE_RESULT,
        GESTURE_RESULT,
        ERROR,
        CLOSE,
    },
)


def carrying(kind: MessageKind, name: str) -> str:
    """Wrap a fixture's bytes in a control message without touching them.

    Args:
        kind: Which contract type the fixture is.
        name: The fixture's file name.

    Returns:
        The control message a groundstation sending that fixture would produce.
    """
    return f'{{"kind":"{kind.value}","message":{fixture_bytes(name).decode("utf-8")}}}'


def build(transport: StubTransport) -> SessionClient:
    """Build a client over one prepared connection.

    Args:
        transport: The connection to hand it.

    Returns:
        The client, not yet connected.
    """
    clock = ManualClock()
    return SessionClient(
        url=URL,
        credential=credential(),
        capabilities=(FACE, GESTURE),
        open_transport=ScriptedTransports(transport),
        clock=clock,
        sleep=RecordedSleep(clock),
    )


async def one_result(transport: StubTransport) -> FrameResult:
    """Connect, take one result and stop.

    Args:
        transport: The connection, already loaded with an agreement and a
            result.

    Returns:
        The result the client applied.
    """
    client = build(transport)
    await client.connect()
    results = client.results()
    try:
        return await anext(results)
    finally:
        await results.aclose()
        await client.aclose()


#:= docs/specs/robot-link/index.md#req-020-the-wire-format-is-pinned-by-shared-fixtures
#:% Every message type MUST have a golden fixture in the shared contracts package,
#:% and both the producing and the consuming implementation MUST be verified against
#:% that same fixture.
@pytest.mark.filesystem  # the committed corpus is the contract; see the module docstring
@pytest.mark.asyncio
async def test_the_offer_this_client_produces_is_the_committed_one() -> None:
    """The producing side, byte for byte: the credential and both capabilities."""
    transport = StubTransport()
    transport.push(carrying(MessageKind.AGREEMENT, AGREEMENT))
    client = build(transport)

    await client.connect()

    assert fixture_bytes(OFFER).decode("utf-8") in transport.sent_text[0]
    assert load_fixture(OFFER, SessionOffer).credential.get_secret_value() == CREDENTIAL


@pytest.mark.filesystem  # the committed corpus is the contract; see the module docstring
def test_a_frame_carries_the_committed_header_unaltered() -> None:
    """The framing embeds the header's canonical bytes and re-encodes nothing."""
    header = load_fixture(FRAME_HEADER, FrameHeader)

    message = encode_frame(header, JPEG)

    assert fixture_bytes(FRAME_HEADER) in message
    assert message.endswith(JPEG)


@pytest.mark.filesystem  # the committed corpus is the contract; see the module docstring
@pytest.mark.asyncio
async def test_the_committed_agreement_is_what_the_client_reports_agreeing_to() -> None:
    """The consuming side of negotiation: one capability of the two offered."""
    transport = StubTransport()
    transport.push(carrying(MessageKind.AGREEMENT, AGREEMENT))
    client = build(transport)

    agreed = await client.connect()

    assert [named.name for named in agreed.capabilities] == ["face"]
    assert client.agreed("face") is not None
    assert client.agreed("gesture") is None


@pytest.mark.filesystem  # the committed corpus is the contract; see the module docstring
@pytest.mark.asyncio
async def test_the_committed_face_result_is_applied_with_both_its_faces() -> None:
    """Two faces answering one frame, at normalised coordinates."""
    transport = StubTransport()
    transport.push(
        carrying(MessageKind.AGREEMENT, AGREEMENT),
        carrying(MessageKind.RESULT, FACE_RESULT),
    )

    result = await one_result(transport)

    assert result.sequence == 41
    assert result.capability == "face"
    assert result.detections == 2
    assert result.captured_at.root == "3894112233445566"


#:= docs/specs/robot-link/index.md#req-013-an-empty-result-is-a-valid-result
#:% A result message carrying no detections MUST be treated as a successful result
#:% for that frame.
@pytest.mark.filesystem  # the committed corpus is the contract; see the module docstring
@pytest.mark.asyncio
async def test_the_committed_empty_result_is_applied_as_a_success() -> None:
    """A successful result for a frame that contained no face."""
    transport = StubTransport()
    transport.push(
        carrying(MessageKind.AGREEMENT, AGREEMENT),
        carrying(MessageKind.RESULT, EMPTY_FACE_RESULT),
    )

    result = await one_result(transport)

    assert result.sequence == 42
    assert result.detections == 0


@pytest.mark.filesystem  # the committed corpus is the contract; see the module docstring
@pytest.mark.asyncio
async def test_the_committed_gesture_result_is_applied() -> None:
    """A capability's payload is routed by the name the result declares.

    The agreement here is built rather than read from the corpus: the committed
    one names face alone, and a client only applies a result for a capability
    this session agreed to. The bytes under test are still the corpus's — what
    changes is only what the session was told it could expect.
    """
    transport = StubTransport()
    transport.push(
        agreement(FACE, GESTURE),
        carrying(MessageKind.RESULT, GESTURE_RESULT),
    )

    result = await one_result(transport)

    assert result.capability == "gesture"
    assert result.detections == 1


@pytest.mark.filesystem  # the committed corpus is the contract; see the module docstring
@pytest.mark.asyncio
async def test_the_committed_error_is_counted_and_carries_on() -> None:
    """A failure report is not an empty result and does not end the session."""
    transport = StubTransport()
    transport.push(
        carrying(MessageKind.AGREEMENT, AGREEMENT),
        carrying(MessageKind.ERROR, ERROR),
        carrying(MessageKind.RESULT, FACE_RESULT),
    )
    client = build(transport)
    await client.connect()

    results = client.results()
    try:
        result = await anext(results)
    finally:
        await results.aclose()

    assert result.sequence == 41
    assert client.stats.errors_received == 1


@pytest.mark.filesystem  # the committed corpus is the contract; see the module docstring
@pytest.mark.asyncio
async def test_the_committed_close_is_read_as_the_groundstation_going_away() -> None:
    """An orderly shutdown, which is answered by reconnecting rather than raising."""
    transport = StubTransport()
    transport.push(carrying(MessageKind.CLOSE, CLOSE))
    client = build(transport)

    with pytest.raises(ConnectionFailedError, match="shutting down"):
        await client.connect()


@pytest.mark.filesystem  # the committed corpus is the contract; see the module docstring
def test_every_committed_fixture_is_driven_through_this_client() -> None:
    """A fixture added to the corpus and not exercised here is untested here."""
    assert set(golden_file_names()) == EXERCISED


@pytest.mark.filesystem  # the committed corpus is the contract; see the module docstring
def test_a_control_message_carrying_a_fixture_is_what_the_framing_produces() -> None:
    """The helper above must build what `encode_control` builds, or it proves nothing."""
    offer = load_fixture(OFFER, SessionOffer)

    assert carrying(MessageKind.OFFER, OFFER) == encode_control(
        MessageKind.OFFER, offer
    )
