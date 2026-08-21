"""The three ports, their fakes, and the two seams the adapters have to fill.

What is being pinned here is the boundary itself rather than any behaviour
behind it: that every port has a fake, that every fake answers the port, that
the real adapters answer the same ports, and that the audio adapters answer the
vendored protocol layer's two seams as well — structurally, without either side
importing the other.

The `isinstance` checks are the run-time half. The annotated assignments beside
them are the other half and the stricter one: a `Protocol`-typed variable makes
mypy check the *signatures*, which `runtime_checkable` deliberately does not, so
an adapter whose `play` grew a required argument fails the type check even
though the attribute is still there.
"""

from __future__ import annotations

from typing import Final

from satellite_support import (
    FakeAudio,
    FakeCapture,
    FakeMedia,
    FakeMotion,
    FakePerception,
    FakePlayback,
    FakeRobot,
    FakeSoundSource,
    face,
    immediately,
)

from reachy_mini_ha_satellite.adapters.audio_reachy import (
    ReachyAudio,
    ReachyCapture,
    ReachyPlayback,
)
from reachy_mini_ha_satellite.adapters.motion_reachy import ReachyMotion
from reachy_mini_ha_satellite.esphome.seams import AudioCapture, MediaPlayback
from reachy_mini_ha_satellite.ports import (
    NEUTRAL_ANTENNAS,
    NEUTRAL_HEAD,
    AntennaPose,
    AudioPort,
    CapturePort,
    Detections,
    DetectionSource,
    HeadPose,
    MotionPort,
    PerceptionPort,
    PlaybackPort,
    SourceSelection,
)

# The two enum members whose dotted form the repository's leak scanner reads as
# an mDNS hostname. Bound once here, with the per-line marker its own docstring
# says this case is what the marker is for.
_ROBOT: Final = DetectionSource.LOCAL  # leak-scan:allow


def _audio() -> ReachyAudio:
    """Build the real audio adapter over fakes.

    Returns:
        The adapter.
    """
    return ReachyAudio(FakeMedia(), FakeSoundSource(), detach=immediately)


class TestEveryPortHasAFake:
    """A port whose only implementation touched a device would strand 0013."""

    def test_the_audio_fake_answers_the_audio_port(self) -> None:
        """The behaviour layer can be handed one without a microphone."""
        port: AudioPort = FakeAudio()
        assert isinstance(port, AudioPort)
        assert isinstance(port.capture, CapturePort)
        assert isinstance(port.music, PlaybackPort)
        assert isinstance(port.speech, PlaybackPort)

    def test_the_motion_fake_answers_the_motion_port(self) -> None:
        """The behaviour layer can be handed one without a robot."""
        port: MotionPort = FakeMotion()
        assert isinstance(port, MotionPort)

    def test_the_perception_fake_answers_the_perception_port(self) -> None:
        """The behaviour layer can be handed one without a camera."""
        port: PerceptionPort = FakePerception()
        assert isinstance(port, PerceptionPort)


class TestTheAdaptersAnswerTheSamePorts:
    """Whatever `main.py` wires in has to be substitutable for a fake."""

    def test_the_audio_adapter_answers_the_audio_port(self) -> None:
        """Nothing is opened by building one, which is what lets this run."""
        port: AudioPort = _audio()
        assert isinstance(port, AudioPort)

    def test_the_motion_adapter_answers_the_motion_port(self) -> None:
        """The daemon handle is a protocol, so a fake robot is enough."""
        port: MotionPort = ReachyMotion(FakeRobot())
        assert isinstance(port, MotionPort)


class TestTheAudioSeamsAreFilled:
    """Change 0011 cut two holes and left them open. This is them closed."""

    def test_the_playback_adapter_satisfies_the_playback_seam(self) -> None:
        """`ServerState.music_player` and `tts_player` can hold one."""
        playback: MediaPlayback = ReachyPlayback(
            FakeMedia(), FakeSoundSource(), detach=immediately
        )
        assert isinstance(playback, MediaPlayback)

    def test_the_capture_adapter_satisfies_the_capture_seam(self) -> None:
        """`ServerState.audio_capture` can hold one."""
        capture: AudioCapture = ReachyCapture(FakeMedia())
        assert isinstance(capture, AudioCapture)

    def test_the_playback_fake_satisfies_the_playback_seam(self) -> None:
        """So the vendored protocol layer can be driven without an adapter."""
        playback: MediaPlayback = FakePlayback()
        assert isinstance(playback, MediaPlayback)

    def test_the_capture_fake_satisfies_the_capture_seam(self) -> None:
        """The same, for the microphone."""
        capture: AudioCapture = FakeCapture()
        assert isinstance(capture, AudioCapture)

    def test_the_seams_are_satisfied_structurally_and_not_by_inheritance(
        self,
    ) -> None:
        """Which is what lets the dependency run one way.

        `just lint-boundary` proves the vendored directory imports nothing
        Reachy-specific. This is the claim on the other side of the seam: an
        adapter never subclasses either protocol, so the vendored code never
        has to be reachable from one. The constants it *does* import — the
        sample rate and width — are values both sides have to agree on, which
        is why they live in the seam rather than in an adapter.
        """
        assert MediaPlayback not in ReachyPlayback.__mro__
        assert AudioCapture not in ReachyCapture.__mro__


class TestTheValueTypesThePortsSpeakIn:
    """Small types, but the behaviour layer's whole vocabulary."""

    def test_neutral_is_a_value_rather_than_three_zeroes(self) -> None:
        """REQ-048 is about arriving here, so it has a name."""
        assert HeadPose(yaw=0.0, pitch=0.0, roll=0.0) == NEUTRAL_HEAD
        assert AntennaPose(left=0.0, right=0.0) == NEUTRAL_ANTENNAS

    def test_nothing_seen_yet_is_not_the_same_as_nothing_there(self) -> None:
        """An empty view before anything arrives is not fresh."""
        assert Detections() == Detections(faces=(), fresh=False, source=None)
        assert not Detections().fresh

    def test_an_empty_fresh_view_is_an_ordinary_answer(self) -> None:
        """Nobody in the room is a successful detection of nobody.

        It has to stay distinguishable from the results having stopped: one
        leaves the head where it is and the other returns it to neutral.
        """
        empty = Detections(faces=(), fresh=True, source=_ROBOT)
        stale = Detections(faces=(), fresh=False, source=_ROBOT)
        assert empty.fresh
        assert not stale.fresh
        assert empty != stale

    def test_a_view_carries_the_faces_it_was_given(self) -> None:
        """The contract's own detection type, not a second copy of it."""
        view = Detections(faces=(face(0.5, -0.25),), fresh=True)
        assert view.faces[0].centre.x == 0.5
        assert view.faces[0].centre.y == -0.25

    def test_the_three_selectable_sources_are_the_three_the_spec_names(
        self,
    ) -> None:
        """REQ-047 lists them, and adding a fourth is a spec change."""
        assert {selection.value for selection in SourceSelection} == {
            "remote",
            "local",
            "remote_with_local_fallback",
        }

    def test_a_detection_says_which_detector_produced_it(self) -> None:
        """Which is a question an operator asks and behaviour does not."""
        assert {source.value for source in DetectionSource} == {"remote", "local"}
