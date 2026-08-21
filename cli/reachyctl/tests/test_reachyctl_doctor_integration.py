"""`doctor` against a real groundstation, run as the command an operator types.

Every test here opens a socket and says so with `@pytest.mark.enable_socket`.
The reason is that the groundstation checks are the only ones this change can
run against something real today: the session is opened with the same protocol
client the robot uses, so a groundstation that gets the protocol wrong fails
these checks in the way it would fail a robot. A fake link can show what the
checks do with a session; only a real one shows that a session happens.

A real uvicorn server runs in-process on the loopback interface with an
ephemeral port, the real `reachy_groundstation` application answers, and the
command goes through Click's runner so that argument parsing, rendering and the
exit status are the real ones too.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Final

import pytest
from reachyctl_server import CREDENTIAL, CountingFace, StaticRegistry, serving, wedged
from typer.testing import CliRunner

from reachy_checks import (
    APPLICATION_RUNNING,
    DAEMON_REACHABLE,
    GROUNDSTATION_CAPABILITIES,
    GROUNDSTATION_ROUND_TRIP,
    GROUNDSTATION_SESSION,
    SessionLink,
)
from reachy_contracts import FACE_CAPABILITY, Capability
from reachy_session_client import Credential
from reachyctl.cli import app
from reachyctl.credentials import CREDENTIAL_VARIABLE
from reachyctl.exits import ExitCode

CONFIGURED: Final = {CREDENTIAL_VARIABLE: CREDENTIAL}

FACE: Final = Capability(name=FACE_CAPABILITY, version=1)

# Long enough that a loaded runner does not flake, short enough that a genuine
# hang fails the suite rather than stalling it.
TIMEOUT: Final = 20.0

runner = CliRunner()


def _rows(output: str) -> dict[str, dict[str, object]]:
    """Read a structured run's rows, keyed by check.

    Args:
        output: What the command wrote to standard output.

    Returns:
        Each row, by the identifier in its `check` field.
    """
    return {str(row["check"]): row for row in json.loads(output)["rows"]}


@pytest.mark.enable_socket(reason="the session is the thing under test")
def test_a_healthy_groundstation_passes_every_link_it_owns() -> None:
    """REQ-054's second scenario, for the half of the chain that exists today."""
    with serving(StaticRegistry(CountingFace())) as url:
        result = runner.invoke(
            app,
            ["--output", "json", "doctor", "--url", url, "--timeout", str(TIMEOUT)],
            env=CONFIGURED,
        )

    document = json.loads(result.stdout)
    rows = _rows(result.stdout)
    assert result.exit_code == ExitCode.OK, result.stdout
    assert rows[GROUNDSTATION_SESSION]["status"] == "passed"
    assert rows[GROUNDSTATION_CAPABILITIES]["status"] == "passed"
    assert FACE_CAPABILITY in str(rows[GROUNDSTATION_CAPABILITIES]["detail"])
    assert rows[GROUNDSTATION_ROUND_TRIP]["status"] == "passed"
    # Measured and reported even though everything passed, which is the point
    # of measuring it: the link is the component least likely to be suspected.
    assert isinstance(document["data"]["round_trip_ms"], float)
    assert document["data"]["failed"] == 0


@pytest.mark.enable_socket(reason="the session is the thing under test")
def test_the_robot_side_is_skipped_and_says_it_is_not_configurable_yet() -> None:
    """Reaching a robot arrives in a later change, and the output says so plainly."""
    with serving(StaticRegistry(CountingFace())) as url:
        result = runner.invoke(
            app,
            ["--output", "json", "doctor", "--url", url, "--timeout", str(TIMEOUT)],
            env=CONFIGURED,
        )

    rows = _rows(result.stdout)
    assert rows[DAEMON_REACHABLE]["status"] == "skipped"
    assert rows[APPLICATION_RUNNING]["status"] == "skipped"
    assert "cannot open a connection to the robot yet" in str(
        rows[DAEMON_REACHABLE]["detail"],
    )


