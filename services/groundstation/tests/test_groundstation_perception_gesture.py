"""Gesture recognition: the two stages, the sampling rate, and the measurement.

The interesting test in this file is the last pair. Perception REQ-037 requires
that the capability be evaluated against scenes with no hands in them and that
the false-positive rate be *reported as a number*, whether or not the number is
acceptable — because the predecessor's classifier reported hand signals at 0.9
confidence in an empty room and nobody had a number to argue with.

**This build wires no gesture model, so the number it reports is zero, and that
zero is not a result about any model.** It is a fact about a capability with
nothing behind it. The report says so in the line it prints, and the test after
it proves the harness is capable of reporting something other than zero — an
evaluation that could only ever say "no false positives" would be worthless the
day a candidate model arrives.

Everything else here is ordinary: the two stages are exercised through scripted
stand-ins, because there is no model to exercise them with and a stand-in is
honest about which of the two things it measures.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import numpy as np
import pytest
from groundstation_perception_support import (
    NEGATIVE_FIXTURES,
    NegativesReport,
    ScriptedClassifier,
    ScriptedHands,
    evaluate_negatives,
    fixture_frame,
    solid_image,
)
from groundstation_support import make_header, make_settings

from reachy_contracts import GESTURE_CAPABILITY, GestureDetection, GestureDetections
from reachy_groundstation.capabilities.perception.gesture import (
    GESTURE_VERSION,
    GestureCapability,
    HandRegion,
    crop,
)
from reachy_groundstation.ports import DecodedFrame

_WAVE = GestureDetection(label="wave", confidence=0.92)
_UNCERTAIN = GestureDetection(label="wave", confidence=0.10)


def _frame(sequence: int = 0, height: int = 64, width: int = 96) -> DecodedFrame:
    """Build a plain frame to hand a capability.

    Args:
        sequence: The frame's number, which the sampling interval is measured
            against.
        height: How many rows.
        width: How many columns.

    Returns:
        The frame.
    """
    return DecodedFrame(
        header=make_header(sequence),
        image=solid_image(height, width),
    )


async def _gestures(
    capability: GestureCapability,
    frame: DecodedFrame,
) -> GestureDetections:
    """Run one frame through a capability.

    Args:
        capability: The capability to ask.
        frame: The frame to ask about.

    Returns:
        The payload, narrowed to the gesture payload it must be.
    """
    payload = await capability.process(frame)
    assert isinstance(payload, GestureDetections)
    return payload


def test_the_capability_negotiates_under_the_contract_s_name() -> None:
    """Routing is by name against the registry, and the name is the contract's."""
    descriptor = GestureCapability(make_settings()).descriptor
    assert descriptor.name == GESTURE_CAPABILITY
    assert descriptor.version == GESTURE_VERSION


def test_this_build_wires_no_gesture_model() -> None:
    """The perception spec defers the model choice, and this is that deferral.

    A build that started wiring one without the negatives evaluation having been
    run against it would be repeating the predecessor's mistake, so the state is
    asserted rather than assumed.
    """
    assert GestureCapability(make_settings()).wired is False


@pytest.mark.asyncio
async def test_an_unwired_capability_answers_with_no_gestures() -> None:
    """Not "fails to answer": robot link REQ-013 makes an empty result a result."""
    capability = GestureCapability(make_settings(gesture_sample_interval=1))
    assert (await _gestures(capability, _frame())).gestures == ()


@pytest.mark.asyncio
async def test_a_capability_missing_only_the_classifier_answers_with_nothing() -> None:
    """Half a two-stage pipeline is not a pipeline, and it is not an error either."""
    hands = ScriptedHands(HandRegion(x=0.0, y=0.0, width=10.0, height=10.0))
    capability = GestureCapability(
        make_settings(gesture_sample_interval=1),
        hands=hands,
    )
    assert (await _gestures(capability, _frame())).gestures == ()
    assert capability.wired is False


#:= docs/specs/perception/index.md#req-039-detection-thresholds-are-configuration
#:% The confidence threshold for each detector MUST be settable without rebuilding
#:% the artifact.
@pytest.mark.asyncio
async def test_both_stages_run_and_the_crop_reaches_the_classifier() -> None:
    """The arrangement is detector then classifier over the region it found."""
    hands = ScriptedHands(HandRegion(x=10.0, y=20.0, width=30.0, height=16.0))
    classifier = ScriptedClassifier(_WAVE)
    capability = GestureCapability(
        make_settings(gesture_sample_interval=1),
        hands=hands,
        gestures=classifier,
    )
    payload = await _gestures(capability, _frame())
    assert payload.gestures == (_WAVE,)
    assert hands.seen == 1
    # Rows then columns, which is the region asked for and not the whole frame.
    assert classifier.crops == [(16, 30)]
    assert capability.wired is True


