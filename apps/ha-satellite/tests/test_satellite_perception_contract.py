"""The robot's end of the link, against the golden fixtures the corpus pins.

Robot-link REQ-020 asks that both the producing and the consuming
implementation be verified against the same fixture. The groundstation is the
producer and has its own tests over these bytes; this is the consumer, and the
bytes below travel the whole way in — off disk, through the real session
client, into the perception port's answer — rather than being parsed and
compared in isolation.

That end-to-end route is the point. A test that parsed `face-result.json` into a
`ResultEnvelope` and asserted on its fields would pin the contracts package,
which already has tests for it. What is unproved without this is that the
robot's adapter reads the *same* fields out of it: that it looks for the face
capability under the name the fixture uses, and that the coordinates it hands
the motion layer are the ones on disk rather than a sign or an axis away from
them.

Reading committed files is input, so these are contract tests and say so.
"""

from __future__ import annotations

import pytest
from satellite_support import FakeMedia, inline
from session_client_support import (
    FACE,
    ManualClock,
    RecordedSleep,
    ScriptedTransports,
    StubTransport,
    agreement,
    credential,
    hand_control_to_the_event_loop,
)

from reachy_contracts import FaceDetections, ResultEnvelope, fixture_bytes, load_fixture
from reachy_mini_ha_satellite.adapters.groundstation import RemotePerception
from reachy_mini_ha_satellite.ports import Detections, DetectionSource
from reachy_session_client import (
    MessageKind,
    SessionClient,
    decode_control,
    encode_control,
)

# RFC 5737 documentation space; this repository is public.
_URL = "ws://192.0.2.10:8765/v1/session"

_FaceResult = ResultEnvelope[FaceDetections]


def _as_control_message(fixture: str) -> str:
    """Frame a golden fixture the way a groundstation would send it.

    The framing is the session client's own, so what the adapter receives is
    what a real groundstation puts on the wire: the fixture's bytes inside the
    envelope this protocol carries them in.

    Args:
        fixture: The fixture's file name.

    Returns:
        The control message.
    """
    return encode_control(MessageKind.RESULT, load_fixture(fixture, _FaceResult))


async def _deliver(fixture: str) -> tuple[Detections, RemotePerception]:
    """Deliver one golden fixture to the robot's adapter and read its answer.

    Args:
        fixture: The fixture's file name.

    Returns:
        What the perception port answered, and the adapter, so the caller can
        close it.
    """
    result = load_fixture(fixture, _FaceResult)
    clock = ManualClock(start=float(result.captured_at.root))
    sleep = RecordedSleep()
    transport = StubTransport()
    transport.push(
        agreement(FACE),
        encode_control(MessageKind.RESULT, result),
    )
    remote = RemotePerception(
        FakeMedia(),
        SessionClient(
            url=_URL,
            credential=credential(),
            capabilities=(FACE,),
            open_transport=ScriptedTransports(transport),
            staleness_seconds=2.0,
            clock=clock,
            sleep=sleep,
        ),
        clock=clock,
        sleep=sleep,
        offload=inline,
    )
    await remote.start()
    await hand_control_to_the_event_loop()
    return remote.latest(), remote


@pytest.mark.filesystem
@pytest.mark.asyncio
async def test_the_golden_face_result_arrives_as_the_faces_it_records() -> None:
    """Two faces, at the coordinates the committed bytes carry.

    The numbers are written out here rather than read back from the fixture, so
    that a fixture edited to match a broken consumer fails this test instead of
    agreeing with itself.
    """
    view, remote = await _deliver("face-result.json")
    try:
        assert len(view.faces) == 2
        assert view.faces[0].centre.x == pytest.approx(-0.25)
        assert view.faces[0].centre.y == pytest.approx(0.5)
        assert view.faces[0].confidence == pytest.approx(0.97)
        assert view.faces[1].centre.x == pytest.approx(0.75)
        assert view.faces[1].centre.y == pytest.approx(-0.125)
        assert view.faces[1].confidence == pytest.approx(0.61)
        assert view.fresh
        assert view.source is DetectionSource.REMOTE
    finally:
        await remote.aclose()


@pytest.mark.filesystem
@pytest.mark.asyncio
async def test_the_golden_empty_result_is_a_successful_answer() -> None:
    """Robot-link REQ-013, pinned by the fixture the corpus keeps for it.

    The predecessor answered an empty payload with a 400. Here it is an
    ordinary fresh view of a room with nobody in it — which the behaviour layer
    must be able to tell apart from the results having stopped.
    """
    view, remote = await _deliver("empty-face-result.json")
    try:
        assert view.faces == ()
        assert view.fresh
    finally:
        await remote.aclose()


@pytest.mark.filesystem
@pytest.mark.parametrize(
    "fixture",
    ["face-result.json", "empty-face-result.json"],
)
def test_the_bytes_the_robot_reads_are_the_bytes_on_disk(fixture: str) -> None:
    """The framing carries the corpus through unaltered.

    Without this, the two tests above could be measuring the session client's
    re-serialisation of a message rather than the committed contract.

    Args:
        fixture: The fixture's file name.
    """
    kind, payload = decode_control(_as_control_message(fixture))
    assert kind is MessageKind.RESULT
    assert payload == fixture_bytes(fixture)
