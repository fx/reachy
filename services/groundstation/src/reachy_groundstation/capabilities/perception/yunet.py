"""Turning a camera frame into YuNet's input, and its heads back into faces.

This module is the highest-risk code in the service and it is deliberately the
plainest. It is hand-written pre- and post-processing around a model: when it is
wrong it is silently wrong — a box a few pixels out, a score read from the wrong
head — and what it is wrong about goes to a motor. So it holds no state, touches
no file, opens no session, and every function in it is a pure function of arrays
that a test can call directly. Perception REQ-036 then compares the whole of it
against the Reachy Mini SDK's own decoder over the same weights, and that
comparison is a merge gate.

**Frames are padded, never letterboxed.** YuNet accepts a dynamic input shape and
needs only that the height and width be multiples of its largest stride, so a
frame is padded up at the bottom and right and fed at its own size. The origin is
therefore unchanged and every coordinate the model reports is already a
coordinate in the original frame — which deletes the scale-and-offset reversal a
letterbox needs, and with it the whole class of coordinate bugs that live in
getting that reversal wrong.

**The confidence is the geometric mean of two heads.** YuNet emits a
classification score and an objectness score per anchor, and the confidence it is
scored on is the square root of their product. Reading either one alone gives a
number that looks like a confidence, is between zero and one, and is not the one
the thresholds were chosen against.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    import numpy.typing as npt

    from reachy_groundstation.ports import ImageArray

__all__ = [
    "MAX_STRIDE",
    "STRIDES",
    "Detection",
    "decode",
    "decode_faces",
    "pad_to_stride",
    "suppress_overlaps",
    "to_blob",
]

# The model's feature-pyramid strides, and the largest of them. Both are
# properties of these weights: the heads are named after the strides, and the
# input dimensions have to be multiples of the largest.
STRIDES: Final[tuple[int, ...]] = (8, 16, 32)
MAX_STRIDE: Final = 32


@dataclass(frozen=True, slots=True)
class Detection:
    """One face, in the pixel coordinates of the frame as it was captured.

    Pixels rather than normalised coordinates, because this is where the parity
    comparison happens and the reference implementation speaks pixels. The
    capability normalises on the way out.

    Attributes:
        x: Left edge of the bounding box.
        y: Top edge of the bounding box.
        width: Box width.
        height: Box height.
        score: The detector's confidence, on the unit interval.
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


def pad_to_stride(image: ImageArray, stride: int = MAX_STRIDE) -> ImageArray:
    """Extend a frame to the next multiple of the model's stride.

    The frame is extended at the bottom and the right, so the origin does not
    move and no coordinate has to be corrected afterwards. The input is never
    written to: capabilities share one decoded frame, and a capability that
    edited it would corrupt the next one's view of it.

    Args:
        image: The decoded frame, height by width by three.
        stride: The alignment required, which is the model's largest stride.

    Returns:
        The frame itself when it is already aligned, and a padded copy
        otherwise.
    """
    height, width = image.shape[0], image.shape[1]
    padded_height = math.ceil(height / stride) * stride
    padded_width = math.ceil(width / stride) * stride
    if (padded_height, padded_width) == (height, width):
        return image
    padded: ImageArray = np.zeros(
        (padded_height, padded_width, image.shape[2]),
        dtype=image.dtype,
    )
    padded[:height, :width] = image
    return padded


def to_blob(image: ImageArray) -> npt.NDArray[np.float32]:
    """Render a padded frame as the tensor the model takes.

    No normalisation and no channel swap: these weights were trained on raw BGR
    values in the zero-to-255 range, which is what the decoder hands over. What
    changes is the layout — a single-image batch of channels, rows, columns.

    Args:
        image: The padded frame, height by width by three.

    Returns:
        A float32 tensor shaped one by three by height by width.
    """
    return np.ascontiguousarray(
        image.astype(np.float32).transpose(2, 0, 1)[np.newaxis],
    )


def _scores(
    outputs: Mapping[str, npt.NDArray[np.float32]],
    stride: int,
) -> npt.NDArray[np.float32]:
    """Combine one stride's two score heads into the confidence to threshold on.

    Args:
        outputs: The model's outputs by name.
        stride: Which stride's heads to read.

    Returns:
        One confidence per anchor, on the unit interval.
    """
    classification = np.clip(outputs[f"cls_{stride}"][0, :, 0], 0.0, 1.0)
    objectness = np.clip(outputs[f"obj_{stride}"][0, :, 0], 0.0, 1.0)
    combined: npt.NDArray[np.float32] = np.sqrt(classification * objectness)
    return combined


