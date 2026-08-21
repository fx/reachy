"""Running every check, whatever the ones before it did.

Two properties, and both of them are about what happens when something is
wrong, which is the only time anybody runs this.

**Independence.** A check that fails does not stop the run and does not change
another check's outcome. An operator whose groundstation is down still learns
whether the daemon is healthy, and the report still names which link broke
first. That includes a check that raises: an adapter reaching a robot over a
network can throw anything, and a diagnosis tool that ends in a traceback has
diagnosed nothing.

That independence extends to the caller's progress callback, which is the one
thing every check is downstream of. An observer that raises would otherwise end
a run partway with the remaining links unreported — the same failure the checks
themselves are guarded against, arriving by a route nobody would think to test.

**Skipping rather than failing.** A check whose prerequisites are absent never
ran, and saying it failed would mean telling an operator with no groundstation
configured that their installation is broken. They would be right to stop
reading the output, and then the output stops working.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from reachy_checks.outcomes import CheckResult, CheckRun, Outcome
from reachy_checks.registry import CHECKS

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from reachy_checks.context import CheckContext
    from reachy_checks.registry import Check

__all__ = ["run_check", "run_checks"]


async def run_check(check: Check, context: CheckContext) -> CheckResult:
    """Run one check, turning every way it can end into a result.

    Args:
        check: The declaration.
        context: What the run was given.

    Returns:
        The result. A check whose requirements are not met is skipped with the
        reason; a probe that raises is a failure naming what it raised, because
        an adapter that threw has told us the thing it was asked about is not
        working.
    """
    missing = context.missing(check.requires)
    if missing:
        return CheckResult(
            identifier=check.identifier,
            description=check.description,
            outcome=Outcome.SKIPPED,
            summary=context.explain(missing),
            detail={"missing": tuple(requirement.value for requirement in missing)},
        )
    try:
        finding = await check.probe(context)
    except Exception as error:
        # Deliberately every exception. This is the boundary between a
        # diagnosis and a crash, and narrowing it to the exception types known
        # today would mean the first unfamiliar adapter failure ends the run
        # with nothing reported about any other link. What was raised is named
        # in the summary; a consumer scrubs it like every other string, because
        # an exception raised three libraries down is exactly where a
        # credential turns up.
        return CheckResult(
            identifier=check.identifier,
            description=check.description,
            outcome=Outcome.FAILED,
            summary=f"the check itself failed: {type(error).__name__}: {error}",
            remediation=check.remediation,
        )
    return CheckResult(
        identifier=check.identifier,
        description=check.description,
        outcome=finding.outcome,
        summary=finding.summary,
        remediation=(check.remediation if finding.outcome is Outcome.FAILED else None),
        detail=finding.detail,
    )


async def run_checks(
    context: CheckContext,
    checks: Sequence[Check] = CHECKS,
    observer: Callable[[CheckResult], None] | None = None,
) -> CheckRun:
    """Run every check in order and collect what they all found.

    Sequentially rather than concurrently, and that is a choice rather than a
    simplification: the groundstation checks share one session, and running
    them at once would mean three sessions measuring three different moments.
    The whole run is one network round trip's worth of work plus whatever the
    robot takes to answer.

    Args:
        context: What the run was given.
        checks: The registry to run. Defaults to the real one.
        observer: Called with each result as it arrives, so a caller can show
            progress on a run that is waiting on a network. It is not given a
            chance to change anything, and it is not given a chance to stop the
            run either — see below.

    Returns:
        Every result, in the order they ran, and one line per time the observer
        raised.
    """
    results: list[CheckResult] = []
    observer_failures: list[str] = []
    for check in checks:
        result = await run_check(check, context)
        results.append(result)
        if observer is None:
            continue
        try:
            observer(result)
        except Exception as error:
            # Every check is independent of every other, and all of them are
            # downstream of this one callback: an observer that throws on the
            # second of nine checks would leave seven links unreported, which
            # is exactly what reachyctl REQ-054 forbids and by a route nobody
            # would think to test. So the run continues — and the failure is
            # recorded rather than swallowed, because a progress display that
            # silently stopped partway is its own puzzle for an operator.
            observer_failures.append(
                f"{check.identifier}: {type(error).__name__}: {error}",
            )
    return CheckRun(tuple(results), tuple(observer_failures))
