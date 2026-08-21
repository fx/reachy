"""Turning a frame's compressed bytes into the array capabilities read.

Decode happens once per frame and the resulting array is shared by every agreed
capability. The measurement behind that: decode is 2 ms against a 39 ms face
pass, which is negligible once and is not negligible multiplied by a capability
count this service exists to grow.

Capabilities receive the decoded frame and never the compressed bytes, so this is
the only place in the service that knows the frames are JPEG at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import cv2
import numpy as np

if TYPE_CHECKING:
    from reachy_groundstation.ports import ImageArray

__all__ = ["DecodeError", "decode_jpeg"]


class DecodeError(ValueError):
    """The payload was not an image this service can decode."""


def decode_jpeg(payload: bytes) -> ImageArray:
    """Decode one frame's compressed bytes.

    Args:
        payload: The frame exactly as the capture hardware produced it.

    Returns:
        The pixels, as height by width by three colour channels.

    Raises:
        DecodeError: If the bytes are not a decodable image.
    """
    if not payload:
        message = "an empty payload is not a decodable image"
        raise DecodeError(message)
    buffer = np.frombuffer(payload, dtype=np.uint8)
    try:
        image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    except cv2.error as error:
        # OpenCV signals some malformed inputs by raising rather than by
        # returning nothing. Both are the same event here, and neither may
        # escape: an exception this function did not declare would unwind the
        # session's whole pipeline over one bad frame.
        message = f"payload of {len(payload)} bytes is not a decodable image"
        raise DecodeError(message) from error
    if image is None:
        message = f"payload of {len(payload)} bytes is not a decodable image"
        raise DecodeError(message)
    # `IMREAD_COLOR` always yields an 8-bit three-channel array; OpenCV's stubs
    # describe the union every decode mode can return, which is wider than the
    # one mode this service uses.
    return cast("ImageArray", image)
