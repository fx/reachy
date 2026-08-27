"""Turning a frame's compressed bytes into the array capabilities read.

Decode happens once per frame and the resulting array is shared by every agreed
capability. The measurement behind that: decode is 2 ms against a 39 ms face
pass, which is negligible once and is not negligible multiplied by a capability
count this service exists to grow.

Capabilities receive the decoded frame and never the compressed bytes, so this is
the only place in the service that knows what format the frames are in — and
`is_jpeg` is here for that reason rather than beside the code that needs it.
Decoding successfully is not the same question: `cv2.imdecode` reads PNG, BMP,
WebP and several more under a flag named for none of them, so a payload the
pipeline decoded is an image and not necessarily a JPEG. The operator feed labels
what it sends `image/jpeg`, so it asks both questions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast

import cv2
import numpy as np

if TYPE_CHECKING:
    from reachy_groundstation.ports import ImageArray

__all__ = ["DecodeError", "decode_jpeg", "is_jpeg"]

# Start of image, immediately followed by the first marker's own prefix. Two
# bytes would match a payload that merely opens the right way; three is the
# signature every JPEG file format registry lists, and no other image format
# this decoder accepts begins with it.
_JPEG_SIGNATURE: Final = b"\xff\xd8\xff"


class DecodeError(ValueError):
    """The payload was not an image this service can decode."""


#:= docs/specs/home-assistant-configuration-and-camera-feed/index.md#req-096-mjpeg-is-a-bounded-latest-frame-view
#:% The groundstation MUST retain at most one original payload globally for a
#:% standards-compatible MJPEG stream only after both explicit JPEG-format signature
#:% validation and successful image decode, replace rather than queue that payload
#:% for slow viewers, and add no robot connection, stream-only decode or re-encode,
#:% or capability-processing blockage.
def is_jpeg(payload: bytes) -> bool:
    """Say whether a payload is in the format the feed labels `image/jpeg`.

    Only the signature is examined, and deliberately: whether the rest of the
    payload is well-formed is what decoding answers, and the two together are
    what the feed requires. A stricter test — insisting on a trailing end-of-image
    marker, say — would refuse the padded frames some capture hardware produces,
    which is a false negative on real input in exchange for nothing the decode
    does not already cover.

    Args:
        payload: The frame exactly as the capture hardware produced it.

    Returns:
        Whether it begins with the JPEG signature.
    """
    return payload.startswith(_JPEG_SIGNATURE)


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
