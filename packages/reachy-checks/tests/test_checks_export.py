"""The generated check reference, and what makes it a contract rather than prose.

`registry.py` calls the identifiers and the remediation strings a published
interface. Publishing them means rendering them, and rendering them means
something has to hold the rendering to the registry — otherwise the document is
a copy that is true on the day it was written.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

from reachy_checks.checks_export import (
    CONTRACTS,
    DOCTOR_CHECKS_PATH,
    anchor_for,
    render_doctor_checks,
)
from reachy_checks.context import Requirement
from reachy_checks.outcomes import Finding, Remediation
from reachy_checks.registry import CHECKS, Check


async def _never_called(_context: object) -> Finding:  # pragma: no cover - a stand-in
    """Stand in for a probe in a check built only to be rendered.

    Args:
        _context: Ignored.

    Returns:
        Nothing; it is never called.

    Raises:
        AssertionError: Always, if anything ever does call it.
    """
    message = "the renderer must not run a probe"
    raise AssertionError(message)


def test_every_registered_check_is_rendered() -> None:
    """A check missing from the document is a check the runbook cannot key on."""
    document = render_doctor_checks()

    for check in CHECKS:
        assert f"### {check.identifier}\n" in document


def test_the_remediation_is_reproduced_word_for_word() -> None:
    """Paraphrasing it here is the drift the registry exists to prevent."""
    document = render_doctor_checks()

    for check in CHECKS:
        assert f"> {check.remediation.explanation}\n" in document
        if check.remediation.command:
            assert f"\n{check.remediation.command}\n" in document


def test_the_table_links_to_the_section_it_names() -> None:
    """The anchor is derived, not written, so a renamed check moves both ends."""
    document = render_doctor_checks()

    for check in CHECKS:
        assert f"[`{check.identifier}`](#{anchor_for(check)})" in document


def test_the_anchor_drops_dots_and_keeps_hyphens() -> None:
    """The two identifiers that exercise both halves of the rule."""
    by_identifier = {check.identifier: check for check in CHECKS}

    assert anchor_for(by_identifier["daemon.reachable"]) == "daemonreachable"
    assert (
        anchor_for(by_identifier["groundstation.round-trip"])
        == "groundstationround-trip"
    )


def test_a_check_that_needs_nothing_supplied_says_so() -> None:
    """Every check registered today needs something; a future one need not.

    Rendering an empty requirement tuple as a blank cell would read as a
    formatting fault rather than as a check that always runs.
    """
    document = render_doctor_checks(
        (
            Check(
                identifier="example.always",
                description="Something that can always be asked",
                requires=(),
                probe=_never_called,
                remediation=Remediation(explanation="Nothing to do."),
            ),
        ),
    )

    assert "**Needs:** —" in document
    assert "| — |" in document


def test_a_requirement_is_named_by_its_own_value() -> None:
    """The names in the document are the ones a caller supplies, not new ones."""
    document = render_doctor_checks()

    for requirement in Requirement:
        assert f"`{requirement.value}`" in document


def test_the_reference_is_registered_as_a_contract() -> None:
    """Registration is what puts it under the drift gate rather than beside it."""
    assert [contract.path for contract in CONTRACTS] == [DOCTOR_CHECKS_PATH]
    assert CONTRACTS[0].render() == render_doctor_checks()


def test_the_document_ends_with_exactly_one_newline() -> None:
    """The drift gate compares bytes, so a trailing blank line is a difference."""
    document = render_doctor_checks()

    assert document.endswith("\n")
    assert not document.endswith("\n\n")
