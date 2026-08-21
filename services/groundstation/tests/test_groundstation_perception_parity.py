"""The parity gate: this repository's decoding against the SDK's, same weights.

This is a merge gate, not an advisory check. Hand-written decoding of a model's
output is the highest-risk code here — it is silently wrong when it is wrong, and
what it is wrong about goes to a motor — so perception REQ-036 requires it be
checked against an independent reference implementation, and this is that check.

**Why the reference is the Reachy Mini SDK's detector.** It runs identical
weights, it is maintained by somebody else, and it already runs on the robot. A
reference written here would be a second implementation to keep correct, and the
two would drift together the first time somebody "fixed" both.

**Why it is loaded by file path.** `import reachy_mini.vision.face_detector`
executes the distribution's `__init__`, which transitively imports its camera
pipeline and with it PyGObject and GStreamer — none of which a runner has.
`load_reference_detector` in the shared helpers explains the bypass in full;
please read it before turning this into a plain import, which cannot work.

**What the tolerances are and where they came from.** They are stated below as
constants with the measured distribution beside them, rather than being implied
by an `approx` somewhere. The observed disagreement across the fixture set is
reported by the summary test at the bottom whether or not it is within tolerance,
so a change that moves the distribution is visible in the output as well as in
the pass or fail.

Every test here runs real inference against committed fixture images rather than
mocking the runtime, which is what this change's testing requirements ask for. It
reads the model file and the fixtures, so it is not a unit test and says so.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import pytest
from groundstation_perception_support import (
    FACE_FIXTURES,
    fixture_image,
    load_reference_detector,
    reference_faces,
    require_model,
)
from groundstation_support import make_settings

from reachy_groundstation.capabilities.perception.face import detect_faces
from reachy_groundstation.models import FACE_DETECTION_YUNET
from reachy_groundstation.runtime import ModelRuntime, RuntimeOptions

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

# The thresholds the reference implementation uses, so the two are compared at
# the same sensitivity rather than at two.
_SCORE_THRESHOLD: Final = 0.6
_NMS_THRESHOLD: Final = 0.3

# How far apart a box centre may be, in pixels of the frame's own resolution.
#
# The observed maximum across the whole fixture set is zero: the two
# implementations agree bit for bit, because they are two spellings of the same
# arithmetic over the same float32 outputs of the same session. The tolerance is
# not zero anyway. A future change that vectorises a loop, or reorders a
# multiplication, can move a result by an ulp or two without being wrong, and a
# gate that failed on that would be a gate people learn to route around. This is
# tight enough that a real decoding error — an anchor decoded against the wrong
# width, a centre confused with a corner, a stride misread — moves a detection
# by tens of pixels and fails immediately.
_CENTRE_TOLERANCE_PIXELS: Final = 0.5

# How far apart a confidence may be. Observed maximum across the fixture set is
# zero, for the same reason, and the same argument applies.
_SCORE_TOLERANCE: Final = 0.005


@pytest.fixture(scope="module")
def reference() -> ModuleType:
    """Load the SDK's decoder, pointed at the weights already on disk.

    Returns:
        The reference module.
    """
    return load_reference_detector(require_model(FACE_DETECTION_YUNET))


@pytest.fixture(scope="module")
def runtime() -> Iterator[ModelRuntime]:
    """Open the same weights through this service's own runtime.

    Yields:
        The runtime, closed when the module's tests are done with it.
    """
    model = ModelRuntime(
        require_model(FACE_DETECTION_YUNET),
        RuntimeOptions.from_settings(make_settings()),
        FACE_DETECTION_YUNET.name,
    )
    try:
        yield model
    finally:
        model.close()


#:= docs/specs/perception/index.md#req-036-post-processing-is-verified-against-a-reference-implementation
#:% The hand-written pre- and post-processing MUST be verified against an
#:% independent reference implementation on a fixture set, asserting agreement on
#:% detection count, position, and confidence within stated tolerances.
@pytest.mark.filesystem
@pytest.mark.parametrize("fixture", FACE_FIXTURES)
def test_decoding_agrees_with_the_reference_implementation(
    fixture: str,
    reference: ModuleType,
    runtime: ModelRuntime,
) -> None:
    """Count, position and confidence, against the SDK, on one fixture.

    Args:
        fixture: The committed image to run both implementations over.
        reference: The SDK's decoder.
        runtime: This service's runtime, holding the same weights.
    """
    image = fixture_image(fixture)
    ours = detect_faces(runtime, image, _SCORE_THRESHOLD, _NMS_THRESHOLD)
    theirs = reference_faces(reference, image)

    assert len(ours) == len(theirs), (
        f"{fixture}: this implementation found {len(ours)} faces and the "
        f"reference found {len(theirs)}"
    )
    # A fixture the model ignores would compare nothing against nothing and pass
    # having checked nothing at all, so every image in the set has to fire.
    assert ours, f"{fixture}: neither implementation detected anything"

    for index, (mine, theirs_face) in enumerate(zip(ours, theirs, strict=True)):
        mine_x, mine_y = mine.centre
        their_x, their_y = theirs_face.centre
        assert abs(mine_x - their_x) <= _CENTRE_TOLERANCE_PIXELS, (
            f"{fixture} face {index}: horizontal centre {mine_x} against {their_x}"
        )
        assert abs(mine_y - their_y) <= _CENTRE_TOLERANCE_PIXELS, (
            f"{fixture} face {index}: vertical centre {mine_y} against {their_y}"
        )
        assert abs(mine.score - theirs_face.score) <= _SCORE_TOLERANCE, (
            f"{fixture} face {index}: confidence {mine.score} against "
            f"{theirs_face.score}"
        )


@pytest.mark.filesystem
def test_the_parity_gate_reports_the_distribution_it_observed(
    reference: ModuleType,
    runtime: ModelRuntime,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Print the worst disagreement over the whole set, not just pass or fail.

    A gate that only says "within tolerance" hides the distribution drifting
    towards the tolerance, which is the shape a decoding bug has while it is
    still small. This reports the numbers so a reviewer reading the run's output
    sees them.

    Args:
        reference: The SDK's decoder.
        runtime: This service's runtime.
        capsys: Used to write the summary where `-s` shows it.
    """
    worst_centre = 0.0
    worst_score = 0.0
    faces = 0
    for fixture in FACE_FIXTURES:
        image = fixture_image(fixture)
        ours = detect_faces(runtime, image, _SCORE_THRESHOLD, _NMS_THRESHOLD)
        theirs = reference_faces(reference, image)
        for mine, theirs_face in zip(ours, theirs, strict=True):
            mine_x, mine_y = mine.centre
            their_x, their_y = theirs_face.centre
            worst_centre = max(
                worst_centre,
                abs(mine_x - their_x),
                abs(mine_y - their_y),
            )
            worst_score = max(worst_score, abs(mine.score - theirs_face.score))
            faces += 1

    with capsys.disabled():
        # Printed rather than only asserted: this is the distribution the
        # tolerances above are stated against, and a reviewer reads it out of
        # the run rather than inferring it from a green tick.
        print(
            f"\nparity over {len(FACE_FIXTURES)} fixtures, {faces} faces: "
            f"maximum centre deviation {worst_centre:.6f} px "
            f"(tolerance {_CENTRE_TOLERANCE_PIXELS}), "
            f"maximum confidence deviation {worst_score:.6f} "
            f"(tolerance {_SCORE_TOLERANCE})",
        )

    assert faces > 0
    assert worst_centre <= _CENTRE_TOLERANCE_PIXELS
    assert worst_score <= _SCORE_TOLERANCE


