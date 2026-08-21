"""The corpus, asserted in both directions.

These two tests are the reason the corpus exists. A pattern tightened to catch
one more shape has to keep every allowed line allowed, and a pattern loosened to
stop a false positive has to keep every caught line caught; asserting only the
first direction is how a hygiene gate quietly starts rejecting documentation.
"""

from __future__ import annotations

import pytest

from reachy_hygiene.corpus import MUST_BE_ALLOWED, MUST_BE_CAUGHT
from reachy_hygiene.patterns import ALLOW_MARKER, RULES, is_documentation_value
from reachy_hygiene.scan import scan_text


@pytest.mark.parametrize("line", MUST_BE_CAUGHT)
def test_every_leaking_corpus_line_is_caught(line: str) -> None:
    """Every corpus line that carries a leak shape produces a finding."""
    assert scan_text(line, "corpus") != []


@pytest.mark.parametrize("line", MUST_BE_ALLOWED)
def test_every_allowed_corpus_line_is_clean(line: str) -> None:
    """No corpus line of legitimate content produces a finding."""
    assert scan_text(line, "corpus") == []


def test_every_rule_is_exercised_by_the_corpus() -> None:
    """The caught half of the corpus covers every rule, not merely most."""
    caught = {
        finding.rule for line in MUST_BE_CAUGHT for finding in scan_text(line, "corpus")
    }
    assert caught == {rule.name for rule in RULES}


def test_the_marker_exempts_the_line_it_is_on() -> None:
    """A line carrying the inline marker is not scanned."""
    line = f"{MUST_BE_CAUGHT[0]}  # {ALLOW_MARKER} synthetic value in a fixture"
    assert scan_text(line, "corpus") == []


@pytest.mark.parametrize(
    "value",
    ["192.0.2.10", "198.51.100.42", "203.0.113.7", "2001:db8::1", "localhost"],
)
def test_documentation_values_are_recognised(value: str) -> None:
    """Reserved values are recognised as documentation regardless of rule."""
    assert is_documentation_value(value)


# The two values below are leak shapes on purpose, so the line holding them
# carries the inline exemption the scanner reads.
@pytest.mark.parametrize(
    "value",
    ["someone@reachy.invalid", "robot.local"],  # leak-scan:allow
)
def test_environment_values_are_not_documentation(value: str) -> None:
    """A value that names an environment is never treated as documentation."""
    assert not is_documentation_value(value)


def test_a_long_line_is_truncated_in_the_excerpt() -> None:
    """A minified file's single enormous line does not flood the log."""
    padding = "x" * 400
    (finding,) = scan_text(f"{padding} {MUST_BE_CAUGHT[0]}", "bundle.js")

    assert finding.excerpt.endswith("…")
    assert len(finding.excerpt) < len(padding)


def test_a_finding_never_carries_the_value_it_found() -> None:
    """The excerpt is redacted, so a public log cannot republish the leak."""
    (finding,) = scan_text(MUST_BE_CAUGHT[0], "corpus")
    _, _, value = MUST_BE_CAUGHT[0].partition("=")
    assert value not in finding.excerpt
    assert "[redacted]" in finding.excerpt
