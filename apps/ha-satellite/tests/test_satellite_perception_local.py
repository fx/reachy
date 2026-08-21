"""Face detection on the robot, and the coordinates it has to report in.

Two different things are pinned here.

**The conversion**, which is exercised through a fake detector reporting known
pixels. That is where robot-link REQ-021 lives on this side of the link: the
same face in the same place has to normalise to the same coordinates whatever
resolution the frame arrived at, and a detector that answered in pixels would
send the head somewhere different every time the capture resolution changed.

**The loading of the SDK**, which is exercised against the real distribution
where it is installed. No inference is run: this repository already compares
these weights against its own decoding in the groundstation's parity gate, and
running the same model again here would measure the same thing more slowly.
What is checked instead is that the module loads without dragging in GStreamer,
and that the weights it is pinned to are the ones the groundstation pins — which
is the whole of what "switching source changes latency, not accuracy" claims.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Final

import pytest
from satellite_support import (
    FakeFaceDetector,
    FakeMedia,
    ManualClock,
    frame,
    inline,
)

from reachy_mini_ha_satellite.adapters.perception_local import (
    SDK_MODEL_REPO,
    SDK_MODEL_REVISION,
    LocalPerception,
    PixelFace,
    SdkFaceDetector,
    load_sdk_face_detector,
    normalised_centre,
)
from reachy_mini_ha_satellite.ports import DetectionSource

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from reachy_mini_ha_satellite.adapters.daemon import ImageArray, Offload

# A face two thirds of the way across the frame and a third of the way down,
# expressed as a box in the pixels of two different capture resolutions. The
# same scene, captured twice.
_AT_640 = PixelFace(x=400.0, y=140.0, width=52.0, height=52.0, confidence=0.88)
_AT_320 = PixelFace(x=200.0, y=70.0, width=26.0, height=26.0, confidence=0.88)


# The two enum members whose dotted form the repository's leak scanner reads as
# an mDNS hostname. Bound once here, with the per-line marker its own docstring
# says this case is what the marker is for.
_ROBOT: Final = DetectionSource.LOCAL  # leak-scan:allow


async def _immediately(seconds: float) -> None:
    """Hand control to the event loop instead of waiting out an interval.

    `asyncio.sleep(0)` reads no clock and schedules no timer — it yields and
    resumes on the loop's next pass — so it is not the sleeping the
    no-input-or-output rule forbids. Yielding rather than returning outright is
    what stops the detection loop spinning without ever letting anything else
    run.

    Args:
        seconds: How long the caller wanted to wait, ignored.
    """
    del seconds
    await asyncio.sleep(0)


def _source(
    media: FakeMedia,
    detector: FakeFaceDetector,
    clock: ManualClock,
    *,
    sleep: Callable[[float], Awaitable[None]] = _immediately,
) -> LocalPerception:
    """Build the local source over a fake camera and a fake detector.

    Args:
        media: The daemon's media layer.
        detector: What looks at a frame.
        clock: The monotonic source freshness is measured against.
        sleep: How the loop waits between looks.

    Returns:
        The source.
    """
    return LocalPerception(
        media,
        detector=lambda: detector,
        clock=clock,
        sleep=sleep,
        offload=inline,
    )


class TestTheContractsCoordinates:
    """REQ-021: normalised, origin at the centre, vertical axis upwards."""

    def test_the_centre_of_the_frame_is_the_origin(self) -> None:
        """Which is what makes the two axes signed rather than offset."""
        point = normalised_centre(320.0, 240.0, 640, 480)
        assert point.x == pytest.approx(0.0)
        assert point.y == pytest.approx(0.0)

    def test_the_upper_left_is_negative_across_and_positive_up(self) -> None:
        """The sign of the vertical axis is the one that is silently wrong."""
        point = normalised_centre(0.0, 0.0, 640, 480)
        assert point.x == pytest.approx(-1.0)
        assert point.y == pytest.approx(1.0)

    def test_the_lower_right_is_positive_across_and_negative_up(self) -> None:
        """The other corner, so the convention is pinned at both ends."""
        point = normalised_centre(640.0, 480.0, 640, 480)
        assert point.x == pytest.approx(1.0)
        assert point.y == pytest.approx(-1.0)

    def test_a_detection_just_outside_the_frame_is_held_to_the_edge(
        self,
    ) -> None:
        """A face at the edge is a detection at the edge, not a lost frame.

        `NormalisedCoordinate` refuses anything outside plus or minus one, and
        a box the regression pushed past the edge would otherwise cost the whole
        frame its answer.
        """
        point = normalised_centre(-40.0, 600.0, 640, 480)
        assert point.x == -1.0
        assert point.y == -1.0


class TestDetectionsFromTheRobotsOwnCores:
    """The loop: look, convert, remember, and say how old it is."""

    @pytest.mark.asyncio
    async def test_a_detected_face_becomes_the_ports_answer(self) -> None:
        """With the source named, so an operator can see which one answered."""
        clock = ManualClock()
        source = _source(
            FakeMedia(image=frame(480, 640)),
            FakeFaceDetector([_AT_640]),
            clock,
        )
        await source.start()
        await _settle()
        view = source.latest()
        assert view.fresh
        assert view.source is _ROBOT
        assert len(view.faces) == 1
        assert view.faces[0].confidence == pytest.approx(0.88)
        await source.aclose()

    @pytest.mark.asyncio
    async def test_the_same_face_at_two_resolutions_reports_the_same_place(
        self,
    ) -> None:
        """REQ-021's scenario: halve the resolution, capture the same scene."""
        clock = ManualClock()
        wide = _source(
            FakeMedia(image=frame(480, 640)),
            FakeFaceDetector([_AT_640]),
            clock,
        )
        narrow = _source(
            FakeMedia(image=frame(240, 320)),
            FakeFaceDetector([_AT_320]),
            clock,
        )
        await wide.start()
        await narrow.start()
        await _settle()
        assert wide.latest().faces[0].centre.x == pytest.approx(
            narrow.latest().faces[0].centre.x,
        )
        assert wide.latest().faces[0].centre.y == pytest.approx(
            narrow.latest().faces[0].centre.y,
        )
        await wide.aclose()
        await narrow.aclose()

    @pytest.mark.asyncio
    async def test_the_detector_is_shown_the_frame_at_its_own_size(
        self,
    ) -> None:
        """No resizing on the robot: the daemon's frame is what is looked at."""
        clock = ManualClock()
        detector = FakeFaceDetector([_AT_640])
        source = _source(FakeMedia(image=frame(480, 640)), detector, clock)
        await source.start()
        await _settle()
        assert detector.seen[0] == (480, 640)
        await source.aclose()

    @pytest.mark.asyncio
    async def test_an_empty_scene_is_a_fresh_answer_carrying_nobody(
        self,
    ) -> None:
        """Which is a different event from the detections having stopped."""
        clock = ManualClock()
        source = _source(FakeMedia(image=frame()), FakeFaceDetector([]), clock)
        await source.start()
        await _settle()
        view = source.latest()
        assert view.fresh
        assert view.faces == ()
        await source.aclose()

    @pytest.mark.asyncio
    async def test_detections_go_stale_when_the_looking_stops(self) -> None:
        """A local detector falls behind for its own reasons; REQ-048 is the same."""
        clock = ManualClock()
        source = _source(
            FakeMedia(image=frame()),
            FakeFaceDetector([_AT_640]),
            clock,
        )
        await source.start()
        await _settle()
        assert source.latest().fresh
        clock.advance(5.0)
        assert not source.latest().fresh
        assert source.latest().faces == ()
        await source.aclose()

    @pytest.mark.asyncio
    async def test_nothing_looked_at_yet_is_not_fresh(self) -> None:
        """A source that has just started has nothing to act on."""
        clock = ManualClock()
        source = _source(FakeMedia(image=None), FakeFaceDetector(), clock)
        await source.start()
        await _settle()
        assert not source.latest().fresh
        await source.aclose()

    @pytest.mark.asyncio
    async def test_a_camera_that_has_no_frame_is_waited_on(self) -> None:
        """No camera is not a detection of nobody; it is no detection."""
        clock = ManualClock()
        detector = FakeFaceDetector([_AT_640])
        source = _source(FakeMedia(image=None), detector, clock)
        await source.start()
        await _settle()
        assert detector.seen == []
        await source.aclose()


