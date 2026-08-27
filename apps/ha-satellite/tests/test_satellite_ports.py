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

import math
from collections.abc import Callable
from typing import Final, get_type_hints

import pytest
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
from reachy_mini_ha_satellite.adapters.daemon import RobotHandle
from reachy_mini_ha_satellite.adapters.motion_reachy import ReachyMotion
from reachy_mini_ha_satellite.esphome.seams import AudioCapture, MediaPlayback
from reachy_mini_ha_satellite.ports import (
    NEUTRAL_ANTENNAS,
    NEUTRAL_HEAD,
    AntennaPose,
    AudioPort,
    CalibratedGaze,
    CalibrationStatus,
    CapturePort,
    Detections,
    DetectionSource,
    GazeCalibration,
    GazeDirective,
    GazeSample,
    HeadPose,
    MotionCommandResult,
    MotionCommandStatus,
    MotionFault,
    MotionMeasurement,
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


class TestMotionValuesRejectMalformedCrossingState:
    """Port-owned values validate the atomic facts hardware consumers receive."""

    @pytest.mark.parametrize(
        ("build", "message"),
        [
            (lambda: GazeSample(math.nan, 0.0, 0.0, 0.0, False), "finite"),
            (lambda: GazeSample(0.2, 0.0, 0.0, 0.1, False), "world yaw"),
            (lambda: GazeSample(0.2, 0.0, 0.1, 0.1, False), "body-disabled"),
        ],
        ids=["finite", "gaze-identity", "disabled-body"],
    )
    def test_gaze_sample_is_finite_coordinated_and_body_consistent(
        self,
        build: Callable[[], object],
        message: str,
    ) -> None:
        """Malformed grouped samples fail before reaching a motion adapter."""
        with pytest.raises(ValueError, match=message):
            build()

    @pytest.mark.parametrize(
        ("build", "message"),
        [
            (lambda: MotionMeasurement(0.1, None, 0.0, None, None), "head"),
            (lambda: MotionMeasurement(None, None, None, 0.1, None), "body"),
            (lambda: MotionMeasurement(math.nan, 0.0, 0.0, None, None), "finite"),
        ],
        ids=["partial-head", "partial-body", "nonfinite"],
    )
    def test_motion_measurement_is_atomic_and_finite(
        self,
        build: Callable[[], object],
        message: str,
    ) -> None:
        """Construction itself rejects malformed measurement shape."""
        with pytest.raises(ValueError, match=message):
            build()

    def test_calibration_status_and_target_are_coherent(self) -> None:
        """Accepted-without-target is not a result a caller can misread."""
        with pytest.raises(ValueError, match="only accepted"):
            GazeCalibration(CalibrationStatus.ACCEPTED)

    @pytest.mark.parametrize("call", [True, -1])
    def test_motion_command_call_is_a_non_negative_real_integer(
        self,
        call: int,
    ) -> None:
        """Boolean or negative call evidence cannot advance recovery."""
        with pytest.raises(ValueError, match="non-negative integer"):
            MotionCommandResult(MotionCommandStatus.ACCEPTED, call=call)

    @pytest.mark.parametrize(
        ("status", "fault"),
        [
            (MotionCommandStatus.ACCEPTED, MotionFault.COMMAND),
            (MotionCommandStatus.REJECTED, MotionFault.NONE),
        ],
    )
    def test_motion_command_status_and_fault_are_coherent(
        self,
        status: MotionCommandStatus,
        fault: MotionFault,
    ) -> None:
        """A caller cannot mistake a rejected daemon call for acceptance."""
        with pytest.raises(ValueError, match="accepted motion commands"):
            MotionCommandResult(status, fault)

    def test_valid_measurements_cannot_also_claim_typed_faults(self) -> None:
        """Typed validity has one unambiguous state per measured channel."""
        with pytest.raises(ValueError, match="valid head"):
            MotionMeasurement(0.0, 0.0, 0.0, None, None, head_fault=MotionFault.POSE)
        with pytest.raises(ValueError, match="valid body"):
            MotionMeasurement(
                None,
                None,
                None,
                0.0,
                0.0,
                body_fault=MotionFault.POSE,
            )

    def test_accepted_calibration_cannot_carry_a_fault(self) -> None:
        """The typed calibration boundary cannot be both valid and invalid."""
        target = CalibratedGaze(
            source=DetectionSource.REMOTE,
            generation=0,
            sequence=0,
            captured_at=0.0,
            received_at=0.1,
            target_epoch=0,
            world_yaw=0.0,
            world_elevation=0.0,
        )
        with pytest.raises(ValueError, match="accepted calibration"):
            GazeCalibration(
                CalibrationStatus.ACCEPTED,
                target,
                MotionFault.CALIBRATION,
            )


class TestDaemonSurfaceIsNarrow:
    """The structural SDK surface names only calls active adapters consume."""

    def test_robot_handle_has_no_legacy_motion_members(self) -> None:
        """A removed runtime path cannot keep widening the SDK boundary."""
        public = {name for name in vars(RobotHandle) if not name.startswith("_")}

        assert public == {
            "enable_motors",
            "enable_motors_confirmed",
            "disable_motors_confirmed",
            "read_motor_torque",
            "wake_up",
            "media",
            "set_target",
            "get_current_head_pose",
            "get_current_joint_positions",
            "look_at_image",
            "set_automatic_body_yaw",
        }


class TestPortAnnotationsResolve:
    """Crossing motion values are owned and resolvable at the port boundary."""

    def test_motion_port_type_hints_resolve_without_import_order(self) -> None:
        """Runtime introspection must not depend on behavior TYPE_CHECKING names."""
        acquire = get_type_hints(MotionPort.acquire)
        observe = get_type_hints(MotionPort.observe)
        calibrate = get_type_hints(MotionPort.calibrate)
        command = get_type_hints(MotionPort.command_gaze)

        assert acquire["return"] is MotionMeasurement
        assert observe["return"] is MotionMeasurement
        assert calibrate["directive"] is GazeDirective
        assert command["sample"] is GazeSample


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

    def test_stale_construction_hides_faces_but_preserves_provenance(self) -> None:
        """A legacy caller cannot make a stale target available to behaviour."""
        stale = Detections(
            faces=(face(0.5, -0.25),),
            fresh=False,
            source=_ROBOT,
            age_seconds=2.5,
            generation=3,
            sequence=7,
            captured_at=10.0,
            received_at=10.2,
        )

        assert stale.faces == ()
        assert stale.identity == (_ROBOT, 3, 7)
        assert stale.captured_at == 10.0
        assert stale.received_at == 10.2

    def test_a_view_carries_the_faces_it_was_given(self) -> None:
        """The contract's own detection type, not a second copy of it."""
        view = Detections(faces=(face(0.5, -0.25),), fresh=True)
        assert view.faces[0].centre.x == 0.5
        assert view.faces[0].centre.y == -0.25

    def test_legacy_construction_leaves_observation_provenance_unknown(self) -> None:
        """Existing callers can keep constructing the four-field vocabulary."""
        view = Detections(faces=(face(0.0, 0.0),), fresh=True, source=_ROBOT)

        assert view.generation is None
        assert view.sequence is None
        assert view.captured_at is None
        assert view.received_at is None
        assert view.identity is None

    def test_complete_provenance_defines_source_qualified_identity(self) -> None:
        """A sequence is meaningful only inside one source generation."""
        view = Detections(
            faces=(face(0.0, 0.0),),
            fresh=True,
            source=_ROBOT,
            generation=3,
            sequence=7,
            captured_at=10.0,
            received_at=10.2,
        )

        assert view.identity == (_ROBOT, 3, 7)

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
