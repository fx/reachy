"""What `doctor` reports, what it refuses, and what it never prints.

The session that works is exercised against a real groundstation in
`test_reachyctl_doctor_integration.py`. What is tested here is everything
around it: the declaration document an operator writes, the shape of the report
both renderings are built from, the exit statuses a script reads, and the one
guarantee that has to hold on every path — that a credential does not appear in
any rendering of any failure.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from reachyctl_support import reporter_for

from reachy_checks import (
    APPLICATION_RUNNING,
    DAEMON_REACHABLE,
    GROUNDSTATION_ROUND_TRIP,
    GROUNDSTATION_SESSION,
    CheckResult,
    CheckRun,
    Intent,
    Outcome,
    Remediation,
)
from reachy_session_client import (
    ClientTransport,
    ConnectionFailedError,
    Credential,
)
from reachyctl.declaration import _configuration, load_intent
from reachyctl.doctor import (
    DoctorPlan,
    execute,
    report_for,
)
from reachyctl.errors import ConfigurationError
from reachyctl.exits import ExitCode
from reachyctl.output import OutputFormat
from reachyctl.probe import DEFAULT_CAPABILITIES

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from reachy_session_client import TransportFactory

# RFC 5737 TEST-NET-2 — see the root AGENTS.md on what may enter a tracked file.
URL: Final = "ws://198.51.100.10:8080/v1/session"

DOCUMENT: Final = Path("/declared/intent.json")

# A placeholder credential carrying every character the renderings transform on
# their way out: a backslash, a tab and a newline. Each of them is rewritten by
# the escaping the plain rendering does and by the `repr` a nested value gets,
# so a redactor shown the value only after one of those transformations matches
# nothing and the secret goes out in transformed form. Never anybody's — see
# the root AGENTS.md.
AWKWARD_CREDENTIAL: Final = "example\\secret\twith\na-newline"


def _reader(content: str) -> Callable[[Path], str]:
    """Build a reader that hands back one document.

    Args:
        content: What the file holds.

    Returns:
        A callable shaped like the reader `load_intent` takes.
    """

    def read(path: Path) -> str:
        """Hand back the prepared document.

        Args:
            path: Ignored.

        Returns:
            The content.
        """
        del path
        return content

    return read


def _load(content: str) -> Intent:
    """Parse a declaration without performing any input.

    Args:
        content: What the document holds.

    Returns:
        The declared intent.
    """
    return load_intent(DOCUMENT, _reader(content))


def _refusing(detail: str) -> TransportFactory:
    """Build a transport factory that never connects.

    Args:
        detail: What the failure says. A test passes a credential here to model
            the case REQ-059 is written about: a secret ending up inside an
            exception raised somewhere nobody controls.

    Returns:
        A callable shaped like a transport factory.
    """

    async def open_transport(url: str) -> ClientTransport:
        """Fail to connect.

        Args:
            url: Ignored.

        Returns:
            Never.

        Raises:
            ConnectionFailedError: Always.
        """
        del url
        raise ConnectionFailedError(detail)

    return open_transport


def _run(*results: CheckResult) -> CheckRun:
    """Build a run holding some results.

    Args:
        results: What the checks found.

    Returns:
        The run.
    """
    return CheckRun(results)


def _result(
    identifier: str,
    outcome: Outcome,
    detail: Mapping[str, object] | None = None,
) -> CheckResult:
    """Build one result.

    Args:
        identifier: Which check.
        outcome: How it ended.
        detail: The measured values.

    Returns:
        The result.
    """
    return CheckResult(
        identifier=identifier,
        description=f"the {identifier} check",
        outcome=outcome,
        summary=f"{identifier} says {outcome.value}",
        remediation=(
            Remediation(explanation="Start it.", command="reachyctl app start")
            if outcome is Outcome.FAILED
            else None
        ),
        detail=detail or {},
    )


PLAN: Final = DoctorPlan(url=URL, capabilities=DEFAULT_CAPABILITIES)


def test_a_declaration_names_the_settings_and_the_identity() -> None:
    """The whole document an operator writes, read back."""
    intent = _load(
        json.dumps(
            {
                "configuration": {"wake_word": "okay nabu"},
                "announced_identity": "reachy-mini-example",
            },
        ),
    )

    assert intent.configuration == {"wake_word": "okay nabu"}
    assert intent.announced_identity == "reachy-mini-example"


def test_a_declaration_may_name_neither() -> None:
    """An empty object is a document that declares nothing, not a malformed one."""
    intent = _load("{}")

    assert intent.configuration == {}
    assert intent.announced_identity is None


def test_a_declaration_that_is_not_json_says_so() -> None:
    """With the position, because that is what makes a typo findable."""
    with pytest.raises(ConfigurationError, match="is not JSON"):
        _load("{not json at all")


def test_a_declaration_that_is_not_an_object_says_what_it_is() -> None:
    """A list is the commonest wrong shape, and it parses as valid JSON."""
    with pytest.raises(ConfigurationError, match="is a list"):
        _load("[]")


def test_a_declaration_with_a_key_the_command_does_not_read_is_refused() -> None:
    """Silently ignoring half a declaration is how configuration goes inert."""
    with pytest.raises(ConfigurationError, match="announced_idenity"):
        _load(json.dumps({"announced_idenity": "typo"}))


def test_a_configuration_that_is_not_an_object_is_refused() -> None:
    """And the message names the shape rather than quoting what was there."""
    with pytest.raises(ConfigurationError, match="configuration that is a str"):
        _load(json.dumps({"configuration": "wake_word=okay nabu"}))


def test_a_setting_that_is_not_a_string_names_the_key_and_not_the_value() -> None:
    """The asymmetry is the point, and both halves of it are asserted.

    A key is a setting's name and is safe to print — without it the message is
    not actionable on a configuration of any size. A value is exactly where a
    credential ends up, so only its type is reported.
    """
    with pytest.raises(ConfigurationError) as raised:
        _load(json.dumps({"configuration": {"api_token": "fine", "threshold": 4}}))

    message = str(raised.value)
    assert "threshold" in message
    assert "int" in message
    assert "4" not in message


def test_a_setting_name_that_is_not_a_string_is_reported_by_its_type() -> None:
    """An object of any kind could be there, and its `repr` is not vouched for.

    Reached through the private helper on purpose: `json.loads` only ever
    produces string keys, so this guard is unreachable through the document an
    operator writes. It is the shape of the function that is being pinned, not
    a path JSON can take — and a message that interpolated an arbitrary object
    would be a leak waiting for the first caller that is not `json.loads`.
    """
    with pytest.raises(ConfigurationError) as raised:
        _configuration({7: "value"}, DOCUMENT)

    message = str(raised.value)
    assert "int" in message
    assert "7" not in message


def test_an_identity_that_is_empty_is_refused() -> None:
    """An empty identity would compare unequal to everything and mean nothing."""
    with pytest.raises(ConfigurationError, match="non-empty string"):
        _load(json.dumps({"announced_identity": ""}))


def test_an_identity_that_is_not_a_string_is_reported_by_its_type() -> None:
    """A number would compare unequal to whatever the satellite announces."""
    with pytest.raises(ConfigurationError) as raised:
        _load(json.dumps({"announced_identity": 7}))

    message = str(raised.value)
    assert "non-empty string" in message
    assert "type int" in message
    assert "7" not in message


def test_a_declaration_that_cannot_be_read_says_why() -> None:
    """The operating system's reason is safe to print; the file's contents are not."""

    def read(path: Path) -> str:
        """Fail to read.

        Args:
            path: Ignored.

        Returns:
            Never.

        Raises:
            FileNotFoundError: Always.
        """
        del path
        raise FileNotFoundError(2, "No such file or directory")

    with pytest.raises(ConfigurationError, match="No such file or directory"):
        load_intent(DOCUMENT, read)


def test_a_declaration_whose_read_carries_no_reason_still_names_the_failure() -> None:
    """A read failure with no reason behind it still names the failure."""

    def read(path: Path) -> str:
        """Fail to read, with no errno behind it.

        Args:
            path: Ignored.

        Returns:
            Never.

        Raises:
            OSError: Always.
        """
        del path
        raise OSError

    with pytest.raises(ConfigurationError, match="OSError"):
        load_intent(DOCUMENT, read)


def test_the_report_carries_one_row_per_check_in_order() -> None:
    """REQ-054 asks for the status of every link individually."""
    run = _run(
        _result(DAEMON_REACHABLE, Outcome.PASSED),
        _result(APPLICATION_RUNNING, Outcome.FAILED),
        _result(GROUNDSTATION_SESSION, Outcome.SKIPPED),
    )

    report = report_for(run, PLAN)

    assert [row["check"] for row in report.rows] == [
        DAEMON_REACHABLE,
        APPLICATION_RUNNING,
        GROUNDSTATION_SESSION,
    ]
    assert [row["status"] for row in report.rows] == ["passed", "failed", "skipped"]


def test_the_report_names_the_first_broken_link() -> None:
    """So a script does not have to scan the rows to find out which one it was."""
    run = _run(
        _result(DAEMON_REACHABLE, Outcome.PASSED),
        _result(APPLICATION_RUNNING, Outcome.FAILED),
        _result(GROUNDSTATION_SESSION, Outcome.FAILED),
    )

    report = report_for(run, PLAN)

    assert report.data["first_failure"] == APPLICATION_RUNNING
    assert not report.ok
    assert APPLICATION_RUNNING in report.summary


def test_a_failing_row_carries_the_command_that_fixes_it() -> None:
    """REQ-055, in the form a script can act on rather than parse out of prose."""
    report = report_for(_run(_result(APPLICATION_RUNNING, Outcome.FAILED)), PLAN)

    assert report.rows[0]["command"] == "reachyctl app start"
    assert report.rows[0]["remediation"] == "Start it."


def test_a_passing_row_carries_no_remediation() -> None:
    """A check that found nothing wrong has nothing to remedy."""
    report = report_for(_run(_result(DAEMON_REACHABLE, Outcome.PASSED)), PLAN)

    assert report.rows[0]["remediation"] is None
    assert report.rows[0]["command"] is None


def test_the_measured_round_trip_is_promoted_out_of_its_row() -> None:
    """It is the number an operator compares between runs, and a monitor graphs."""
    run = _run(
        _result(DAEMON_REACHABLE, Outcome.PASSED),
        _result(GROUNDSTATION_ROUND_TRIP, Outcome.PASSED, {"round_trip_ms": 118.25}),
    )

    assert report_for(run, PLAN).data["round_trip_ms"] == 118.25


def test_the_round_trip_is_absent_rather_than_zero_when_nothing_measured_one() -> None:
    """A zero would read as a measurement that was taken."""
    run = _run(_result(GROUNDSTATION_ROUND_TRIP, Outcome.FAILED))

    assert report_for(run, PLAN).data["round_trip_ms"] is None


def test_the_counts_are_present_whatever_the_run_found() -> None:
    """A script reading `failed` cannot be made to raise by a healthy installation."""
    report = report_for(_run(_result(DAEMON_REACHABLE, Outcome.PASSED)), PLAN)

    assert report.data["passed"] == 1
    assert report.data["failed"] == 0
    assert report.data["skipped"] == 0
    assert report.data["checks"] == 1


def test_the_report_always_carries_the_observer_failures_field() -> None:
    """Empty in the ordinary case rather than missing from it, so a script can read it."""
    report = report_for(_run(_result(DAEMON_REACHABLE, Outcome.PASSED)), PLAN)

    assert report.data["observer_failures"] == ()


def test_a_progress_callback_that_threw_is_reported_rather_than_hidden() -> None:
    """It is a defect in this tool, and an operator should not have to guess at it."""
    run = CheckRun(
        (_result(DAEMON_REACHABLE, Outcome.PASSED),),
        ("daemon.reachable: RuntimeError: the display fell over",),
    )

    report = report_for(run, PLAN)

    assert report.data["observer_failures"] == (
        "daemon.reachable: RuntimeError: the display fell over",
    )
    # The verdict about the robot is still the verdict.
    assert report.ok


def test_a_run_with_no_groundstation_reports_none_rather_than_a_blank() -> None:
    """The field is always there, so a consumer never has to guess why it is missing."""
    plan = DoctorPlan(url=None, capabilities=DEFAULT_CAPABILITIES)

    assert report_for(_run(), plan).data["groundstation"] is None


def test_a_run_with_nothing_configured_skips_everything_and_exits_zero() -> None:
    """Not having configured something is not the same as it being broken."""
    reporter, streams = reporter_for(output_format=OutputFormat.JSON)
    plan = DoctorPlan(url=None, capabilities=DEFAULT_CAPABILITIES)

    code = execute(plan, None, reporter)

    document = json.loads(streams.result)
    assert code == ExitCode.OK
    assert document["ok"]
    assert document["data"]["skipped"] == document["data"]["checks"]
    assert document["data"]["failed"] == 0
    assert "not everything was checked" in document["summary"]


def test_a_skipped_robot_check_says_why_rather_than_implying_a_mistake() -> None:
    """An operator who configured everything they could is told what is missing."""
    reporter, streams = reporter_for(output_format=OutputFormat.JSON)
    plan = DoctorPlan(url=None, capabilities=DEFAULT_CAPABILITIES)

    execute(plan, None, reporter)

    rows = {row["check"]: row for row in json.loads(streams.result)["rows"]}
    assert "--robot" in rows[DAEMON_REACHABLE]["detail"]
    assert "--url" in rows[GROUNDSTATION_SESSION]["detail"]


def test_a_groundstation_that_is_not_there_fails_rather_than_erroring() -> None:
    """`probe` exits UNREACHABLE; `doctor` was asked to find this out, so it reports it."""
    reporter, streams = reporter_for(output_format=OutputFormat.JSON)

    code = execute(
        PLAN,
        Credential("example-credential"),
        reporter,
        _refusing("the connection was refused"),
    )

    document = json.loads(streams.result)
    rows = {row["check"]: row for row in document["rows"]}
    assert code == ExitCode.FAILURE
    assert not document["ok"]
    assert document["data"]["first_failure"] == GROUNDSTATION_SESSION
    assert "the connection was refused" in rows[GROUNDSTATION_SESSION]["detail"]
    assert GROUNDSTATION_SESSION in document["summary"]


#:= docs/specs/reachyctl/index.md#req-059-secrets-are-never-written-to-output
#:% The tool MUST NOT write credentials to its output, its logs, or its error
#:% messages.
@pytest.mark.parametrize(
    ("output_format", "terminal"),
    [
        (OutputFormat.JSON, False),
        (OutputFormat.TEXT, False),
        (OutputFormat.TEXT, True),
    ],
    ids=["structured", "plain", "rich"],
)
def test_a_credential_inside_a_failure_reaches_no_rendering(
    output_format: OutputFormat,
    *,
    terminal: bool,
) -> None:
    """A credential never reaches any rendering, which is reachyctl REQ-059.

    The credential carries a backslash, a tab and a newline, because each of
    them is rewritten by an escape or a `repr` somewhere between a check and a
    stream — and a value the redactor sees only after that transformation
    matches nothing and goes out in transformed form.
    """
    reporter, streams = reporter_for(
        output_format=output_format,
        verbose=True,
        terminal=terminal,
        secrets=(AWKWARD_CREDENTIAL,),
    )

    execute(
        PLAN,
        Credential(AWKWARD_CREDENTIAL),
        reporter,
        # The credential inside the exception is the whole point: this is what
        # a library three levels down does when it puts what it was given into
        # the message it raises.
        _refusing(f"refused for {AWKWARD_CREDENTIAL}"),
    )

    written = streams.result + streams.diagnostics
    # The value as it was configured, the value as the plain rendering would
    # escape it, and the part of it before the first transformed character —
    # which is what survives when a scrub happens after an escape rather than
    # before one.
    escaped = (
        AWKWARD_CREDENTIAL.replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
    )
    for fragment in (AWKWARD_CREDENTIAL, escaped, "example\\secret", "a-newline"):
        assert fragment not in written
