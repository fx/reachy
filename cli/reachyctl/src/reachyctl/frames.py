"""Where `probe` gets frames: a directory of recordings, or a local camera.

Both are behind one interface, and the interface exists for the reason the
architecture spec gives: no test may require a camera, so anything that needs
one is reached through a seam and exercised with a fake. What that buys is
honesty about what is covered. The recorded source is exercised for real. The
camera source is exercised through an injected capture, and the OpenCV code
behind it is exercised through an injected module — so every line runs in the
suite, and **no camera is opened anywhere in it**. Whether this tool can drive
an actual camera is answered by running it against one, which is the
end-to-end session and not this file.

OpenCV is imported inside the function that needs it rather than at the top of
the module. It is tens of megabytes, it is an optional extra of this
distribution, and every command but `probe --camera` runs without it — so a
missing install produces the sentence that names the extra, at the moment
somebody asks for a camera, instead of an ImportError at start-up on a machine
that was never going to use one.
"""

from __future__ import annotations

import asyncio
import importlib
from typing import TYPE_CHECKING, Any, Final, Protocol

from reachyctl.errors import ConfigurationError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path
    from types import ModuleType

__all__ = [
    "CameraCapture",
    "CameraFrames",
    "FrameSource",
    "OpenCvCamera",
    "RecordedFrames",
    "open_camera",
]

# What a recorded frame looks like on disk. The protocol carries frames as the
# bytes the capture hardware produced, and the hardware produces JPEG.
_SUFFIXES: Final = (".jpg", ".jpeg")


class FrameSource(Protocol):
    """Where one probe run's frames come from."""

    @property
    def description(self) -> str:
        """What to call this source in the report.

        Returns:
            A short phrase naming where the frames came from.
        """
        ...

    def frames(self) -> AsyncIterator[bytes]:
        """Produce frames until there are no more.

        Returns:
            The frames, each already compressed, in the order to send them.
        """
        ...

    def close(self) -> None:
        """Release whatever the source holds."""
        ...


class RecordedFrames:
    """Frames read from a directory, in name order.

    Name order rather than modification time: a recording is a sequence, and
    the sequence is what the file names carry. Ordering by timestamp would make
    the same directory replay differently after a copy.
    """

    def __init__(self, directory: Path) -> None:
        """Find the frames in a directory.

        Args:
            directory: Where the recordings are.

        Raises:
            ConfigurationError: If the directory is not one, or holds no frames.
                Both are the operator's input rather than a diagnosis of
                anything, so neither is a failed probe.
        """
        if not directory.is_dir():
            message = f"{directory} is not a directory of recorded frames"
            raise ConfigurationError(message)
        self._paths = sorted(
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() in _SUFFIXES
        )
        if not self._paths:
            message = (
                f"{directory} holds no recorded frames; expected files ending "
                f"in {' or '.join(_SUFFIXES)}"
            )
            raise ConfigurationError(message)
        self._directory = directory

    @property
    def description(self) -> str:
        """What to call this source in the report.

        Returns:
            The directory and how many frames were found in it.
        """
        return f"{len(self._paths)} recorded frames from {self._directory}"

    async def frames(self) -> AsyncIterator[bytes]:
        """Read each recorded frame in turn.

        Yields:
            Each file's bytes, unaltered — they are already compressed, and the
            protocol carries them as they are.

        Raises:
            ConfigurationError: If a frame that was there when the directory was
                listed cannot be read now, or if one of them holds nothing.
        """
        for path in self._paths:
            try:
                # Off the event loop. `probe` runs this producer and the
                # session's result loop on one loop, so a blocking read here
                # stops the client observing a result that has already arrived
                # — and the delay lands in the round-trip figure the probe
                # exists to measure.
                payload = await asyncio.to_thread(path.read_bytes)
            except OSError as error:
                reason = error.strerror or type(error).__name__
                message = f"the recorded frame {path} could not be read: {reason}"
                raise ConfigurationError(message) from error
            if not payload:
                # Refused here rather than left to the framing, which is right
                # that an empty payload is not a frame but is three layers away
                # from the thing an operator can fix. An empty file in a
                # directory of recordings is an ordinary accident — an
                # interrupted copy, a truncated download — and it deserves the
                # name of the file rather than a sentence about the protocol.
                message = f"the recorded frame {path} is empty"
                raise ConfigurationError(message)
            yield payload

    def close(self) -> None:
        """Release whatever the source holds, which is nothing."""
        return