class TestFailuresDoNotTakeTheApplicationDown:
    """This source is often only ever the fallback; it must fail quietly."""

    @pytest.mark.asyncio
    async def test_a_detector_that_will_not_load_leaves_the_source_silent(
        self,
    ) -> None:
        """Which reads as "not fresh", and returns the head to neutral.

        The alternative is the application refusing to start over a detector it
        may never have needed.
        """

        def _explode() -> FakeFaceDetector:
            message = "no model here"
            raise RuntimeError(message)

        source = LocalPerception(
            FakeMedia(image=frame()),
            detector=_explode,
            clock=ManualClock(),
            sleep=_immediately,
            offload=inline,
        )
        await source.start()
        await _settle()
        assert not source.latest().fresh
        await source.aclose()

    @pytest.mark.asyncio
    async def test_a_detector_that_fails_on_a_frame_keeps_looking(self) -> None:
        """One bad frame is not a reason to stop watching the room."""
        clock = ManualClock()
        detector = _FailsOnce([_AT_640])
        source = LocalPerception(
            FakeMedia(image=frame()),
            detector=lambda: detector,
            clock=clock,
            sleep=_immediately,
            offload=inline,
        )
        await source.start()
        await _settle()
        assert detector.calls > 1
        assert source.latest().fresh
        await source.aclose()

    @pytest.mark.asyncio
    async def test_closing_releases_the_inference_session(self) -> None:
        """It owns an arena and a thread, and the robot has four cores."""
        detector = FakeFaceDetector([_AT_640])
        source = _source(FakeMedia(image=frame()), detector, ManualClock())
        await source.start()
        await _settle()
        await source.aclose()
        assert detector.closed == 1
        assert not source.latest().fresh

    @pytest.mark.asyncio
    async def test_starting_twice_starts_one_loop(self) -> None:
        """A fallback source can be asked to start while it already is."""
        detector = FakeFaceDetector([_AT_640])
        source = _source(FakeMedia(image=frame()), detector, ManualClock())
        await source.start()
        await source.start()
        await _settle()
        await source.aclose()
        assert detector.closed == 1

    def test_a_detection_interval_of_nothing_is_refused(self) -> None:
        """It would run the model as fast as a core allows, on four of them."""
        with pytest.raises(ValueError, match="detection interval"):
            LocalPerception(
                FakeMedia(),
                detector=FakeFaceDetector,
                interval=0.0,
            )

    def test_a_staleness_window_of_nothing_is_refused(self) -> None:
        """Every detection would be stale the moment it was made."""
        with pytest.raises(ValueError, match="staleness window"):
            LocalPerception(
                FakeMedia(),
                detector=FakeFaceDetector,
                staleness_seconds=0.0,
            )


