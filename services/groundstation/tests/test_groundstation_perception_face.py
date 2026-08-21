"""The face capability end to end: real weights, committed frames, real answers.

Every test here runs the model. That is deliberate and it is what this change's
testing requirements ask for: an integration test that mocked the runtime would
be a test of the mock, and the questions being asked — does a face in the upper
left report a negative horizontal and a positive vertical component, does the
same scene at two resolutions report the same place — are questions only real
inference can answer.

They read the model file and the fixtures, so they are not unit tests and say so
with `@pytest.mark.filesystem`. No socket is opened: the weights are already on
disk, because groundstation REQ-023 forbids fetching them at run time.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import numpy as np
import pytest
import pytest_asyncio
from groundstation_perception_support import (
    fixture_frame,
    model_directory,
    require_model,
)
from groundstation_support import make_settings

from reachy_contracts import FACE_CAPABILITY, FaceDetections
from reachy_groundstation.capabilities.perception.face import (
    FACE_VERSION,
    FaceCapability,
)
from reachy_groundstation.models import FACE_DETECTION_YUNET, ModelStoreError
from reachy_groundstation.runtime import ModelRuntime

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from reachy_groundstation.config import Settings

# How far apart the same scene's reported centres may be when it is captured at
# two resolutions, in normalised units — where the whole frame is two units
# across, so this is one percent of the frame's width, or about six pixels of a
# 640-wide capture.
#
# The observed disagreement between the 640 by 480 and 320 by 240 renderings of
# the fixture scene is 0.013 at its worst, and the test prints what it measured
# so a drift towards the tolerance is visible rather than merely absent.
#
# It is not tighter because the two are genuinely different captures rather than
# one capture resampled: the smaller is drawn with half as many pixels, so the
# drawing itself lands about a pixel differently before the detector sees it.
# That is the situation the requirement describes — a camera at two resolutions —
# and a comparison against a resampled copy would mostly test the resampler.
_RESOLUTION_TOLERANCE: Final = 0.02

# A model directory that does not exist and is never created. `tmp_path` would
# be a real directory, and creating one is input and output a unit test may not
# perform — which matters most in the test whose whole point is that building a
# capability opens nothing. It is also the stronger check: nothing here can
# succeed by finding a directory that happens to be there.
_ABSENT_MODELS_DIR: Final = "/nonexistent/reachy-groundstation-models"


def _settings(**overrides: object) -> Settings:
    """Build settings pointed at the model directory the weights are in.

    Args:
        overrides: Settings to change from their defaults.

    Returns:
        The settings.
    """
    values: dict[str, object] = {"models_dir": str(model_directory())}
    values.update(overrides)
    return make_settings(**values)


# `pytest_asyncio.fixture` rather than `pytest.fixture`: the suite runs in
# asyncio strict mode, where an async fixture without this decorator is handed
# to the test as a coroutine object rather than awaited.
@pytest_asyncio.fixture
async def capability() -> AsyncIterator[FaceCapability]:
    """Build and warm up the real capability.

    Yields:
        The capability, closed afterwards.
    """
    require_model(FACE_DETECTION_YUNET)
    built = FaceCapability(_settings())
    await built.warm_up()
    try:
        yield built
    finally:
        await built.aclose()


async def _faces(capability: FaceCapability, fixture: str) -> FaceDetections:
    """Run one fixture through a capability.

    Args:
        capability: The warmed-up capability.
        fixture: The committed image to answer.

    Returns:
        The payload, narrowed to the face payload it must be.
    """
    payload = await capability.process(fixture_frame(fixture))
    assert isinstance(payload, FaceDetections)
    return payload


def test_the_capability_negotiates_under_the_contract_s_name() -> None:
    """Routing is by name against the registry, and the name is the contract's."""
    descriptor = FaceCapability(_settings()).descriptor
    assert descriptor.name == FACE_CAPABILITY
    assert descriptor.version == FACE_VERSION


def test_building_the_capability_opens_nothing() -> None:
    """The registry builds every capability before warming any of them.

    A constructor that opened its model would make a missing file a build
    failure rather than a warm-up failure, and would do input and output while
    the composition root is still being assembled. The directory named does not
    exist, so a constructor that looked would fail here.
    """
    assert FaceCapability(_settings(models_dir=_ABSENT_MODELS_DIR)) is not None


#:= docs/specs/groundstation/index.md#req-023-model-files-are-present-in-the-image
#:% The service MUST load every model from a file already present in its deployed
#:% artifact, and MUST NOT fetch model weights over the network at run time.
@pytest.mark.filesystem
@pytest.mark.asyncio
async def test_warming_up_loads_the_model_from_the_artifact(
    capability: FaceCapability,
) -> None:
    """The scenario: a host with no outbound internet access, and a ready service.

    The suite runs with sockets disabled, so a capability that tried to fetch
    its weights would fail here rather than quietly succeeding on a machine that
    happened to have a network.

    Args:
        capability: The warmed-up capability.
    """
    assert (await _faces(capability, "face_single.jpg")).faces


