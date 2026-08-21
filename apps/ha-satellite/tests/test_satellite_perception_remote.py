"""The groundstation source, driving the real session client over a stub link.

The client here is the real `SessionClient` — the one implementation of the
robot link's client half — connected through the stub transport its own test
suite uses. That is deliberate and it is the point of reachyctl REQ-057 seen
from the robot's side: what is being exercised is negotiation, sequencing,
supersession and reconnection *as the robot will do them*, because it is the
same code doing them. A hand-written fake client here would pass and prove
nothing.

Everything that is about time is injected. The staleness window is measured
against a manual clock and every delay is a `RecordedSleep`, so an outage that
would take half a minute of growing reconnection delays is driven in
microseconds and the delays themselves are asserted on rather than waited out.
"""

from __future__ import annotations

import pytest
from satellite_support import FakeMedia, inline
from session_client_support import (
    FACE,
    GESTURE,
    ManualClock,
    RecordedSleep,
    ScriptedTransports,
    StubTransport,
    agreement,
    credential,
    empty_face_result,
    face_result,
    gesture_result,
    hand_control_to_the_event_loop,
    session_close,
)

from reachy_contracts import Capability
from reachy_mini_ha_satellite.adapters.groundstation import RemotePerception
from reachy_mini_ha_satellite.ports import Detections, DetectionSource
from reachy_session_client import SessionClient

# RFC 5737 documentation space. This repository is public and no real address
# goes into a tracked file.
_URL = "ws://192.0.2.10:8765/v1/session"


def _client(
    transports: ScriptedTransports,
    clock: ManualClock,
    sleep: RecordedSleep,
    *,
    capabilities: tuple[Capability, ...] = (FACE,),
) -> SessionClient:
    """Build the real session client over a scripted transport.

    Args:
        transports: What each connection attempt gets.
        clock: The monotonic source.
        sleep: How the client waits.
        capabilities: What it offers.

    Returns:
        The client.
    """
    return SessionClient(
        url=_URL,
        credential=credential(),
        capabilities=capabilities,
        open_transport=transports,
        staleness_seconds=2.0,
        clock=clock,
        sleep=sleep,
    )


class TestTheSessionIsHeldAndUsed:
    """One session, opened outbound, carrying frames up and results down."""

    @pytest.mark.asyncio
    async def test_a_result_becomes_the_ports_answer(self) -> None:
        """Faces in, faces out, with the source named."""
        clock = ManualClock()
        sleep = RecordedSleep()
        transport = StubTransport()
        transport.push(agreement(FACE), face_result(0, faces=2))
        remote = RemotePerception(
            FakeMedia(),
            _client(ScriptedTransports(transport), clock, sleep),
            clock=clock,
            sleep=sleep,
            offload=inline,
        )
        await remote.start()
        await hand_control_to_the_event_loop()
        view = remote.latest()
        assert len(view.faces) == 2
        assert view.fresh
        assert view.source is DetectionSource.REMOTE
        await remote.aclose()

    @pytest.mark.asyncio
    async def test_an_empty_result_is_a_successful_one(self) -> None:
        """Robot-link REQ-013 as the robot experiences it: nobody is there.

        It has to stay distinguishable from the results having stopped, because
        one leaves the head alone and the other returns it to neutral.
        """
        clock = ManualClock()
        sleep = RecordedSleep()
        transport = StubTransport()
        transport.push(agreement(FACE), empty_face_result(0))
        remote = RemotePerception(
            FakeMedia(),
            _client(ScriptedTransports(transport), clock, sleep),
            clock=clock,
            sleep=sleep,
            offload=inline,
        )
        await remote.start()
        await hand_control_to_the_event_loop()
        view = remote.latest()
        assert view.faces == ()
        assert view.fresh
        await remote.aclose()

    @pytest.mark.asyncio
    async def test_frames_go_up_as_the_bytes_the_camera_produced(self) -> None:
        """Hardware-encoded JPEG, passed through and never re-encoded."""
        clock = ManualClock()
        sleep = RecordedSleep()
        transport = StubTransport()
        transport.push(agreement(FACE))
        media = FakeMedia(jpeg=b"\xff\xd8hardware-encoded\xff\xd9")
        client = _client(ScriptedTransports(transport), clock, sleep)
        remote = RemotePerception(
            media,
            client,
            clock=clock,
            sleep=sleep,
            offload=inline,
        )
        await remote.start()
        await hand_control_to_the_event_loop()
        assert client.stats.frames_submitted >= 1
        assert b"\xff\xd8hardware-encoded\xff\xd9" in transport.sent_bytes[0]
        await remote.aclose()

    @pytest.mark.asyncio
    async def test_a_camera_with_no_frame_sends_nothing(self) -> None:
        """A daemon with no camera is not a link that is losing frames."""
        clock = ManualClock()
        sleep = RecordedSleep()
        transport = StubTransport()
        transport.push(agreement(FACE))
        client = _client(ScriptedTransports(transport), clock, sleep)
        remote = RemotePerception(
            FakeMedia(jpeg=None),
            client,
            clock=clock,
            sleep=sleep,
            offload=inline,
        )
        await remote.start()
        await hand_control_to_the_event_loop()
        assert client.stats.frames_submitted == 0
        assert transport.sent_bytes == []
        await remote.aclose()

    @pytest.mark.asyncio
    async def test_another_capabilitys_result_is_not_an_absence_of_faces(
        self,
    ) -> None:
        """A gesture answering frame 7 must not erase the faces in frame 6."""
        clock = ManualClock()
        sleep = RecordedSleep()
        transport = StubTransport()
        transport.push(agreement(FACE, GESTURE), face_result(0), gesture_result(1))
        remote = RemotePerception(
            FakeMedia(),
            _client(
                ScriptedTransports(transport),
                clock,
                sleep,
                capabilities=(FACE, GESTURE),
            ),
            clock=clock,
            sleep=sleep,
            offload=inline,
        )
        await remote.start()
        await hand_control_to_the_event_loop()
        assert len(remote.latest().faces) == 1
        await remote.aclose()


