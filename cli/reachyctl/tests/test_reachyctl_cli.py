"""The command surface: what is registered, what it refuses, and what it exits with.

These run the real application through Click's own runner, so what is exercised
is the argument parsing, the global options, the error handling and the exit
statuses an operator and a script will meet. No session is opened by any of
them: every one stops before the groundstation is reached, which is what makes
them a test of the command layer rather than a slower copy of the integration
test.

Nothing here writes a file either. The paths that would need one — a directory
of frames, a declaration of intent — are exercised by naming something that is
not there, because that is the same branch and the operator's commonest
mistake. Naming a path that does not exist reads nothing; what the parsing
rules do with a document they can read is exercised without any input at all,
against an injected reader, in the per-command test modules.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import json
from typing import Final

import pytest
from reachyctl_support import CREDENTIAL
from typer.testing import CliRunner

from reachyctl.cli import app
from reachyctl.credentials import CREDENTIAL_VARIABLE, URL_VARIABLE
from reachyctl.exits import ExitCode

# RFC 5737 TEST-NET-2 — see the root AGENTS.md on what may enter a tracked file.
URL: Final = "ws://198.51.100.10:8080/v1/session"

CONFIGURED: Final = {CREDENTIAL_VARIABLE: CREDENTIAL}

runner = CliRunner()


def test_the_help_lists_every_command_the_tool_has() -> None:
    """Including `bench`, whose name is registered before its body exists."""
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == ExitCode.OK
    assert "probe" in result.stdout
    assert "bench" in result.stdout


def test_the_version_is_answerable_without_a_credential_or_a_groundstation() -> None:
    """It is the first thing anybody runs, and it must not need anything."""
    result = runner.invoke(app, ["--version"], env={})

    assert result.exit_code == ExitCode.OK
    assert result.stdout.startswith("reachyctl ")


def test_running_the_tool_with_no_arguments_shows_the_help() -> None:
    """Rather than a stack trace or a silent success."""
    result = runner.invoke(app, [])

    assert "Usage" in result.stdout


def test_an_unknown_command_is_a_usage_error() -> None:
    """Which is Click's own status, and is why nothing else claims that number."""
    result = runner.invoke(app, ["teleport"])

    assert result.exit_code == ExitCode.USAGE


def test_probe_without_an_address_is_a_usage_error() -> None:
    """There is no default address: this repository tracks nobody's network."""
    result = runner.invoke(app, ["probe"], env={CREDENTIAL_VARIABLE: CREDENTIAL})

    assert result.exit_code == ExitCode.USAGE


#:= docs/specs/reachyctl/index.md#req-059-secrets-are-never-written-to-output
#:% The tool MUST NOT write credentials to its output, its logs, or its error
#:% messages.
def test_probe_with_no_credential_says_where_to_put_one() -> None:
    """And names no option that would take the credential itself."""
    result = runner.invoke(app, ["probe", "--url", URL], env={})

    assert result.exit_code == ExitCode.CONFIGURATION
    assert CREDENTIAL_VARIABLE in result.stdout
    assert "--credential" in result.stdout
    assert "--credential " not in result.stdout


def test_probe_with_no_frame_source_says_which_options_provide_one() -> None:
    """Nothing was asked of the groundstation, so this is not a failed probe."""
    result = runner.invoke(app, ["probe", "--url", URL], env=CONFIGURED)

    assert result.exit_code == ExitCode.CONFIGURATION
    assert "--frames" in result.stdout
    assert "--camera" in result.stdout


def test_the_address_can_come_from_the_environment() -> None:
    """A session that is always the same one is configured, not retyped."""
    result = runner.invoke(app, ["probe"], env={**CONFIGURED, URL_VARIABLE: URL})

    # Past argument parsing — a missing address would have been a usage error —
    # and stopped at the next thing that was not given.
    assert result.exit_code == ExitCode.CONFIGURATION
    assert "--frames" in result.stdout


def test_probe_with_two_frame_sources_refuses_rather_than_choosing() -> None:
    """A run that quietly ignored half of what it was told is not a diagnostic."""
    result = runner.invoke(
        app,
        ["probe", "--url", URL, "--frames", "/recordings", "--camera", "0"],
        env=CONFIGURED,
    )

    assert result.exit_code == ExitCode.CONFIGURATION
    assert "exactly one" in result.stdout


def test_probe_pointed_at_a_directory_that_is_not_there() -> None:
    """The operator's commonest mistake, and it costs no session."""
    result = runner.invoke(
        app,
        ["probe", "--url", URL, "--frames", "/not-a-directory-anybody-has"],
        env=CONFIGURED,
    )

    assert result.exit_code == ExitCode.CONFIGURATION
    assert "is not a directory" in result.stdout


def test_probe_with_a_capability_the_contract_would_refuse() -> None:
    """Refused locally, before a session is opened against it."""
    result = runner.invoke(
        app,
        ["probe", "--url", URL, "--capability", "Face"],
        env=CONFIGURED,
    )

    assert result.exit_code == ExitCode.CONFIGURATION
    assert "is not a capability" in result.stdout


