"""Draw the perception fixture images the groundstation's tests run against.

The fixtures are committed; this is what produced them, and it is committed for
the same reason a model's source URL is. Every model, asset and image in this
repository has to be answerable for — where it came from and under what terms —
and the honest answer for these is "this file drew them, from nothing". There is
no photograph, no dataset, no third party with an interest, and nothing to check
a licence against.

**What a drawn face proves, and what it does not.** It proves the pipeline: that
YuNet is fed correctly, that its heads are decoded correctly, that overlapping
candidates are suppressed correctly, and that the same scene at two resolutions
reports the same place. It does not prove accuracy on real faces — nothing here
is evidence about how the detector behaves on a person, and the WIDER Face
figures in the perception spec are where that question is answered. The parity
test in particular is a comparison between two implementations over identical
weights, so what the image contains matters only in that it must make the model
fire; a scene it ignored would compare zero detections against zero and pass
having checked nothing.

Regenerating is deterministic — every random draw is seeded — so a rerun that
changes a file means the drawing changed, not the noise:

    just perception-fixtures
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

import cv2
import numpy as np
import numpy.typing as npt

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

__all__ = ["BUILDERS", "main"]

# An 8-bit BGR image, which is what OpenCV reads, writes and draws into.
type Image = npt.NDArray[np.uint8]

# Skin, sclera, iris, pupil, hair and lip, in BGR. Chosen for contrast at the
# scales below rather than for realism: the detector is looking for the pattern.
_SKIN: Final = (150, 178, 205)
_HAIR: Final = (38, 42, 52)
_SCLERA: Final = (238, 240, 244)
_IRIS: Final = (78, 66, 52)
_PUPIL: Final = (18, 16, 16)
_BROW: Final = (44, 48, 62)
_LID: Final = (52, 48, 56)
_NOSTRIL: Final = (70, 74, 92)
_LIP: Final = (78, 76, 118)
_LIP_EDGE: Final = (58, 56, 96)

# What the fixtures are encoded as. JPEG because that is what the robot link
# carries and what the pipeline's decoder is given, so a fixture is the same kind
# of thing a frame is; quality 92 because the artefacts at that setting are well
# below the tolerances the parity test asserts, and the files stay a few
# kilobytes each.
_JPEG_QUALITY: Final = 92


def _shade(colour: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    """Lighten or darken a colour.

    Args:
        colour: The base colour, in BGR.
        factor: What to multiply each channel by.

    Returns:
        The scaled colour, held inside the 8-bit range.
    """
    return (
        min(255, int(colour[0] * factor)),
        min(255, int(colour[1] * factor)),
        min(255, int(colour[2] * factor)),
    )


def _background(width: int, height: int, seed: int) -> Image:
    """Draw a softly lit, slightly noisy wall.

    A flat colour would be an easier scene than any camera ever produces, and a
    detector reading a flat field says nothing about one reading a room.

    Args:
        width: Image width in pixels.
        height: Image height in pixels.
        seed: What to seed the noise with.

    Returns:
        The background.
    """
    rng = np.random.default_rng(seed)
    horizontal = np.linspace(0.0, 1.0, width, dtype=np.float32)
    vertical = np.linspace(0.0, 1.0, height, dtype=np.float32)
    across, down = np.meshgrid(horizontal, vertical)
    base = 44.0 + 58.0 * down + 22.0 * np.sin(across * 6.0 + seed)
    image = np.stack([base * 1.06, base * 0.98, base * 0.90], axis=-1)
    image += rng.normal(0.0, 2.5, image.shape)
    drawn: Image = np.clip(image, 0, 255).astype(np.uint8)
    return drawn


def _face(canvas: Image, centre_x: int, centre_y: int, width: int) -> None:
    """Draw one face, in place.

    Every proportion is a fraction of the face's width, so the same call at
    twice the size draws the same face twice as large — which is what lets one
    scene be rendered at two resolutions rather than resampled to them.

    Args:
        canvas: What to draw into.
        centre_x: Horizontal centre of the face.
        centre_y: Vertical centre of the face.
        width: How wide the face is, in pixels.
    """
    height = int(width * 1.32)
    half_width, half_height = width // 2, height // 2

    cv2.ellipse(
        canvas,
        (centre_x, centre_y - int(height * 0.06)),
        (int(half_width * 1.12), int(half_height * 1.12)),
        0,
        0,
        360,
        _HAIR,
        -1,
    )
    cv2.ellipse(
        canvas,
        (centre_x, centre_y),
        (half_width, half_height),
        0,
        0,
        360,
        _SKIN,
        -1,
    )
    cv2.ellipse(
        canvas,
        (centre_x, centre_y - int(half_height * 0.18)),
        (int(half_width * 0.86), int(half_height * 0.72)),
        0,
        0,
        360,
        _shade(_SKIN, 1.07),
        -1,
    )

    eye_offset = int(width * 0.21)
    eye_y = centre_y - int(height * 0.10)
    eye_width = max(3, int(width * 0.13))
    eye_height = max(2, int(width * 0.070))
    for side in (-1, 1):
        eye_x = centre_x + side * eye_offset
        cv2.ellipse(
            canvas,
            (eye_x, eye_y),
            (int(eye_width * 1.35), int(eye_height * 1.7)),
            0,
            0,
            360,
            _shade(_SKIN, 0.80),
            -1,
        )
        cv2.ellipse(
            canvas,
            (eye_x, eye_y),
            (eye_width, eye_height),
            0,
            0,
            360,
            _SCLERA,
            -1,
        )
        cv2.circle(canvas, (eye_x, eye_y), max(2, int(eye_height * 0.95)), _IRIS, -1)
        cv2.circle(canvas, (eye_x, eye_y), max(1, int(eye_height * 0.45)), _PUPIL, -1)
        cv2.ellipse(
            canvas,
            (eye_x, eye_y),
            (eye_width, eye_height),
            0,
            185,
            355,
            _LID,
            max(1, eye_height // 3),
        )
        cv2.ellipse(
            canvas,
            (eye_x, eye_y - int(height * 0.062)),
            (int(eye_width * 1.25), int(eye_height * 0.9)),
            0,
            190,
            350,
            _BROW,
            max(1, int(width * 0.022)),
        )

    nose_y = centre_y + int(height * 0.06)
    cv2.ellipse(
        canvas,
        (centre_x, nose_y),
        (int(width * 0.085), int(height * 0.085)),
        0,
        0,
        360,
        _shade(_SKIN, 0.90),
        -1,
    )
    for side in (-1, 1):
        cv2.circle(
            canvas,
            (centre_x + side * int(width * 0.055), nose_y + int(height * 0.045)),
            max(1, int(width * 0.018)),
            _NOSTRIL,
            -1,
        )

    mouth_y = centre_y + int(height * 0.235)
    cv2.ellipse(
        canvas,
        (centre_x, mouth_y),
        (int(width * 0.19), int(height * 0.055)),
        0,
        0,
        180,
        _LIP,
        -1,
    )
    cv2.ellipse(
        canvas,
        (centre_x, mouth_y),
        (int(width * 0.19), int(height * 0.052)),
        0,
        0,
        180,
        _LIP_EDGE,
        max(1, int(width * 0.014)),
    )
    cv2.ellipse(
        canvas,
        (centre_x, centre_y + int(half_height * 0.72)),
        (int(half_width * 0.55), int(half_height * 0.22)),
        0,
        0,
        180,
        _shade(_SKIN, 0.92),
        -1,
    )


def _soften(image: Image) -> Image:
    """Blur an image very slightly, the way a lens does.

    Args:
        image: What to soften.

    Returns:
        The softened image.
    """
    # OpenCV's stubs declare every filter as returning an array of unspecified
    # integer or floating dtype, so the 8-bit result has to be restated. A cast
    # rather than a suppression: nothing is being silenced, the stub is simply
    # less specific than the call.
    return cast("Image", cv2.GaussianBlur(image, (3, 3), 0.8))


def _one_face(width: int, height: int, seed: int, at: tuple[int, int, int]) -> Image:
    """Draw a background with a single face on it.

    Args:
        width: Image width in pixels.
        height: Image height in pixels.
        seed: What to seed the background noise with.
        at: The face's horizontal centre, vertical centre and width.

    Returns:
        The finished image.
    """
    image = _background(width, height, seed)
    _face(image, at[0], at[1], at[2])
    return _soften(image)


def _two_faces(width: int, height: int, seed: int) -> Image:
    """Draw two faces of different sizes, which is what makes suppression work.

    A single face already lights up several anchors of one stride. Two faces at
    different scales light up more than one stride, so the suppression step has
    to keep two boxes rather than collapsing everything to the loudest.

    Args:
        width: Image width in pixels.
        height: Image height in pixels.
        seed: What to seed the background noise with.

    Returns:
        The finished image.
    """
    image = _background(width, height, seed)
    _face(image, 120, 130, 104)
    _face(image, 280, 110, 68)
    return _soften(image)


def _scene(width: int, height: int, seed: int) -> Image:
    """Draw a room with two people in it, from proportions rather than pixels.

    Called at two sizes it renders the same scene twice, which is what
    perception REQ-035 is about: the same scene captured at two resolutions,
    rather than one capture resampled — a resample would test the resampler.

    Args:
        width: Image width in pixels.
        height: Image height in pixels.
        seed: What to seed the background noise with.

    Returns:
        The finished image.
    """
    image = _background(width, height, seed)
    cv2.rectangle(image, (0, int(height * 0.72)), (width, height), (58, 70, 92), -1)
    cv2.rectangle(
        image,
        (int(width * 0.04), int(height * 0.10)),
        (int(width * 0.24), int(height * 0.52)),
        (96, 104, 120),
        -1,
    )
    _face(image, int(width * 0.34), int(height * 0.44), int(width * 0.24))
    _face(image, int(width * 0.72), int(height * 0.50), int(width * 0.18))
    return _soften(image)


def _empty_wall(width: int, height: int, seed: int) -> Image:
    """Draw a lit wall and nothing else.

    Args:
        width: Image width in pixels.
        height: Image height in pixels.
        seed: What to seed the noise with.

    Returns:
        The finished image.
    """
    return _soften(_background(width, height, seed))


def _clutter(width: int, height: int, seed: int) -> Image:
    """Draw scattered rectangles: edges and corners, no face and no hand.

    Args:
        width: Image width in pixels.
        height: Image height in pixels.
        seed: What to seed the layout with.

    Returns:
        The finished image.
    """
    image = _background(width, height, seed)
    rng = np.random.default_rng(seed + 100)
    for _ in range(14):
        left = int(rng.integers(0, width))
        top = int(rng.integers(0, height))
        box_width = int(rng.integers(12, 70))
        box_height = int(rng.integers(12, 70))
        colour = tuple(int(value) for value in rng.integers(30, 210, 3))
        cv2.rectangle(
            image,
            (left, top),
            (left + box_width, top + box_height),
            colour,
            -1,
        )
    return _soften(image)


def _blinds(width: int, height: int, seed: int) -> Image:
    """Draw strong repeating diagonals, which is what false positives like.

    Args:
        width: Image width in pixels.
        height: Image height in pixels.
        seed: What to seed the background noise with.

    Returns:
        The finished image.
    """
    image = _background(width, height, seed)
    for x in range(0, width, 18):
        cv2.line(image, (x, 0), (x + height // 2, height), (200, 196, 188), 5)
    for y in range(0, height, 26):
        cv2.line(image, (0, y), (width, y), (60, 64, 74), 3)
    return _soften(image)


def _static(width: int, height: int, seed: int) -> Image:
    """Draw sensor noise: no structure at all, at every spatial frequency.

    Args:
        width: Image width in pixels.
        height: Image height in pixels.
        seed: What to seed the noise with.

    Returns:
        The finished image.
    """
    rng = np.random.default_rng(seed)
    noise = rng.integers(0, 256, (height, width, 3), dtype=np.uint16)
    return cast("Image", cv2.GaussianBlur(noise.astype(np.uint8), (5, 5), 1.6))


def _shelves(width: int, height: int, seed: int) -> Image:
    """Draw shelves of books: many upright blobs at head-like spacing.

    Args:
        width: Image width in pixels.
        height: Image height in pixels.
        seed: What to seed the layout with.

    Returns:
        The finished image.
    """
    image = _background(width, height, seed)
    rng = np.random.default_rng(seed + 3)
    for row in range(3):
        shelf_y = int(height * (0.28 + 0.24 * row))
        cv2.rectangle(image, (0, shelf_y), (width, shelf_y + 6), (52, 62, 84), -1)
        left = 8
        while left < width - 20:
            book_width = int(rng.integers(10, 26))
            book_height = int(rng.integers(20, 46))
            colour = tuple(int(value) for value in rng.integers(40, 190, 3))
            cv2.rectangle(
                image,
                (left, shelf_y - book_height),
                (left + book_width, shelf_y),
                colour,
                -1,
            )
            left += book_width + int(rng.integers(3, 10))
    return _soften(image)


def _foliage(width: int, height: int, seed: int) -> Image:
    """Draw a plant: hundreds of overlapping organic shapes.

    Args:
        width: Image width in pixels.
        height: Image height in pixels.
        seed: What to seed the layout with.

    Returns:
        The finished image.
    """
    image = _background(width, height, seed)
    rng = np.random.default_rng(seed + 11)
    for _ in range(160):
        centre = (int(rng.integers(0, width)), int(rng.integers(0, height)))
        axes = (int(rng.integers(6, 22)), int(rng.integers(3, 9)))
        angle = int(rng.integers(0, 180))
        green = int(rng.integers(60, 170))
        cv2.ellipse(image, centre, axes, angle, 0, 360, (40, green, 46), -1)
    return _soften(image)


# Every fixture, by the name it is committed under. The `face_` and `scene_`
# images carry faces; the `negative_` images carry no face, no hand and no
# person, which is what makes them the fixture set perception REQ-037 measures
# the gesture capability against.
BUILDERS: Final[dict[str, Callable[[], Image]]] = {
    "face_single.jpg": lambda: _one_face(320, 240, 7, (160, 118, 96)),
    "face_upper_left.jpg": lambda: _one_face(320, 240, 13, (96, 84, 76)),
    "face_pair.jpg": lambda: _two_faces(384, 288, 21),
    # 300 by 220 is a multiple of neither the model's largest stride nor
    # anything else convenient, so both dimensions are padded before inference.
    # Every other fixture here has a stride-aligned width, and a width that
    # needed no padding would never exercise the anchor arithmetic that depends
    # on the padded one.
    "face_unaligned.jpg": lambda: _one_face(300, 220, 17, (150, 104, 84)),
    "scene_full.jpg": lambda: _scene(640, 480, 31),
    "scene_half.jpg": lambda: _scene(320, 240, 31),
    "negative_wall.jpg": lambda: _empty_wall(320, 240, 41),
    "negative_clutter.jpg": lambda: _clutter(320, 240, 43),
    "negative_blinds.jpg": lambda: _blinds(320, 240, 47),
    "negative_static.jpg": lambda: _static(320, 240, 53),
    "negative_shelves.jpg": lambda: _shelves(320, 240, 59),
    "negative_foliage.jpg": lambda: _foliage(320, 240, 61),
}


def main(argv: Sequence[str] | None = None) -> int:
    """Write every fixture into a directory.

    Args:
        argv: Command-line arguments, or `None` to read the real ones.

    Returns:
        The process exit status.
    """
    parser = argparse.ArgumentParser(
        prog="python scripts/generate_perception_fixtures.py",
        description="Draw the committed perception fixture images.",
    )
    parser.add_argument(
        "directory",
        type=Path,
        help="where to write the images",
    )
    arguments = parser.parse_args(argv)
    arguments.directory.mkdir(parents=True, exist_ok=True)

    for name, build in BUILDERS.items():
        destination = arguments.directory / name
        written = cv2.imwrite(
            str(destination),
            build(),
            [cv2.IMWRITE_JPEG_QUALITY, _JPEG_QUALITY],
        )
        if not written:
            sys.stderr.write(f"perception fixtures: could not write {destination}\n")
            return 1
        sys.stdout.write(f"perception fixtures: wrote {destination}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
