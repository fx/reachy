"""Diff parsing and commit-message scanning, over in-memory inputs only."""

from __future__ import annotations

from reachy_hygiene.corpus import MUST_BE_ALLOWED, MUST_BE_CAUGHT
from reachy_hygiene.patterns import EXEMPT_PATHS
from reachy_hygiene.scan import (
    PATH_FINDING,
    Commit,
    scan_commits,
    scan_diff,
    scan_text,
)

_LEAK = MUST_BE_CAUGHT[0]
_CLEAN = MUST_BE_ALLOWED[0]
_SHA = "0123456789abcdef0123456789abcdef01234567"

# A path that is itself a leak, and an address whose domain is itself a leak.
# Both are shapes on purpose, so the lines holding them carry the inline
# exemption the scanner reads.
_LEAKING_PATH = "docs/setup-robot.local.md"  # leak-scan:allow
_NESTED_ADDRESS = "operator@storage.internal"  # leak-scan:allow


def _diff(path: str, *body: str, start: int = 1) -> str:
    """Build a unified diff of one hunk against one file.

    Args:
        path: The repository path the hunk applies to.
        body: The hunk's lines, each already carrying its diff prefix.
        start: The line number the hunk starts at on the new side.

    Returns:
        A unified diff as `git diff` writes it.
    """
    added = sum(1 for line in body if line.startswith(("+", " ")))
    header = f"@@ -{start},0 +{start},{added} @@"
    return "\n".join([f"diff --git a/{path} b/{path}", f"+++ b/{path}", header, *body])


def test_an_added_line_is_reported_with_its_path_and_line_number() -> None:
    """A leak in an added line names the file and the line it landed on."""
    diff = _diff("services/groundstation/config.py", f"+{_LEAK}", start=12)

    (finding,) = scan_diff(diff)

    assert finding.origin == "services/groundstation/config.py"
    assert finding.line == 12
    assert finding.rule == "private-ipv4"


def test_line_numbers_advance_across_added_and_context_lines() -> None:
    """The reported line number tracks the new side of the hunk."""
    diff = _diff(
        "docs/ops/runbook.md", f"+{_CLEAN}", " untouched", f"+{_LEAK}", start=5
    )

    (finding,) = scan_diff(diff)

    assert finding.line == 7


def test_a_removed_line_is_not_a_finding() -> None:
    """Deleting a leaking line is a fix, not a new offence."""
    diff = _diff("services/groundstation/config.py", f"-{_LEAK}")

    assert scan_diff(diff) == []


def test_a_deleted_file_contributes_nothing() -> None:
    """A hunk whose new side is /dev/null has no added lines to scan."""
    diff = "\n".join(
        [
            "diff --git a/gone.py b/gone.py",
            "+++ /dev/null",
            "@@ -1,1 +0,0 @@",
            f"-{_LEAK}",
        ]
    )

    assert scan_diff(diff) == []


def test_the_corpus_path_is_exempt_from_the_diff_scan() -> None:
    """The one exempt path is exempt, and it is exempt by exact name."""
    (exempt,) = EXEMPT_PATHS

    assert scan_diff(_diff(exempt, f"+{_LEAK}")) == []
    assert scan_diff(_diff(f"{exempt}.bak", f"+{_LEAK}")) != []


def test_a_leak_in_a_commit_message_is_reported_against_the_commit() -> None:
    """A message is scanned whether or not the diff beside it is clean."""
    commit = Commit(sha=_SHA, message=f"fix: use {_LEAK}")

    (finding,) = scan_commits([commit])

    assert finding.origin == f"commit {_SHA[:12]}"
    assert finding.line == 1


def test_a_leak_in_a_message_body_reports_its_own_line() -> None:
    """The line number is the line within the message, not within the diff."""
    commit = Commit(sha=_SHA, message=f"fix: tidy up\n\nSeen at {_LEAK}\n")

    (finding,) = scan_commits([commit])

    assert finding.line == 3


def test_scan_text_starts_where_it_is_told() -> None:
    """An offset origin reports absolute line numbers."""
    (finding,) = scan_text(_LEAK, "notes.md", first_line=40)

    assert finding.line == 40


def test_a_leaking_path_is_caught_even_when_the_content_is_clean() -> None:
    """A file named after somebody's robot leaks the name by existing."""
    diff = _diff(_LEAKING_PATH, f"+{_CLEAN}")

    (finding,) = scan_diff(diff)

    assert finding.origin == "docs/[redacted].md"
    assert finding.line == PATH_FINDING
    assert finding.rule == "internal-hostname"


def test_a_leaking_path_is_redacted_everywhere_it_is_reported() -> None:
    """Naming the file verbatim would republish the value just rejected."""
    diff = _diff(_LEAKING_PATH, f"+{_LEAK}")

    findings = scan_diff(diff)

    assert len(findings) == 2
    for finding in findings:
        assert _LEAKING_PATH not in finding.describe()
        assert "[redacted]" in finding.origin


def test_a_path_finding_reads_without_a_line_number() -> None:
    """`path:0:` would read as line zero, which is not where the match is."""
    (finding,) = scan_diff(_diff(_LEAKING_PATH, f"+{_CLEAN}"))

    assert finding.describe() == "docs/[redacted].md: internal-hostname in the path"


def test_an_ordinary_path_produces_no_finding_of_its_own() -> None:
    """Only shapes are rejected; a path is not suspicious by being a path."""
    assert scan_diff(_diff("services/groundstation/config.py", f"+{_CLEAN}")) == []


def test_overlapping_matches_are_redacted_as_one_span() -> None:
    """An address whose domain is itself a match must not survive in halves.

    Redacting rule by rule would replace the hostname first, hide the address
    from the email rule, and leave the local part in a public log.
    """
    local_part, _, domain = _NESTED_ADDRESS.partition("@")
    findings = scan_text(f"contact = {_NESTED_ADDRESS}", "notes.md")

    assert findings != []
    for finding in findings:
        assert local_part not in finding.excerpt
        assert domain not in finding.excerpt


def test_a_pure_rename_to_a_leaking_name_is_caught() -> None:
    """A rename has no `+++` header, so the destination is named nowhere else."""
    diff = "\n".join(
        [
            f"diff --git a/docs/setup.md b/{_LEAKING_PATH}",
            "similarity index 100%",
            "rename from docs/setup.md",
            f"rename to {_LEAKING_PATH}",
        ]
    )

    (finding,) = scan_diff(diff)

    assert finding.line == PATH_FINDING
    assert finding.origin == "docs/[redacted].md"


def test_a_copy_to_a_leaking_name_is_caught() -> None:
    """Copy detection names its destination the same way a rename does."""
    diff = "\n".join(
        [
            f"diff --git a/docs/setup.md b/{_LEAKING_PATH}",
            "similarity index 100%",
            "copy from docs/setup.md",
            f"copy to {_LEAKING_PATH}",
        ]
    )

    assert len(scan_diff(diff)) == 1


def test_a_renamed_and_edited_file_is_reported_once_for_its_path() -> None:
    """`rename to` and `+++` name the same destination; it is not two leaks."""
    diff = "\n".join(
        [
            f"diff --git a/docs/setup.md b/{_LEAKING_PATH}",
            "similarity index 90%",
            "rename from docs/setup.md",
            f"rename to {_LEAKING_PATH}",
            f"+++ b/{_LEAKING_PATH}",
            "@@ -1,0 +1,1 @@",
            f"+{_CLEAN}",
        ]
    )

    findings = scan_diff(diff)

    assert len(findings) == 1
    assert findings[0].line == PATH_FINDING