@pytest.mark.asyncio
async def test_a_gesture_below_the_threshold_is_not_reported() -> None:
    """Thresholding is configuration, and it is applied to what comes back."""
    capability = GestureCapability(
        make_settings(gesture_sample_interval=1, gesture_score_threshold=0.5),
        hands=ScriptedHands(HandRegion(x=0.0, y=0.0, width=20.0, height=20.0)),
        gestures=ScriptedClassifier(_UNCERTAIN),
    )
    assert (await _gestures(capability, _frame())).gestures == ()


@pytest.mark.asyncio
async def test_lowering_the_threshold_admits_the_same_gesture() -> None:
    """The setting has to actually do something in both directions."""
    capability = GestureCapability(
        make_settings(gesture_sample_interval=1, gesture_score_threshold=0.05),
        hands=ScriptedHands(HandRegion(x=0.0, y=0.0, width=20.0, height=20.0)),
        gestures=ScriptedClassifier(_UNCERTAIN),
    )
    assert (await _gestures(capability, _frame())).gestures == (_UNCERTAIN,)


@pytest.mark.asyncio
async def test_a_classifier_that_recognises_nothing_reports_nothing() -> None:
    """A hand the classifier cannot name is an empty result, not a failure."""
    capability = GestureCapability(
        make_settings(gesture_sample_interval=1),
        hands=ScriptedHands(HandRegion(x=0.0, y=0.0, width=20.0, height=20.0)),
        gestures=ScriptedClassifier(None),
    )
    assert (await _gestures(capability, _frame())).gestures == ()


@pytest.mark.asyncio
async def test_no_hand_means_the_classifier_is_never_asked() -> None:
    """The first stage is what bounds the second stage's cost."""
    classifier = ScriptedClassifier(_WAVE)
    capability = GestureCapability(
        make_settings(gesture_sample_interval=1),
        hands=ScriptedHands(),
        gestures=classifier,
    )
    assert (await _gestures(capability, _frame())).gestures == ()
    assert classifier.crops == []


def test_the_sampling_interval_is_configuration_and_not_a_constant() -> None:
    """The predecessor's every-fourth-frame is a default here, not a rule."""
    every_fourth = GestureCapability(make_settings(gesture_sample_interval=4))
    assert [every_fourth.samples(n) for n in range(5)] == [
        True,
        False,
        False,
        False,
        True,
    ]
    every_frame = GestureCapability(make_settings(gesture_sample_interval=1))
    assert all(every_frame.samples(n) for n in range(5))


def test_the_default_sampling_interval_is_the_documented_one() -> None:
    """Four, as the predecessor sampled. Change the setting and this together."""
    assert make_settings().gesture_sample_interval == 4


@pytest.mark.asyncio
async def test_a_frame_between_samples_costs_nothing_and_answers_with_nothing() -> None:
    """The interval is a cost bound, so an unsampled frame runs neither stage.

    It answers with an empty payload rather than repeating the last
    classification: a stale conclusion delivered in a result carrying this
    frame's capture token would look exactly like a fresh one, which is the
    cross-clock illusion the opaque token exists to prevent.
    """
    hands = ScriptedHands(HandRegion(x=0.0, y=0.0, width=20.0, height=20.0))
    capability = GestureCapability(
        make_settings(gesture_sample_interval=4),
        hands=hands,
        gestures=ScriptedClassifier(_WAVE),
    )
    assert (await _gestures(capability, _frame(sequence=0))).gestures == (_WAVE,)
    for sequence in (1, 2, 3):
        assert (await _gestures(capability, _frame(sequence=sequence))).gestures == ()
    assert (await _gestures(capability, _frame(sequence=4))).gestures == (_WAVE,)
    assert hands.seen == 2


@pytest.mark.asyncio
async def test_sampling_follows_the_frame_number_and_not_a_counter() -> None:
    """Frames are dropped under load, and a counter would drift when they are."""
    hands = ScriptedHands(HandRegion(x=0.0, y=0.0, width=20.0, height=20.0))
    capability = GestureCapability(
        make_settings(gesture_sample_interval=4),
        hands=hands,
        gestures=ScriptedClassifier(_WAVE),
    )
    # Sequences 5, 6 and 7 never arrived, which is what a shed frame looks like.
    for sequence in (0, 1, 2, 3, 4, 8):
        await capability.process(_frame(sequence=sequence))
    assert hands.seen == 3


def test_a_region_is_cut_to_the_size_asked_for() -> None:
    """Rows then columns, and the pixels are the ones inside the region."""
    image = solid_image(40, 60)
    image[10:20, 5:25] = 200
    cut = crop(image, HandRegion(x=5.0, y=10.0, width=20.0, height=10.0))
    assert cut is not None
    assert cut.shape[:2] == (10, 20)
    assert np.all(cut == 200)


def test_a_region_running_off_the_frame_is_held_to_it() -> None:
    """A negative slice start means something else entirely in numpy."""
    cut = crop(
        solid_image(40, 60), HandRegion(x=-30.0, y=-20.0, width=50.0, height=40.0)
    )
    assert cut is not None
    assert cut.shape[:2] == (20, 20)


