"""Shared machinery for the perception tests: fixtures, weights, the reference.

Three things live here, and each of them is here because more than one test file
needs it and none of them should have its own copy.

**The fixture images.** Committed, drawn by
`scripts/generate_perception_fixtures.py`, and read back through the service's
own JPEG decoder so that what a test hands a capability is the same kind of array
a session would.

**The weights.** Models are never committed — they are fetched and hash-verified
when the artifact is built — so the tests that run real inference need a
directory somebody has already filled. `require_model` finds it, and either skips
or fails depending on whether this run is one where a skip would be a lie. See
its docstring: on a runner, a skipped parity test is worse than no parity test.

**The reference implementation, loaded the awkward way.** See
`load_reference_detector`. It is loaded by file path rather than imported, and
the reason is written out there because the next person to read it will otherwise
"fix" it into a plain import that cannot work.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

import cv2
import numpy as np
import pytest
from groundstation_support import make_header

from reachy_contracts import GestureDetection, GestureDetections
from reachy_groundstation.capabilities.perception.gesture import HandRegion
from reachy_groundstation.models import Model, ModelStore
from reachy_groundstation.pipeline.decode import decode_jpeg
from reachy_groundstation.ports import DecodedFrame

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence
    from types import ModuleType

    from reachy_groundstation.capabilities.perception.gesture import GestureCapability
    from reachy_groundstation.ports import ImageArray

__all__ = [
    "FACE_FIXTURES",
    "FIXTURES",
    "NEGATIVE_FIXTURES",
    "NegativesReport",
    "ReferenceDetector",
    "ReferenceFace",
    "ScriptedClassifier",
    "ScriptedHands",
    "evaluate_negatives",
    "fixture_frame",
    "fixture_image",
    "load_reference_detector",
    "model_directory",
    "reference_faces",
    "require_model",
    "scaled",
    "solid_image",
]

# Where the committed fixture images live: `tests/fixtures/perception`, reached
# from `tests/support/`.
FIXTURES: Final = Path(__file__).parent.parent / "fixtures" / "perception"

# The reference implementation's own defaults, restated so that the code under
# test is run at the same thresholds rather than at whatever its settings hold.
_REFERENCE_SCORE_THRESHOLD: Final = 0.6
_REFERENCE_NMS_THRESHOLD: Final = 0.3

# The repository root, which is where `just models` writes by default.
_REPO_ROOT: Final = Path(__file__).resolve().parents[4]

# Where the weights are, unless an operator said otherwise. Untracked, and
# gitignored, because weights are never committed.
_DEFAULT_MODELS_DIR: Final = _REPO_ROOT / ".models"

# The environment variable the service itself reads for its model directory. The
# tests honour it so that a contributor who keeps weights elsewhere does not have
# to keep two answers to the same question.
_MODELS_DIR_VARIABLE: Final = "REACHY_GROUNDSTATION_MODELS_DIR"

# Set on a runner. It turns "no weights, so skip" into "no weights, so fail":
# perception REQ-036 makes the parity comparison a merge gate, and a gate that
# quietly skips itself when its inputs are missing is not a gate. Locally it is
# unset, and a contributor who has not run `just models` gets a skip that says
# what to run.
_REQUIRE_MODELS_VARIABLE: Final = "REACHY_REQUIRE_MODELS"

FACE_FIXTURES: Final[tuple[str, ...]] = (
    "face_single.jpg",
    "face_upper_left.jpg",
    "face_pair.jpg",
    "face_unaligned.jpg",
    "scene_full.jpg",
    "scene_half.jpg",
)

#:= docs/specs/perception/index.md#req-037-gesture-accuracy-is-measured-against-a-negatives-fixture-set
#:% The gesture capability MUST be evaluated against a fixture set containing scenes
#:% with no hands present, and its false-positive rate on that set MUST be reported
#:% by the test suite.
NEGATIVE_FIXTURES: Final[tuple[str, ...]] = (
    "negative_wall.jpg",
    "negative_clutter.jpg",
    "negative_blinds.jpg",
    "negative_static.jpg",
    "negative_shelves.jpg",
    "negative_foliage.jpg",
)


def fixture_image(name: str) -> ImageArray:
    """Read one committed fixture through the service's own decoder.

    Args:
        name: The file's name within the fixture directory.

    Returns:
        The decoded frame, as a session would have decoded it.
    """
    return decode_jpeg((FIXTURES / name).read_bytes())


def fixture_frame(name: str, sequence: int = 0) -> DecodedFrame:
    """Wrap a fixture as the frame a capability is handed.

    Args:
        name: The file's name within the fixture directory.
        sequence: The frame number to give it, which is what the gesture
            capability's sampling interval is measured against.

    Returns:
        The frame.
    """
    return DecodedFrame(header=make_header(sequence), image=fixture_image(name))


def model_directory() -> Path:
    """Say where the weights are expected to be.

    Returns:
        The directory named by the service's own model-directory variable when
        it is set, and the repository's `.models` otherwise — which is where
        `just models` writes.
    """
    configured = os.environ.get(_MODELS_DIR_VARIABLE)
    return Path(configured) if configured else _DEFAULT_MODELS_DIR


def require_model(model: Model) -> Path:
    """Find a verified model file, or decide what its absence means.

    Args:
        model: The registered model the test needs.

    Returns:
        The path to the file, digest already checked.

    Raises:
        pytest.fail.Exception: On a runner, where the weights are fetched before
            the suite runs and their absence means the fetch step is broken
            rather than that the test is inapplicable.
        pytest.skip.Exception: Locally, where the message says what to run.
    """
    store = ModelStore(model_directory())
    path = store.path_for(model)
    if not path.is_file():
        message = (
            f"{model.name} is not in {model_directory()}. Models are never "
            f"committed; run `just models` to fetch and verify them."
        )
        if os.environ.get(_REQUIRE_MODELS_VARIABLE):
            pytest.fail(message)
        pytest.skip(message)
    return store.resolve(model)


# --- The reference implementation -------------------------------------------


class ReferenceDetector(Protocol):
    """What `load_reference_detector` hands back, in the terms tests use it in."""

    def detect(self, frame_bgr: ImageArray) -> Sequence[Any]:
        """Detect every face in a BGR frame.

        Args:
            frame_bgr: The frame.

        Returns:
            The reference implementation's own face records.
        """
        ...


@dataclass(frozen=True, slots=True)
class ReferenceFace:
    """One face as the reference implementation reported it.

    The reference's own record carries a box and three landmarks and no score,
    so the score is recovered separately — see `reference_faces`.

    Attributes:
        x: Left edge of the bounding box, in pixels.
        y: Top edge of the bounding box, in pixels.
        width: Box width in pixels.
        height: Box height in pixels.
        score: The confidence the reference computed for it.
    """

    x: float
    y: float
    width: float
    height: float
    score: float

    @property
    def centre(self) -> tuple[float, float]:
        """The box centre.

        Returns:
            The horizontal and vertical centre, in pixels.
        """
        return (self.x + self.width / 2.0, self.y + self.height / 2.0)


def load_reference_detector(model_path: Path) -> ModuleType:
    """Load the Reachy Mini SDK's YuNet decoder without importing the SDK.

    **This is deliberately not a plain import, and it cannot be one.** The
    distribution's `reachy_mini/__init__.py` transitively imports
    `reachy_mini.vision.face_tracking`, which does `import gi` — so
    `import reachy_mini.vision.face_detector` pulls in PyGObject and the whole
    GStreamer stack, none of which a test runner has or needs. The module this
    repository actually wants imports only `math`, `dataclasses`, `numpy`,
    `onnxruntime`, `huggingface_hub` and `numpy.typing`, and none of its
    siblings, so loading it by file path gets the reference implementation
    without the package around it. The root `pyproject.toml` matches this at the
    resolver level: `[[tool.uv.dependency-metadata]]` declares the distribution
    as needing those three third-party packages and nothing else.

    The downloader is redirected too. The SDK fetches its weights from the Hugging
    Face hub at run time; this service is forbidden from doing that at all
    (groundstation REQ-023), and the test suite runs with sockets disabled. Both
    resolve to the same file, which is the property that makes the comparison
    meaningful: the reference and the implementation run identical bytes.

    Args:
        model_path: The already-verified local weights to point the SDK at.

    Returns:
        The loaded module, with `FaceDetector`, `Face` and `_nms` on it.

    Raises:
        RuntimeError: If the distribution is installed but the module is not
            where it has always been, which would make the reference something
            other than what this docstring describes.
    """
    package = importlib.util.find_spec("reachy_mini")
    locations = None if package is None else package.submodule_search_locations
    if not locations:
        message = "reachy-mini is not installed; the parity reference is missing"
        raise RuntimeError(message)
    source = Path(next(iter(locations))) / "vision" / "face_detector.py"
    if not source.is_file():
        message = f"the parity reference has moved: {source} does not exist"
        raise RuntimeError(message)

    name = "reachy_mini_sdk_face_detector"
    spec = importlib.util.spec_from_file_location(name, source)
    if spec is None or spec.loader is None:
        message = f"cannot load the parity reference from {source}"
        raise RuntimeError(message)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # A module that failed to execute is left registered otherwise, and
        # every later load hands back that half-initialised object instead of
        # trying again — so the second failure looks nothing like the first.
        del sys.modules[name]
        raise

    def _local_weights(*args: object, **kwargs: object) -> str:
        """Hand the SDK the weights already on disk.

        Args:
            args: The repository and filename it asked for, unused.
            kwargs: The revision it asked for, unused.

        Returns:
            The local path.
        """
        del args, kwargs
        return str(model_path)

    # The loaded module is third-party code with no stubs, so its attributes
    # are reached through a deliberately untyped view of it rather than through
    # a suppression on each line.
    reference: Any = module
    reference.hf_hub_download = _local_weights
    return module


def reference_faces(module: ModuleType, image: ImageArray) -> tuple[ReferenceFace, ...]:
    """Run the reference implementation and recover its scores as well as its boxes.

    The reference's public `Face` record carries a box and three landmarks and no
    confidence, so a straight call cannot answer the "agreement on confidence"
    half of perception REQ-036. Rather than recomputing the score — which would
    be this repository writing the reference it is supposed to be measured
    against — the reference's own suppression step is wrapped for the duration of
    the call. It is handed the boxes and the scores and returns the indices it
    kept, in the order it kept them, which is exactly the order of the faces it
    returns; so the scores that come back out are the reference's own numbers for
    the faces the reference itself chose.

    Args:
        module: The loaded reference module.
        image: The frame to detect in.

    Returns:
        The reference's faces, with its scores, in the order it reported them.
    """
    captured: list[tuple[Sequence[float], Sequence[int]]] = []
    reference: Any = module
    original: Callable[..., list[int]] = reference._nms

    def _recording(
        boxes: Sequence[tuple[float, float, float, float]],
        scores: Sequence[float],
        iou_threshold: float,
    ) -> list[int]:
        """Record what the reference suppressed with, and let it get on with it.

        Args:
            boxes: The candidate boxes.
            scores: The candidate scores.
            iou_threshold: The overlap threshold.

        Returns:
            Whatever the reference kept.
        """
        kept = original(boxes, scores, iou_threshold)
        captured.append((scores, kept))
        return kept

    reference._nms = _recording
    try:
        detector: ReferenceDetector = reference.FaceDetector(
            score_threshold=_REFERENCE_SCORE_THRESHOLD,
            nms_threshold=_REFERENCE_NMS_THRESHOLD,
        )
        faces = detector.detect(image)
    finally:
        reference._nms = original

    if not captured:
        # The reference returned without suppressing anything, which is what a
        # frame carrying no candidate above the score threshold looks like: its
        # decoding loop skips every stride and never reaches suppression. That
        # is zero faces reported, not a failure to report them.
        assert not faces
        return ()

    scores, kept = captured[-1]
    return tuple(
        ReferenceFace(
            x=float(face.bbox[0]),
            y=float(face.bbox[1]),
            width=float(face.bbox[2]),
            height=float(face.bbox[3]),
            score=float(scores[index]),
        )
        for face, index in zip(faces, kept, strict=True)
    )


# --- The gesture evaluation --------------------------------------------------


@dataclass(frozen=True, slots=True)
class NegativesReport:
    """What the negatives evaluation measured.

    Attributes:
        scenes: How many empty scenes were shown.
        false_positives: How many of them produced at least one gesture above
            the configured threshold.
        detections: How many gestures were reported in total, which is a
            different number when one scene produces several.
        wired: Whether the capability had both stages behind it. A rate measured
            with no model wired is a fact about this build rather than about any
            candidate model, and reporting the two indistinguishably is how a
            vacuous zero gets quoted as a result.
    """

    scenes: int
    false_positives: int
    detections: int
    wired: bool

    @property
    def rate(self) -> float:
        """The proportion of empty scenes that produced a gesture.

        Returns:
            The false-positive rate, or zero when nothing was shown.
        """
        return self.false_positives / self.scenes if self.scenes else 0.0

    def render(self) -> str:
        """Say what was measured, in one line fit to print.

        Returns:
            The report.
        """
        wiring = (
            "with both stages wired"
            if self.wired
            else "with NO gesture model wired, so this number describes this "
            "build and not any candidate model"
        )
        return (
            f"gesture false-positive rate: {self.rate:.3f} "
            f"({self.false_positives}/{self.scenes} empty scenes, "
            f"{self.detections} detections total, {wiring})"
        )


async def evaluate_negatives(
    capability: GestureCapability,
    fixtures: Iterable[str] = NEGATIVE_FIXTURES,
) -> NegativesReport:
    """Show a gesture capability scenes with no hands and count what it claims.

    Every scene is shown on a frame the capability samples, so what is measured
    is the classifier rather than the sampling interval: a rate that fell because
    three frames in four were skipped would say nothing about the model.

    Args:
        capability: The capability to measure. Built with whatever stages and
            threshold are being evaluated.
        fixtures: The empty scenes to show.

    Returns:
        What it claimed to see.
    """
    scenes = 0
    false_positives = 0
    detections = 0
    for name in fixtures:
        # Frame zero falls on every sampling interval, whatever it is set to, so
        # every scene below is one the capability actually classifies.
        payload = await capability.process(fixture_frame(name, sequence=0))
        assert isinstance(payload, GestureDetections)
        found = payload.gestures
        scenes += 1
        detections += len(found)
        if found:
            false_positives += 1
    return NegativesReport(
        scenes=scenes,
        false_positives=false_positives,
        detections=detections,
        wired=capability.wired,
    )


class ScriptedHands:
    """A hand detector that reports whatever a test told it to.

    It exists so the two-stage path is exercised end to end without a model. A
    stage that returns a fixed answer proves the plumbing; it proves nothing
    about a model, and the evaluation says which of the two it measured.
    """

    def __init__(self, *regions: HandRegion) -> None:
        """Create a detector that always finds these regions.

        Args:
            regions: What to report for every frame.
        """
        self.regions = regions
        self.seen = 0
        self.closed = False

    async def detect(self, image: ImageArray) -> tuple[HandRegion, ...]:
        """Report the scripted regions.

        Args:
            image: The frame, unused beyond counting the call.

        Returns:
            The regions this detector was built with.
        """
        del image
        self.seen += 1
        return self.regions

    async def aclose(self) -> None:
        """Record that the stage was closed."""
        self.closed = True


class ScriptedClassifier:
    """A classifier that answers with whatever a test told it to."""

    def __init__(self, gesture: GestureDetection | None) -> None:
        """Create a classifier with one fixed answer.

        Args:
            gesture: What to report for every crop, or `None` to recognise
                nothing.
        """
        self.gesture = gesture
        self.crops: list[tuple[int, int]] = []
        self.closed = False

    async def classify(self, image: ImageArray) -> GestureDetection | None:
        """Answer with the scripted gesture.

        Args:
            image: The crop, whose shape is recorded so a test can check that
                the region it asked for is the region that arrived.

        Returns:
            The scripted answer.
        """
        self.crops.append((int(image.shape[0]), int(image.shape[1])))
        return self.gesture

    async def aclose(self) -> None:
        """Record that the stage was closed."""
        self.closed = True


def solid_image(height: int, width: int, fill: int = 128) -> ImageArray:
    """Build a plain frame of a given size.

    Args:
        height: How many rows.
        width: How many columns.
        fill: The grey level.

    Returns:
        The frame.
    """
    image: ImageArray = np.zeros((height, width, 3), dtype=np.uint8)
    image[:] = fill
    return image


def scaled(image: ImageArray, factor: float) -> ImageArray:
    """Resample a frame, for the tests that need one size from another.

    Args:
        image: The frame to resample.
        factor: What to multiply both dimensions by.

    Returns:
        The resampled frame.
    """
    # OpenCV's stubs declare `resize` as returning an array of unspecified
    # integer or floating dtype; the 8-bit result is restated rather than
    # silenced.
    return cast(
        "ImageArray",
        cv2.resize(
            image,
            (int(image.shape[1] * factor), int(image.shape[0] * factor)),
            interpolation=cv2.INTER_AREA,
        ),
    )
