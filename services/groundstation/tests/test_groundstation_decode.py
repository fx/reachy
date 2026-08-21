"""Decoding a frame, and refusing to decode one.

Nothing a client can send may escape this module as an exception the pipeline
does not expect: a frame is arbitrary bytes from the network, and an
undeclared exception here would unwind a whole session's pipeline over one bad
frame.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`. Nothing here touches a socket, a clock or a file: encoding and
decoding an image in memory is arithmetic.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest
from groundstation_support import jpeg_bytes

from reachy_groundstation.pipeline.decode import DecodeError, decode_jpeg


def test_a_frame_decodes_to_its_pixels() -> None:
    """The decoded frame is height by width by three colour channels."""
    image = decode_jpeg(jpeg_bytes(width=64, height=48))
    assert image.shape == (48, 64, 3)
    assert image.dtype == np.uint8


def test_an_empty_payload_is_refused() -> None:
    """Zero bytes is not an image, and saying so early costs nothing."""
    with pytest.raises(DecodeError, match="empty payload"):
        decode_jpeg(b"")


@pytest.mark.parametrize(
    "payload",
    [
        b"this is not a jpeg",
        b"\xff\xd8\xff\xe0 truncated",
        bytes(range(256)),
    ],
)
def test_bytes_that_are_not_an_image_are_refused(payload: bytes) -> None:
    """A malformed frame is one error for one frame, not a failed session."""
    with pytest.raises(DecodeError):
        decode_jpeg(payload)


def test_a_decoder_that_raises_is_reported_as_a_decode_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenCV signals some malformed inputs by raising rather than returning.

    The raising path is patched rather than induced: which byte sequences make
    OpenCV raise instead of return `None` is a property of the build it was
    compiled from, so an input that provokes it here would not provoke it on the
    robot's architecture. What has to hold either way is that nothing but
    `DecodeError` leaves this function.

    Args:
        monkeypatch: Used to make the decoder raise the way OpenCV can.
    """

    def _raise(*args: object, **kwargs: object) -> None:
        """Fail the way OpenCV does.

        Args:
            args: Ignored.
            kwargs: Ignored.

        Raises:
            cv2.error: Always.
        """
        del args, kwargs
        raise cv2.error("decoder said no")

    monkeypatch.setattr(cv2, "imdecode", _raise)
    with pytest.raises(DecodeError, match="not a decodable image"):
        decode_jpeg(jpeg_bytes())


def test_the_decoded_frame_is_independent_of_the_capture_resolution() -> None:
    """Both capture resolutions decode.

    Normalising the geometry is the capability's job, not the decoder's, so what
    this asserts is that the decoder reports the size it was given rather than
    one it chose.
    """
    small = decode_jpeg(jpeg_bytes(width=32, height=24))
    large = decode_jpeg(jpeg_bytes(width=64, height=48))
    assert (small.shape[1], large.shape[1]) == (32, 64)
