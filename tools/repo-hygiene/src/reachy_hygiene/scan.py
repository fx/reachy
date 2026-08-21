"""Matching the leak rules against text, unified diffs and commit messages.

Nothing here reads a file or starts a process. Every entry point takes strings
and returns values, which is what lets the whole scanner be covered by unit
tests that perform no input or output; collecting those strings from a
repository is `gitrange`'s job and is one function wide.

A finding never carries the value that produced it. Continuous integration logs
on a public repository are public, so a scan that echoed what it found would
publish the leak in the act of reporting it. The excerpt is the offending line
with every match replaced by a placeholder — enough to recognise the line in the
editor, not enough to reconstruct it from the log.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Final

from reachy_hygiene.patterns import (
    ALLOW_MARKER,
    EXEMPT_PATHS,
    RULES,
    Rule,
    is_documentation_value,
)

__all__ = [
    "PATH_FINDING",
    "Commit",
    "Finding",
    "scan_commits",
    "scan_diff",
    "scan_text",
]

_REDACTION: Final = "[redacted]"
_EXCERPT_LIMIT: Final = 120

# The line number of a finding that is in a file's path rather than in its
# content. Zero rather than `None` so the field stays a plain integer.
PATH_FINDING: Final = 0


@dataclass(frozen=True, slots=True)
class Commit:
    """One commit's identity and message.

    Attributes:
        sha: The full commit identifier.
        message: The commit message, subject and body together.
    """

    sha: str
    message: str


@dataclass(frozen=True, slots=True)
class Finding:
    """One line that matched a leak rule.

    The origin is redacted on the same terms as the excerpt. Almost always that
    changes nothing, because almost no path matches a rule — but when the leak
    is the filename, a report naming the file verbatim republishes the value it
    just rejected, into a log that is public on this repository. The redacted
    form still identifies the file to the person who added it.

    Attributes:
        origin: Where the line came from — a repository path, or a commit —
            with any match in it replaced by a placeholder.
        line: The one-based line number within that origin, or `PATH_FINDING`
            when the match is in the path rather than in the content.
        rule: The name of the rule that matched.
        excerpt: The offending text with every match replaced by a placeholder.
    """

    origin: str
    line: int
    rule: str
    excerpt: str

    def describe(self) -> str:
        """Render the finding as one line for a failure report.

        Returns:
            An `origin:line: rule — excerpt` string carrying no matched value.
        """
        if self.line == PATH_FINDING:
            return f"{self.origin}: {self.rule} in the path"
        return f"{self.origin}:{self.line}: {self.rule} — {self.excerpt}"


def _merged_spans(text: str, rules: Sequence[Rule]) -> list[tuple[int, int]]:
    """Collect every rule's match spans against one text and merge overlaps.

    Every rule matches the original text rather than the partly-redacted
    result of the rule before it. Redacting in sequence would hide a match
    from a later rule and leave part of the value in the report: an internal
    hostname replaced first stops the email rule matching the address it was
    the domain of, and the local part survives into the log.

    Args:
        text: The text to find matches in.
        rules: The rules to apply.

    Returns:
        Non-overlapping `(start, end)` spans in ascending order.
    """
    spans = sorted(
        match.span() for rule in rules for match in rule.pattern.finditer(text)
    )
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def _redact(text: str, rules: Sequence[Rule]) -> str:
    """Replace every rule match in a text with a placeholder.

    Args:
        text: The text to redact.
        rules: The rules whose matches are replaced.

    Returns:
        The text with matched values replaced, truncated for a log.
    """
    pieces: list[str] = []
    cursor = 0
    for start, end in _merged_spans(text, rules):
        pieces.append(text[cursor:start])
        pieces.append(_REDACTION)
        cursor = end
    pieces.append(text[cursor:])
    redacted = "".join(pieces).strip()
    if len(redacted) > _EXCERPT_LIMIT:
        redacted = f"{redacted[:_EXCERPT_LIMIT]}…"
    return redacted


def _rules_matching(text: str, rules: Sequence[Rule]) -> Iterator[Rule]:
    """Yield every rule that matches something the exclusions do not allow.

    Args:
        text: The text to inspect.
        rules: The rules to apply.

    Yields:
        Each matching rule once, in the order they are given.
    """
    for rule in rules:
        if any(
            not is_documentation_value(match.group(0))
            for match in rule.pattern.finditer(text)
        ):
            yield rule


def _scan_line(
    line: str,
    origin: str,
    number: int,
    rules: Sequence[Rule],
) -> Iterator[Finding]:
    """Yield a finding for every rule the line matches.

    Args:
        line: The line to inspect, without any diff prefix.
        origin: Where the line came from.
        number: The one-based line number within that origin.
        rules: The rules to apply.

    Yields:
        One finding per matching rule, at most one per rule.
    """
    if ALLOW_MARKER in line:
        return
    for rule in _rules_matching(line, rules):
        yield Finding(
            origin=origin,
            line=number,
            rule=rule.name,
            excerpt=_redact(line, rules),
        )


def scan_text(
    text: str,
    origin: str,
    *,
    first_line: int = 1,
    rules: Sequence[Rule] = RULES,
) -> list[Finding]:
    """Scan a block of text line by line.

    Args:
        text: The text to scan.
        origin: Where the text came from, reported with every finding.
        first_line: The line number of the first line of `text`.
        rules: The rules to apply.

    Returns:
        Every finding, in the order the lines appear.
    """
    findings: list[Finding] = []
    for offset, line in enumerate(text.splitlines()):
        findings.extend(_scan_line(line, origin, first_line + offset, rules))
    return findings


_DIFF_TARGET: Final = re.compile(r"^\+\+\+ (?:b/)?(.*)$")
_DIFF_HUNK: Final = re.compile(r"^@@ -[0-9]+(?:,[0-9]+)? \+([0-9]+)(?:,[0-9]+)? @@")
# A pure rename or copy has no `+++` header at all — git writes only `rename to`
# or `copy to` — so a file moved to a leaking name would otherwise arrive
# unscanned. The destination is the newly tracked path either way.
_DIFF_DESTINATION: Final = re.compile(r"^(?:rename|copy) to (.*)$")


def scan_diff(
    diff: str,
    *,
    rules: Sequence[Rule] = RULES,
    exempt_paths: frozenset[str] = EXEMPT_PATHS,
) -> list[Finding]:
    """Scan the added lines of a unified diff.

    Removed and context lines are ignored: a change is only responsible for what
    it introduces, and a line it deletes is already in the history behind it.

    Paths come from the `+++` header and from the `rename to` and `copy to`
    headers, which is every destination git names. It omits all three for a
    binary file, so a binary added under a leaking *name*, with no text hunk
    beside it, is the one thing this misses; the secret scan and review cover
    that corner, and reconstructing paths from the `diff --git` line instead
    would mean re-deriving which side of the change each one is on.

    Args:
        diff: A unified diff, as `git diff` writes it.
        rules: The rules to apply.
        exempt_paths: Repository paths whose added lines are not scanned.

    Returns:
        Every finding, in the order the diff presents them.
    """
    findings: list[Finding] = []
    path: str | None = None
    reported = ""
    seen_paths: set[str] = set()
    number = 0
    for raw in diff.splitlines():
        destination = _DIFF_DESTINATION.match(raw)
        if destination is not None:
            # A rename with no content change reaches no `+++` header, so this
            # is the only place its destination is named.
            moved = destination.group(1)
            if moved not in exempt_paths and moved not in seen_paths:
                seen_paths.add(moved)
                findings.extend(_scan_path(moved, _redact(moved, rules), rules))
            continue
        target = _DIFF_TARGET.match(raw)
        if target is not None:
            candidate = target.group(1)
            path = None if candidate == "/dev/null" else candidate
            # A path is content too. A file named after somebody's robot leaks
            # the name whether or not anything inside it does, and a diff that
            # only adds such a file has no added line to catch it on. It is
            # also what every finding in the file is reported against, so it is
            # redacted once here and reused rather than reported raw.
            if path is not None:
                reported = _redact(path, rules)
                if path not in exempt_paths and path not in seen_paths:
                    seen_paths.add(path)
                    findings.extend(_scan_path(path, reported, rules))
            continue
        hunk = _DIFF_HUNK.match(raw)
        if hunk is not None:
            number = int(hunk.group(1))
            continue
        if raw.startswith("+") and path is not None:
            if path not in exempt_paths:
                findings.extend(_scan_line(raw[1:], reported, number, rules))
            number += 1
        elif raw.startswith(" "):
            number += 1
    return findings


def _scan_path(path: str, reported: str, rules: Sequence[Rule]) -> Iterator[Finding]:
    """Yield a finding for every rule the path itself matches.

    Args:
        path: The repository path a hunk applies to.
        reported: The redacted form of that path, used as the origin.
        rules: The rules to apply.

    Yields:
        One finding per matching rule, at most one per rule.
    """
    for rule in _rules_matching(path, rules):
        yield Finding(
            origin=reported,
            line=PATH_FINDING,
            rule=rule.name,
            excerpt=reported,
        )


def scan_commits(
    commits: Iterable[Commit],
    *,
    rules: Sequence[Rule] = RULES,
) -> list[Finding]:
    """Scan commit messages.

    A value committed to a message cannot be retracted by a later file edit, so
    messages are scanned whether or not the diff beside them is clean.

    Args:
        commits: The commits whose messages are scanned.
        rules: The rules to apply.

    Returns:
        Every finding, in the order the commits are given.
    """
    findings: list[Finding] = []
    for commit in commits:
        findings.extend(
            scan_text(commit.message, f"commit {commit.sha[:12]}", rules=rules)
        )
    return findings