@pytest.mark.filesystem
def test_the_parity_comparison_fails_when_the_decoding_moves(
    reference: ModuleType,
    runtime: ModelRuntime,
) -> None:
    """A gate nobody has watched fail is a gate that does not exist.

    The mistake staged here is the one this whole module is about: decoding the
    anchor indices against the frame's own width rather than the width the model
    was run at. It is invisible in the output — plausible boxes, plausible
    scores, in the wrong places — and it is caught here by tens of pixels.

    Args:
        reference: The SDK's decoder.
        runtime: This service's runtime.
    """
    # The one fixture whose width is not already a multiple of the stride. On
    # the others the padded and unpadded widths are equal, so this particular
    # mistake is unstageable — which is itself worth knowing: a deployment that
    # only ever saw stride-aligned captures would carry the bug invisibly.
    image = fixture_image("face_unaligned.jpg")
    assert image.shape[1] % 32 != 0
    theirs = reference_faces(reference, image)
    assert theirs

    # Imported here rather than at module scope: this is the only test that
    # reaches past `detect_faces` into the steps it composes, in order to stage
    # a mistake `detect_faces` cannot be asked to make.
    from reachy_groundstation.capabilities.perception.yunet import (
        decode_faces,
        pad_to_stride,
        to_blob,
    )

    padded = pad_to_stride(image)
    outputs = runtime.run({runtime.input_name: to_blob(padded)})
    wrong = decode_faces(
        outputs,
        # The unpadded width, which is the mistake.
        int(image.shape[1]),
        _SCORE_THRESHOLD,
        _NMS_THRESHOLD,
    )

    # The gate above asserts on the count first and then on each position, so
    # the staged mistake has to fail one of the two. It fails whichever it
    # happens to fail; what matters is that it cannot pass.
    deviations = [
        max(abs(a.centre[0] - b.centre[0]), abs(a.centre[1] - b.centre[1]))
        for a, b in zip(wrong, theirs, strict=False)
    ]
    assert len(wrong) != len(theirs) or max(deviations) > _CENTRE_TOLERANCE_PIXELS
