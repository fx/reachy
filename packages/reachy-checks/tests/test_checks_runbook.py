"""The troubleshooting runbook, held to the registry it is keyed to.

`registry.py` says the identifiers and the remediation strings are a published
interface, and that the runbook shares this text rather than restating it. This
module is what makes that a check rather than a request: it reads
`docs/ops/troubleshooting.md` and requires its per-check sections to be exactly
the registered identifiers, in the registry's order, each quoting that check's
remediation word for word.

Two copies of "how do I fix this?" drift, and the copy in the tool is the one
people actually see. Holding the document to the registry means the document is
either right or red — a paraphrase fails here rather than being discovered by
somebody following it.

Whitespace is normalised before comparing, and only whitespace: the document
wraps its blockquotes to fit and the registry writes one long string, so an
exact-bytes comparison would forbid the document from being readable. Every word
still has to match.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from reachy_checks.checks_export import DOCTOR_CHECKS_PATH, anchor_for
from reachy_checks.registry import CHECKS, Check

RUNBOOK: Final = (
    Path(__file__).resolve().parents[3] / "docs" / "ops" / "troubleshooting.md"
)

# Where the per-check sections begin and end. Everything between these two
# headings is keyed to the registry; the headings after are ordinary prose about
# failures no check covers, and are none of this module's business.
_REGION_START: Final = "\n## The checks\n"
_REGION_END: Final = "\n## Failures `doctor` does not have a check for\n"

# The line a section puts immediately before the remediation it quotes. Written
# out rather than derived, because it is the anchor the parsing below depends on
# and a section that dropped it would otherwise silently stop being checked.
_REMEDIATION_MARKER: Final = "**Remediation, as `doctor` prints it:**"

_HEADING: Final = re.compile(r"^### (.+)$", re.MULTILINE)


def _document() -> str:
    """Read the runbook.

    Returns:
        Its whole text.
    """
    return RUNBOOK.read_text(encoding="utf-8")


def _region(document: str) -> str:
    """Cut out the part of the runbook that is keyed to the registry.

    Args:
        document: The whole runbook.

    Returns:
        The text between the two section headings.
    """
    _, _, after = document.partition(_REGION_START)
    region, separator, _ = after.partition(_REGION_END)
    assert separator, f"{RUNBOOK.name} no longer carries both region headings"
    return region


def _sections(region: str) -> dict[str, str]:
    """Split the region into one body per heading.

    Args:
        region: The text between the two region headings.

    Returns:
        A mapping of heading to the text under it, up to the next heading.
    """
    headings = list(_HEADING.finditer(region))
    bodies: dict[str, str] = {}
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(region)
        bodies[heading.group(1)] = region[heading.end() : end]
    return bodies


def _normalised(text: str) -> str:
    """Collapse every run of whitespace, so wrapping is not part of the compare.

    Args:
        text: What to normalise.

    Returns:
        The same words, single-spaced and stripped.
    """
    return " ".join(text.split())


def _quoted_remediation(body: str) -> str:
    """Pull the remediation a section quotes out of it.

    Args:
        body: The text under one check's heading.

    Returns:
        The blockquote following the remediation marker, normalised.
    """
    _, marker, after = body.partition(_REMEDIATION_MARKER)
    assert marker, "the section carries no remediation marker"
    quoted = [
        line.removeprefix(">").strip()
        for line in after.splitlines()
        if line.startswith(">")
    ]
    assert quoted, "the remediation marker is not followed by a blockquote"
    return _normalised(" ".join(quoted))


def test_the_runbook_sections_are_exactly_the_registered_checks() -> None:
    """In the registry's order, which is the order the chain runs in.

    A renamed check fails both ways round: the old heading is no longer a
    registered identifier and the new identifier has no heading.
    """
    headings = list(_sections(_region(_document())))

    assert headings == [check.identifier for check in CHECKS]


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.identifier)
def test_each_section_quotes_its_remediation_word_for_word(check: Check) -> None:
    """The whole point. A paraphrase here is the drift the registry prevents.

    Args:
        check: The check whose section is being read.
    """
    body = _sections(_region(_document()))[check.identifier]

    assert _quoted_remediation(body) == _normalised(check.remediation.explanation)


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.identifier)
def test_a_section_carries_the_command_its_check_carries(check: Check) -> None:
    """Including carrying none, for a check that genuinely has no command.

    Nothing here starts a daemon it cannot reach, and a plausible-looking
    command invented for the document would be worse than saying so.

    Only what follows the remediation marker is inspected. A section is free to
    carry any number of fenced blocks above it — the diagnostic commands and the
    transcripts they produced are the whole point of the page — and none of
    those is a remedy.

    Args:
        check: The check whose section is being read.
    """
    body = _sections(_region(_document()))[check.identifier]
    _, _, after = body.partition(_REMEDIATION_MARKER)
    fenced = [
        block.strip()
        for index, block in enumerate(after.split("```"))
        if index % 2 == 1
    ]

    if check.remediation.command:
        assert fenced[:1] == [check.remediation.command]
    else:
        assert fenced == [], "the check declares no command, so the section shows none"


def test_the_summary_table_links_every_check_to_its_own_section() -> None:
    """A table that named a check it did not link to would send a reader nowhere."""
    document = _document()

    for check in CHECKS:
        assert f"[`{check.identifier}`](#{_anchor(check)})" in document


def test_the_runbook_points_at_the_generated_reference() -> None:
    """The other half of the mechanism, and the one that is machine-checked.

    The generated artifact is what the contract-drift gate regenerates; a
    runbook that stopped mentioning it would leave a reader with only this
    document's word for what a check does.
    """
    assert DOCTOR_CHECKS_PATH in _document()


def _anchor(check: Check) -> str:
    """Give the fragment this document uses for one check's section.

    The headings here are the identifiers alone, exactly as they are in the
    generated reference, so the two derive the same anchor from the same rule.

    Args:
        check: The check to address.

    Returns:
        The fragment, without its leading `#`.
    """
    return anchor_for(check)


pytestmark = pytest.mark.filesystem  # reads the committed runbook; it is the subject