def decode(
    outputs: Mapping[str, npt.NDArray[np.float32]],
    padded_width: int,
    score_threshold: float,
) -> tuple[Detection, ...]:
    """Turn the model's heads into candidate boxes above a threshold.

    Every anchor is a cell of a feature map laid out row by row, so its position
    in the flat head is its position in that map: the column is the index modulo
    the map's width and the row is the index divided by it. The box regression is
    an offset from that cell in cell units and a log-scaled size, which is why
    the centre is added and the size exponentiated before both are multiplied
    back up by the stride.

    Args:
        outputs: The model's outputs by name.
        padded_width: The width the model was actually run at, which is what the
            feature maps were laid out against. Passing the unpadded width here
            shifts every detection by a growing amount down the image, which is
            the mistake this argument exists to make explicit.
        score_threshold: The confidence a candidate must reach to be kept.

    Returns:
        The candidates, in stride order and unsuppressed.
    """
    detections: list[Detection] = []
    for stride in STRIDES:
        scores = _scores(outputs, stride)
        kept = np.nonzero(scores >= score_threshold)[0]
        if kept.size == 0:
            continue
        boxes = outputs[f"bbox_{stride}"][0][kept]
        columns = padded_width // stride
        column = (kept % columns).astype(np.float32)
        row = (kept // columns).astype(np.float32)
        centre_x = (column + boxes[:, 0]) * stride
        centre_y = (row + boxes[:, 1]) * stride
        width = np.exp(boxes[:, 2]) * stride
        height = np.exp(boxes[:, 3]) * stride
        detections.extend(
            Detection(
                x=float(centre_x[index] - width[index] / 2.0),
                y=float(centre_y[index] - height[index] / 2.0),
                width=float(width[index]),
                height=float(height[index]),
                score=float(scores[anchor]),
            )
            for index, anchor in enumerate(kept)
        )
    return tuple(detections)


def suppress_overlaps(
    detections: Sequence[Detection],
    iou_threshold: float,
) -> tuple[Detection, ...]:
    """Keep the best of each cluster of boxes covering the same face.

    Greedy suppression by intersection over union: take the highest-scoring box,
    discard everything overlapping it by more than the threshold, repeat. One
    face lights up several anchors and often more than one stride, so without
    this a single face is reported five or six times.

    Args:
        detections: The candidates, in any order.
        iou_threshold: How much overlap is too much, as intersection over union.

    Returns:
        The survivors, highest score first.
    """
    if not detections:
        return ()
    left = np.array([d.x for d in detections], dtype=np.float32)
    top = np.array([d.y for d in detections], dtype=np.float32)
    widths = np.array([d.width for d in detections], dtype=np.float32)
    heights = np.array([d.height for d in detections], dtype=np.float32)
    right = left + widths
    bottom = top + heights
    areas = widths * heights
    scores = np.array([d.score for d in detections], dtype=np.float32)

    # Ascending sort reversed, which is descending by score with ties broken by
    # descending index. The tie-breaking matters only when two boxes score
    # bit-for-bit identically, and it is written this way so that when they do,
    # this and the reference implementation the parity test compares against
    # keep the same one.
    order = scores.argsort()[::-1]
    kept: list[int] = []
    while order.size:
        best = int(order[0])
        kept.append(best)
        if order.size == 1:
            break
        rest = order[1:]
        overlap_width = np.clip(
            np.minimum(right[best], right[rest]) - np.maximum(left[best], left[rest]),
            0.0,
            None,
        )
        overlap_height = np.clip(
            np.minimum(bottom[best], bottom[rest]) - np.maximum(top[best], top[rest]),
            0.0,
            None,
        )
        intersection = overlap_width * overlap_height
        union = areas[best] + areas[rest] - intersection
        order = rest[intersection / union <= iou_threshold]
    return tuple(detections[index] for index in kept)


#:= docs/specs/perception/index.md#req-036-post-processing-is-verified-against-a-reference-implementation
#:% The hand-written pre- and post-processing MUST be verified against an
#:% independent reference implementation on a fixture set, asserting agreement on
#:% detection count, position, and confidence within stated tolerances.
def decode_faces(
    outputs: Mapping[str, npt.NDArray[np.float32]],
    padded_width: int,
    score_threshold: float,
    nms_threshold: float,
) -> tuple[Detection, ...]:
    """Decode the model's heads and reduce them to one box per face.

    Args:
        outputs: The model's outputs by name.
        padded_width: The width the model was run at.
        score_threshold: The confidence a candidate must reach.
        nms_threshold: How much two boxes may overlap before the lower-scoring
            one is discarded.

    Returns:
        One detection per face, highest score first.
    """
    return suppress_overlaps(
        decode(outputs, padded_width, score_threshold),
        nms_threshold,
    )