def test_a_region_wholly_outside_the_frame_is_skipped() -> None:
    """Nothing to classify is not an empty array to classify."""
    assert (
        crop(solid_image(40, 60), HandRegion(x=80.0, y=0.0, width=10.0, height=10.0))
        is None
    )
    assert (
        crop(solid_image(40, 60), HandRegion(x=0.0, y=0.0, width=0.0, height=10.0))
        is None
    )


@pytest.mark.asyncio
async def test_an_uncroppable_region_does_not_stop_the_others() -> None:
    """One bad region costs its own classification and nothing else's."""
    classifier = ScriptedClassifier(_WAVE)
    capability = GestureCapability(
        make_settings(gesture_sample_interval=1),
        hands=ScriptedHands(
            HandRegion(x=500.0, y=500.0, width=10.0, height=10.0),
            HandRegion(x=0.0, y=0.0, width=12.0, height=8.0),
        ),
        gestures=classifier,
    )
    assert (await _gestures(capability, _frame())).gestures == (_WAVE,)
    assert classifier.crops == [(8, 12)]


@pytest.mark.asyncio
async def test_closing_releases_both_stages() -> None:
    """Whatever a stage holds is released when the service stops."""
    hands = ScriptedHands()
    classifier = ScriptedClassifier(None)
    capability = GestureCapability(make_settings(), hands=hands, gestures=classifier)
    await capability.aclose()
    assert (hands.closed, classifier.closed) == (True, True)


@pytest.mark.asyncio
async def test_closing_an_unwired_capability_is_not_an_error() -> None:
    """This build ships unwired, and shutdown runs on it like any other."""
    await GestureCapability(make_settings()).aclose()


#:= docs/specs/perception/index.md#req-037-gesture-accuracy-is-measured-against-a-negatives-fixture-set
#:% The gesture capability MUST be evaluated against a fixture set containing scenes
#:% with no hands present, and its false-positive rate on that set MUST be reported
#:% by the test suite.
@pytest.mark.filesystem
@pytest.mark.asyncio
async def test_the_negatives_evaluation_reports_a_false_positive_rate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The requirement's scenario, run against the capability this build ships.

    The number is reported whether or not it is acceptable — that is the whole
    requirement. What it says today is that a capability with no model behind it
    claims nothing, which is true and is not evidence about any candidate model;
    the report says as much in the line it prints, and the test below is what
    shows the harness can report something else.

    Args:
        capsys: Used to print the measurement where a reviewer reads it.
    """
    capability = GestureCapability(make_settings(gesture_sample_interval=1))
    report = await evaluate_negatives(capability)

    with capsys.disabled():
        print(
            f"\n{report.render()}"
        )  # not selected; this is the number perception REQ-037 asks the suite to report

    assert report.scenes == len(NEGATIVE_FIXTURES)
    assert report.scenes >= 6
    assert 0.0 <= report.rate <= 1.0
    assert report.wired is False
    assert report.rate == 0.0


@pytest.mark.filesystem
@pytest.mark.asyncio
async def test_the_negatives_evaluation_detects_a_model_that_claims_a_hand(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An evaluation that could only ever report zero would measure nothing.

    This is the predecessor's failure staged deliberately: stages that report a
    confident gesture in an empty room. The harness has to name every one of
    those scenes, and it does.

    Args:
        capsys: Used to print what the harness said about the staged model.
    """
    confident = GestureCapability(
        make_settings(gesture_sample_interval=1),
        hands=ScriptedHands(HandRegion(x=0.0, y=0.0, width=40.0, height=40.0)),
        gestures=ScriptedClassifier(
            GestureDetection(label="fist", confidence=0.9),
        ),
    )
    report = await evaluate_negatives(confident)

    with capsys.disabled():
        print(
            f"\nharness self-check, {report.render()}"
        )  # not selected; printed beside the measurement above so the two read together

    assert report.rate == 1.0
    assert report.false_positives == report.scenes == len(NEGATIVE_FIXTURES)
    assert report.wired is True


@pytest.mark.filesystem
@pytest.mark.asyncio
async def test_a_negatives_scene_reaches_the_capability_as_a_real_frame() -> None:
    """The fixtures are decoded by the service's own decoder, not conjured."""
    frame = fixture_frame("negative_static.jpg")
    assert (frame.height, frame.width) == (240, 320)
    assert frame.image.dtype == np.uint8


def test_the_report_of_an_empty_evaluation_does_not_divide_by_zero() -> None:
    """A rate over no scenes is zero rather than an exception."""
    report = NegativesReport(scenes=0, false_positives=0, detections=0, wired=False)
    assert report.rate == 0.0
    assert "0.000" in report.render()


def test_the_report_says_whether_a_model_was_behind_the_number() -> None:
    """A zero measured with nothing wired must not read as a zero measured."""
    unwired = NegativesReport(scenes=6, false_positives=0, detections=0, wired=False)
    wired = NegativesReport(scenes=6, false_positives=0, detections=0, wired=True)
    assert "NO gesture model wired" in unwired.render()
    assert "NO gesture model wired" not in wired.render()
