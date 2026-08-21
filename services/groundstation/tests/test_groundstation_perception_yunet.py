"""The hand-written pre- and post-processing, exercised as arithmetic.

Everything here is a unit test in the strict sense: no file is opened, no model
is loaded, and the model's outputs are constructed rather than produced. That is
the point. The parity test beside this one compares this code against the SDK's
decoder over real weights and catches a disagreement; these tests say what each
step is supposed to do, so that when the parity test goes red there is something
that says which step moved.

The arithmetic being checked is the part that is silently wrong when it is
wrong. An anchor index is decoded against the width the model was *run* at, which
is the padded width — decode against the unpadded one and every detection slides
sideways by an amount that grows down the image, which looks like a slightly
inaccurate detector rather than like a bug.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import math

import numpy as np
import numpy.typing as npt
import pytest

from reachy_groundstation.capabilities.perception.yunet import (
    MAX_STRIDE,
    STRIDES,
    Detection,
    decode,
    decode_faces,
    pad_to_stride,
    suppress_overlaps,
    to_blob,
)
from reachy_groundstation.ports import ImageArray

# A frame small enough to reason about and not a multiple of the stride, so the
# padding path is the one taken.
_UNALIGNED = (100, 140)


def _image(height: int, width: int, fill: int = 90) -> ImageArray:
    """Build a plain frame.

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


def _heads(
    padded_height: int,
    padded_width: int,
    hits: dict[int, list[tuple[int, int, float, float, tuple[float, ...]]]],
) -> dict[str, npt.NDArray[np.float32]]:
    """Build the twelve outputs the model produces, with chosen anchors lit up.

    Args:
        padded_height: The height the model was run at.
        padded_width: The width the model was run at.
        hits: Per stride, the anchors to set: row, column, classification score,
            objectness score, and the four box regression values.

    Returns:
        The outputs, by name, exactly as the model names them.
    """
    outputs: dict[str, npt.NDArray[np.float32]] = {}
    for stride in STRIDES:
        rows = padded_height // stride
        columns = padded_width // stride
        anchors = rows * columns
        outputs[f"cls_{stride}"] = np.zeros((1, anchors, 1), dtype=np.float32)
        outputs[f"obj_{stride}"] = np.zeros((1, anchors, 1), dtype=np.float32)
        outputs[f"bbox_{stride}"] = np.zeros((1, anchors, 4), dtype=np.float32)
        outputs[f"kps_{stride}"] = np.zeros((1, anchors, 10), dtype=np.float32)
        for row, column, classification, objectness, box in hits.get(stride, []):
            anchor = row * columns + column
            outputs[f"cls_{stride}"][0, anchor, 0] = classification
            outputs[f"obj_{stride}"][0, anchor, 0] = objectness
            outputs[f"bbox_{stride}"][0, anchor] = box
    return outputs


def test_a_frame_already_aligned_is_handed_back_unchanged() -> None:
    """Padding a frame that needs none must not cost a copy of it."""
    image = _image(480, 640)
    assert pad_to_stride(image) is image


def test_a_frame_is_padded_up_and_never_down() -> None:
    """The model needs stride-aligned dimensions; the frame is grown to reach them."""
    padded = pad_to_stride(_image(*_UNALIGNED))
    assert padded.shape[0] == math.ceil(_UNALIGNED[0] / MAX_STRIDE) * MAX_STRIDE
    assert padded.shape[1] == math.ceil(_UNALIGNED[1] / MAX_STRIDE) * MAX_STRIDE
    assert padded.shape[0] >= _UNALIGNED[0]
    assert padded.shape[1] >= _UNALIGNED[1]


def test_padding_leaves_the_origin_where_it_was() -> None:
    """This is the whole reason there is no letterbox to reverse.

    Growing at the bottom and the right means every pixel keeps its coordinates,
    so a position the model reports is already a position in the frame. A frame
    centred on a canvas instead would need every coordinate corrected, and
    getting that correction wrong is the coordinate bug this design deletes.
    """
    image = _image(*_UNALIGNED)
    image[0, 0] = (1, 2, 3)
    image[_UNALIGNED[0] - 1, _UNALIGNED[1] - 1] = (4, 5, 6)
    padded = pad_to_stride(image)
    assert tuple(padded[0, 0]) == (1, 2, 3)
    assert tuple(padded[_UNALIGNED[0] - 1, _UNALIGNED[1] - 1]) == (4, 5, 6)
    # And the added region is black rather than an edge repeated into it, which
    # would invent structure for the detector to find.
    assert padded[_UNALIGNED[0], 0].sum() == 0