def test_probe_with_an_address_that_is_not_a_session_url() -> None:
    """Retrying a configuration mistake is a way to never be told about it."""
    result = runner.invoke(
        app,
        ["probe", "--url", "http://198.51.100.10/v1/session"],
        env=CONFIGURED,
    )

    assert result.exit_code == ExitCode.CONFIGURATION
    assert "ws://" in result.stdout


#:= docs/specs/reachyctl/index.md#req-058-output-is-machine-readable-on-request
#:% Every command that reports results MUST offer a structured output format
#:% suitable for consumption by another program.
@pytest.mark.parametrize(
    ("arguments", "command"),
    [
        (["bench"], "bench"),
        (["probe", "--url", URL], "probe"),
    ],
)
def test_a_failure_is_a_parsable_document_when_structured_output_was_asked_for(
    arguments: list[str],
    command: str,
) -> None:
    """A script gets the same shape whether the answer was yes or no.

    Args:
        arguments: The command to run.
        command: What the document should name.
    """
    result = runner.invoke(app, ["--output", "json", *arguments], env=CONFIGURED)

    document = json.loads(result.stdout)
    assert document["command"] == command
    assert document["ok"] is False
    assert document["summary"]


def test_bench_is_a_registered_name_with_nothing_behind_it_yet() -> None:
    """Registered so the change that implements it adds a body, not a command."""
    result = runner.invoke(app, ["bench"], env=CONFIGURED)

    assert result.exit_code == ExitCode.FAILURE
    assert "not implemented" in result.stdout


def test_the_plain_rendering_is_what_a_script_gets_without_asking() -> None:
    """Click's runner is not a terminal, and neither is a pipe."""
    result = runner.invoke(app, ["bench"], env=CONFIGURED)

    assert "\t" in result.stdout
    assert "\x1b[" not in result.stdout


def test_verbose_detail_goes_to_standard_error_and_not_into_the_result() -> None:
    """So that a structured run stays parsable with `--verbose` on."""
    result = runner.invoke(
        app,
        ["--output", "json", "--verbose", "probe", "--url", URL],
        env=CONFIGURED,
    )

    json.loads(result.stdout)
    assert result.exit_code == ExitCode.CONFIGURATION


#:= docs/specs/reachyctl/index.md#req-059-secrets-are-never-written-to-output
#:% The tool MUST NOT write credentials to its output, its logs, or its error
#:% messages.
def test_an_address_with_a_credential_in_it_is_refused_and_not_echoed() -> None:
    """The address is echoed into the report, so a credential inside one leaks."""
    result = runner.invoke(
        app,
        ["probe", "--url", "wss://someone:example-secret@198.51.100.10/v1/session"],
        env=CONFIGURED,
    )

    assert result.exit_code == ExitCode.CONFIGURATION
    assert "carries no credential" in result.stdout
    assert "example-secret" not in result.stdout


def test_the_help_lists_doctor() -> None:
    """The command an operator reaches for when something is wrong."""
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == ExitCode.OK
    assert "doctor" in result.stdout


def test_doctor_needs_no_address_at_all() -> None:
    """An operator with nothing configured gets a diagnosis of nothing, not an error."""
    result = runner.invoke(app, ["doctor"], env={})

    assert result.exit_code == ExitCode.OK
    assert "not everything was checked" in result.stdout


def test_doctor_with_an_address_and_no_credential_says_where_to_put_one() -> None:
    """A configured groundstation with no credential is a mistake, not a diagnosis."""
    result = runner.invoke(app, ["doctor", "--url", URL], env={})

    assert result.exit_code == ExitCode.CONFIGURATION
    assert CREDENTIAL_VARIABLE in result.stdout
    assert "--credential " not in result.stdout


def test_doctor_with_an_address_that_is_not_a_session_url_contacts_nothing() -> None:
    """Answered beside the option that carried it, before anything is opened."""
    result = runner.invoke(
        app, ["doctor", "--url", "http://198.51.100.10/"], env=CONFIGURED
    )

    assert result.exit_code == ExitCode.CONFIGURATION


def test_doctor_pointed_at_a_declaration_that_is_not_there() -> None:
    """The commonest mistake, and it stops before anything is contacted."""
    result = runner.invoke(
        app,
        ["doctor", "--intent", "/not/a/declaration.json"],
        env=CONFIGURED,
    )

    assert result.exit_code == ExitCode.CONFIGURATION
    assert "could not be read" in result.stdout


def test_doctor_with_a_capability_the_contract_would_refuse() -> None:
    """A typo costs a message rather than a session that negotiates to nothing."""
    result = runner.invoke(
        app,
        ["doctor", "--url", URL, "--capability", "Face"],
        env=CONFIGURED,
    )

    assert result.exit_code == ExitCode.CONFIGURATION
    assert "is not a capability" in result.stdout


def test_doctor_reports_a_failure_as_a_document_when_asked_for_one() -> None:
    """A structured run gets a document for a refusal exactly as for a result."""
    result = runner.invoke(
        app,
        ["--output", "json", "doctor", "--url", URL, "--capability", "Face"],
        env=CONFIGURED,
    )

    document = json.loads(result.stdout)
    assert document["command"] == "doctor"
    assert document["ok"] is False
