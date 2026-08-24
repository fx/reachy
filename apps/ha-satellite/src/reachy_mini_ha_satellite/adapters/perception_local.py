"""Face detection on the robot's own cores, wrapping the SDK's own detector.

The detector this wraps runs **the same YuNet weights the groundstation runs**:
change 0005 pinned `pollen-robotics/face_detection_yunet_2026may` at revision
`2b8e922362946a0db67e861bae0f77826980effd`, MIT, and that is the file the SDK
downloads. That is what makes ha-satellite REQ-047 a real choice rather than a
trade of one accuracy for another — switching source changes latency and CPU
cost, and does not change what is detected. `SDK_MODEL_REVISION` below records
the claim, and a test in this repository reads the SDK's own constant back and
fails if the two ever diverge.

**The SDK is loaded by file path, inside a function, and that is not
stylistic.** `import reachy_mini.vision.face_detector` executes the
distribution's `__init__`, which transitively imports `reachy_mini.vision.
face_tracking`, which does `import gi` — so an ordinary import drags PyGObject
and the whole GStreamer stack into anything that touches this module, including
a continuous integration runner that has neither. The module actually wanted
imports only `math`, `dataclasses`, `numpy`, `onnxruntime`, `huggingface_hub`
and `numpy.typing`, and none of its siblings, so loading it on its own gets the
detector without the package around it. Change 0005 established the same bypass
for the groundstation's parity gate; this is the second and last place in the
repository that needs it.

The one awkward part is the confidence. The SDK's public `Face` record carries a
box and three landmarks and no score, and `FaceDetection` requires a confidence
— so the module's own suppression step is wrapped, permanently and on this
module object alone, and hands back the scores for exactly the faces it chose to
keep. Recomputing the score here instead would be this repository writing a
second detector and calling it the SDK's.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import importlib.util
import logging
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

from reachy_contracts import FaceDetection, NormalisedPoint
from reachy_mini_ha_satellite.adapters.daemon import in_thread
from reachy_mini_ha_satellite.ports import Detections, DetectionSource

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from types import ModuleType

    from reachy_mini_ha_satellite.adapters.daemon import (
        ImageArray,
        MediaInterface,
        Offload,
    )

__all__ = [
    "DEFAULT_DETECTION_INTERVAL",
    "DEFAULT_NMS_THRESHOLD",
    "DEFAULT_SCORE_THRESHOLD",
    "DEFAULT_STALENESS_SECONDS",
    "SDK_MODEL_REPO",
    "SDK_MODEL_REVISION",
    "FaceDetectorPort",
    "LocalPerception",
    "PixelFace",
    "SdkFaceDetector",
    "load_sdk_face_detector",
    "normalised_centre",
]

_LOGGER: Final = logging.getLogger(__name__)

# What the SDK pins its weights to, recorded here so that the "same weights as
# the groundstation" claim is checkable rather than asserted in prose. The
# groundstation's registry pins the identical repository and revision; a test
# reads both this constant and the SDK's own and fails when they part company.
SDK_MODEL_REPO: Final = "pollen-robotics/face_detection_yunet_2026may"
SDK_MODEL_REVISION: Final = "2b8e922362946a0db67e861bae0f77826980effd"

# The module the SDK keeps its detector in, relative to the package root, and
# the name it is registered under once loaded. The name is this repository's,
# not `reachy_mini.vision.face_detector`: registering it under the SDK's own
# name would make a later ordinary import of the SDK find this half-package
# instead of loading the real one.
_SDK_DETECTOR_PATH: Final = ("vision", "face_detector.py")
_SDK_DETECTOR_MODULE: Final = "reachy_mini_satellite_face_detector"

# The SDK's own defaults, restated so that the local source runs at the same
# sensitivity the groundstation's face capability does rather than at whatever
# the SDK's signature happens to default to next release.
DEFAULT_SCORE_THRESHOLD: Final = 0.6
DEFAULT_NMS_THRESHOLD: Final = 0.3

# How often the robot looks, in seconds. Slower than the ten per second the
# remote source runs at, and deliberately: this is the path taken when the
# groundstation is gone, on a machine that is simultaneously running motion
# control, audio and a wake-word model, and where the measured cost of local
# detection was a saturated processor. Five per second is enough to follow a
# person and leaves the rest of the application somewhere to run.
DEFAULT_DETECTION_INTERVAL: Final = 0.2

# How long a detection stays worth acting on. The same window the remote source
# uses, so that "stale" means one thing whichever source produced the answer.
DEFAULT_STALENESS_SECONDS: Final = 2.0

# What every answer from this module is labelled with.
# Bound once rather than spelled at each site, because the repository's leak
# scanner reads this member's dotted form as an mDNS hostname suffix — a shape
# its own docstring warns is what the per-line marker exists for — and one
# exempted line is better than several.
_SOURCE: Final = DetectionSource.LOCAL  # leak-scan:allow


@dataclass(frozen=True, slots=True)
class PixelFace:
    """One face as a detector found it, in the frame's own pixels.

    The seam between "a model ran" and "a detection was reported" is here, in
    pixels, rather than in normalised coordinates — so that the conversion to
    the coordinates the contract fixes is exercised by every test that drives a
    detector, including the ones that drive a fake.

    Attributes:
        x: Left edge of the bounding box, in pixels from the frame's left.
        y: Top edge of the bounding box, in pixels from the frame's top.
        width: The box's width in pixels.
        height: The box's height in pixels.
        confidence: How much the detector believed itself, on the unit
            interval.
    """

    x: float
    y: float
    width: float
    height: float
    confidence: float

    @property
    def centre(self) -> tuple[float, float]:
        """Where the middle of the box is.

        Returns:
            The horizontal and vertical centre, in pixels.
        """
        return (self.x + self.width / 2.0, self.y + self.height / 2.0)


class FaceDetectorPort(Protocol):
    """What `LocalPerception` requires of the thing that looks at a frame."""

    def detect(self, image: ImageArray) -> Sequence[PixelFace]:
        """Find every face in one frame.

        Args:
            image: The decoded frame, treated as read-only.

        Returns:
            The faces, in pixels, highest confidence first.
        """
        ...

    def close(self) -> None:
        """Release whatever the detector holds."""
        ...


#:= docs/specs/robot-link/index.md#req-021-detection-geometry-is-resolution-independent
#:% Positions in results MUST be expressed in normalised image coordinates rather
#:% than pixels.
def normalised_centre(
    x: float,
    y: float,
    width: int,
    height: int,
) -> NormalisedPoint:
    """Express a pixel position the way the contract requires it be expressed.

    The origin is the frame's centre, both axes run to plus or minus one at the
    edges, and the vertical axis points **up** — so a face in the upper left has
    a negative horizontal and a positive vertical component. The sign of that
    vertical axis is the one thing in this conversion that is silently wrong for
    a whole release when it is wrong, which is why it is stated rather than
    implied.

    The groundstation performs the identical conversion at its own boundary, and
    that is not a duplicated implementation of a shared thing: each side turns
    *its own* detector's pixels into the contract's coordinates, and the shared
    thing is `NormalisedPoint` and the rule it carries, both of which are
    declared once in `reachy_contracts` and imported here. Two detectors that
    both converted through one function would still be two detectors; what
    matters is that they agree on the convention, and the contract is where the
    convention lives.

    Args:
        x: Horizontal pixel position, measured from the left edge.
        y: Vertical pixel position, measured from the top edge.
        width: The frame's width in pixels.
        height: The frame's height in pixels.

    Returns:
        The same position in normalised image coordinates.
    """
    return NormalisedPoint(
        x=_clamp((x / width) * 2.0 - 1.0),
        y=_clamp(1.0 - (y / height) * 2.0),
    )


def _clamp(value: float) -> float:
    """Hold a coordinate inside the interval the contract allows.

    A detector can put a box centre just outside the frame — a face at the edge,
    a regression that pushed the box past it, a candidate found in the padding
    added to reach the model's stride. That is a detection at the edge, not a
    fault, and `NormalisedCoordinate` would reject it and cost the whole frame
    its answer.

    Args:
        value: The normalised coordinate, possibly out of range.

    Returns:
        The value, held to plus or minus one.
    """
    return max(-1.0, min(1.0, value))


def load_sdk_face_detector(model_path: Path) -> ModuleType:
    """Load the Reachy Mini SDK's YuNet detector without importing the SDK.

    **This cannot be a plain import.** See this module's docstring: importing
    the package executes an `import gi` three modules away, and the runner has
    no GStreamer. Loading the one module by file path gets the detector and
    leaves the package unexecuted.

    The SDK's downloader is redirected at the file already on disk. The
    detector fetches its weights from the Hugging Face hub on construction;
    this application must not reach the network for a model at run time, and
    the test suite runs with sockets disabled. Both resolve to the same bytes,
    which is what makes "the same weights as the groundstation" true rather
    than approximately true.

    Args:
        model_path: The weights to point the detector at.

    Returns:
        The loaded module, carrying `FaceDetector`, `Face` and `_nms`.

    Raises:
        RuntimeError: If the SDK is not installed, or if the module is not
            where it has always been — which would make what got loaded
            something other than what this docstring describes.
    """
    package = importlib.util.find_spec("reachy_mini")
    locations = None if package is None else package.submodule_search_locations
    if not locations:
        message = (
            "reachy-mini is not installed, so the local detector is "
            "unavailable; install this package's `local-detection` extra, or "
            "select the groundstation as the detection source"
        )
        raise RuntimeError(message)
    source = Path(next(iter(locations))).joinpath(*_SDK_DETECTOR_PATH)
    if not source.is_file():
        message = f"the SDK's face detector has moved: {source} does not exist"
        raise RuntimeError(message)

    spec = importlib.util.spec_from_file_location(_SDK_DETECTOR_MODULE, source)
    if spec is None or spec.loader is None:
        message = f"cannot load the SDK's face detector from {source}"
        raise RuntimeError(message)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_SDK_DETECTOR_MODULE] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # A module that failed to execute stays registered otherwise, and every
        # later load hands back that half-initialised object rather than trying
        # again — so the second failure looks nothing like the first.
        del sys.modules[_SDK_DETECTOR_MODULE]
        raise

    def _local_weights(*args: object, **kwargs: object) -> str:
        """Hand the detector the weights already on disk.

        Args:
            args: The repository and filename it asked for, unused.
            kwargs: The revision it asked for, unused.

        Returns:
            The local path.
        """
        del args, kwargs
        return str(model_path)

    # Third-party code with no stubs, reached through a deliberately untyped
    # view of it rather than through a suppression on each line.
    detector: Any = module
    detector.hf_hub_download = _local_weights
    return module


def _close_when_built(build: asyncio.Future[FaceDetectorPort]) -> None:
    """Release a detector nobody is waiting for any more.

    Attached when a shutdown gives up waiting for a model that is still
    loading. The worker thread cannot be cancelled, so the inference session
    comes into existence regardless — and by then the source that asked for it
    has gone, and any later `start` has begun a build of its own. Closing it
    here is what keeps a slow load from leaving an arena and a thread behind
    on a robot with four cores.

    Args:
        build: The finished construction.
    """
    if build.cancelled() or build.exception() is not None:
        return
    build.result().close()


class SdkFaceDetector:
    """The SDK's detector, with the confidences its public record leaves out.

    Constructed lazily by `LocalPerception`, which is what keeps the SDK out of
    the import graph of anything that does not use it.
    """

    def __init__(
        self,
        module: ModuleType,
        *,
        score_threshold: float = DEFAULT_SCORE_THRESHOLD,
        nms_threshold: float = DEFAULT_NMS_THRESHOLD,
    ) -> None:
        """Build the detector and wire the score recovery into it.

        The module's suppression step is wrapped permanently rather than for
        the duration of each call, and that is safe precisely because the
        module was loaded by file path: this object is the only reference to it
        in the process, so nothing else's detector is affected. The recorded
        scores are guarded by a lock because detection runs on a worker thread.

        Args:
            module: What `load_sdk_face_detector` returned.
            score_threshold: The confidence a detection must reach.
            nms_threshold: How much two boxes may overlap before the
                lower-scoring one is dropped.
        """
        detector: Any = module
        self._lock = threading.Lock()
        # A list of at most one entry rather than an optional value: the
        # suppression step writes it and `detect` reads it back, and an
        # attribute a type checker can narrow to `None` at the write would make
        # the read look like dead code.
        self._scores: list[tuple[Sequence[float], Sequence[int]]] = []
        # Reaching into the module's private suppression step, deliberately.
        # It is safe because the module was loaded by file path and this object
        # holds the only reference to it in the process, so nothing else's
        # detector is affected; and it is the only way to answer the
        # "confidence" half of a `FaceDetection` without recomputing the score,
        # which would make this repository the reference it is meant to match.
        original: Callable[..., list[int]] = detector._nms

        def _recording(
            boxes: Sequence[tuple[float, float, float, float]],
            scores: Sequence[float],
            iou_threshold: float,
        ) -> list[int]:
            """Let the detector suppress, and keep what it suppressed with.

            Args:
                boxes: The candidate boxes.
                scores: The candidate scores.
                iou_threshold: The overlap threshold.

            Returns:
                Whatever the detector kept, untouched.
            """
            kept = original(boxes, scores, iou_threshold)
            self._scores.append((scores, kept))
            return kept

        detector._nms = _recording
        self._detector: Any = detector.FaceDetector(
            score_threshold=score_threshold,
            nms_threshold=nms_threshold,
        )

    def detect(self, image: ImageArray) -> Sequence[PixelFace]:
        """Find every face in one frame.

        Args:
            image: The decoded frame, in the blue-green-red order the daemon
                produces and the detector expects.

        Returns:
            The faces, in pixels, in the order the detector reported them —
            which is highest confidence first, because that is the order its
            suppression step keeps. Empty once the detector has been closed:
            `close` and a detection racing it is an ordinary shutdown, because
            a detection already running on a worker thread is not cancelled by
            the task that started it being cancelled.
        """
        with self._lock:
            detector = self._detector
            if detector is None:
                return ()
            self._scores.clear()
            faces = detector.detect(image)
            recorded = self._scores[-1] if self._scores else None
        if not faces:
            return ()
        if recorded is None:
            # The detector returned faces without suppressing anything, which
            # its own code cannot do — every face it reports comes out of the
            # list its suppression step chose. Reaching here means the module
            # changed shape under us, and reporting a made-up confidence would
            # be worse than reporting nothing.
            _LOGGER.error(
                "the SDK detector reported %d face(s) without a suppression "
                "step; no confidence can be recovered, so the frame is dropped",
                len(faces),
            )
            return ()
        scores, kept = recorded
        if len(kept) != len(faces):
            # The detector reached its suppression step more than once for this
            # frame — per scale, or per class — so the recorded scores are the
            # last call's and do not line up with the faces it reported.
            # Dropped for the same reason as above: a confidence that belongs
            # to a different candidate is a made-up number on its way to a
            # motor, and pairing them anyway would raise once per frame.
            _LOGGER.error(
                "the SDK detector reported %d face(s) against %d suppressed "
                "candidate(s); no confidence can be recovered, so the frame is "
                "dropped",
                len(faces),
                len(kept),
            )
            return ()
        return tuple(
            PixelFace(
                x=float(face.bbox[0]),
                y=float(face.bbox[1]),
                width=float(face.bbox[2]),
                height=float(face.bbox[3]),
                confidence=min(1.0, max(0.0, float(scores[index]))),
            )
            for face, index in zip(faces, kept, strict=True)
        )

    def close(self) -> None:
        """Let go of the inference session.

        The SDK's detector holds an ONNX Runtime session, which owns an arena
        and a thread; dropping the reference is what releases them, and there
        is nothing else to close. Idempotent, and taken under the lock so that
        a detection already on a worker thread finishes against the session it
        started with rather than against a half-released one.
        """
        with self._lock:
            self._detector = None
            self._scores.clear()


#:= docs/specs/ha-satellite/index.md#req-047-detection-source-is-selectable
#:% The source of face detections MUST be selectable between the groundstation, the
#:% robot's own detector, and the groundstation with local fallback.
class LocalPerception:
    """Face detections from a model running on the robot itself."""

    def __init__(
        self,
        media: MediaInterface,
        *,
        detector: Callable[[], FaceDetectorPort],
        interval: float = DEFAULT_DETECTION_INTERVAL,
        staleness_seconds: float = DEFAULT_STALENESS_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        offload: Offload = in_thread,
    ) -> None:
        """Describe the source without loading a model.

        Args:
            media: The daemon's media interface, which the frames come off.
            detector: How to build the thing that looks at a frame. A factory
                rather than an instance, because building one loads a model
                and opens an inference session — which a source that is only
                ever a fallback should not pay for until it is needed.
            interval: How long to wait between looks, in seconds.
            staleness_seconds: How long a detection stays worth acting on.
            clock: The monotonic source freshness is measured against.
            sleep: How to wait between looks.
            offload: How to run the model and read the camera without stalling
                the event loop. Inference is a hundred milliseconds of
                arithmetic, so this one is not optional on the robot.

        Raises:
            ValueError: If the interval or the staleness window is not a
                positive number of seconds.
        """
        if interval <= 0:
            message = f"the detection interval must be positive, not {interval}"
            raise ValueError(message)
        if staleness_seconds <= 0:
            message = f"the staleness window must be positive, not {staleness_seconds}"
            raise ValueError(message)
        self._media = media
        self._build_detector = detector
        self._interval = interval
        self._staleness_seconds = staleness_seconds
        self._clock = clock
        self._sleep = sleep
        self._offload = offload

        self._faces: tuple[FaceDetection, ...] = ()
        self._generation: int | None = None
        self._sequence: int | None = None
        self._captured_at: float | None = None
        self._received_at: float | None = None
        self._detector: FaceDetectorPort | None = None
        self._loop: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Load the model, then begin looking. Idempotent.

        **The model is loaded here rather than inside the loop**, and that is
        what makes a failure to load visible. Spawning the loop and returning
        would report success on a detector that does not exist — and the one
        caller that matters is `FallbackPerception`, which marks the robot as
        having taken over the moment this returns. The robot would then have no
        detections from either source while reporting that fallback is engaged,
        and nothing would retry, because as far as the composite knew nothing
        had failed. A local source is the thing that exists to survive another
        failure, so failing silently is the one way it must not fail.

        Raising also leaves this object retryable: nothing is assigned until
        the build succeeds, so a later `start` builds again rather than
        short-circuiting on a loop that was never created.

        Raises:
            Exception: Whatever building the detector raised — a model file
                that is missing or corrupt, a runtime that will not open a
                session. It is re-raised rather than logged and swallowed so
                the caller can retry it or report it.
        """
        if self._loop is not None:
            return
        # Shielded, because cancelling this coroutine cannot stop the worker
        # thread: the inference session would come into existence with nothing
        # holding a reference to it, and hold an arena and a thread for the
        # life of the process on a robot with four cores.
        build = asyncio.ensure_future(self._offload(self._build_detector))
        try:
            detector = await asyncio.shield(build)
        except asyncio.CancelledError:
            build.add_done_callback(_close_when_built)
            raise
        self._generation = 0 if self._generation is None else self._generation + 1
        self._sequence = None
        self._captured_at = None
        self._received_at = None
        self._faces = ()
        self._detector = detector
        self._loop = asyncio.create_task(
            self._run(detector),
            name="local-detection",
        )

    def latest(self) -> Detections:
        """Say what the robot last saw, if it is still current.

        Returns:
            The faces from the most recent detection, or an empty, not-fresh
            answer once the staleness window has elapsed. A local detector goes
            stale when it stops keeping up rather than when a network does, and
            the behaviour that follows is the same either way.
        """
        if self._received_at is None:
            # Nothing has been looked at yet, so no field describes a
            # detection — `source` included. See the same branch in
            # `groundstation.py`: "the detector found nobody" and "the detector
            # has not run yet" are different facts, and `age_seconds` already
            # keeps them apart by staying `None` here.
            return Detections()
        age = self._clock() - self._received_at
        fresh = age < self._staleness_seconds
        return Detections(
            faces=self._faces if fresh else (),
            fresh=fresh,
            source=_SOURCE,
            age_seconds=age,
            generation=self._generation,
            sequence=self._sequence,
            captured_at=self._captured_at,
            received_at=self._received_at,
        )

    async def aclose(self) -> None:
        """Stop looking and release the inference session.

        There is no build to wait for here. `start` does not return until the
        model has loaded or has failed to, so by the time anything can call
        this there is either a detector to close or nothing at all — and a
        `start` cancelled mid-build disowns its own build on the way out.
        """
        loop, self._loop = self._loop, None
        if loop is not None:
            loop.cancel()
            # Every exception, not only the cancellation: a task that had
            # already failed is not cancelled by `cancel`, and awaiting it here
            # would re-raise its failure out of a shutdown path.
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await loop
        detector, self._detector = self._detector, None
        if detector is not None:
            detector.close()
        self._sequence = None
        self._captured_at = None
        self._received_at = None
        self._faces = ()

    async def _run(self, detector: FaceDetectorPort) -> None:
        """Look at a frame, then wait, for as long as the source is open.

        Args:
            detector: The loaded detector. Passed in rather than read off the
                instance, because `start` has already proved it exists — this
                loop never has to ask whether the model loaded.
        """
        while True:
            await self._sleep(self._interval)
            try:
                await self._look(detector)
            except Exception:
                # Every failure of one turn — the camera, the runtime, the
                # arithmetic. This loop is a task nobody awaits until shutdown,
                # so an exception escaping it would end detection silently and
                # then surface out of `aclose` rather than out of the frame
                # that caused it.
                _LOGGER.exception("the local detection loop failed a turn")

    async def _look(self, detector: FaceDetectorPort) -> None:
        """Take one frame, detect in it, and record what was found.

        Args:
            detector: The loaded detector.
        """
        captured_at = self._clock()
        frame = await self._offload(self._media.get_frame)
        if frame is None:
            return
        # `partial` rather than a lambda: the lambda would close over the
        # loop's `frame` and read whatever it held when the thread got round
        # to it, which is the next frame.
        found = await self._offload(functools.partial(detector.detect, frame))
        height, width = int(frame.shape[0]), int(frame.shape[1])
        faces = tuple(
            FaceDetection(
                centre=normalised_centre(*face.centre, width, height),
                confidence=face.confidence,
            )
            for face in found
        )
        received_at = self._clock()
        self._faces = faces
        self._sequence = 0 if self._sequence is None else self._sequence + 1
        self._captured_at = captured_at
        self._received_at = received_at
