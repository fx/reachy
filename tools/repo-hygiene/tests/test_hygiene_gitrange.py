"""Parsing what `git` returns, with the process boundary replaced by a fake."""

from __future__ import annotations

from collections.abc import Sequence

from reachy_hygiene.corpus import MUST_BE_CAUGHT
from reachy_hygiene.gitrange import (
    commits_in_range,
    decode_git_output,
    diff_of_range,
    parse_commit_log,
)
from reachy_hygiene.scan import scan_commits, scan_text

_SHA = "0123456789abcdef0123456789abcdef01234567"


def test_a_single_commit_parses_into_its_identity_and_message() -> None:
    """One record yields one commit, with the subject line separated off."""
    (commit,) = parse_commit_log(f"{_SHA}\nfeat: add a thing\n\nWith a body.\n\0")

    assert commit.sha == _SHA
    assert commit.message == "feat: add a thing\n\nWith a body."


def test_a_message_containing_a_separator_looking_line_stays_one_record() -> None:
    """Records end at a NUL, which is the one byte a message cannot hold."""
    (commit,) = parse_commit_log(f"{_SHA}\nfix: rename\n\n@@ -1 +1 @@\n\0")

    assert "@@ -1 +1 @@" in commit.message


def test_a_message_holding_a_printable_control_character_stays_one_record() -> None:
    """A record separator a message can contain would split it in two.

    That split is not cosmetic: everything after it is read as the next
    commit's identifier, so a leak on the far side of it walks past the scan.
    """
    (commit,) = parse_commit_log(f"{_SHA}\nfix: tidy\n\n\x1e{MUST_BE_CAUGHT[0]}\n\0")

    assert MUST_BE_CAUGHT[0] in commit.message
    assert scan_commits([commit]) != []


def test_an_empty_range_yields_no_commits() -> None:
    """A range with nothing in it parses to an empty list, not to a blank."""
    assert list(parse_commit_log("")) == []
    assert list(parse_commit_log("\n")) == []


def test_the_diff_is_requested_without_context_or_colour() -> None:
    """Context lines and colour codes are noise the scanner would parse."""
    seen: list[Sequence[str]] = []

    def run(arguments: Sequence[str]) -> str:
        seen.append(arguments)
        return "diff body"

    assert diff_of_range("main", "HEAD", run=run) == "diff body"
    assert seen == [["diff", "--unified=0", "--no-color", "main...HEAD"]]


def test_the_commit_list_is_requested_nul_terminated() -> None:
    """The log format is what `parse_commit_log` is written against."""
    seen: list[Sequence[str]] = []

    def run(arguments: Sequence[str]) -> str:
        seen.append(arguments)
        return f"{_SHA}\nchore: nothing\n\0"

    (commit,) = commits_in_range("main", "HEAD", run=run)

    assert commit.sha == _SHA
    assert seen == [["log", "-z", "--format=%H%n%B", "main..HEAD"]]


def test_undecodable_bytes_do_not_stop_the_scan() -> None:
    """Git imposes no encoding on a message, so this is an input, not an edge.

    Raising here would fail the gate for a reason unrelated to leak detection,
    which is a red check that says nothing about whether anything leaked.
    """
    raw = f"{_SHA}\nfix: seen at {MUST_BE_CAUGHT[0]}".encode() + b" \xff\xfe\x80\n\0"

    (commit,) = parse_commit_log(decode_git_output(raw))

    assert MUST_BE_CAUGHT[0] in commit.message
    assert scan_commits([commit]) != []


def test_an_undecodable_byte_becomes_the_replacement_character() -> None:
    """Lossy, and lossy in a shape no rule matches, so it cannot mask a leak."""
    decoded = decode_git_output(b"host \xff\xfe end")

    assert decoded == "host \ufffd\ufffd end"
    assert scan_text(decoded, "notes.md") == []


def test_valid_utf8_survives_the_decode_intact() -> None:
    """Lenient decoding must not mangle text that was fine to begin with."""
    text = "résumé: 半角 ✓"

    assert decode_git_output(text.encode()) == text