def test_padding_does_not_write_to_the_frame_it_was_given() -> None:
    """One decode is shared by every agreed capability; editing it corrupts the rest."""
    image = _image(*_UNALIGNED)
    before = image.copy()
    padded = pad_to_stride(image)
    padded[_UNALIGNED[0] :, :] = 255
    assert np.array_equal(image, before)


def test_the_blob_is_a_single_image_batch_of_channels_rows_columns() -> None:
    """The model takes NCHW float32; the decoder produces HWC uint8."""
    blob = to_blob(_image(64, 96))
    assert blob.shape == (1, 3, 64, 96)
    assert blob.dtype == np.float32


def test_the_blob_neither_normalises_nor_swaps_channels() -> None:
    """These weights were trained on raw BGR values, and expect exactly those.

    Dividing by 255 or swapping to RGB both leave a detector that still
    detects — worse, and only sometimes — which is why this is asserted rather
    than assumed.
    """
    image = _image(32, 32)
    image[0, 0] = (10, 20, 30)
    blob = to_blob(image)
    assert (blob[0, 0, 0, 0], blob[0, 1, 0, 0], blob[0, 2, 0, 0]) == (10.0, 20.0, 30.0)


def test_a_decoded_box_lands_where_its_anchor_says() -> None:
    """Cell offset plus regression, times the stride, is the whole geometry."""
    outputs = _heads(64, 96, {8: [(3, 5, 1.0, 1.0, (0.5, 0.25, 0.0, 0.0))]})
    (detection,) = decode(outputs, 96, score_threshold=0.5)
    # Centre: (column + offset) * stride, (row + offset) * stride. Size:
    # exp(0) * stride, so eight by eight around that centre.
    assert detection.centre == pytest.approx((44.0, 26.0))
    assert (detection.width, detection.height) == pytest.approx((8.0, 8.0))


def test_decoding_against_the_unpadded_width_moves_every_detection() -> None:
    """The argument exists because getting it wrong is invisible in the output.

    Both decodes below are of the same model output. One is told the width the
    model was actually run at and one is told the frame's own width, and the
    second lands somewhere else entirely — with no error, no warning, and a
    detection that looks perfectly plausible.
    """
    outputs = _heads(64, 96, {8: [(3, 5, 1.0, 1.0, (0.5, 0.5, 0.0, 0.0))]})
    (correct,) = decode(outputs, 96, score_threshold=0.5)
    (wrong,) = decode(outputs, 88, score_threshold=0.5)
    assert correct.centre != wrong.centre


def test_the_confidence_is_the_geometric_mean_of_the_two_heads() -> None:
    """Either head alone is a plausible-looking number that is not the score."""
    outputs = _heads(64, 96, {16: [(1, 1, 0.81, 0.49, (0.0, 0.0, 0.0, 0.0))]})
    (detection,) = decode(outputs, 96, score_threshold=0.5)
    assert detection.score == pytest.approx(math.sqrt(0.81 * 0.49), abs=1e-6)


def test_a_head_outside_the_unit_interval_is_clipped_rather_than_trusted() -> None:
    """A negative product would be the square root of a negative number.

    Clipping is what makes the confidence a confidence: a head above one is held
    to one rather than inflating the score, and a head below zero drops it to
    zero rather than producing a NaN that compares false against every threshold
    and disappears without saying so.
    """
    negative = _heads(64, 96, {32: [(0, 0, 1.4, -0.2, (0.0, 0.0, 0.0, 0.0))]})
    assert decode(negative, 96, score_threshold=1e-6) == ()

    above_one = _heads(64, 96, {32: [(0, 0, 1.4, 0.81, (0.0, 0.0, 0.0, 0.0))]})
    (detection,) = decode(above_one, 96, score_threshold=0.5)
    assert detection.score == pytest.approx(0.9, abs=1e-6)


