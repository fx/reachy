"""Fakes and builders shared by the reachyctl tests.

Nothing here fakes a command. What is faked is the world a command talks to: a
camera that produces bytes without a device, an OpenCV that is or is not
installed, and a pair of streams a reporter writes into. The commands, the
reporter and the session client are always the real ones, and the integration
test drives the real transport against the real groundstation.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any, Final

from reachyctl.output import OutputFormat, Reporter
from reachyctl.redaction import Redactor

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import ModuleType

__all__ = [
    "CREDENTIAL",
    "JPEG",
    "FakeCapture",
    "FakeOpenCv",
    "FakeVideoCapture",
    "Streams",
    "opencv_module",
    "reporter_for",
]

# A placeholder credential. Not anybody's, and never a real one — see the root
# AGENTS.md on what may enter a tracked file in a public repository.
CREDENTIAL: Final = "example-credential"

# Opaque bytes standing in for a compressed frame. The protocol never looks
# inside one, so a test does not need a real JPEG unless a decoder is involved —
# and where one is, the integration test encodes a real image.
JPEG: Final = b"\xff\xd8\xff\xe0 opaque compressed bytes"


class Streams:
    """The two streams a reporter writes to, held for inspection.

    Attributes:
        out: Where the result went.
        err: Where progress and diagnostics went.
    """

    def __init__(self) -> None:
        """Create empty streams."""
        self.out = io.StringIO()
        self.err = io.StringIO()

    @property
    def result(self) -> str:
        """What was written to standard output.

        Returns:
            The result, which is the only thing that stream carries.
        """
        return self.out.getvalue()

    @property
    def diagnostics(self) -> str:
        """What was written to standard error.

        Returns:
            The progress and detail lines.
        """
        return self.err.getvalue()


def reporter_for(
    *,
    output_format: OutputFormat = OutputFormat.TEXT,
    verbose: bool = False,
    terminal: bool = False,
    secrets: Sequence[str] = (),
) -> tuple[Reporter, Streams]:
    """Build a reporter writing into strings rather than into a process.

    Args:
        output_format: Which rendering the result gets.
        verbose: Whether to write the detail lines.
        terminal: Whether to render as though attached to a terminal. Passed
            explicitly, which is how both renderings are exercised without a
            pseudo-terminal.
        secrets: Values that must not appear in anything written.

    Returns:
        The reporter and the streams it writes into.
    """
    streams = Streams()
    reporter = Reporter(
        out=streams.out,
        err=streams.err,
        output_format=output_format,
        verbose=verbose,
        terminal=terminal,
        redactor=Redactor(secrets),
    )
    return reporter, streams


class FakeCapture:
    """A `CameraCapture` that hands out prepared frames and then stops.

    Attributes:
        released: Whether the device was let go of.
    """

    def __init__(self, *frames: bytes) -> None:
        """Prepare what the camera will produce.

        Args:
            frames: The frames to hand out, in order. After the last one the
                camera reports that it has nothing more, which ends a run
                rather than failing it.
        """
        self._frames = list(frames)
        self.released = False

    def grab_jpeg(self) -> bytes | None:
        """Take the next prepared frame.

        Returns:
            The frame, or `None` once they have run out.
        """
        if not self._frames:
            return None
        return self._frames.pop(0)

    def release(self) -> None:
        """Record that the device was let go of."""
        self.released = True


class FakeVideoCapture:
    """An OpenCV video capture, reduced to what `OpenCvCamera` calls.

    Attributes:
        released: Whether `release` was called.
    """

    def __init__(self, *images: object, opened: bool = True) -> None:
        """Prepare what the device will produce.

        Args:
            images: The decoded images to hand out, in order.
            opened: What `isOpened` should answer.
        """
        self._images = list(images)
        self._opened = opened
        self.released = False

    def isOpened(self) -> bool:  # noqa: N802  # OpenCV's own spelling; this object exists to be indistinguishable from a cv2.VideoCapture
        """Say whether the device opened.

        Returns:
            What this fake was built to answer.
        """
        return self._opened

    def read(self) -> tuple[bool, object]:
        """Take the next prepared image.

        Returns:
            Whether there was one, and the image if there was.
        """
        if not self._images:
            return False, None
        return True, self._images.pop(0)

    def release(self) -> None:
        """Record that the device was let go of."""
        self.released = True


class _Encoded:
    """What OpenCV's encoder hands back: something with `tobytes`."""

    def __init__(self, payload: bytes) -> None:
        """Hold the encoded bytes.

        Args:
            payload: What `tobytes` should return.
        """
        self._payload = payload

    def tobytes(self) -> bytes:
        """Hand over the encoded bytes.

        Returns:
            The payload.
        """
        return self._payload


class FakeOpenCv:
    """The OpenCV module, reduced to the two names this tool uses."""

    def __init__(self, capture: FakeVideoCapture, *, encodes: bool = True) -> None:
        """Prepare what the module will hand out.

        Args:
            capture: What `VideoCapture` should return.
            encodes: Whether `imencode` should succeed.
        """
        self._capture = capture
        self._encodes = encodes
        self.opened_indexes: list[int] = []

    def VideoCapture(self, index: int) -> FakeVideoCapture:  # noqa: N802  # OpenCV's own spelling, for the same reason as above
        """Open a device.

        Args:
            index: Which device was asked for.

        Returns:
            The prepared capture.
        """
        self.opened_indexes.append(index)
        return self._capture

    def imencode(self, suffix: str, image: Any) -> tuple[bool, _Encoded]:  # noqa: ANN401  # OpenCV ships no type information, so an image is `Any` however it is spelled
        """Compress an image.

        Args:
            suffix: The format, which this fake records by ignoring.
            image: The decoded image.

        Returns:
            Whether it worked, and the encoded bytes.
        """
        del suffix, image
        return self._encodes, _Encoded(JPEG)


def opencv_module(module: FakeOpenCv) -> ModuleType:
    """Present a fake OpenCV where a module is expected.

    `importlib.import_module` is typed as returning a `ModuleType`, and what
    this tool needs of OpenCV is two attributes rather than a module. The cast
    is here, once, rather than in each test.

    Args:
        module: The fake to present.

    Returns:
        The same object, typed as a module.
    """
    return module  # type: ignore[return-value]  # a module is a namespace, and this fake is the namespace the caller reads two names out of