class TestFreshness:
    """Robot-link REQ-017, which is what REQ-048's neutral head turns on."""

    @pytest.mark.asyncio
    async def test_a_result_goes_stale_when_the_window_elapses(self) -> None:
        """The link is up and the groundstation has stopped answering."""
        clock = ManualClock()
        sleep = RecordedSleep()
        transport = StubTransport()
        transport.push(agreement(FACE), face_result(0))
        remote = RemotePerception(
            FakeMedia(),
            _client(ScriptedTransports(transport), clock, sleep),
            staleness_seconds=2.0,
            clock=clock,
            sleep=sleep,
            offload=inline,
        )
        await remote.start()
        await hand_control_to_the_event_loop()
        assert remote.latest().fresh
        clock.advance(2.5)
        view = remote.latest()
        assert not view.fresh
        assert view.faces == ()
        assert view.age_seconds == pytest.approx(2.5)
        await remote.aclose()

    @pytest.mark.asyncio
    async def test_a_stale_source_is_still_connected(self) -> None:
        """The distinction the whole fallback decision rests on.

        A groundstation that has gone quiet is not a groundstation that has
        gone away, and answering the two the same way would start a second
        detector on the strength of a stall that may last one frame.
        """
        clock = ManualClock()
        sleep = RecordedSleep()
        transport = StubTransport()
        transport.push(agreement(FACE), face_result(0))
        remote = RemotePerception(
            FakeMedia(),
            _client(ScriptedTransports(transport), clock, sleep),
            clock=clock,
            sleep=sleep,
            offload=inline,
        )
        await remote.start()
        await hand_control_to_the_event_loop()
        clock.advance(60.0)
        assert not remote.latest().fresh
        assert remote.connected
        await remote.aclose()

    @pytest.mark.asyncio
    async def test_nothing_received_yet_is_not_fresh_and_names_no_age(
        self,
    ) -> None:
        """A robot that has just started has nothing to act on."""
        clock = ManualClock()
        sleep = RecordedSleep()
        transport = StubTransport()
        transport.push(agreement(FACE))
        remote = RemotePerception(
            FakeMedia(),
            _client(ScriptedTransports(transport), clock, sleep),
            clock=clock,
            sleep=sleep,
            offload=inline,
        )
        await remote.start()
        await hand_control_to_the_event_loop()
        view = remote.latest()
        assert not view.fresh
        assert view.age_seconds is None
        # And it names no source. `source` describes what produced a view, not
        # which adapter was asked — naming the groundstation here would report
        # a detection of an empty room, which is a live source's ordinary
        # success and a different fact from the session not having answered.
        # `age_seconds` already keeps the two apart by staying `None`.
        assert view.source is None
        assert view == Detections()
        await remote.aclose()


