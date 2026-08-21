"""The command line the `just leak-scan` recipe and continuous integration run.

The scan covers both halves of what a change adds: the lines its diff
introduces, and the messages of the commits that carry them. The second half is
not a refinement of the first — a value in a file can be deleted in a follow-up
commit, and a value in a message cannot be retracted at all short of rewriting
history that other people have already fetched.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import TextIO

from reachy_hygiene.gitrange import (
    Runner,
    commits_in_range,
    diff_of_range,
    run_git,
)
from reachy_hygiene.scan import Finding, scan_commits, scan_diff

__all__ = ["main"]

_EPILOGUE = """\
A finding is a shape, not a judgement: private address ranges, internal
hostname suffixes and email addresses are rejected because they belong to
somebody's environment and this repository is public. Move the value to an
untracked local file or a repository secret, leave a tracked `.example`
sibling documenting its shape, and use a documentation-reserved value in
anything tracked. A line that matches a shape without being one carries the
`leak-scan:allow` marker, which a reviewer sees on the line it applies to.
"""


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    Returns:
        The parser for the leak-scan command line.
    """
    parser = argparse.ArgumentParser(
        prog="python -m reachy_hygiene",
        description=(
            "Scan the diff and the commit messages of a range for values that "
            "belong to somebody's environment."
        ),
        epilog=_EPILOGUE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--base",
        required=True,
        help="the revision the range starts from",
    )
    parser.add_argument(
        "--head",
        default="HEAD",
        help="the revision the range ends at (default: %(default)s)",
    )
    return parser


def _report(findings: Sequence[Finding], stream: TextIO) -> None:
    """Write the failure report for a non-empty set of findings.

    Args:
        findings: The findings to report.
        stream: Where to write the report.
    """
    plural = "" if len(findings) == 1 else "s"
    header = f"Environment-leak scan failed: {len(findings)} finding{plural}."
    print(f"{header}\n", file=stream)
    for finding in findings:
        print(f"  {finding.describe()}", file=stream)
    print(f"\n{_EPILOGUE}", file=stream)


def main(
    argv: Sequence[str] | None = None,
    *,
    run: Runner = run_git,
    stream: TextIO | None = None,
) -> int:
    """Scan a range and report the outcome.

    Args:
        argv: The command-line arguments, or `None` to read `sys.argv`.
        run: The `git` runner, injectable so this function stays testable.
        stream: Where to write the report, defaulting to standard output.

    Returns:
        `0` when the range is clean, `1` when anything matched.
    """
    output = sys.stdout if stream is None else stream
    arguments = _build_parser().parse_args(argv)

    findings = [
        *scan_diff(diff_of_range(arguments.base, arguments.head, run=run)),
        *scan_commits(commits_in_range(arguments.base, arguments.head, run=run)),
    ]
    if not findings:
        print(
            f"Environment-leak scan clean: {arguments.base}...{arguments.head}",
            file=output,
        )
        return 0

    _report(findings, output)
    return 1