@pytest.mark.enable_socket(reason="the session is the thing under test")
def test_a_groundstation_that_agrees_to_nothing_fails_the_capability_check() -> None:
    """A session that would never answer a frame is up, and that is not healthy."""
    with serving(StaticRegistry()) as url:
        result = runner.invoke(
            app,
            ["--output", "json", "doctor", "--url", url, "--timeout", str(TIMEOUT)],
            env=CONFIGURED,
        )

    document = json.loads(result.stdout)
    rows = _rows(result.stdout)
    assert result.exit_code == ExitCode.FAILURE
    assert rows[GROUNDSTATION_SESSION]["status"] == "passed"
    assert rows[GROUNDSTATION_CAPABILITIES]["status"] == "failed"
    # Skipped rather than failed: there was never anything to time, and the
    # check above has already named the fault.
    assert rows[GROUNDSTATION_ROUND_TRIP]["status"] == "skipped"
    assert document["data"]["first_failure"] == GROUNDSTATION_CAPABILITIES
    assert GROUNDSTATION_CAPABILITIES in document["summary"]


@pytest.mark.enable_socket(reason="the session is the thing under test")
def test_a_wedged_service_is_reported_rather_than_waited_out() -> None:
    """A process that accepts and then answers nothing is the case a bound exists for."""
    with wedged() as url:
        result = runner.invoke(
            app,
            ["--output", "json", "doctor", "--url", url, "--timeout", "1.0"],
            env=CONFIGURED,
        )

    rows = _rows(result.stdout)
    assert result.exit_code == ExitCode.FAILURE
    assert rows[GROUNDSTATION_SESSION]["status"] == "failed"
    assert "no session was opened within" in str(rows[GROUNDSTATION_SESSION]["detail"])
    # One fault, one red line: the two checks downstream of the session report
    # themselves skipped and point at it rather than repeating it.
    assert rows[GROUNDSTATION_CAPABILITIES]["status"] == "skipped"
    assert rows[GROUNDSTATION_ROUND_TRIP]["status"] == "skipped"
    assert json.loads(result.stdout)["data"]["failed"] == 1


@pytest.mark.enable_socket(reason="the session is the thing under test")
def test_the_human_rendering_carries_the_same_verdict_as_the_structured_one() -> None:
    """The plain table is what a person reads first, and it must not say less."""
    with serving(StaticRegistry()) as url:
        result = runner.invoke(
            app,
            ["doctor", "--url", url, "--timeout", str(TIMEOUT)],
            env=CONFIGURED,
        )

    assert result.exit_code == ExitCode.FAILURE
    assert GROUNDSTATION_CAPABILITIES in result.stdout
    assert "failed" in result.stdout
    assert "doctor\tfailed" in result.stdout


@pytest.mark.enable_socket(reason="the session is the thing under test")
def test_a_frame_the_groundstation_cannot_decode_is_reported_as_an_error() -> None:
    """A link that answers with errors rather than results is named as that.

    This drives `SessionLink` directly rather than through the command, because
    the frame it sends is not an operator's to choose: a real compressed image
    is what the command always sends, and what is being exercised here is what
    the link says when the far end rejects whatever it was given.
    """

    async def measure(url: str) -> tuple[bool, str]:
        """Open a session and send bytes that are not an image.

        Args:
            url: Where the groundstation is listening.

        Returns:
            Whether a session was established, and the complaint about the
            result that never arrived.
        """
        link = SessionLink(
            url=url,
            credential=Credential(CREDENTIAL),
            capabilities=(FACE,),
            timeout=5.0,
            frame=b"these bytes are not a compressed image",
        )
        try:
            report = await link.inspect()
        finally:
            await link.aclose()
        return report.established, report.result_complaint

    with serving(StaticRegistry(CountingFace())) as url:
        established, complaint = asyncio.run(measure(url))

    assert established
    assert "error" in complaint
