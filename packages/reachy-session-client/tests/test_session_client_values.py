"""The small values the session is built from: secrets, delays and clocks.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import pytest
from session_client_support import CREDENTIAL as SECRET
from session_client_support import ManualClock

from reachy_contracts import CaptureTimestamp, FaceDetection, FaceDetections
from reachy_contracts import NormalisedPoint as Point
from reachy_session_client import (
    REDACTED,
    Backoff,
    Credential,
    MonotonicStamps,
    count_detections,
    describe_validation,
    result_model_for,
)


#:= docs/specs/reachyctl/index.md#req-059-secrets-are-never-written-to-output
#:% The tool MUST NOT write credentials to its output, its logs, or its error
#:% messages.
def test_a_credential_does_not_render_itself() -> None:
    """Neither `repr` nor `str` shows the value, which is where it escapes."""
    held = Credential(SECRET)

    assert SECRET not in repr(held)
    assert SECRET not in str(held)
    assert SECRET not in f"{held}"
    assert SECRET not in f"{held!r}"
    assert REDACTED in repr(held)
    assert held.reveal() == SECRET


def test_a_credential_inside_a_container_still_does_not_render_itself() -> None:
    """A container reprs its members, which is how a secret reaches a log line."""
    held = {"credential": Credential(SECRET), "url": "ws://198.51.100.10/v1/session"}

    assert SECRET not in repr(held)


def test_an_empty_credential_is_refused_where_it_is_read() -> None:
    """Nothing configured fails here, not at an authentication check."""
    with pytest.raises(ValueError, match="must not be empty"):
        Credential("")


#:= docs/specs/robot-link/index.md#req-018-reconnection-is-automatic-and-rate-limited
#:% A client MUST re-establish a dropped session automatically, and MUST increase
#:% the delay between successive failed attempts up to a bound.
def test_the_delay_grows_and_then_stops_growing() -> None:
    """It doubles until the bound, and stays there however long the outage."""
    backoff = Backoff(initial_seconds=0.5, multiplier=2.0, maximum_seconds=4.0)

    growing = [backoff.delay(attempt) for attempt in range(1, 5)]
    assert growing == [0.5, 1.0, 2.0, 4.0]

    # Several minutes of outage at four seconds an attempt.
    bounded = {backoff.delay(attempt) for attempt in range(5, 100)}
    assert bounded == {4.0}


#:= docs/specs/robot-link/index.md#req-018-reconnection-is-automatic-and-rate-limited
#:% A client MUST re-establish a dropped session automatically, and MUST increase
#:% the delay between successive failed attempts up to a bound.
def test_the_delay_survives_an_outage_of_any_length() -> None:
    """Reconnection is unbounded, so the delay has to answer for any attempt.

    Float exponentiation raises `OverflowError` rather than saturating, and
    with the default policy the exponent reaches 1024 after about eight hours
    of outage — the second scenario REQ-018 names. An exception there would
    leave the reconnection loop and end the session for good, which is the
    opposite of what the requirement asks for.
    """
    for attempt in (1_000, 10_000, 10**6, 10**9):
        assert Backoff().delay(attempt) == Backoff().maximum_seconds


#:= docs/specs/robot-link/index.md#req-018-reconnection-is-automatic-and-rate-limited
#:% A client MUST re-establish a dropped session automatically, and MUST increase
#:% the delay between successive failed attempts up to a bound.
@pytest.mark.parametrize(
    ("initial", "multiplier", "maximum"),
    [
        (0.0, 2.0, 30.0),
        (-1.0, 2.0, 30.0),
        (0.5, 0.5, 30.0),
        (0.5, 2.0, 0.25),
        # The two ways of writing a delay that never increases. REQ-018 asks
        # for one that grows as well as one that is bounded, so a policy which
        # cannot grow is refused where it is built rather than reached by a
        # robot that retries at the same interval for an afternoon.
        (1.0, 1.0, 5.0),
        (5.0, 2.0, 5.0),
        # And the non-finite values, which are the same defect wearing a
        # disguise: every comparison against `nan` is false, so `nan` satisfies
        # each of the rules above rather than failing one. An infinite
        # multiplier is the worst of them because it raises nothing — it makes
        # the exponent clamp zero and yields the first delay forever, which is
        # exactly the constant delay the two cases above refuse. The others
        # raise out of `delay`, inside the reconnection loop, which is the one
        # place an exception ends the session for good.
        (float("inf"), 2.0, 30.0),
        (float("nan"), 2.0, 30.0),
        (0.5, float("inf"), 30.0),
        (0.5, float("nan"), 30.0),
        (0.5, 2.0, float("inf")),
        (0.5, 2.0, float("nan")),
    ],
)
def test_a_policy_that_would_not_grow_or_would_not_wait_is_refused(
    initial: float,
    multiplier: float,
    maximum: float,
) -> None:
    """Every policy REQ-018 could not be satisfied by is refused where it is built.

    `Backoff` is public API and the robot adapter that constructs one is a
    later change, so the check has to cover the value domain rather than the
    handful of policies this repository happens to write down today.

    Args:
        initial: What the first retry would wait.
        multiplier: What each subsequent wait would be multiplied by.
        maximum: The bound.
    """
    with pytest.raises(ValueError, match=r"delay|multiplier|bound"):
        Backoff(
            initial_seconds=initial,
            multiplier=multiplier,
            maximum_seconds=maximum,
        )


def test_attempts_are_counted_from_one() -> None:
    """A zeroth attempt is a caller that has miscounted, not a zero delay."""
    with pytest.raises(ValueError, match="counted from one"):
        Backoff().delay(0)


def test_capture_tokens_never_go_backwards() -> None:
    """Successive stamps are ordered, because the source is monotonic."""
    clock = ManualClock(start=10.0)
    stamps = MonotonicStamps(clock)

    first = stamps.stamp()
    clock.advance(0.25)
    second = stamps.stamp()

    assert float(first.root) < float(second.root)


#:= docs/specs/robot-link/index.md#req-016-results-return-the-capture-timestamp-unaltered
#:% Every result MUST carry the capture timestamp of the frame it derives from,
#:% byte-for-byte as the capturing side supplied it, so that the capturing side can
#:% compute the result's age against the same clock that produced it.
def test_a_returned_token_is_aged_against_the_clock_that_minted_it() -> None:
    """The subtraction is single-clock, however many machines it crossed."""
    clock = ManualClock(start=10.0)
    stamps = MonotonicStamps(clock)
    token = stamps.stamp()

    clock.advance(0.75)

    assert stamps.age_of(token, clock()) == pytest.approx(0.75)


def test_a_token_this_client_did_not_mint_has_no_age() -> None:
    """There is no clock to compare it against, so no number is returned."""
    stamps = MonotonicStamps(ManualClock())

    assert stamps.age_of(CaptureTimestamp("2026-08-21T00:00:00Z"), 0.0) is None


def test_detections_are_counted_without_naming_a_capability() -> None:
    """Counting the tuples keeps this true for the next capability too."""
    two = FaceDetections(
        faces=(
            FaceDetection(centre=Point(x=0.0, y=0.0), confidence=0.5),
            FaceDetection(centre=Point(x=0.5, y=0.5), confidence=0.5),
        ),
    )

    assert count_detections(two) == 2
    assert count_detections(FaceDetections()) == 0


def test_a_capability_this_build_does_not_know_has_no_result_type() -> None:
    """Which is what makes it a message to ignore rather than one to refuse."""
    assert result_model_for("face") is not None
    assert result_model_for("telepathy") is None


def test_a_failure_with_no_field_information_is_named_by_its_kind() -> None:
    """`describe_validation` is fed whatever a parse raised, not only pydantic's."""
    assert describe_validation(ValueError("something went wrong")) == "ValueError"
