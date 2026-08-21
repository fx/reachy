"""Pixels in, normalised coordinates out, in the one place that does it.

Robot link REQ-021 fixes what a position in a result means: the origin is the
image centre, both axes run to the edges at plus or minus one, and the vertical
axis points up. Perception produces those numbers; the link contract owns their
meaning. Doing the conversion here rather than inside each detector is what keeps
a second detector from inventing a second convention — the sign of the vertical
axis in particular, which is the one that is wrong for a whole release before
anybody notices the head tilting the wrong way.
"""

from __future__ import annotations

from reachy_contracts import NormalisedPoint

__all__ = ["normalised_centre"]


def _clamp(value: float) -> float:
    """Hold a coordinate inside the interval the contract allows.

    A detector can place a box centre slightly outside the frame — a face at the
    edge, a box the regression pushed past it, or a candidate found in the
    padding added to reach the model's stride. That is an ordinary detection
    reported at the edge, not a fault, but `NormalisedCoordinate` rejects
    anything outside plus or minus one and the rejection would cost the whole
    frame its answer.

    Args:
        value: The normalised coordinate, possibly out of range.

    Returns:
        The value, held to the interval.
    """
    return max(-1.0, min(1.0, value))


#:= docs/specs/perception/index.md#req-034-face-detections-report-a-normalised-centre-and-a-confidence
#:% Each face detection MUST report the face's centre in normalised image
#:% coordinates together with a confidence value.
def normalised_centre(
    x: float,
    y: float,
    width: int,
    height: int,
) -> NormalisedPoint:
    """Express a pixel position as a resolution-independent one.

    Args:
        x: Horizontal pixel position, measured from the left edge.
        y: Vertical pixel position, measured from the top edge.
        width: The frame's width in pixels.
        height: The frame's height in pixels.

    Returns:
        The same position with the origin at the frame's centre, the axes
        running to plus or minus one, and the vertical axis pointing up — so a
        face in the upper left has a negative horizontal and a positive vertical
        component.
    """
    return NormalisedPoint(
        x=_clamp((x / width) * 2.0 - 1.0),
        y=_clamp(1.0 - (y / height) * 2.0),
    )
