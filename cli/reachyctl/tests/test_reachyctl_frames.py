"""Where probe's frames come from, exercised without a camera and without a disk.

No test in this repository may require a camera, so the camera is reached
through a seam and driven by a fake — and the OpenCV adapter behind that seam is
driven by a fake module, so that the code which would touch a device is still
executed here rather than being excluded from the suite. What is **not** claimed
is that this tool can drive a real camera. That is answered by pointing it at
one, which is the deferred end-to-end session and not this file.

The recorded source reads files, so its tests read files — from `pyfakefs`'s
in-memory filesystem, which performs no input or output at all and leaves them
unit tests. The `filesystem` marker is for a test that touches a real one, and
is on the integration tests that do.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from reachyctl_support import (
    JPEG,
    FakeCapture,
    FakeOpenCv,
    FakeVideoCapture,
    opencv_module,
)

from reachyctl.errors import ConfigurationError
from reachyctl.frames import (
    CameraFrames,
    FrameSource,
    OpenCvCamera,
    RecordedFrames,
    open_camera,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

    from pyfakefs.fake_filesystem import FakeFilesystem

RECORDINGS: Final = Path("/recordings")


async def drained(source: FrameSource) -> list[bytes]:
    """Read every frame a source produces.

    Args:
        source: The frame source.

    Returns:
        The frames, in order.
    """
    return [frame async for frame in source.frames()]


@pytest.mark.asyncio
async def test_recorded_frames_are_read_in_name_order(fs: FakeFilesystem) -> None:
    """A recording is a sequence, and the file names are what carry it.

    Args:
        fs: An in-memory filesystem.
    """
    fs.create_file(str(RECORDINGS / "frame-002.jpg"), contents="second")
    fs.create_file(str(RECORDINGS / "frame-001.jpg"), contents="first")
    fs.create_file(str(RECORDINGS / "frame-003.JPEG"), contents="third")
    fs.create_file(str(RECORDINGS / "notes.txt"), contents="not a frame")

    source = RecordedFrames(RECORDINGS)

    assert await drained(source) == [b"first", b"second", b"third"]
    assert "3 recorded frames" in source.description
    source.close()


def test_a_directory_with_no_frames_in_it_is_the_operators_input(
    fs: FakeFilesystem,
) -> None:
    """Not a failed probe: nothing was asked of the groundstation.

    Args:
        fs: An in-memory filesystem.
    """
    fs.create_dir(str(RECORDINGS))

    with pytest.raises(ConfigurationError, match="holds no recorded frames"):
        RecordedFrames(RECORDINGS)


def test_a_path_that_is_not_a_directory_says_so(fs: FakeFilesystem) -> None:
    """Naming a single file is the commonest way to get this wrong.

    Args:
        fs: An in-memory filesystem.
    """
    fs.create_file("/recordings.jpg", contents="one frame, not a directory")

    with pytest.raises(ConfigurationError, match="is not a directory"):
        RecordedFrames(Path("/recordings.jpg"))


@pytest.mark.asyncio
async def test_a_frame_that_vanishes_between_listing_and_reading(
    fs: FakeFilesystem,
) -> None:
    """The directory is listed once; the world can change before it is read.

    Args:
        fs: An in-memory filesystem.
    """
    fs.create_file(str(RECORDINGS / "frame-001.jpg"), contents="first")
    source = RecordedFrames(RECORDINGS)
    fs.remove(str(RECORDINGS / "frame-001.jpg"))

    with pytest.raises(ConfigurationError, match="could not be read"):
        await drained(source)


@pytest.mark.asyncio
async def test_a_camera_produces_frames_until_it_stops() -> None:
    """Running out is how a live source ends, not how it fails."""
    capture = FakeCapture(b"one", b"two")
    source = CameraFrames(3, lambda _index: capture)

    frames = await drained(source)
    source.close()

    assert frames == [b"one", b"two"]
    assert source.description == "live frames from camera 3"
    assert capture.released


def test_the_opencv_adapter_compresses_what_the_device_produced() -> None:
    """Driven by a fake device and a fake encoder; no camera is opened."""
    device = FakeVideoCapture("an image")
    camera = OpenCvCamera(device, FakeOpenCv(device).imencode)

    assert camera.grab_jpeg() == JPEG
    assert camera.grab_jpeg() is None

    camera.release()
    assert device.released


def test_a_frame_the_encoder_declines_ends_the_run_rather_than_failing_it() -> None:
    """One frame OpenCV will not compress is not a diagnosis of anything."""
    device = FakeVideoCapture("an image")
    camera = OpenCvCamera(device, FakeOpenCv(device, encodes=False).imencode)

    assert camera.grab_jpeg() is None


def test_opening_a_camera_asks_opencv_for_the_index_it_was_given() -> None:
    """The whole of `open_camera` runs here, with OpenCV itself injected."""
    device = FakeVideoCapture("an image")
    module = FakeOpenCv(device)

    camera = open_camera(2, _module_returning(module))

    assert module.opened_indexes == [2]
    assert camera.grab_jpeg() == JPEG


def test_a_camera_that_does_not_open_is_reported_by_index() -> None:
    """And the device is let go of rather than left half-held."""
    device = FakeVideoCapture(opened=False)
    module = FakeOpenCv(device)

    with pytest.raises(ConfigurationError, match="no camera at index 7"):
        open_camera(7, _module_returning(module))

    assert device.released


def test_opencv_not_being_installed_names_the_extra_that_installs_it() -> None:
    """Rather than an ImportError from a module nobody asked this tool to need."""

    def missing(name: str) -> ModuleType:
        """Refuse to import anything.

        Args:
            name: What was asked for.

        Returns:
            Nothing; this always raises.

        Raises:
            ImportError: Always.
        """
        message = f"No module named {name!r}"
        raise ImportError(message)

    with pytest.raises(ConfigurationError, match=r"reachyctl\[camera\]"):
        open_camera(0, missing)


def _module_returning(module: FakeOpenCv) -> Callable[[str], ModuleType]:
    """Build an importer that hands over one prepared module.

    Args:
        module: What to hand over.

    Returns:
        Something to pass as `import_module`.
    """

    def import_module(name: str) -> ModuleType:
        """Answer an import.

        Args:
            name: What was asked for.

        Returns:
            The prepared module.

        Raises:
            AssertionError: If anything but OpenCV was asked for.
        """
        if name != "cv2":
            message = f"the camera source imported {name!r}, not 'cv2'"
            raise AssertionError(message)
        return opencv_module(module)

    return import_module
