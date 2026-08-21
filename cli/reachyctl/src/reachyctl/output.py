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
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from rich.console import Console
from rich.table import Table

from reachyctl.exits import ExitCode
from reachyctl.redaction import Redactor

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
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
        document: dict[str, Any] = {
            "command": report.command,
            "ok": report.ok,
            "summary": report.summary,
            "data": dict(report.data),
            "rows": [dict(row) for row in report.rows],
        }
        self._write(self._out, json.dumps(document, indent=2))

    def _emit_plain(self, report: Report) -> None:
        """Write the result as tab-separated text.

        Args:
            report: What the command produced.
        """
        if report.columns:
            self._write(self._out, _SEPARATOR.join(report.columns))
            for row in report.rows:
                self._write(self._out, self._plain_row(report.columns, row))
        for name, value in report.data.items():
            self._write(self._out, f"{name}{_SEPARATOR}{_render(value)}")
        status = "ok" if report.ok else "failed"
        summary = f"{_SEPARATOR}{report.summary}" if report.summary else ""
        self._write(self._out, f"{report.command}{_SEPARATOR}{status}{summary}")

    def _plain_row(self, columns: Sequence[str], row: Mapping[str, object]) -> str:
        """Render one row of the plain table.

        Args:
            columns: The field names, in order.
            row: The row's values.

        Returns:
            The line to write.
        """
        return _SEPARATOR.join(_render(row.get(name)) for name in columns)

    def _emit_rich(self, report: Report) -> None:
        """Write the result for a person at a terminal.

        Args:
            report: What the command produced.
        """
        if report.columns:
            table = Table(show_header=True, header_style="bold")
            for name in report.columns:
                table.add_column(self._scrub(name))
            for row in report.rows:
                table.add_row(
                    *(self._scrub(_render(row.get(name))) for name in report.columns),
                )
            self._console.print(table)
        for name, value in report.data.items():
            self._print(f"{name}: {_render(value)}")
        status = "[green]ok[/green]" if report.ok else "[red]failed[/red]"
        summary = f" — {report.summary}" if report.summary else ""
        self._print(f"{report.command}: {status}{summary}")

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

        Args:
            text: What to write.
        """
        self._console.print(self._scrub(text), highlight=False)


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
