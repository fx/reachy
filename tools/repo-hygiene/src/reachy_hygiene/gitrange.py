"""Collecting the diff and the commit messages of a range from `git`.

This module is the scanner's only process boundary, and it is deliberately one
function wide. Every caller takes the runner as an argument, so the parsing
above it is exercised by unit tests that start no subprocess; only `run_git`
itself talks to the outside world.

The diff is taken with three dots — `base...head` — so a branch is judged on
what it added since it diverged rather than on everything that landed on the
base in the meantime. The commit list uses two dots, which is the same set of
commits.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator, Sequence
from typing import Final

from reachy_hygiene.scan import Commit

__all__ = [
    "Runner",
    "commits_in_range",
    "diff_of_range",
    "parse_commit_log",
    "run_git",
]

Runner = Callable[[Sequence[str]], str]

# `git log -z` terminates each entry with a NUL, which is the one byte a commit
# message cannot contain: git stores messages as C strings. Any printable
# delimiter, however unlikely, can be typed into a message — and a record split
# in the wrong place turns the rest of that message into what the parser reads
# as the next commit's identifier, which is a leak walking straight past the
# scan.
_RECORD_SEPARATOR: Final = "\0"


def run_git(arguments: Sequence[str]) -> str:  # pragma: no cover - process boundary
    """Run `git` with the given arguments and return its standard output.

    Args:
        arguments: The arguments to pass to `git`, without the program name.

    Returns:
        The command's standard output.

    Raises:
        subprocess.CalledProcessError: If `git` exits non-zero.
    """
    # S603: the argument vector is built here from literals and refs supplied by
    # the caller, is passed as a list, and never reaches a shell.
    # S607: `git` is resolved from PATH on purpose — the scanner runs against
    # whichever git the checkout was made with, on a runner and on a laptop
    # alike, and hard-coding a path would break one of them.
    completed = subprocess.run(  # noqa: S603  # a list, never a shell
        ["git", *arguments],  # noqa: S607  # the checkout's git, from PATH
        capture_output=True,
        check=True,
        text=True,
    )
    return completed.stdout


def diff_of_range(base: str, head: str, *, run: Runner = run_git) -> str:
    """Return the unified diff a range introduces.

    Args:
        base: The revision the range starts from.
        head: The revision the range ends at.
        run: The command runner, injectable so callers stay testable.

    Returns:
        A unified diff with no context lines.
    """
    return run(["diff", "--unified=0", "--no-color", f"{base}...{head}"])


def parse_commit_log(raw: str) -> Iterator[Commit]:
    """Parse the NUL-terminated log format this module asks `git` for.

    Args:
        raw: The output of the `git log` invocation below.

    Yields:
        One commit per record, in the order `git` emitted them.
    """
    for record in raw.split(_RECORD_SEPARATOR):
        stripped = record.strip("\n")
        if not stripped:
            continue
        sha, _, message = stripped.partition("\n")
        yield Commit(sha=sha.strip(), message=message)


def commits_in_range(base: str, head: str, *, run: Runner = run_git) -> list[Commit]:
    """Return every commit a range introduces, with its message.

    Args:
        base: The revision the range starts from.
        head: The revision the range ends at.
        run: The command runner, injectable so callers stay testable.

    Returns:
        The commits, newest first, as `git log` orders them.
    """
    raw = run(["log", "-z", "--format=%H%n%B", f"{base}..{head}"])
    return list(parse_commit_log(raw))
