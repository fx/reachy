"""The small values the session is built from: secrets, delays and clocks.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

from urllib.parse import urlsplit

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
    redact_url,
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
        # each of the rules above rather than failing one, and then makes
        # `delay` answer `nan` — a wait nobody can predict, inside the loop
        # where REQ-018's bounded growth is supposed to hold. An infinite bound
        # or first delay is the same story from the other end.
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


#:= docs/specs/robot-link/index.md#req-018-reconnection-is-automatic-and-rate-limited
#:% A client MUST re-establish a dropped session automatically, and MUST increase
#:% the delay between successive failed attempts up to a bound.
def test_a_policy_spanning_the_whole_float_range_still_answers() -> None:
    """Every value finite and every rule satisfied, and the ratio still is not.

    `maximum / initial` overflows to infinity here while both ends are ordinary
    finite numbers, which is why the bound is decided from the difference of
    two logarithms rather than from the logarithm of a quotient. The delay for
    an early attempt is the first delay, and a late one is the bound; neither
    may raise, because the only caller is the reconnection loop and an
    exception there ends the session for good.
    """
    span = Backoff(initial_seconds=5e-324, multiplier=2.0, maximum_seconds=1e308)

    assert span.delay(1) == 5e-324
    assert span.delay(10**9) == 1e308


#:= docs/specs/robot-link/index.md#req-018-reconnection-is-automatic-and-rate-limited
#:% A client MUST re-establish a dropped session automatically, and MUST increase
#:% the delay between successive failed attempts up to a bound.
def test_an_attempt_count_too_large_to_be_a_float_still_answers() -> None:
    """`attempt` is an `int`, and an `int` has no range for a policy to exceed.

    The bound is decided by comparing the attempt against a float threshold
    rather than by multiplying it into one, because Python compares those two
    exactly while converting an integer this size to a float raises
    `OverflowError` — inside the reconnection loop, which is the one place an
    exception ends the session for good.
    """
    assert Backoff().delay(10**1000) == Backoff().maximum_seconds


#:= docs/specs/reachyctl/index.md#req-059-secrets-are-never-written-to-output
#:% The tool MUST NOT write credentials to its output, its logs, or its error
#:% messages.
@pytest.mark.parametrize(
    "url",
    [
        "wss://someone:example-secret@198.51.100.10/v1/session",
        "ws://198.51.100.10:8080/v1/session?credential=example-secret",
        "wss://198.51.100.10/v1/session#example-secret",
    ],
)
def test_an_address_is_rendered_without_the_parts_a_secret_fits_in(url: str) -> None:
    """The rendering has to be safe on its own, not because a caller validated.

    `validate_session_url` refuses these, so the configured address never
    reaches here carrying one — but that is a property of the caller, and
    `open_websocket` is public API that formats a URL into a connection failure
    without having validated anything. A guarantee that holds only because the
    one caller happens to sanitise first is not a guarantee.

    Args:
        url: An address carrying a secret in one of its three hiding places.
    """
    rendered = redact_url(url)

    assert "example-secret" not in rendered
    assert "someone" not in rendered
    # Still says where the connection was to, which is the whole job.
    assert "198.51.100.10" in rendered


def test_an_address_that_cannot_be_taken_apart_is_replaced_entirely() -> None:
    """Echoing back the string that failed to parse is how one reaches a log."""
    assert redact_url("ws://198.51.100.10:not-a-port/v1/session") == REDACTED
    assert redact_url("not an address at all") == REDACTED
    assert redact_url("") == REDACTED


@pytest.mark.parametrize(
    "url",
    [
        # RFC 3849 documentation prefix, and the loopback. No address belonging
        # to anybody's network enters a tracked file — see the root AGENTS.md.
        "ws://[::1]:8080/v1/session",
        "wss://[2001:db8::1]/v1/session",
        "ws://[fe80::1%25eth0]:8080/v1/session",
        "ws://198.51.100.10:8080/v1/session",
        "wss://example.invalid/v1/session",
    ],
)
def test_a_rendered_address_parses_back_to_the_one_it_came_from(url: str) -> None:
    """What this prints is what somebody pastes, so it has to be an address.

    An IPv6 literal is written in brackets and `hostname` strips them, so
    reassembling without them turns `[::1]:8080` into `::1:8080` — which does
    not parse. Worse is the case with no port: `[2001:db8::1]` becomes an
    address whose host reads as `2001`, which parses and is wrong, and sends
    somebody to debug a host that was never involved.

    Asserted by re-parsing rather than by comparing strings, because the
    property that matters is that the output is usable, not that it is
    spelled a particular way.

    Args:
        url: An address to render and read back.
    """
    rendered = redact_url(url)

    was, now = urlsplit(url), urlsplit(rendered)
    assert now.scheme == was.scheme
    assert now.hostname == was.hostname
    assert now.port == was.port
    assert now.path == was.path


#:= docs/specs/reachyctl/index.md#req-059-secrets-are-never-written-to-output
#:% The tool MUST NOT write credentials to its output, its logs, or its error
#:% messages.
def test_redaction_and_bracketing_compose_on_one_address() -> None:
    """Each is proved alone elsewhere; this is the address that needs both.

    A credential in the user information of an IPv6 address exercises the
    dropping and the re-bracketing at once, which is where a fix to either one
    could quietly undo the other.
    """
    rendered = redact_url("wss://someone:example-secret@[2001:db8::1]:9000/v1/session")

    assert "example-secret" not in rendered
    assert "someone" not in rendered
    assert rendered == "wss://[2001:db8::1]:9000/v1/session"
    assert urlsplit(rendered).hostname == "2001:db8::1"
