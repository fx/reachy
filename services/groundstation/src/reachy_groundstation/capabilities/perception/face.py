"""Face detection: YuNet, at the frame's own size, reported as a centre.

The capability is thin on purpose. Everything that could be subtly wrong lives in
`yunet.py`, which is pure and is compared against the Reachy Mini SDK's own
decoder by a merge gate; what is left here is the lifecycle — load the pinned
model at warm-up, run one inference per frame off the event loop, and convert
pixels to the contract's coordinates.

Two things it deliberately does not do.

It does not load its model on the first frame. Groundstation REQ-026 makes
readiness mean warm-up finished, so the model is opened and one inference is paid
for in `warm_up`, before the service reports itself ready and before any session
exists to be slowed down by it.

It does not report landmarks. YuNet emits five of them and `FaceDetection`
carries a centre and a confidence, because that is what the robot's motion layer
consumes and the perception spec leaves the richer payload open. Widening the
message type belongs to the change that introduces something that reads it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from reachy_contracts import (
    FACE_CAPABILITY,
    Capability,
    FaceDetection,
    FaceDetections,
)
from reachy_groundstation.capabilities.base import CapabilityBase
from reachy_groundstation.capabilities.perception.geometry import normalised_centre
from reachy_groundstation.capabilities.perception.yunet import (
    Detection,
    decode_faces,
    pad_to_stride,
    to_blob,
)
from reachy_groundstation.capabilities.registry import CapabilityDisabledError, register
from reachy_groundstation.models import FACE_DETECTION_YUNET, ModelStore
from reachy_groundstation.runtime import ModelRuntime, RuntimeOptions

if TYPE_CHECKING:
    from reachy_contracts import WireModel
    from reachy_groundstation.config import Settings
    from reachy_groundstation.ports import CapabilityPort, DecodedFrame, ImageArray

__all__ = [
    "FACE_VERSION",
    "FaceCapability",
    "build_face_capability",
    "detect_faces",
]

# The revision of this capability's behaviour that negotiation agrees on. It
# changes when what a client receives changes, not when this file does.
FACE_VERSION: Final = 1

# What warm-up runs one inference at. The predecessor captured 640 by 480 and
# both are already multiples of the model's largest stride, so this is the shape
# the first real frame is most likely to arrive in. Warming up at a different
# shape still pays for the arena allocation and the thread pool, which is most
# of a cold session's cost; the input is dynamic, so nothing is pinned by it.
_WARM_UP_SHAPE: Final = (1, 3, 480, 640)


def detect_faces(
    runtime: ModelRuntime,
    image: ImageArray,
    score_threshold: float,
    nms_threshold: float,
) -> tuple[Detection, ...]:
    """Run one detection pass, blocking the calling thread.

    This is the whole pipeline in one place — pad, blob, infer, decode, suppress
    — and it is what the parity test drives, so the thing compared against the
    reference implementation is the thing the service runs rather than a
    reassembly of its parts.

    Args:
        runtime: The loaded model.
        image: The decoded frame, treated as read-only.
        score_threshold: The confidence a detection must reach.
        nms_threshold: How much two boxes may overlap before the lower-scoring
            one is discarded.

    Returns:
        One detection per face, in pixels, highest score first.
    """
    padded = pad_to_stride(image)
    outputs = runtime.run({runtime.input_name: to_blob(padded)})
    return decode_faces(outputs, int(padded.shape[1]), score_threshold, nms_threshold)


class FaceCapability(CapabilityBase):
    """Answers each frame with the faces in it, as centres and confidences."""

    def __init__(self, settings: Settings) -> None:
        """Record what to load and how, without loading anything.

        Construction happens while the composition root is being assembled, so
        it does no input or output at all: the registry builds every capability
        before warming any of them, and a constructor that opened a file would
        make "built" and "loaded" the same event.

        Args:
            settings: The settings in effect.
        """
        super().__init__(Capability(name=FACE_CAPABILITY, version=FACE_VERSION))
        self._store = ModelStore(settings.models_dir)
        self._options = RuntimeOptions.from_settings(settings)
        self._score_threshold = settings.face_score_threshold
        self._nms_threshold = settings.face_nms_threshold
        self._runtime: ModelRuntime | None = None

    #:= docs/specs/groundstation/index.md#req-023-model-files-are-present-in-the-image
    #:% The service MUST load every model from a file already present in its deployed
    #:% artifact, and MUST NOT fetch model weights over the network at run time.
    async def warm_up(self) -> None:
        """Open the pinned model and pay for the first inference.

        Raises:
            ModelStoreError: If the model file is absent from the artifact or
                does not hash to the digest the registry pins. The registry
                records the capability as unhealthy and the service carries on
                without it.
        """
        path = self._store.resolve(FACE_DETECTION_YUNET)
        runtime = ModelRuntime(path, self._options, FACE_DETECTION_YUNET.name)
        # The runtime exists from here, and `self._runtime` does not until the
        # last line — so a warm-up that raises or is cancelled would leave a
        # session and a worker thread that `aclose` has no way to reach. The
        # registry records the failure and carries on serving, which means the
        # leak would last for the life of the process.
        try:
            await runtime.warm_up(_WARM_UP_SHAPE)
        except BaseException:
            await runtime.aclose()
            raise
        self._runtime = runtime

    #:= docs/specs/perception/index.md#req-035-detection-output-is-independent-of-input-resolution
    #:% The same scene captured at different resolutions MUST produce detections whose
    #:% reported positions agree within a stated tolerance.
    async def process(self, frame: DecodedFrame) -> WireModel:
        """Answer one frame with every face in it.

        The frame is fed at its own dimensions, padded up to the model's stride,
        so the resolution it arrived at affects nothing but the pixel positions —
        which are divided back out here.

        Args:
            frame: The decoded frame, shared with every other agreed capability
                and treated as read-only.

        Returns:
            The faces found, possibly none, which is an ordinary successful
            answer rather than a failure.

        Raises:
            RuntimeError: If the frame arrives before warm-up loaded the model.
                The registry does not offer a capability that has not warmed up,
                so reaching this means the lifecycle was bypassed.
        """
        if self._runtime is None:
            message = "the face capability was asked for a frame before warming up"
            raise RuntimeError(message)
        padded = pad_to_stride(frame.image)
        outputs = await self._runtime.infer(
            {self._runtime.input_name: to_blob(padded)},
        )
        detections = decode_faces(
            outputs,
            int(padded.shape[1]),
            self._score_threshold,
            self._nms_threshold,
        )
        return FaceDetections(
            faces=tuple(
                self._as_face(detection, frame.width, frame.height)
                for detection in detections
            ),
        )

    @staticmethod
    def _as_face(detection: Detection, width: int, height: int) -> FaceDetection:
        """Render one detection in the contract's terms.

        Args:
            detection: The detection, in pixels.
            width: The frame's width, before padding.
            height: The frame's height, before padding.

        Returns:
            The wire form: a normalised centre and a confidence.
        """
        centre_x, centre_y = detection.centre
        return FaceDetection(
            centre=normalised_centre(centre_x, centre_y, width, height),
            # The confidence is a geometric mean of two clipped heads, so it is
            # already on the unit interval; the bound is restated rather than
            # assumed because floating-point rounding at exactly one is the
            # difference between a result and a validation error.
            confidence=min(1.0, max(0.0, detection.score)),
        )

    async def aclose(self) -> None:
        """Release the runtime and the thread it holds."""
        if self._runtime is not None:
            await self._runtime.aclose()
            self._runtime = None


#:= docs/specs/perception/index.md#req-038-a-capability-can-be-disabled-without-disabling-the-session
#:% Each detector MUST be independently switchable at run time, and disabling one
#:% MUST leave the others operating.
@register
def build_face_capability(settings: Settings) -> CapabilityPort:
    """Build the face capability, unless this deployment switched it off.

    Args:
        settings: The settings in effect.

    Returns:
        The capability.

    Raises:
        CapabilityDisabledError: If face detection is switched off. The registry
            records it as disabled rather than failed and offers it to nobody;
            every other capability is unaffected.
    """
    if not settings.face_enabled:
        raise CapabilityDisabledError(FACE_CAPABILITY)
    return FaceCapability(settings)