@pytest.mark.filesystem
@pytest.mark.asyncio
async def test_a_frame_before_warm_up_is_refused_rather_than_loading_lazily() -> None:
    """Readiness means warm-up finished, so a lazy load would make it mean nothing."""
    capability = FaceCapability(_settings(models_dir=_ABSENT_MODELS_DIR))
    with pytest.raises(RuntimeError, match="before warming up"):
        await capability.process(fixture_frame("face_single.jpg"))


#:= docs/specs/perception/index.md#req-034-face-detections-report-a-normalised-centre-and-a-confidence
#:% Each face detection MUST report the face's centre in normalised image
#:% coordinates together with a confidence value.
@pytest.mark.filesystem
@pytest.mark.asyncio
async def test_a_face_in_the_upper_left_reports_the_quadrant_it_is_in(
    capability: FaceCapability,
) -> None:
    """The requirement's own scenario, with the signs it names.

    Negative horizontal and positive vertical: the origin is the frame's centre
    and the vertical axis points up. Getting that sign wrong is a whole release
    of the head tilting the wrong way, and it is invisible in any test that only
    checks the magnitude.

    Args:
        capability: The warmed-up capability.
    """
    payload = await _faces(capability, "face_upper_left.jpg")
    assert len(payload.faces) == 1
    face = payload.faces[0]
    assert face.centre.x < 0.0
    assert face.centre.y > 0.0
    assert 0.0 <= face.confidence <= 1.0
    assert face.confidence > 0.6


@pytest.mark.filesystem
@pytest.mark.asyncio
async def test_the_centre_is_where_the_face_was_drawn(
    capability: FaceCapability,
) -> None:
    """A face drawn in the middle reports the middle, not merely something.

    Args:
        capability: The warmed-up capability.
    """
    payload = await _faces(capability, "face_single.jpg")
    assert len(payload.faces) == 1
    # Drawn centred at (160, 118) of a 320 by 240 frame, which normalises to
    # very nearly the origin.
    assert payload.faces[0].centre.x == pytest.approx(0.0, abs=0.05)
    assert payload.faces[0].centre.y == pytest.approx(0.02, abs=0.05)


@pytest.mark.filesystem
@pytest.mark.asyncio
async def test_two_faces_are_reported_as_two(capability: FaceCapability) -> None:
    """Suppression removes duplicates of one face, not the second face.

    Args:
        capability: The warmed-up capability.
    """
    payload = await _faces(capability, "face_pair.jpg")
    assert len(payload.faces) == 2
    # And they are at different places, which is the part a collapsed
    # suppression would get wrong while still reporting two.
    assert abs(payload.faces[0].centre.x - payload.faces[1].centre.x) > 0.4


