"""How every command says what it found, and where.

Three conventions land here, once, and every command added later inherits them
rather than deciding again. Retrofitting output format command by command
produces a tool where some commands are scriptable and nobody can predict which,
which is the failure reachyctl REQ-058 exists to prevent.

**Non-interactive by default.** A command prints a stable, tab-separated table
unless it is attached to a terminal, in which case it prints a rich one. Neither
is behind a flag: a script gets the machine-shaped rendering by being a script,
and a person gets the readable one by being a person. The two modes carry the
same fields, because the moment a rich rendering says something the plain one
does not, the plain one has stopped being an alternative.

**Two streams with two jobs.** Standard output carries the result and nothing
else, so `reachyctl probe --output json | jq` works with no filtering. Progress,
warnings and verbose detail go to standard error, where they can be read or
discarded without either affecting the result.

**One place everything leaves through.** Every string written here is scrubbed
by the `Redactor` first — the structured path, the text path, the rich path, the
verbose path and the error path. That is deliberately more than REQ-059 needs on
any single path, because the paths a credential actually escapes on are the ones
nobody remembered to guard.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from reachyctl.exits import ExitCode
from reachyctl.redaction import Redactor

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import TextIO

__all__ = ["OutputFormat", "Report", "Reporter", "build_reporter"]

# The separator between columns in the plain rendering. A tab, because a value
# can contain a space and a column that shifts is a column nothing can cut on.
_SEPARATOR: Final = "\t"


class OutputFormat(StrEnum):
    """How a command's result is rendered.

    Attributes:
        TEXT: For a person: a table, rich when attached to a terminal.
        JSON: For another program: one JSON document, parsed rather than
            scraped.
    """

    TEXT = "text"
    JSON = "json"


@dataclass(frozen=True, slots=True)
class Report:
    """What a command produced, before it is rendered.

    A command builds one of these and hands it over. It does not know which
    format was asked for, which is what keeps a field from existing in one
    rendering and not the other.

    Attributes:
        command: Which command produced it.
        ok: Whether what was asked for succeeded. This alone decides the exit
            status.
        summary: One line a person reads first.
        data: The result's scalar fields.
        columns: The names of the per-row fields, in the order to show them.
        rows: One mapping per row, keyed by the names in `columns`.
    """

    command: str
    ok: bool
    summary: str = ""
    data: Mapping[str, object] = field(default_factory=dict)
    columns: tuple[str, ...] = ()
    rows: tuple[Mapping[str, object], ...] = ()


class Reporter:
    """The one way a command writes anything."""

    def __init__(
        self,
        *,
        out: TextIO,
        err: TextIO,
        output_format: OutputFormat = OutputFormat.TEXT,
        verbose: bool = False,
        terminal: bool = False,
        redactor: Redactor | None = None,
    ) -> None:
        """Create a reporter for one invocation.

        Args:
            out: Where the result goes.
            err: Where progress and diagnostics go.
            output_format: Which rendering the result gets.
            verbose: Whether to write the detail lines.
            terminal: Whether the result stream is a terminal. Passed in rather
                than detected here, so that both renderings are exercisable
                without a pseudo-terminal.
            redactor: What to scrub every written string with. A redactor that
                knows no secrets is still applied, so the path cannot be added
                later and forgotten.
        """
        self._out = out
        self._err = err
        self._format = output_format
        self._verbose = verbose
        self._terminal = terminal
        self._redactor = Redactor() if redactor is None else redactor
        self._console = Console(file=out, force_terminal=terminal, soft_wrap=True)

    @property
    def redactor(self) -> Redactor:
        """What every written string is scrubbed with.

        Returns:
            The redactor, so a command can add a secret it has just loaded.
        """
        return self._redactor

    @property
    def output_format(self) -> OutputFormat:
        """Which rendering the result will get.

        Returns:
            The format asked for.
        """
        return self._format

    def detail(self, message: str) -> None:
        """Write a line only a verbose run wants, to standard error.

        This is the path a secret escapes on most often: verbose output exists
        to say what the tool is doing, and what it is doing involves a
        credential. It is scrubbed like everything else.

        Args:
            message: What to say.
        """
        if self._verbose:
            self._write(self._err, message)

    def note(self, message: str) -> None:
        """Write a line of progress to standard error.

        Args:
            message: What to say.
        """
        self._write(self._err, message)

    def emit(self, report: Report) -> ExitCode:
        """Render a command's result and say how the process should exit.

        Args:
            report: What the command produced.

        Returns:
            The exit status, which follows from `report.ok` and nothing else.
        """
        if self._format is OutputFormat.JSON:
            self._emit_json(report)
        elif self._terminal:
            self._emit_rich(report)
        else:
            self._emit_plain(report)
        return ExitCode.OK if report.ok else ExitCode.FAILURE

    def failure(
        self,
        command: str,
        message: str,
        code: ExitCode = ExitCode.FAILURE,
    ) -> ExitCode:
        """Report that a command could not do what it was asked, and why.

        The message is rendered through the same machinery as a success, so a
        script parsing structured output gets a document it can read rather than
        a line on standard error and an exit status to guess from.

        Args:
            command: Which command failed.
            message: Why. Scrubbed, like everything else — this is the other
                path a credential escapes on, because an exception's text is
                written by code that never heard of this rule.
            code: The exit status to return.

        Returns:
            The exit status.
        """
        self.emit(Report(command=command, ok=False, summary=message))
        return code

    def _emit_json(self, report: Report) -> None:
        """Write the result as one JSON document.

        Args:
            report: What the command produced.
        """
        # Scrubbed field by field, *before* `json.dumps` runs, and then again
        # by `_write` on the way out. The second pass is not the one that
        # matters here: `ensure_ascii` defaults to true, so a credential with a
        # non-ASCII character in it leaves the serialiser as `\uXXXX` escapes
        # and no longer matches what the redactor was given. Scrubbing the raw
        # values is what makes the structured path — the one a script captures
        # and stores — as strong as the text path rather than weaker.
        document: dict[str, Any] = {
            "command": self._scrub(report.command),
            "ok": report.ok,
            "summary": self._scrub(report.summary),
            "data": {
                self._scrub(name): self._scrub_field(value)
                for name, value in report.data.items()
            },
            "rows": [
                {
                    self._scrub(name): self._scrub_field(value)
                    for name, value in row.items()
                }
                for row in report.rows
            ],
        }
        self._write(self._out, json.dumps(document, indent=2))

    def _scrub_field(self, value: object) -> object:
        """Scrub one structured field, whatever shape it is.

        Args:
            value: The field's value.

        Returns:
            The value with every string inside it scrubbed, and every other
            kind of value unchanged — a number cannot carry a credential and
            rendering one to scrub it would change the document's types.

        Every container a report's type permits is descended into, not only the
        ones a command happens to produce today. `Report.data` and each row are
        `Mapping[str, object]`, so a value can be a mapping or a sequence of
        them — which is the shape a per-check report naturally takes, and the
        commands that produce one arrive in later changes. A scrubber that
        covered only what exists now would be a guarantee that quietly stopped
        holding the first time somebody nested a field.
        """
        if isinstance(value, str):
            return self._scrub(value)
        if isinstance(value, Mapping):
            return {
                self._scrub(str(name)): self._scrub_field(item)
                for name, item in value.items()
            }
        if isinstance(value, tuple | list):
            return [self._scrub_field(item) for item in value]
        return value

    def _emit_plain(self, report: Report) -> None:
        """Write the result as tab-separated text.

        Args:
            report: What the command produced.
        """
        if report.columns:
            self._write(
                self._out,
                _SEPARATOR.join(self._plain(name) for name in report.columns),
            )
            for row in report.rows:
                self._write(self._out, self._plain_row(report.columns, row))
        for name, value in report.data.items():
            self._write(
                self._out, f"{self._plain(name)}{_SEPARATOR}{self._plain(value)}"
            )
        status = "ok" if report.ok else "failed"
        summary = f"{_SEPARATOR}{self._plain(report.summary)}" if report.summary else ""
        self._write(
            self._out,
            f"{self._plain(report.command)}{_SEPARATOR}{status}{summary}",
        )

    def _plain_row(self, columns: Sequence[str], row: Mapping[str, object]) -> str:
        """Render one row of the plain table.

        Args:
            columns: The field names, in order.
            row: The row's values.

        Returns:
            The line to write.
        """
        return _SEPARATOR.join(self._plain(row.get(name)) for name in columns)

    def _emit_rich(self, report: Report) -> None:
        """Write the result for a person at a terminal.

        Args:
            report: What the command produced.
        """
        if report.columns:
            table = Table(show_header=True, header_style="bold")
            for name in report.columns:
                table.add_column(self._safe(name))
            for row in report.rows:
                table.add_row(
                    *(_markup(self._field(row.get(name))) for name in report.columns),
                )
            self._console.print(table)
        for name, value in report.data.items():
            self._print(f"{self._safe(name)}: {_markup(self._field(value))}")
        status = "[green]ok[/green]" if report.ok else "[red]failed[/red]"
        summary = f" — {self._safe(report.summary)}" if report.summary else ""
        self._print(f"{self._safe(report.command)}: {status}{summary}")

    def _field(self, value: object) -> str:
        """Render a field for a text rendering, scrubbed inside and out.

        The inner scrub is not belt-and-braces. `_render` falls back to `str`
        for a value it has no rule for, and `str` of a container is a `repr` —
        which escapes a backslash, a tab and a newline exactly as the
        transformations below do. So a credential nested inside a mapping is
        already rewritten by the time a scrub of the rendered line sees it, and
        that scrub matches nothing. Scrubbing the structure first puts
        `<redacted>` in before anything can repr it.

        Args:
            value: The field's value, of whatever shape a report allows.

        Returns:
            Its text form, with every known secret gone from it.
        """
        return self._scrub(_render(self._scrub_field(value)))

    def _plain(self, value: object) -> str:
        """Scrub a field and then escape it for the tab-separated rendering.

        The same order, and for the same reason, as `_safe` on the rich path:
        the escaping rewrites exactly the characters a credential may contain,
        so a value escaped first is one the redactor can no longer recognise —
        and it goes out escaped rather than redacted, with the scrubber
        reporting that it found nothing.

        Args:
            value: The field's value.

        Returns:
            The value rendered, scrubbed, and safe to put between tabs.
        """
        return _escape_plain(self._field(value))

    def _safe(self, text: str) -> str:
        r"""Scrub a command's string and then escape it, in that order.

        **The order is the whole point, and getting it the wrong way round is a
        silent REQ-059 failure rather than a visible one.** `_markup` escapes a
        `[`, so a credential like `tok[en]-abc` becomes `tok\[en]-abc`, which
        no longer matches the value the redactor was given — and the console
        then renders the escape away and puts the secret back on the terminal
        whole. Scrubbing first means the redactor sees the value as it was
        configured, and only what survives redaction is ever escaped.

        This exists so that no call site has to remember the order. Every
        untrusted string in the rich rendering goes through here; `_print`
        scrubs again on the way out, which is the choke point for the styling
        tags this class adds itself.

        Args:
            text: What a command produced.

        Returns:
            The text with every known secret removed and its brackets shown
            rather than read.
        """
        return _markup(self._scrub(text))

    def _scrub(self, text: str) -> str:
        """Remove every known secret from a string about to be written.

        Args:
            text: What was about to be written.

        Returns:
            The scrubbed text.
        """
        return self._redactor.scrub(text)

    def _write(self, stream: TextIO, line: str) -> None:
        """Scrub one line and put it on a stream.

        Every plain line this class writes goes through here, and the scrubbing
        happens here rather than at each call site. That is the difference
        between a rule and a habit: a rendering added later is covered without
        its author having read REQ-059, and the first version of this class —
        which scrubbed at the call sites — let a credential through the
        tab-separated rendering while the other two were clean.

        Args:
            stream: Where it goes.
            line: What to write.
        """
        stream.write(f"{self._scrub(line)}\n")

    def _print(self, text: str) -> None:
        """Scrub one line and hand it to the rich console.

        The rich rendering does not go through `_write` — the console owns the
        wrapping and the styling — so it has its own choke point, and both of
        them are this class's only ways out.

        The caller escapes the parts of `text` that came from a command; the
        styling tags this class adds itself are what is left interpreted.

        Args:
            text: What to write, with any untrusted part already escaped.
        """
        self._console.print(self._scrub(text), highlight=False)


def _markup(text: str) -> str:
    """Make a string the console will show rather than read.

    Rich interprets square brackets as styling tags, and every string a command
    puts in a report is untrusted in that sense: a summary carries exception
    text, and a field carries a URL or a path. Two things go wrong unescaped.
    Text disappears — `install reachyctl[camera]`, whose whole purpose is the
    exact package name, renders on a terminal as `install reachyctl`. And some
    text aborts the command: an unmatched `[/` raises `MarkupError` out of the
    console.

    Escaping rather than turning markup off keeps the `[green]ok[/green]` this
    class adds itself, which is the one place a tag is meant.

    Args:
        text: What a command produced.

    Returns:
        The same text, with its brackets shown rather than read.
    """
    return escape(text)


def _escape_plain(text: str) -> str:
    """Escape a rendered field so that it cannot add a column or a row.

    The plain rendering is the one REQ-058 promises a script can read without
    screen-scraping, and it separates fields with a tab and rows with a
    newline. A value carrying either takes that promise away: a summary carries
    exception text and a field carries a path or a URL, so a tab shifts every
    column after it and a newline turns one row into two. A script that cuts on
    the tab then reads a different field than it asked for and cannot tell.

    Escaping rather than stripping, so nothing is silently lost — and the
    backslash first, so an escape this function produces is distinguishable
    from one the value already contained.

    **Takes already-scrubbed text, and that is not an implementation detail.**
    Every character escaped here is one a credential may contain, so escaping
    an unscrubbed value rewrites the bytes the redactor is looking for and it
    then matches nothing — reporting success while the secret goes out in
    escaped form. `Reporter._plain` is the only caller and it scrubs first.

    Args:
        text: The field, already rendered and already scrubbed.

    Returns:
        The same text, with the separator and any line break shown rather than
        acted on.
    """
    return (
        text.replace("\\", "\\\\")
        .replace(_SEPARATOR, "\\t")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def _render(value: object) -> str:
    """Render one field for a text rendering.

    Args:
        value: The field's value.

    Returns:
        Its text form. `None` becomes a dash rather than the word "None",
        because a column of "None" reads as a value that was measured.
    """
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.1f}"
    if isinstance(value, tuple | list):
        return ",".join(_render(item) for item in value) or "-"
    return str(value)


def build_reporter(
    *,
    output_format: OutputFormat,
    verbose: bool,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> Reporter:
    """Build the reporter one invocation writes through.

    Args:
        output_format: Which rendering the result gets.
        verbose: Whether to write the detail lines.
        out: Where the result goes. Defaults to standard output.
        err: Where progress goes. Defaults to standard error.

    Returns:
        The reporter, with the rich rendering enabled only when the result
        stream is a terminal — so output is scriptable without a flag.
    """
    result_stream = sys.stdout if out is None else out
    return Reporter(
        out=result_stream,
        err=sys.stderr if err is None else err,
        output_format=output_format,
        verbose=verbose,
        terminal=_is_terminal(result_stream),
    )


def _is_terminal(stream: TextIO) -> bool:
    """Say whether a stream is attached to a terminal.

    Args:
        stream: The stream to ask.

    Returns:
        True when it is a terminal. A stream that cannot answer is treated as
        not one, which is the safe direction: the worst case is a person
        getting the plain rendering, rather than a pipeline getting escape
        codes in the middle of a field.
    """
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False