class TestTheSdkStaysOutOfTheImportGraph:
    """Architecture REQ-005: the suite runs with no robot attached."""

    def test_importing_the_adapter_does_not_import_the_sdk(self) -> None:
        """An ordinary import of it executes `import gi` three modules away.

        Every module in this package has been imported by the time this runs —
        `conftest.py` preloads them — so the SDK being absent from
        `sys.modules` is what "the import is lazy" means concretely.
        """
        assert "reachy_mini" not in sys.modules
        assert "gi" not in sys.modules

    @pytest.mark.filesystem
    def test_the_local_detector_runs_the_weights_the_groundstation_runs(
        self,
    ) -> None:
        """REQ-047's claim, checked rather than asserted in a comment.

        Switching between the groundstation and the robot's own detector is
        supposed to change latency and CPU cost and nothing else. That is only
        true while both run the same file, so the revision the SDK pins is read
        back out of its own source and compared against the one recorded here —
        which is the revision `services/groundstation/.../models/registry.py`
        pins for the same model.

        The file is read rather than imported, for the reason the adapter reads
        it by path: importing it would bring GStreamer with it.
        """
        source = _sdk_detector_source()
        text = source.read_text(encoding="utf-8")
        assert f'_MODEL_REPO = "{SDK_MODEL_REPO}"' in text
        assert f'_MODEL_REVISION = "{SDK_MODEL_REVISION}"' in text

    @pytest.mark.filesystem
    def test_the_sdk_detector_loads_by_file_path_without_gstreamer(
        self,
    ) -> None:
        """The bypass itself, run against the real distribution.

        It reads a file, so it is not a unit test and says so. What it proves
        is the thing the whole local source depends on: that one module of the
        SDK can be executed on its own, and that doing so pulls in none of the
        system libraries a runner has not got.
        """
        from reachy_mini_ha_satellite.adapters.perception_local import (
            load_sdk_face_detector,
        )

        module = load_sdk_face_detector(_sdk_detector_source())
        assert hasattr(module, "FaceDetector")
        assert hasattr(module, "_nms")
        assert "gi" not in sys.modules

    def test_a_missing_distribution_is_reported_as_a_configuration_problem(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """And says what to do, because it is a deployment mistake.

        An operator who selected local detection without the extra installed
        should be told that, rather than watching a robot that never tracks
        anything.

        Args:
            monkeypatch: Used to make the distribution look absent for the
                duration of this test, which is cheaper and more reversible
                than uninstalling it.
        """
        from reachy_mini_ha_satellite.adapters.perception_local import (
            load_sdk_face_detector,
        )

        monkeypatch.setattr(importlib.util, "find_spec", lambda *_args: None)
        with pytest.raises(RuntimeError, match="local-detection"):
            load_sdk_face_detector(Path("/unused"))


class _FailsOnce(FakeFaceDetector):
    """A detector whose first frame raises and whose later ones do not."""

    def __init__(self, faces: list[PixelFace]) -> None:
        """Load the detector.

        Args:
            faces: What every frame after the first produces.
        """
        super().__init__(faces)
        self.calls = 0

    def detect(self, image: object) -> list[PixelFace]:
        """Fail the first time and answer after that.

        Args:
            image: The frame.

        Returns:
            The scripted faces.

        Raises:
            RuntimeError: On the first call only.
        """
        del image
        self.calls += 1
        if self.calls == 1:
            message = "the runtime declined this frame"
            raise RuntimeError(message)
        return list(self.faces)


def _sdk_detector_source() -> Path:
    """Find the SDK's face detector module on disk.

    Returns:
        The path to it.
    """
    package = importlib.util.find_spec("reachy_mini")
    locations = None if package is None else package.submodule_search_locations
    if not locations:
        pytest.skip("reachy-mini is not installed; the local detector is unavailable")
    return Path(next(iter(locations))) / "vision" / "face_detector.py"


async def _settle() -> None:
    """Let the detection loop run a few turns without waiting for a clock."""
    for _ in range(50):
        await asyncio.sleep(0)


class _StandInSdk:
    """A stand-in for the SDK's detector module, in the shape it really has.

    Not a mock of `SdkFaceDetector`: it is the thing on the *other* side of it,
    so the wrapping, the score recovery and the ordering below are the real
    code. The SDK's own `Face` carries a box and three landmarks and no score,
    and its `FaceDetector.detect` reaches its suppression step through the
    module's global — both of which are what make the recovery necessary and
    possible, so both are reproduced here.
    """

    def __init__(
        self,
        *,
        boxes: list[tuple[float, float, float, float]],
        scores: list[float],
        kept: list[int],
        suppress: bool = True,
    ) -> None:
        """Script what the detector finds and what its suppression keeps.

        Args:
            boxes: The candidate boxes it decodes.
            scores: The confidence it computed for each.
            kept: Which indices its suppression step keeps, in order.
            suppress: Whether it reaches its suppression step at all. False
                stands in for a module that has changed shape under us.
        """
        self.module = ModuleType("satellite_test_sdk_detector")
        self.built: list[tuple[float, float]] = []
        stand_in: Any = self.module
        stand_in._nms = _keep_these(kept)
        stand_in.Face = _StandInFace
        stand_in.FaceDetector = _detector_class(self, boxes, scores, kept, suppress)
        stand_in.hf_hub_download = lambda *_a, **_k: "/models/unused.onnx"


class _StandInFace:
    """One face as the SDK reports it: a bounding box and no confidence."""

    def __init__(self, bbox: tuple[float, float, float, float]) -> None:
        """Record the box.

        Args:
            bbox: Left, top, width and height, in pixels.
        """
        self.bbox = bbox


def _keep_these(kept: list[int]) -> Callable[..., list[int]]:
    """Build a suppression step that keeps a fixed set of indices.

    Args:
        kept: What it keeps.

    Returns:
        The suppression step.
    """

    def _nms(
        boxes: object,
        scores: object,
        iou_threshold: object,
    ) -> list[int]:
        del boxes, scores, iou_threshold
        return list(kept)

    return _nms


def _detector_class(
    owner: _StandInSdk,
    boxes: list[tuple[float, float, float, float]],
    scores: list[float],
    kept: list[int],
    suppress: bool,
) -> type:
    """Build the module's `FaceDetector`, reaching its suppression by global.

    Args:
        owner: The stand-in module holder, so construction is recorded.
        boxes: The candidate boxes.
        scores: The confidence for each.
        kept: Which indices suppression keeps.
        suppress: Whether to reach the suppression step at all.

    Returns:
        The class to put on the module.
    """

    class _Detector:
        def __init__(self, score_threshold: float, nms_threshold: float) -> None:
            owner.built.append((score_threshold, nms_threshold))
            self._nms_threshold = nms_threshold

        def detect(self, frame_bgr: object) -> list[_StandInFace]:
            del frame_bgr
            if suppress:
                # Through the module's own global, which is what the wrapper
                # replaced. Calling a captured reference instead would not
                # exercise the recovery at all.
                current: Any = owner.module
                chosen = current._nms(boxes, scores, self._nms_threshold)
            else:
                chosen = kept
            return [_StandInFace(boxes[index]) for index in chosen]

    return _Detector


class TestRecoveringTheConfidenceTheSdkDoesNotReport:
    """`Face` carries no score, and `FaceDetection` requires one."""

    def test_each_face_carries_the_score_the_detector_computed_for_it(
        self,
    ) -> None:
        """And the scores line up with the faces suppression actually kept."""
        sdk = _StandInSdk(
            boxes=[(10.0, 20.0, 30.0, 40.0), (50.0, 60.0, 10.0, 10.0)],
            scores=[0.91, 0.64],
            kept=[1, 0],
        )
        found = SdkFaceDetector(sdk.module).detect(frame())
        assert [face.confidence for face in found] == [0.64, 0.91]
        assert found[0].x == 50.0
        assert found[1].width == 30.0

    def test_the_detector_is_built_at_the_thresholds_it_was_given(self) -> None:
        """The same sensitivity the groundstation's face capability runs at."""
        sdk = _StandInSdk(boxes=[], scores=[], kept=[])
        SdkFaceDetector(sdk.module, score_threshold=0.7, nms_threshold=0.2)
        assert sdk.built == [(0.7, 0.2)]

    def test_a_frame_with_no_face_reports_none(self) -> None:
        """An empty scene, which is an answer rather than a failure."""
        sdk = _StandInSdk(boxes=[], scores=[], kept=[])
        assert SdkFaceDetector(sdk.module).detect(frame()) == ()

    def test_faces_without_a_suppression_step_are_dropped_rather_than_guessed(
        self,
    ) -> None:
        """A made-up confidence is worse than no detection.

        The SDK's own code cannot report a face without going through its
        suppression step, so reaching this means the module has changed shape.
        Reporting a plausible number for it would send a number to a motor.
        """
        sdk = _StandInSdk(
            boxes=[(1.0, 2.0, 3.0, 4.0)],
            scores=[0.9],
            kept=[0],
            suppress=False,
        )
        assert SdkFaceDetector(sdk.module).detect(frame()) == ()

    def test_two_frames_do_not_share_one_frames_scores(self) -> None:
        """The recording is cleared per call, so a stale score cannot leak."""
        sdk = _StandInSdk(
            boxes=[(1.0, 2.0, 3.0, 4.0)],
            scores=[0.55],
            kept=[0],
        )
        detector = SdkFaceDetector(sdk.module)
        assert detector.detect(frame())[0].confidence == pytest.approx(0.55)
        assert detector.detect(frame())[0].confidence == pytest.approx(0.55)

    def test_closing_drops_the_inference_session(self) -> None:
        """Which is what releases the runtime's arena and its thread.

        A detection that arrives after it finds nothing rather than raising: a
        detection already running on a worker thread is not cancelled by the
        task that started it being cancelled, so it can outlive the close.
        """
        sdk = _StandInSdk(
            boxes=[(1.0, 2.0, 3.0, 4.0)],
            scores=[0.8],
            kept=[0],
        )
        detector = SdkFaceDetector(sdk.module)
        assert detector.detect(frame())
        detector.close()
        assert detector.detect(frame()) == ()

    def test_closing_twice_is_harmless(self) -> None:
        """A shutdown and a termination signal can both arrive."""
        sdk = _StandInSdk(boxes=[], scores=[], kept=[])
        detector = SdkFaceDetector(sdk.module)
        detector.close()
        detector.close()
        assert detector.detect(frame()) == ()


class TestLoadingTheSdkModuleFailsLoudly:
    """A detector that will not load must say why, not answer nothing."""

    def test_a_distribution_whose_layout_moved_is_reported(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Otherwise what got loaded is not what the docstring describes.

        Args:
            monkeypatch: Used to point the lookup at a directory that has no
                detector in it.
        """
        moved = importlib.util.spec_from_loader("reachy_mini", loader=None)
        assert moved is not None
        moved.submodule_search_locations = ["/nowhere-that-exists"]
        monkeypatch.setattr(importlib.util, "find_spec", lambda *_args: moved)
        with pytest.raises(RuntimeError, match="has moved"):
            load_sdk_face_detector(Path("/unused"))

    def test_a_module_that_will_not_load_is_reported(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A spec with no loader is not something to carry on from.

        Args:
            monkeypatch: Used to make the loader lookup fail.
        """
        monkeypatch.setattr(
            importlib.util,
            "spec_from_file_location",
            lambda *_args, **_kwargs: None,
        )
        with pytest.raises(RuntimeError, match="cannot load"):
            load_sdk_face_detector(_sdk_detector_source())

    def test_a_module_that_raises_while_loading_is_not_left_registered(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A half-initialised module makes the second failure unlike the first.

        Left in `sys.modules`, every later load hands back the broken object
        rather than trying again — so the error a person eventually sees has
        nothing to do with what actually went wrong.

        Args:
            monkeypatch: Used to substitute a loader that raises.
        """
        exploding = importlib.util.spec_from_loader(
            "satellite_test_exploding",
            loader=_ExplodingLoader(),
        )
        monkeypatch.setattr(
            importlib.util,
            "spec_from_file_location",
            lambda *_args, **_kwargs: exploding,
        )
        with pytest.raises(RuntimeError, match="while loading"):
            load_sdk_face_detector(_sdk_detector_source())
        assert "reachy_mini_satellite_face_detector" not in sys.modules


class _ExplodingLoader:
    """A loader whose module raises the moment it is executed."""

    def load_module(self, fullname: str, /) -> ModuleType:
        """The legacy entry point, present only to satisfy `LoaderProtocol`.

        Args:
            fullname: The module's name, unused.

        Returns:
            Never.

        Raises:
            NotImplementedError: Always. `importlib` reaches a loader through
                `exec_module`; this exists because the protocol still names the
                interface it replaced.
        """
        raise NotImplementedError

    def create_module(self, spec: ModuleSpec) -> ModuleType | None:
        """Use the default module creation.

        Args:
            spec: The specification, unused.

        Returns:
            `None`, which asks for the default.
        """
        del spec
        return None

    def exec_module(self, module: ModuleType) -> None:
        """Fail the way a broken third-party module would.

        Args:
            module: The module being executed, unused.

        Raises:
            RuntimeError: Always.
        """
        del module
        message = "the module failed while loading"
        raise RuntimeError(message)


class TestOneBadTurnIsNotTheEndOfDetection:
    """The loop runs in a task nobody awaits until shutdown."""

    @pytest.mark.asyncio
    async def test_a_camera_read_that_raises_does_not_stop_the_loop(
        self,
    ) -> None:
        """The daemon's camera can fail a pull; the room is still worth watching."""
        clock = ManualClock()
        detector = FakeFaceDetector([_AT_640])
        media = _CameraFailsOnce()
        source = LocalPerception(
            media,
            detector=lambda: detector,
            clock=clock,
            sleep=_immediately,
            offload=inline,
        )
        await source.start()
        await _settle()
        assert media.reads > 1
        assert source.latest().fresh
        await source.aclose()

    @pytest.mark.asyncio
    async def test_a_failed_turn_does_not_surface_out_of_the_shutdown(
        self,
    ) -> None:
        """Otherwise `aclose` reports a failure from long before it was called."""
        clock = ManualClock()
        source = LocalPerception(
            _CameraAlwaysFails(),
            detector=FakeFaceDetector,
            clock=clock,
            sleep=_immediately,
            offload=inline,
        )
        await source.start()
        await _settle()
        await source.aclose()
        assert not source.latest().fresh


class TestTheScoresHaveToLineUpWithTheFaces:
    """A confidence belonging to a different candidate is a made-up number."""

    def test_a_second_suppression_pass_drops_the_frame(self) -> None:
        """Rather than pairing scores with faces they do not belong to.

        The recorded scores are the last suppression call's. Where the detector
        reached that step twice for one frame, they describe candidates the
        reported faces are not — and zipping them strictly would raise once per
        frame, which is five tracebacks a second and a permanently blind
        detector.
        """
        sdk = _StandInSdk(
            boxes=[(1.0, 2.0, 3.0, 4.0), (5.0, 6.0, 7.0, 8.0)],
            scores=[0.9, 0.8],
            kept=[0, 1],
        )
        stand_in: Any = sdk.module
        original = stand_in._nms

        def _keeps_fewer(
            boxes: object,
            scores: object,
            iou_threshold: object,
        ) -> list[int]:
            original(boxes, scores, iou_threshold)
            return [0]

        stand_in._nms = _keeps_fewer
        detector = SdkFaceDetector(sdk.module)
        # Put the two-face answer back, so the count the detector reports and
        # the count suppression recorded disagree — which is the shape a
        # per-scale suppression pass produces.
        stand_in._nms = original
        assert detector.detect(frame()) == ()


class _CameraFailsOnce(FakeMedia):
    """A daemon whose first camera read raises and whose later ones do not."""

    def __init__(self) -> None:
        """Start with a frame to hand out, after the first failure."""
        super().__init__(image=frame(480, 640))
        self.reads = 0

    def get_frame(self) -> ImageArray | None:
        """Fail once, then behave.

        Returns:
            The scripted frame.

        Raises:
            RuntimeError: On the first call only.
        """
        self.reads += 1
        if self.reads == 1:
            message = "the pipeline had nothing to pull"
            raise RuntimeError(message)
        return super().get_frame()


class _CameraAlwaysFails(FakeMedia):
    """A daemon whose camera never answers."""

    def get_frame(self) -> ImageArray | None:
        """Fail every time.

        Returns:
            Never.

        Raises:
            RuntimeError: Always.
        """
        message = "the pipeline had nothing to pull"
        raise RuntimeError(message)


class TestShutdownRacingTheModelLoad:
    """A worker thread cannot be cancelled, so its result has to be caught."""

    @pytest.mark.asyncio
    async def test_a_detector_that_arrives_after_the_close_is_still_closed(
        self,
    ) -> None:
        """Otherwise its inference session runs for the life of the process.

        The race is not only a shutdown: a fallback source closes the local
        detector when the groundstation comes back, precisely in order to give
        the robot its cores back — and a session leaked there holds exactly the
        core that close was for.
        """
        detector = FakeFaceDetector([_AT_640])
        loading = asyncio.Event()
        source = LocalPerception(
            FakeMedia(image=frame()),
            detector=lambda: detector,
            clock=ManualClock(),
            sleep=_immediately,
            offload=_waits_for(loading),
        )
        await source.start()
        await _settle()
        # The close and the load, racing. `aclose` waits for the build it
        # cannot cancel and then releases it; the release below is what lets
        # the "thread" finish.
        closing = asyncio.ensure_future(source.aclose())
        await _settle()
        assert detector.closed == 0
        loading.set()
        await closing
        assert detector.closed == 1

    @pytest.mark.asyncio
    async def test_a_restart_never_overlaps_a_build_it_does_not_own(
        self,
    ) -> None:
        """Because the close has finished with the previous one before it returns.

        This is the fallback source's ordinary cycle, not only a shutdown: the
        groundstation comes back, the local detector is closed to give the
        cores back, and the link drops again a moment later. A build left
        running across that boundary would be owned by nobody.
        """
        first = FakeFaceDetector([_AT_640])
        second = FakeFaceDetector([_AT_640])
        built: list[FakeFaceDetector] = []
        loading = asyncio.Event()
        loading.set()

        def _next() -> FakeFaceDetector:
            detector = first if not built else second
            built.append(detector)
            return detector

        source = LocalPerception(
            FakeMedia(image=frame()),
            detector=_next,
            clock=ManualClock(),
            sleep=_immediately,
            offload=_waits_for(loading),
        )
        await source.start()
        await _settle()
        await source.aclose()
        await source.start()
        await _settle()
        await source.aclose()
        assert built == [first, second]
        assert first.closed == 1
        assert second.closed == 1

    @pytest.mark.asyncio
    async def test_a_build_slower_than_the_bound_is_still_closed(self) -> None:
        """A shutdown must be prompt, and it must not leak an inference session.

        `aclose` waits for the build only so long — REQ-050 asks for a prompt
        exit — and then disowns it. The worker cannot be stopped, so the
        session arrives regardless, and it is closed on arrival rather than
        left holding an arena and a thread.
        """
        detector = FakeFaceDetector([_AT_640])
        loading = asyncio.Event()
        source = LocalPerception(
            FakeMedia(image=frame()),
            detector=lambda: detector,
            clock=ManualClock(),
            sleep=_immediately,
            offload=_waits_for(loading),
            # Already elapsed by the time the event loop looks at it, so
            # nothing here waits for a clock.
            close_build_seconds=0.0,
        )
        await source.start()
        await _settle()
        await source.aclose()
        assert detector.closed == 0
        loading.set()
        await _settle()
        assert detector.closed == 1

    @pytest.mark.asyncio
    async def test_a_detector_that_arrives_before_the_close_is_closed_once(
        self,
    ) -> None:
        """The ordinary path is unchanged, and nothing closes twice."""
        detector = FakeFaceDetector([_AT_640])
        source = _source(FakeMedia(image=frame()), detector, ManualClock())
        await source.start()
        await _settle()
        await source.aclose()
        assert detector.closed == 1


def _waits_for(event: asyncio.Event) -> Offload:
    """Build an offload whose first call blocks until a test releases it.

    Args:
        event: What releases it.

    Returns:
        The offload.
    """
    released = {"first": False}

    async def _offload[ResultT](function: Callable[[], ResultT]) -> ResultT:
        if not released["first"]:
            released["first"] = True
            await event.wait()
        return function()

    return _offload
