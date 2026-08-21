"""That one check going wrong changes nothing about the others.

The two properties this file exists for are both about what happens when
something is broken, which is the only time anybody runs a diagnosis. A check
that fails, or raises, must not stop the run or alter another check's outcome;
and a check whose prerequisites are absent must be skipped rather than failed,
because telling an operator who has not configured a groundstation that their
installation is broken is how output stops being read.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import pytest
from checks_support import FakeDaemon, healthy_context

from reachy_checks import (
    CHECKS,
    DAEMON_REACHABLE,
    GROUNDSTATION_CAPABILITIES,
    GROUNDSTATION_ROUND_TRIP,
    GROUNDSTATION_SESSION,
    Check,
    CheckContext,
    CheckResult,
    DaemonInfo,
    Finding,
    Outcome,
    Probe,
    Remediation,
    Requirement,
    counts_of,
    identifiers,
    run_checks,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

REMEDIATION: Final = Remediation(explanation="Do the thing.", command="do-the-thing")


async def _passes(context: CheckContext) -> Finding:
    """Find nothing wrong.

    Args:
        context: Ignored.

    Returns:
        A passing finding.
    """
    del context
    return Finding.passed("all well")


async def _fails(context: CheckContext) -> Finding:
    """Find something wrong.

    Args:
        context: Ignored.

    Returns:
        A failing finding.
    """
    del context
    return Finding.failed("not well")


async def _explodes(context: CheckContext) -> Finding:
    """Fall over rather than answering.

    Args:
        context: Ignored.

    Returns:
        Never.

    Raises:
        RuntimeError: Always. This models an adapter reaching a robot over a
            network, which can throw anything at all.
    """
    del context
    message = "the adapter fell over"
    raise RuntimeError(message)


def _check(identifier: str, probe: Probe, *requires: Requirement) -> Check:
    """Build a check for these tests.

    Args:
        identifier: What to call it.
        probe: What it calls.
        requires: What it declares it needs.

    Returns:
        The declaration.
    """
    return Check(
        identifier=identifier,
        description=f"the {identifier} check",
        requires=requires,
        probe=probe,
        remediation=REMEDIATION,
    )


def _by_identifier(results: Sequence[CheckResult], identifier: str) -> CheckResult:
    """Find one result by name.

    Args:
        results: What the run produced.
        identifier: Which one to take.

    Returns:
        That result.
    """
    return next(result for result in results if result.identifier == identifier)


@pytest.mark.asyncio
async def test_a_failing_check_does_not_stop_the_ones_after_it() -> None:
    """An operator with a broken groundstation still learns about the daemon."""
    checks = (
        _check("first", _fails),
        _check("second", _passes),
        _check("third", _passes),
    )

    run = await run_checks(CheckContext(), checks)

    assert [result.outcome for result in run.results] == [
        Outcome.FAILED,
        Outcome.PASSED,
        Outcome.PASSED,
    ]


@pytest.mark.asyncio
async def test_a_check_that_raises_becomes_a_failure_rather_than_a_traceback() -> None:
    """A diagnosis tool that ends in a traceback has diagnosed nothing."""
    checks = (_check("boom", _explodes), _check("after", _passes))

    run = await run_checks(CheckContext(), checks)

    boom = _by_identifier(run.results, "boom")
    assert boom.outcome is Outcome.FAILED
    assert "RuntimeError" in boom.summary
    assert "the adapter fell over" in boom.summary
    assert boom.remediation is REMEDIATION
    assert _by_identifier(run.results, "after").passed


@pytest.mark.asyncio
async def test_a_check_whose_requirements_are_absent_is_skipped_not_failed() -> None:
    """Not having configured something is not the same as it being broken."""
    checks = (_check("needs-one", _passes, Requirement.GROUNDSTATION),)

    run = await run_checks(CheckContext(), checks)

    result = run.results[0]
    assert result.outcome is Outcome.SKIPPED
    assert result.remediation is None
    assert result.detail["missing"] == ("groundstation",)
    assert run.ok


@pytest.mark.asyncio
async def test_a_skip_carries_the_caller_s_reason_when_it_gave_one() -> None:
    """Absence means different things to different callers, so the caller says why."""
    checks = (_check("needs-one", _passes, Requirement.DAEMON),)
    context = CheckContext(
        unavailable={Requirement.DAEMON: "this tool cannot reach a robot yet"},
    )

    run = await run_checks(context, checks)

    assert run.results[0].summary == "this tool cannot reach a robot yet"


@pytest.mark.asyncio
async def test_a_skip_falls_back_to_a_neutral_reason() -> None:
    """A caller that says nothing still produces a line rather than a blank."""
    checks = (_check("needs-one", _passes, Requirement.MODELS),)

    run = await run_checks(CheckContext(), checks)

    assert run.results[0].summary == "no model directory was supplied"


@pytest.mark.asyncio
async def test_a_skip_names_every_requirement_that_was_missing() -> None:
    """A check needing two things absent says so about both, not about the first."""
    checks = (_check("needs-two", _passes, Requirement.DAEMON, Requirement.INTENT),)

    run = await run_checks(CheckContext(), checks)

    result = run.results[0]
    assert result.detail["missing"] == ("daemon", "intent")
    assert "robot's daemon" in result.summary
    assert "supposed to be" in result.summary


@pytest.mark.asyncio
async def test_a_partially_supplied_check_still_runs_when_all_of_it_is_there() -> None:
    """Two requirements both met is a check that runs, not one that half-skips."""
    checks = (_check("needs-two", _passes, Requirement.DAEMON, Requirement.INTENT),)

    run = await run_checks(healthy_context(), checks)

    assert run.results[0].passed


@pytest.mark.asyncio
async def test_the_observer_sees_every_result_as_it_arrives() -> None:
    """A run waits on a network, so progress is reported while it is happening."""
    seen: list[str] = []
    checks = (_check("first", _passes), _check("second", _fails))

    await run_checks(
        CheckContext(), checks, observer=lambda r: seen.append(r.identifier)
    )

    assert seen == ["first", "second"]


@pytest.mark.asyncio
async def test_the_whole_real_registry_runs_against_a_healthy_world() -> None:
    """Every declaration is exercised together, not only the ones a test named."""
    run = await run_checks(healthy_context())

    assert [result.identifier for result in run.results] == list(identifiers())
    assert run.ok
    assert not run.skipped
    assert run.summary().startswith("every link is healthy")


@pytest.mark.asyncio
async def test_the_whole_real_registry_names_the_first_broken_link() -> None:
    """REQ-054's scenario: the daemon is fine and the groundstation is not."""
    context = healthy_context()
    broken = CheckContext(
        daemon=context.daemon,
        groundstation=None,
        models=context.models,
        intent=context.intent,
        unavailable={Requirement.GROUNDSTATION: "no groundstation is configured"},
    )

    run = await run_checks(broken)

    assert run.ok
    assert len(run.skipped) == 3
    assert {result.identifier for result in run.skipped} == {
        GROUNDSTATION_SESSION,
        GROUNDSTATION_CAPABILITIES,
        GROUNDSTATION_ROUND_TRIP,
    }
    assert "not everything was checked" in run.summary()


@pytest.mark.asyncio
async def test_a_broken_daemon_is_named_as_the_first_broken_link() -> None:
    """The summary answers "which link?" without the reader scanning the table."""
    context = CheckContext(
        daemon=FakeDaemon(info=DaemonInfo(responding=False, complaint="no route")),
        groundstation=healthy_context().groundstation,
    )

    run = await run_checks(context)

    assert not run.ok
    assert run.first_failure is not None
    assert run.first_failure.identifier == DAEMON_REACHABLE
    assert run.summary().startswith(f"the first broken link is {DAEMON_REACHABLE}")
    assert counts_of(run.results)["failed"] >= 1


def test_the_counts_carry_every_outcome_even_when_nothing_produced_one() -> None:
    """A script that reads `failed` cannot be made to raise by a healthy robot."""
    assert counts_of(()) == {"passed": 0, "failed": 0, "skipped": 0}


def test_the_registry_is_what_the_runner_runs_by_default() -> None:
    """The default is the real registry, not a copy a test could keep in step."""
    assert identifiers() == tuple(check.identifier for check in CHECKS)