class TestWhenTheLinkGoesAway:
    """What "session loss" means concretely, since fallback turns on it."""

    @pytest.mark.asyncio
    async def test_a_groundstation_that_is_not_there_yet_is_retried(
        self,
    ) -> None:
        """A robot turned on before the rest of the house is an ordinary case."""
        clock = ManualClock()
        sleep = RecordedSleep()
        second = StubTransport()
        second.push(agreement(FACE), face_result(0))
        transports = ScriptedTransports(None, None, second)
        remote = RemotePerception(
            FakeMedia(),
            _client(transports, clock, sleep),
            clock=clock,
            sleep=sleep,
            offload=inline,
        )
        await remote.start()
        await hand_control_to_the_event_loop()
        assert transports.attempts == 3
        assert remote.connected
        assert sleep.delays[0] < sleep.delays[1]
        await remote.aclose()

    @pytest.mark.asyncio
    async def test_starting_does_not_wait_for_the_first_connection(
        self,
    ) -> None:
        """The voice pipeline does not need the groundstation at all."""
        clock = ManualClock()
        sleep = RecordedSleep()
        remote = RemotePerception(
            FakeMedia(),
            _client(ScriptedTransports(None), clock, sleep),
            clock=clock,
            sleep=sleep,
            offload=inline,
        )
        await remote.start()
        assert not remote.connected
        await remote.aclose()

    @pytest.mark.asyncio
    async def test_a_refused_session_is_not_retried_and_reports_disconnected(
        self,
    ) -> None:
        """A rejected credential is not a thing a delay fixes.

        Reporting it as disconnected is what puts a fallback source onto the
        robot's own detector rather than leaving it waiting for a session that
        is never going to open.
        """
        clock = ManualClock()
        sleep = RecordedSleep()
        transport = StubTransport()
        transport.push(session_close())
        transports = ScriptedTransports(transport)
        remote = RemotePerception(
            FakeMedia(),
            _client(transports, clock, sleep),
            clock=clock,
            sleep=sleep,
            offload=inline,
        )
        await remote.start()
        await hand_control_to_the_event_loop()
        assert not remote.connected
        assert transports.attempts == 1
        await remote.aclose()

    @pytest.mark.asyncio
    async def test_a_dropped_session_is_re_established_by_the_client(
        self,
    ) -> None:
        """Reconnection is the client's, and this is a consumer of it."""
        clock = ManualClock()
        sleep = RecordedSleep()
        first = StubTransport()
        first.push(agreement(FACE))
        second = StubTransport()
        second.push(agreement(FACE), face_result(0))
        client = _client(ScriptedTransports(first, second), clock, sleep)
        remote = RemotePerception(
            FakeMedia(),
            client,
            clock=clock,
            sleep=sleep,
            offload=inline,
        )
        await remote.start()
        await hand_control_to_the_event_loop()
        first.drop()
        await hand_control_to_the_event_loop()
        assert client.stats.reconnections == 1
        assert remote.connected
        assert remote.latest().fresh
        await remote.aclose()

    @pytest.mark.asyncio
    async def test_no_frame_is_read_while_there_is_nowhere_to_send_it(
        self,
    ) -> None:
        """Reading the camera for a frame with nowhere to go is work for nothing."""
        clock = ManualClock()
        sleep = RecordedSleep()
        media = FakeMedia()
        remote = RemotePerception(
            media,
            # Enough failures that the turns below cannot exhaust the script:
            # running off the end is an `AssertionError` inside the task, which
            # would be reported as this test failing for the wrong reason.
            _client(ScriptedTransports(*([None] * 100)), clock, sleep),
            clock=clock,
            sleep=sleep,
            offload=inline,
        )
        await remote.start()
        await hand_control_to_the_event_loop(20)
        assert media.jpeg_reads == 0
        await remote.aclose()


class TestConfigurationMistakes:
    """Caught where they are made, not by a robot that never tracks anything."""

    def test_a_frame_interval_of_nothing_is_refused(self) -> None:
        """It would submit frames as fast as the camera can be read."""
        with pytest.raises(ValueError, match="frame interval"):
            RemotePerception(FakeMedia(), _unused_client(), frame_interval=0.0)

    def test_a_staleness_window_of_nothing_is_refused(self) -> None:
        """Every result would be stale on arrival."""
        with pytest.raises(ValueError, match="staleness window"):
            RemotePerception(FakeMedia(), _unused_client(), staleness_seconds=0.0)


def _unused_client() -> SessionClient:
    """Build a client that is never connected, for the validation tests.

    Returns:
        The client.
    """
    return SessionClient(url=_URL, credential=credential())