#:= docs/specs/perception/index.md#req-035-detection-output-is-independent-of-input-resolution
#:% The same scene captured at different resolutions MUST produce detections whose
#:% reported positions agree within a stated tolerance.
@pytest.mark.filesystem
@pytest.mark.asyncio
async def test_the_same_scene_at_two_resolutions_reports_the_same_places(
    capability: FaceCapability,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The requirement's scenario: one scene, two capture resolutions.

    Args:
        capability: The warmed-up capability.
        capsys: Used to report the observed disagreement alongside the verdict.
    """
    full = await _faces(capability, "scene_full.jpg")
    half = await _faces(capability, "scene_half.jpg")

    assert len(full.faces) == len(half.faces) == 2
    ordered_full = sorted(full.faces, key=lambda face: face.centre.x)
    ordered_half = sorted(half.faces, key=lambda face: face.centre.x)

    worst = max(
        max(abs(a.centre.x - b.centre.x), abs(a.centre.y - b.centre.y))
        for a, b in zip(ordered_full, ordered_half, strict=True)
    )
    with capsys.disabled():
        # Printed rather than only asserted: the tolerance above is stated
        # against a measured distribution, so the measurement belongs in the
        # run's output where a reviewer can watch it move.
        print(
            f"\nresolution independence: 640x480 against 320x240, maximum "
            f"centre disagreement {worst:.6f} normalised units "
            f"(tolerance {_RESOLUTION_TOLERANCE})",
        )
    assert worst <= _RESOLUTION_TOLERANCE


#:= docs/specs/robot-link/index.md#req-013-an-empty-result-is-a-valid-result
#:% A result message carrying no detections MUST be treated as a successful result
#:% for that frame.
@pytest.mark.filesystem
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fixture",
    ["negative_wall.jpg", "negative_clutter.jpg", "negative_shelves.jpg"],
)
async def test_a_scene_with_no_face_answers_with_no_faces(
    capability: FaceCapability,
    fixture: str,
) -> None:
    """An empty answer is a successful answer, and it is what these scenes get.

    Args:
        capability: The warmed-up capability.
        fixture: A committed scene with nobody in it.
    """
    assert (await _faces(capability, fixture)).faces == ()


#:= docs/specs/perception/index.md#req-039-detection-thresholds-are-configuration
#:% The confidence threshold for each detector MUST be settable without rebuilding
#:% the artifact.
@pytest.mark.filesystem
@pytest.mark.asyncio
async def test_raising_the_threshold_takes_effect(capability: FaceCapability) -> None:
    """The operator's scenario: too many low-confidence faces, so raise the bar.

    Args:
        capability: A capability at the default threshold, for comparison.
    """
    strict = FaceCapability(_settings(face_score_threshold=0.999))
    await strict.warm_up()
    try:
        loose_payload = await _faces(capability, "face_single.jpg")
        strict_payload = await _faces(strict, "face_single.jpg")
    finally:
        await strict.aclose()
    assert loose_payload.faces
    assert strict_payload.faces == ()


@pytest.mark.filesystem
@pytest.mark.asyncio
async def test_the_capability_does_not_write_to_the_frame_it_was_given(
    capability: FaceCapability,
) -> None:
    """One decode is shared by every agreed capability in the session.

    Args:
        capability: The warmed-up capability.
    """
    frame = fixture_frame("face_unaligned.jpg")
    before = frame.image.copy()
    await capability.process(frame)
    assert np.array_equal(frame.image, before)


@pytest.mark.filesystem
@pytest.mark.asyncio
async def test_a_frame_whose_dimensions_need_padding_is_answered(
    capability: FaceCapability,
) -> None:
    """Neither dimension of this fixture is a multiple of the model's stride.

    Args:
        capability: The warmed-up capability.
    """
    frame = fixture_frame("face_unaligned.jpg")
    assert frame.width % 32 != 0
    assert frame.height % 32 != 0
    payload = await _faces(capability, "face_unaligned.jpg")
    assert len(payload.faces) == 1
    # And the reported centre is inside the frame rather than out in the padding.
    assert -1.0 < payload.faces[0].centre.x < 1.0
    assert -1.0 < payload.faces[0].centre.y < 1.0


@pytest.mark.filesystem
@pytest.mark.asyncio
async def test_warming_up_fails_loudly_when_the_model_is_not_in_the_artifact(
    tmp_path: Path,
) -> None:
    """The registry contains this: the capability goes unhealthy, nothing else does.

    Args:
        tmp_path: An empty model directory.
    """
    capability = FaceCapability(_settings(models_dir=str(tmp_path)))
    with pytest.raises(ModelStoreError, match="MODELS_DIR"):
        await capability.warm_up()
    # And closing after a failed warm-up is still safe, whether the failure
    # happened before a runtime existed or after one was opened.
    await capability.aclose()


@pytest.mark.filesystem
@pytest.mark.asyncio
async def test_a_runtime_whose_warm_up_failed_is_released_rather_than_leaked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A capability that never became ready still opened a session and a thread.

    The registry records a failed warm-up as unhealthy and carries on, so
    nothing will ever call this capability again — and `aclose` could not reach
    a runtime that warm-up had opened but not yet assigned. It would hold its
    session and its worker thread for the life of the process.

    Args:
        monkeypatch: Used to make warm-up fail after the runtime exists, which
            is the only window in which the leak is possible.
    """
    require_model(FACE_DETECTION_YUNET)
    capability = FaceCapability(_settings())
    opened: list[ModelRuntime] = []

    async def _fail(runtime: ModelRuntime, shape: tuple[int, ...]) -> None:
        """Record the opened runtime, then fail the way a bad shape would.

        Args:
            runtime: The runtime being warmed up.
            shape: The shape it was asked to warm up at, unused.

        Raises:
            RuntimeError: Always.
        """
        del shape
        opened.append(runtime)
        message = "warm-up failed"
        raise RuntimeError(message)

    monkeypatch.setattr(ModelRuntime, "warm_up", _fail)
    with pytest.raises(RuntimeError, match="warm-up failed"):
        await capability.warm_up()

    assert len(opened) == 1
    # Its executor is shut down, which is the observable half of "released": a
    # runtime still holding its worker thread would accept work here.
    with pytest.raises(RuntimeError, match="shutdown"):
        await opened[0].infer({})


@pytest.mark.filesystem
@pytest.mark.asyncio
async def test_closing_twice_is_not_an_error() -> None:
    """Shutdown paths run more than once and must not care."""
    require_model(FACE_DETECTION_YUNET)
    capability = FaceCapability(_settings())
    await capability.warm_up()
    await capability.aclose()
    await capability.aclose()