def test_candidates_below_the_threshold_are_not_decoded() -> None:
    """Thresholding is configuration, and it happens before anything is built."""
    outputs = _heads(64, 96, {8: [(2, 2, 0.5, 0.5, (0.0, 0.0, 0.0, 0.0))]})
    assert decode(outputs, 96, score_threshold=0.6) == ()
    assert len(decode(outputs, 96, score_threshold=0.4)) == 1


def test_every_stride_is_decoded() -> None:
    """A face lights up whichever pyramid level matches its size."""
    outputs = _heads(
        64,
        96,
        {stride: [(1, 1, 1.0, 1.0, (0.0, 0.0, 0.0, 0.0))] for stride in STRIDES},
    )
    assert len(decode(outputs, 96, score_threshold=0.5)) == len(STRIDES)


def test_suppression_keeps_the_best_of_a_cluster() -> None:
    """One face lights up several anchors, and the report is of one face."""
    kept = suppress_overlaps(
        [
            Detection(x=10.0, y=10.0, width=40.0, height=40.0, score=0.7),
            Detection(x=12.0, y=11.0, width=40.0, height=40.0, score=0.9),
            Detection(x=11.0, y=12.0, width=40.0, height=40.0, score=0.8),
        ],
        iou_threshold=0.3,
    )
    assert [detection.score for detection in kept] == [0.9]


def test_suppression_keeps_two_faces_that_do_not_overlap() -> None:
    """Suppression is about duplicates, not about reporting one face per frame."""
    kept = suppress_overlaps(
        [
            Detection(x=0.0, y=0.0, width=20.0, height=20.0, score=0.9),
            Detection(x=100.0, y=100.0, width=20.0, height=20.0, score=0.7),
        ],
        iou_threshold=0.3,
    )
    assert len(kept) == 2
    assert [detection.score for detection in kept] == [0.9, 0.7]


def test_suppression_returns_them_highest_score_first() -> None:
    """The order is part of the contract the parity comparison relies on."""
    kept = suppress_overlaps(
        [
            Detection(x=0.0, y=0.0, width=10.0, height=10.0, score=0.5),
            Detection(x=50.0, y=50.0, width=10.0, height=10.0, score=0.95),
            Detection(x=100.0, y=100.0, width=10.0, height=10.0, score=0.7),
        ],
        iou_threshold=0.3,
    )
    assert [detection.score for detection in kept] == [0.95, 0.7, 0.5]


def test_suppression_of_nothing_is_nothing() -> None:
    """An empty frame is an ordinary frame, and it must not raise."""
    assert suppress_overlaps([], iou_threshold=0.3) == ()


def test_a_higher_overlap_threshold_keeps_more() -> None:
    """The threshold is configuration, so it has to actually do something."""
    crowded = [
        Detection(x=0.0, y=0.0, width=40.0, height=40.0, score=0.9),
        Detection(x=20.0, y=0.0, width=40.0, height=40.0, score=0.8),
    ]
    assert len(suppress_overlaps(crowded, iou_threshold=0.1)) == 1
    assert len(suppress_overlaps(crowded, iou_threshold=0.5)) == 2


def test_decoding_and_suppression_together_report_one_box_per_face() -> None:
    """The whole post-processing step, over anchors arranged as a real one is."""
    outputs = _heads(
        64,
        96,
        {
            8: [
                (3, 5, 1.0, 1.0, (0.5, 0.5, 1.6, 1.6)),
                (3, 6, 0.9, 0.9, (0.4, 0.5, 1.6, 1.6)),
                (4, 5, 0.8, 0.8, (0.5, 0.4, 1.6, 1.6)),
            ],
        },
    )
    assert len(decode(outputs, 96, score_threshold=0.6)) == 3
    assert len(decode_faces(outputs, 96, 0.6, 0.3)) == 1


def test_a_detection_reports_its_centre_from_its_corner_and_size() -> None:
    """The box is a corner and a size; what the robot consumes is the centre."""
    detection = Detection(x=10.0, y=20.0, width=30.0, height=40.0, score=0.5)
    assert detection.centre == (25.0, 40.0)