class TestReportingOnTheLink:
    """What a settings interface or a diagnostic reads off this adapter."""

    @pytest.mark.asyncio
    async def test_the_sessions_counters_are_readable(self) -> None:
        """The client's own, handed through rather than recounted here."""
        clock = ManualClock()
        sleep = RecordedSleep()
        transport = StubTransport()
        transport.push(agreement(FACE), face_result(0))
        remote = RemotePerception(
            FakeMedia(),
            _client(ScriptedTransports(transport), clock, sleep),
            clock=clock,
            sleep=sleep,
            offload=inline,
        )
        await remote.start()
        await hand_control_to_the_event_loop()
        assert remote.stats.results_applied == 1
        assert remote.stats.connection_attempts == 1
        await remote.aclose()

    @pytest.mark.asyncio
    async def test_starting_twice_opens_one_session(self) -> None:
        """Composition roots are allowed to be careless about this."""
        clock = ManualClock()
        sleep = RecordedSleep()
        transport = StubTransport()
        transport.push(agreement(FACE))
        transports = ScriptedTransports(transport)
        remote = RemotePerception(
            FakeMedia(),
            _client(transports, clock, sleep),
            clock=clock,
            sleep=sleep,
            offload=inline,
        )
        await remote.start()
        await remote.start()
        await hand_control_to_the_event_loop()
        assert transports.attempts == 1
        await remote.aclose()

    @pytest.mark.asyncio
    async def test_a_session_ended_for_a_reason_a_person_must_fix_stops_it(
        self,
    ) -> None:
        """Mid-session, rather than at the handshake, which is a second path.

        A groundstation that closes an established session as unauthenticated —
        a credential rotated out from under a running robot — is not answered
        by reconnecting either. The adapter reports itself disconnected, which
        is what puts a fallback source onto the robot's own detector.
        """
        clock = ManualClock()
        sleep = RecordedSleep()
        transport = StubTransport()
        transport.push(agreement(FACE))
        media = FakeMedia()
        remote = RemotePerception(
            media,
            _client(ScriptedTransports(transport), clock, sleep),
            clock=clock,
            sleep=sleep,
            offload=inline,
        )
        await remote.start()
        await hand_control_to_the_event_loop()
        # Read into locals rather than asserting on the property twice: an
        # assertion narrows it for the rest of the function, and the second
        # would then be reported as dead code.
        before = remote.connected
        transport.push(session_close())
        await hand_control_to_the_event_loop()
        after = remote.connected
        assert before
        assert not after
        # And it stops rather than going round again: `connect` would hand back
        # the agreement the client is still holding, so a loop that only tested
        # whether the adapter had been closed would sit in `_exchange` against
        # a session it has already given up on, still reading the camera.
        settled = media.jpeg_reads
        await hand_control_to_the_event_loop()
        assert media.jpeg_reads == settled
        await remote.aclose()

    @pytest.mark.asyncio
    async def test_closing_a_source_that_never_started_is_harmless(self) -> None:
        """A composition root that fails half-way still gets to clean up."""
        clock = ManualClock()
        sleep = RecordedSleep()
        remote = RemotePerception(
            FakeMedia(),
            _client(ScriptedTransports(), clock, sleep),
            clock=clock,
            sleep=sleep,
            offload=inline,
        )
        await remote.aclose()
        await remote.start()
        assert not remote.connected
        await remote.aclose()


class TestOneBadTurnDoesNotStopTheFrames:
    """The frame pump runs in a task `_exchange` only awaits in a `finally`."""

    @pytest.mark.asyncio
    async def test_a_camera_read_that_raises_does_not_stop_submission(
        self,
    ) -> None:
        """The failure mode it prevents is the one this adapter promises against.

        A pump that ended on its first exception would leave the session up
        with no frames on it: results stop, `latest()` goes stale, and
        `connected` stays true — which the adapter's own docstring says is
        deliberately *not* a reason to fall back, so nothing would take over.
        """
        clock = ManualClock()
        sleep = RecordedSleep()
        transport = StubTransport()
        transport.push(agreement(FACE))
        media = _CameraFailsOnce()
        client = _client(ScriptedTransports(transport), clock, sleep)
        remote = RemotePerception(
            media,
            client,
            clock=clock,
            sleep=sleep,
            offload=inline,
        )
        await remote.start()
        await hand_control_to_the_event_loop()
        assert media.reads > 1
        assert client.stats.frames_submitted >= 1
        await remote.aclose()


class _CameraFailsOnce(FakeMedia):
    """A daemon whose first camera read raises and whose later ones do not."""

    def __init__(self) -> None:
        """Start with bytes to hand out, after the first failure."""
        super().__init__(jpeg=b"jpeg-bytes")
        self.reads = 0

    def get_frame_jpeg(self) -> bytes | None:
        """Fail once, then behave.

        Returns:
            The scripted bytes.

        Raises:
            RuntimeError: On the first call only.
        """
        self.reads += 1
        if self.reads == 1:
            message = "the pipeline had nothing to pull"
            raise RuntimeError(message)
        return super().get_frame_jpeg()