class CameraCapture(Protocol):
    """A camera, reduced to what a probe run asks of one."""

    def grab_jpeg(self) -> bytes | None:
        """Take one frame and compress it.

        Returns:
            The compressed frame, or `None` when the camera has nothing more to
            give — which ends the run rather than failing it.
        """
        ...

    def release(self) -> None:
        """Let go of the device."""
        ...


class OpenCvCamera:
    """A `CameraCapture` over an OpenCV video capture.

    The capture and the encoder are both parameters, so this class is driven in
    the suite by fakes and by nothing else. Whether OpenCV can find a real
    camera is not a question this file can answer.
    """

    def __init__(
        self,
        capture: Any,  # noqa: ANN401  # OpenCV ships no type information, so a `VideoCapture` is `Any` however it is spelled; the narrow shape this class needs is the two methods called below
        encode: Callable[..., tuple[bool, Any]],
    ) -> None:
        """Wrap an opened capture.

        Args:
            capture: The OpenCV capture, already opened.
            encode: OpenCV's `imencode`, or a stand-in for it.
        """
        self._capture = capture
        self._encode = encode

    def grab_jpeg(self) -> bytes | None:
        """Take one frame and compress it to JPEG.

        Returns:
            The compressed frame, or `None` when the camera stopped producing
            them or the encoder declined this one.
        """
        ok, image = self._capture.read()
        if not ok:
            return None
        encoded_ok, encoded = self._encode(".jpg", image)
        if not encoded_ok:
            return None
        return bytes(encoded.tobytes())

    def release(self) -> None:
        """Let go of the device."""
        self._capture.release()


def _opencv(
    import_module: Callable[[str], ModuleType] = importlib.import_module,
) -> ModuleType:
    """Load OpenCV, or explain that it was not installed.

    Args:
        import_module: How to import. Injected so that the "not installed"
            answer is exercised on a machine where it is.

    Returns:
        The OpenCV module.

    Raises:
        ConfigurationError: If OpenCV is not installed, naming the extra that
            installs it rather than reporting an ImportError.
    """
    try:
        return import_module("cv2")
    except ImportError as error:
        message = (
            "live frames need OpenCV, which is an optional extra of this tool: "
            "install reachyctl[camera]. Recorded frames need nothing extra."
        )
        raise ConfigurationError(message) from error


def open_camera(
    index: int,
    import_module: Callable[[str], ModuleType] = importlib.import_module,
) -> CameraCapture:
    """Open a local camera by index.

    Args:
        index: Which camera, as the operating system numbers them.
        import_module: How to import OpenCV. Injected so this function is
            exercised without a camera.

    Returns:
        The camera, ready to be read from.

    Raises:
        ConfigurationError: If OpenCV is not installed, or if there is no
            camera at that index.
    """
    cv2 = _opencv(import_module)
    capture = cv2.VideoCapture(index)
    if not capture.isOpened():
        capture.release()
        message = f"there is no camera at index {index}"
        raise ConfigurationError(message)
    return OpenCvCamera(capture, cv2.imencode)


class CameraFrames:
    """Frames grabbed from a local camera, one at a time, until it stops."""

    def __init__(
        self,
        index: int,
        open_capture: Callable[[int], CameraCapture] = open_camera,
    ) -> None:
        """Open the camera.

        Args:
            index: Which camera, as the operating system numbers them.
            open_capture: How to open one. Injected so that the whole of this
                class runs in the suite against a fake, because no test in this
                repository may require a camera.

        Raises:
            ConfigurationError: If the camera cannot be opened.
        """
        self._index = index
        self._capture = open_capture(index)

    @property
    def description(self) -> str:
        """What to call this source in the report.

        Returns:
            The camera index.
        """
        return f"live frames from camera {self._index}"

    async def frames(self) -> AsyncIterator[bytes]:
        """Grab frames until the camera stops producing them.

        Yields:
            Each grabbed frame, compressed to JPEG.
        """
        while True:
            # Off the event loop, for the reason `RecordedFrames` gives and
            # more so: a camera read costs at least one frame period and the
            # JPEG encode costs more, which is long enough to starve the
            # session's result loop and the staleness window with it.
            frame = await asyncio.to_thread(self._capture.grab_jpeg)
            if frame is None:
                return
            yield frame

    def close(self) -> None:
        """Let go of the device."""
        self._capture.release()
