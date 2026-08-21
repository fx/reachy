"""The output conventions every command inherits, and the redaction under them.

The tests here are about the machinery rather than about `probe`, because that
is the point of the machinery: `doctor` and `deploy` will produce different
reports and the same three guarantees — scriptable without a flag, structured on
request, and an exit status that follows from the result — have to hold for all
of them.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import io
import json
from typing import TYPE_CHECKING, Final, cast

import pytest
from reachyctl_support import CREDENTIAL, reporter_for

from reachyctl.exits import ExitCode
from reachyctl.output import OutputFormat, Report, build_reporter
from reachyctl.redaction import Redactor

if TYPE_CHECKING:
    from typing import TextIO

ROWS: Final = (
    {"sequence": 0, "capability": "face", "detections": 1, "round_trip_ms": 12.34},
    {"sequence": 1, "capability": "face", "detections": 0, "round_trip_ms": None},
)

REPORT: Final = Report(
    command="probe",
    ok=True,
    summary="2 result(s) over one session",
    data={"agreed": ("face",), "frames_submitted": 2, "reconnections": 0},
    columns=("sequence", "capability", "detections", "round_trip_ms"),
    rows=ROWS,
)


#:= docs/specs/reachyctl/index.md#req-058-output-is-machine-readable-on-request
#:% Every command that reports results MUST offer a structured output format
#:% suitable for consumption by another program.
def test_structured_output_parses_without_being_scraped() -> None:
    """One document, with the same fields the human rendering carries."""
    reporter, streams = reporter_for(output_format=OutputFormat.JSON)

    code = reporter.emit(REPORT)

    document = json.loads(streams.result)
    assert code is ExitCode.OK
    assert document["command"] == "probe"
    assert document["ok"] is True
    assert document["data"]["frames_submitted"] == 2
    assert document["rows"][0]["capability"] == "face"
    assert document["rows"][1]["round_trip_ms"] is None


#:= docs/specs/reachyctl/index.md#req-058-output-is-machine-readable-on-request
#:% Every command that reports results MUST offer a structured output format
#:% suitable for consumption by another program.
def test_the_exit_status_follows_from_the_result_and_nothing_else() -> None:
    """Which is what lets a script react to the answer rather than to the text."""
    reporter, _streams = reporter_for()

    assert reporter.emit(REPORT) is ExitCode.OK
    assert (
        reporter.emit(Report(command="probe", ok=False, summary="nothing arrived"))
        is ExitCode.FAILURE
    )


def test_the_plain_rendering_is_the_default_and_is_cuttable() -> None:
    """No flag was passed, and the columns are separated by a tab."""
    reporter, streams = reporter_for()

    reporter.emit(REPORT)

    lines = streams.result.splitlines()
    assert lines[0].split("\t") == [
        "sequence",
        "capability",
        "detections",
        "round_trip_ms",
    ]
    assert lines[1].split("\t") == ["0", "face", "1", "12.3"]
    # A measurement that was not taken is a dash rather than the word "None",
    # which would read as a value somebody recorded.
    assert lines[2].split("\t")[3] == "-"
    assert lines[-1].startswith("probe\tok\t")


def test_the_rich_rendering_appears_only_when_attached_to_a_terminal() -> None:
    """Both renderings carry the same fields; only the decoration differs."""
    plain, plain_streams = reporter_for(terminal=False)
    rich, rich_streams = reporter_for(terminal=True)

    plain.emit(REPORT)
    rich.emit(REPORT)

    assert "\t" in plain_streams.result
    assert "\x1b[" not in plain_streams.result
    assert "\x1b[" in rich_streams.result
    for rendering in (plain_streams.result, rich_streams.result):
        assert "face" in rendering
        assert "frames_submitted" in rendering


def test_the_result_and_the_progress_go_to_different_streams() -> None:
    """So that `--output json | jq` needs no filtering in front of it."""
    reporter, streams = reporter_for(output_format=OutputFormat.JSON, verbose=True)

    reporter.note("connecting")
    reporter.detail("negotiated: face")
    reporter.emit(REPORT)

    json.loads(streams.result)
    assert "connecting" in streams.diagnostics
    assert "negotiated: face" in streams.diagnostics


def test_detail_lines_are_written_only_when_they_were_asked_for() -> None:
    """Verbose is a choice, and progress is not."""
    quiet, quiet_streams = reporter_for(verbose=False)
    loud, loud_streams = reporter_for(verbose=True)

    for reporter in (quiet, loud):
        reporter.note("connecting")
        reporter.detail("negotiated: face")

    assert quiet_streams.diagnostics == "connecting\n"
    assert loud_streams.diagnostics == "connecting\nnegotiated: face\n"


def test_a_failure_is_reported_through_the_same_machinery_as_a_success() -> None:
    """A structured run gets a document for a failure too, not a bare status."""
    reporter, streams = reporter_for(output_format=OutputFormat.JSON)

    code = reporter.failure(
        "probe", "the groundstation is not there", ExitCode.UNREACHABLE
    )

    document = json.loads(streams.result)
    assert code is ExitCode.UNREACHABLE
    assert document["ok"] is False
    assert document["summary"] == "the groundstation is not there"


#:= docs/specs/reachyctl/index.md#req-059-secrets-are-never-written-to-output
#:% The tool MUST NOT write credentials to its output, its logs, or its error
#:% messages.
@pytest.mark.parametrize(
    "output_format",
    [OutputFormat.TEXT, OutputFormat.JSON],
)
@pytest.mark.parametrize("terminal", [False, True])
def test_a_credential_reaches_none_of_the_paths_a_command_writes_on(
    output_format: OutputFormat,
    terminal: bool,
) -> None:
    """Every path, because the ones a secret escapes on are the forgotten ones.

    Args:
        output_format: Which rendering to check.
        terminal: Whether to render as though attached to a terminal.
    """
    reporter, streams = reporter_for(
        output_format=output_format,
        verbose=True,
        terminal=terminal,
        secrets=[CREDENTIAL],
    )

    reporter.note(f"connecting with {CREDENTIAL}")
    reporter.detail(f"presenting {CREDENTIAL}")
    reporter.failure("probe", f"refused: {CREDENTIAL} is not the configured one")
    reporter.emit(
        Report(
            command="probe",
            ok=True,
            summary=f"used {CREDENTIAL}",
            data={"credential": CREDENTIAL},
            columns=("credential",),
            rows=({"credential": CREDENTIAL},),
        ),
    )

    assert CREDENTIAL not in streams.result
    assert CREDENTIAL not in streams.diagnostics
    assert "<redacted>" in streams.result
    assert "<redacted>" in streams.diagnostics


def test_a_secret_that_contains_another_is_removed_whole() -> None:
    """Otherwise the longer one is left as a redacted fragment plus its tail."""
    redactor = Redactor(["short", "short-and-then-some"])

    assert redactor.scrub("short-and-then-some") == "<redacted>"


def test_an_empty_secret_is_not_a_secret() -> None:
    """It appears between every pair of characters, so guarding it guards nothing."""
    redactor = Redactor([""])

    assert redactor.scrub("nothing to hide here") == "nothing to hide here"


def test_a_secret_learned_later_is_removed_from_then_on() -> None:
    """A command guards the credential the moment it has resolved one."""
    reporter, streams = reporter_for()
    reporter.redactor.guard(CREDENTIAL)

    reporter.note(f"presenting {CREDENTIAL}")

    assert CREDENTIAL not in streams.diagnostics


def test_the_default_reporter_renders_plainly_when_nothing_is_a_terminal() -> None:
    """`build_reporter` is what the callback uses, and it detects rather than asks."""
    _reporter, streams = reporter_for()
    built = build_reporter(
        output_format=OutputFormat.TEXT,
        verbose=False,
        out=streams.out,
        err=streams.err,
    )

    built.emit(REPORT)

    assert built.output_format is OutputFormat.TEXT
    assert "\x1b[" not in streams.result


def test_a_stream_that_cannot_say_whether_it_is_a_terminal_is_treated_as_not_one() -> (
    None
):
    """The safe direction: a person sees plain text, a pipeline sees no escapes."""
    _reporter, streams = reporter_for()
    built = build_reporter(
        output_format=OutputFormat.TEXT,
        verbose=False,
        out=cast("TextIO", _Mute(streams.out)),
        err=streams.err,
    )

    built.emit(REPORT)

    assert "\x1b[" not in streams.result


class _Mute:
    """A stream that raises when asked whether it is a terminal.

    A closed file does exactly this, and so does a stream some libraries wrap
    one in. Treating the question as unanswered rather than letting it escape is
    the behaviour under test.
    """

    def __init__(self, inner: io.StringIO) -> None:
        """Wrap a stream.

        Args:
            inner: What to write through to.
        """
        self._inner = inner

    def isatty(self) -> bool:
        """Refuse to say.

        Raises:
            ValueError: Always, which is what a closed stream raises.
        """
        message = "I/O operation on closed file"
        raise ValueError(message)

    def write(self, text: str) -> int:
        """Write through to the wrapped stream.

        Args:
            text: What to write.

        Returns:
            How many characters were written.
        """
        return self._inner.write(text)


def test_a_true_or_false_field_reads_as_a_word_rather_than_a_capital() -> None:
    """`True` in a column is a Python value; a report is read by people too."""
    reporter, streams = reporter_for()

    reporter.emit(
        Report(
            command="doctor",
            ok=True,
            data={"reachable": True, "degraded": False},
        ),
    )

    assert "reachable\tyes" in streams.result
    assert "degraded\tno" in streams.result


def test_a_list_field_is_rendered_as_its_members() -> None:
    """An empty one is a dash, for the same reason a missing measurement is."""
    reporter, streams = reporter_for()

    reporter.emit(
        Report(
            command="probe",
            ok=True,
            data={"agreed": ("face", "gesture"), "offered": ()},
        ),
    )

    assert "agreed\tface,gesture" in streams.result
    assert "offered\t-" in streams.result


#:= docs/specs/reachyctl/index.md#req-059-secrets-are-never-written-to-output
#:% The tool MUST NOT write credentials to its output, its logs, or its error
#:% messages.
def test_a_credential_with_a_character_json_escapes_is_removed_anyway() -> None:
    r"""The structured path is the one a script captures and stores.

    `json.dumps` escapes a non-ASCII character to `\uXXXX` before anything
    downstream sees the document, so a redactor applied to the serialised text
    no longer recognises the secret it was given. The values are scrubbed
    before serialisation for exactly that reason.
    """
    # Non-ASCII on purpose, and a placeholder like every other credential in
    # this repository — see the root AGENTS.md.
    unicode_credential = "exemplaire-crédentiel-ünicode"
    reporter, streams = reporter_for(
        output_format=OutputFormat.JSON,
        secrets=[unicode_credential],
    )

    reporter.emit(
        Report(
            command="probe",
            ok=False,
            summary=f"refused: {unicode_credential}",
            data={
                "credential": unicode_credential,
                "tried": (unicode_credential,),
            },
            columns=("credential",),
            rows=({"credential": unicode_credential},),
        ),
    )

    document = json.loads(streams.result)
    assert unicode_credential not in streams.result
    assert document["data"]["credential"] == "<redacted>"
    assert document["data"]["tried"] == ["<redacted>"]
    assert document["rows"][0]["credential"] == "<redacted>"
    assert "<redacted>" in document["summary"]


def test_a_value_with_brackets_in_it_survives_the_rich_rendering() -> None:
    """Rich reads a bracket as a styling tag, and eats the text inside it.

    The string this checks is the one whose whole purpose is an exact package
    name, so losing the bracketed half of it is losing the answer.
    """
    reporter, streams = reporter_for(terminal=True)

    reporter.emit(
        Report(
            command="probe",
            ok=False,
            summary="live frames need OpenCV: install reachyctl[camera]",
            data={"hint": "install reachyctl[camera]"},
            columns=("hint",),
            rows=({"hint": "install reachyctl[camera]"},),
        ),
    )

    assert streams.result.count("reachyctl[camera]") == 3


def test_a_value_rich_would_read_as_an_unmatched_tag_does_not_crash_the_command() -> (
    None
):
    """An unmatched close tag raises out of the console, aborting the render.

    Exception text reaches the summary through `Reporter.failure`, and nothing
    stops a path or a message containing one.
    """
    reporter, streams = reporter_for(terminal=True)

    code = reporter.failure("probe", "no such file: /tmp/[/oddly-named")

    assert code is ExitCode.FAILURE
    assert "[/oddly-named" in streams.result
