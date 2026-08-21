"""The command line, driven with a fake `git` runner and an in-memory stream."""

from __future__ import annotations

import contextlib
import io
from collections.abc import Sequence

import pytest

from reachy_hygiene.cli import main
from reachy_hygiene.corpus import MUST_BE_ALLOWED, MUST_BE_CAUGHT
from reachy_hygiene.gitrange import Runner

_LEAK = MUST_BE_CAUGHT[0]
_CLEAN = MUST_BE_ALLOWED[0]
_SHA = "0123456789abcdef0123456789abcdef01234567"


def _runner(diff: str = "", log: str = "") -> tuple[list[list[str]], Runner]:
    """Build a fake `git` runner that records what it was asked for.

    Args:
        diff: The output to return for `git diff`.
        log: The output to return for `git log`.

    Returns:
        The recorded argument vectors, and the runner itself.
    """
    calls: list[list[str]] = []

    def run(arguments: Sequence[str]) -> str:
        calls.append(list(arguments))
        return diff if arguments[0] == "diff" else log

    return calls, run


def _commit_log(*messages: str) -> str:
    """Render the NUL-terminated log format the scanner asks `git` for.

    Args:
        messages: One commit message per commit, newest first.

    Returns:
        The rendered log output.
    """
    return "".join(f"{_SHA}\n{message}\n\0" for message in messages)


def test_a_clean_range_succeeds() -> None:
    """Nothing matching means exit 0 and a one-line confirmation."""
    diff = f"+++ b/notes.md\n@@ -1,0 +1,1 @@\n+{_CLEAN}\n"
    _, run = _runner(diff=diff, log=_commit_log("chore: tidy"))
    stream = io.StringIO()

    assert main(["--base", "main", "--head", "HEAD"], run=run, stream=stream) == 0
    assert "clean" in stream.getvalue()


def test_a_leak_in_the_diff_fails_and_names_the_file_and_line() -> None:
    """A finding in a file is reported with its path and line number."""
    diff = f"+++ b/services/groundstation/config.py\n@@ -1,0 +1,1 @@\n+{_LEAK}\n"
    _, run = _runner(diff=diff, log=_commit_log("feat: add a setting"))
    stream = io.StringIO()

    assert main(["--base", "main", "--head", "HEAD"], run=run, stream=stream) == 1
    report = stream.getvalue()
    assert "services/groundstation/config.py:1: private-ipv4" in report
    assert "1 finding." in report


def test_a_leak_only_in_a_commit_message_still_fails() -> None:
    """A clean diff does not excuse a message that cannot be retracted."""
    _, run = _runner(diff="", log=_commit_log(f"fix: point at {_LEAK}"))
    stream = io.StringIO()

    assert main(["--base", "main", "--head", "HEAD"], run=run, stream=stream) == 1
    assert f"commit {_SHA[:12]}:1: private-ipv4" in stream.getvalue()


def test_the_report_never_repeats_the_value() -> None:
    """A public log must not gain the value the scan rejected."""
    _, run = _runner(diff="", log=_commit_log(f"fix: point at {_LEAK}"))
    stream = io.StringIO()

    main(["--base", "main", "--head", "HEAD"], run=run, stream=stream)

    _, _, value = _LEAK.partition("=")
    assert value not in stream.getvalue()


def test_the_range_is_asked_for_with_three_dots_and_two() -> None:
    """The diff is merge-base scoped; the commit list is the same set."""
    calls, run = _runner()

    main(["--base", "origin/main", "--head", "feature"], run=run, stream=io.StringIO())

    assert calls[0][:2] == ["diff", "--unified=0"]
    assert calls[0][-1] == "origin/main...feature"
    assert calls[1][0] == "log"
    assert calls[1][-1] == "origin/main..feature"


def test_head_defaults_to_the_working_revision() -> None:
    """`--head` is optional and defaults to HEAD."""
    calls, run = _runner()

    main(["--base", "origin/main"], run=run, stream=io.StringIO())

    assert calls[0][-1] == "origin/main...HEAD"


def test_a_missing_base_is_a_usage_error() -> None:
    """`--base` has no sensible default, so omitting it fails loudly.

    `stream` covers the report, not the parser: argparse writes its own usage
    error to `sys.stderr` before `main` reaches anything it was given. The
    redirect is what keeps this a unit test that performs no output.
    """
    _, run = _runner()
    complaint = io.StringIO()

    with contextlib.redirect_stderr(complaint), pytest.raises(SystemExit):
        main([], run=run, stream=io.StringIO())

    assert "--base" in complaint.getvalue()
