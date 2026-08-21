"""What `probe` reports, and what it refuses before opening a session.

The run itself is exercised against a real groundstation over a real transport
in `test_reachyctl_probe_integration.py`, because that is the only kind of
evidence reachyctl REQ-057 accepts. What is tested here is everything either
side of it: the capability strings an operator types, and the shape of the
report both renderings are built from.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

from typing import Final

import pytest

from reachy_contracts import Capability
from reachy_session_client import SessionStats
from reachyctl.errors import ConfigurationError
from reachyctl.probe import (
    DEFAULT_CAPABILITIES,
    FrameOutcome,
    ProbeOutcome,
    ProbePlan,
    parse_capability,
    report_for,
    shortfall,
)

# RFC 5737 TEST-NET-2 — see the root AGENTS.md on what may enter a tracked file.
URL: Final = "ws://198.51.100.10:8080/v1/session"

PLAN: Final = ProbePlan(
    url=URL,
    capabilities=DEFAULT_CAPABILITIES,
    count=2,
    interval=0.0,
    timeout=5.0,
    staleness=1.0,
)


def outcome(
    *frames: FrameOutcome,
    agreed: tuple[str, ...] = ("face",),
    complete: bool = True,
    complaint: str = "",
    stats: SessionStats | None = None,
) -> ProbeOutcome:
    """Build a finished run for the report to be shaped from.

    Args:
        frames: The results that arrived.
        agreed: What negotiation settled on.
        complete: Whether every expected result arrived.
        complaint: Why it stopped short, when it did.
        stats: What the session counted.

    Returns:
        The outcome.
    """
    return ProbeOutcome(
        agreed=agreed,
        frames=frames,
        stats=SessionStats() if stats is None else stats,
        complete=complete,
        complaint=complaint,
    )


@pytest.mark.parametrize(
    ("text", "name", "version"),
    [("face", "face", 1), ("gesture:2", "gesture", 2), ("face:1", "face", 1)],
)
def test_a_capability_is_a_name_and_optionally_a_version(
    text: str,
    name: str,
    version: int,
) -> None:
    """Version one when none is given, because that is what exists.

    Args:
        text: What the operator typed.
        name: The name it should parse to.
        version: The version it should parse to.
    """
    assert parse_capability(text) == Capability(name=name, version=version)


@pytest.mark.parametrize("text", ["Face", "face:", "face:two", "", "face:0", "-face"])
def test_a_capability_the_contract_would_refuse_is_refused_here(text: str) -> None:
    """A typo costs a message rather than a session that agrees to nothing.

    Args:
        text: What the operator typed.
    """
    with pytest.raises(ConfigurationError, match="is not a capability"):
        parse_capability(text)


def test_the_default_offer_is_every_capability_this_build_knows_about() -> None:
    """Which is what makes the report say which of them was actually agreed."""
    assert [named.name for named in DEFAULT_CAPABILITIES] == ["face", "gesture"]
    assert {named.version for named in DEFAULT_CAPABILITIES} == {1}


#:= docs/specs/reachyctl/index.md#req-058-output-is-machine-readable-on-request
#:% Every command that reports results MUST offer a structured output format
#:% suitable for consumption by another program.
def test_the_report_carries_a_row_per_result_and_the_timings_over_them() -> None:
    """One report, from which both the structured and the human rendering follow."""
    report = report_for(
        outcome(
            FrameOutcome(
                sequence=0, capability="face", detections=1, round_trip_ms=10.0
            ),
            FrameOutcome(
                sequence=1, capability="face", detections=0, round_trip_ms=30.0
            ),
            FrameOutcome(
                sequence=2, capability="face", detections=2, round_trip_ms=20.0
            ),
        ),
        PLAN,
        "3 recorded frames from a directory",
    )

    assert report.command == "probe"
    assert report.ok is True
    assert len(report.rows) == 3
    assert report.rows[1]["detections"] == 0
    assert report.data["round_trip_ms_fastest"] == 10.0
    assert report.data["round_trip_ms_median"] == 20.0
    assert report.data["round_trip_ms_slowest"] == 30.0
    assert report.data["offered"] == ("face", "gesture")
    assert report.data["agreed"] == ("face",)


def test_a_run_with_no_measurable_timing_reports_no_timing() -> None:
    """Rather than a zero, which reads as a measurement somebody took."""
    report = report_for(
        outcome(
            FrameOutcome(
                sequence=0,
                capability="face",
                detections=0,
                round_trip_ms=None,
            ),
        ),
        PLAN,
        "one frame",
    )

    assert report.data["round_trip_ms_fastest"] is None
    assert report.data["round_trip_ms_median"] is None


#:= docs/specs/robot-link/index.md#req-013-an-empty-result-is-a-valid-result
#:% A result message carrying no detections MUST be treated as a successful result
#:% for that frame.
def test_a_run_where_nothing_was_detected_is_still_a_successful_run() -> None:
    """Zero detections is what the groundstation found, not what went wrong."""
    report = report_for(
        outcome(
            FrameOutcome(
                sequence=0, capability="face", detections=0, round_trip_ms=8.0
            ),
        ),
        PLAN,
        "one frame",
    )

    assert report.ok is True
    assert report.rows[0]["detections"] == 0


def test_a_run_that_stopped_short_says_what_was_missing() -> None:
    """A probe that reported success on half the answers would be worse than none."""
    report = report_for(
        outcome(complete=False, complaint="1 of 4 expected results arrived"),
        PLAN,
        "two frames",
    )

    assert report.ok is False
    assert report.summary == "1 of 4 expected results arrived"


def test_what_the_session_counted_is_carried_into_the_report() -> None:
    """Drops, supersessions and reconnections are what a diagnostic is read for."""
    stats = SessionStats(
        frames_submitted=4,
        frames_dropped=2,
        results_applied=4,
        results_superseded=1,
        results_ignored=0,
        errors_received=1,
        reconnections=1,
    )

    report = report_for(outcome(stats=stats), PLAN, "four frames")

    assert report.data["frames_dropped"] == 2
    assert report.data["results_superseded"] == 1
    assert report.data["errors_received"] == 1
    assert report.data["reconnections"] == 1


def test_a_shortfall_names_what_was_missing() -> None:
    """The summary an operator reads when a run did not get every answer."""
    assert "1 of 4" in shortfall(1, 4)


def test_a_run_where_no_frame_ever_left_says_that_instead() -> None:
    """`0 of 0 expected results` would read as a run that asked for nothing."""
    assert shortfall(0, 0) == "no frame reached the groundstation"
