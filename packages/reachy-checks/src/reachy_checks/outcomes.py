"""What a check produced, and what a whole run of them adds up to.

Three outcomes, not two. A check that ran and found the thing wrong is
`FAILED`; a check whose prerequisites were not there never ran and is
`SKIPPED`. Collapsing the second into the first is the change that makes
diagnosis output worth ignoring: an operator who has not configured a
groundstation would be told their installation is broken, and after the third
time they stop reading the output at all.

Nothing here knows how a result is rendered or what a process exits with. That
is the consumer's — `reachyctl` decided it once in `reachyctl.output` and
`reachyctl.exits`, and an Ansible play decides it differently — and a registry
that made either choice would be making it for both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "CheckResult",
    "CheckRun",
    "Finding",
    "Outcome",
    "Remediation",
    "counts_of",
]


class Outcome(StrEnum):
    """How one check ended.

    Attributes:
        PASSED: The check ran and found what it was looking for.
        FAILED: The check ran and did not.
        SKIPPED: The check did not run, because something it needs was absent.
    """

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True, kw_only=True)
class Remediation:
    """How to fix a check that failed.

    reachyctl REQ-055 asks that a failing check say how to fix it, and the
    useful form of that is a command an operator can run, not a paragraph
    describing what they should arrange to happen. So the command is a field of
    its own rather than a sentence inside the explanation: a script reading the
    structured output gets something it can execute, and the runbook keyed to
    these identifiers can quote it without extracting it from prose.

    Some checks genuinely have no command. Nothing this tool ships starts a
    daemon it cannot reach, and pretending otherwise by naming a plausible
    command would be worse than saying so. For those, `command` is empty and
    the explanation carries the whole remedy.

    Attributes:
        explanation: What is wrong and what fixing it means. Always present.
        command: A command that fixes it, when one exists. Empty when none
            does.
    """

    explanation: str
    command: str = ""

    def render(self) -> str:
        """Say the whole remedy in one line.

        Returns:
            The explanation, followed by the command when there is one.
        """
        if not self.command:
            return self.explanation
        return f"{self.explanation} Run: {self.command}"


@dataclass(frozen=True, slots=True, kw_only=True)
class Finding:
    """What a probe found, before the check it belongs to is named.

    A probe answers about the world; the identifier, the description and the
    remediation come from the declaration. Keeping them apart is what stops a
    probe inventing a remediation of its own — the strings are a published
    interface and a per-run variant of one would not be greppable.

    Attributes:
        outcome: How it ended.
        summary: One line an operator reads, naming what was actually found.
        detail: The measured values, for the structured output. Never a
            credential and never a configuration value: see the module
            documentation of `reachy_checks.probes`.
    """

    outcome: Outcome
    summary: str
    detail: Mapping[str, object] = field(default_factory=dict)

    @classmethod
    def passed(
        cls, summary: str, detail: Mapping[str, object] | None = None
    ) -> Finding:
        """Report that the check found what it was looking for.

        Args:
            summary: What was found.
            detail: The measured values.

        Returns:
            The finding.
        """
        return cls(outcome=Outcome.PASSED, summary=summary, detail=detail or {})

    @classmethod
    def failed(
        cls, summary: str, detail: Mapping[str, object] | None = None
    ) -> Finding:
        """Report that the check ran and did not find it.

        Args:
            summary: What was found instead.
            detail: The measured values.

        Returns:
            The finding.
        """
        return cls(outcome=Outcome.FAILED, summary=summary, detail=detail or {})

    @classmethod
    def skipped(
        cls,
        summary: str,
        detail: Mapping[str, object] | None = None,
    ) -> Finding:
        """Report that the check could not run, and why that is not a failure.

        The runner already skips a check whose declared prerequisites are
        absent. This is the finer grain underneath that: a check may have
        everything it declared and still find that the particular thing it
        compares against was not declared — an intent that names no announced
        identity, for instance.

        Args:
            summary: What was missing.
            detail: The measured values.

        Returns:
            The finding.
        """
        return cls(outcome=Outcome.SKIPPED, summary=summary, detail=detail or {})


@dataclass(frozen=True, slots=True, kw_only=True)
class CheckResult:
    """One check, run or skipped, with everything a report needs about it.

    Attributes:
        identifier: The check's stable name. Greppable, and what the
            troubleshooting runbook is keyed to.
        description: What the check is for, in one line.
        outcome: How it ended.
        summary: What was found.
        remediation: How to fix it. Present only when the outcome is `FAILED` —
            a passing check has nothing to remedy, and a skipped one is not
            broken.
        detail: The measured values.
    """

    identifier: str
    description: str
    outcome: Outcome
    summary: str
    remediation: Remediation | None = None
    detail: Mapping[str, object] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        """Whether this check ran and found something wrong.

        Returns:
            True when the outcome is `FAILED`.
        """
        return self.outcome is Outcome.FAILED

    @property
    def passed(self) -> bool:
        """Whether this check ran and found nothing wrong.

        Returns:
            True when the outcome is `PASSED`.
        """
        return self.outcome is Outcome.PASSED

    @property
    def skipped(self) -> bool:
        """Whether this check did not run.

        Returns:
            True when the outcome is `SKIPPED`.
        """
        return self.outcome is Outcome.SKIPPED


@dataclass(frozen=True, slots=True)
class CheckRun:
    """Every result from one pass over the registry, and what they amount to.

    The chain is walked in registry order, which is the order the links sit in
    between an operator and a working robot. That order is for reading: no
    check depends on another's result, so the first failure in this sequence is
    the earliest broken link rather than the cause of everything after it.

    Attributes:
        results: One per registered check, in the order they ran.
        observer_failures: One line per time the caller's progress callback
            raised. It is carried rather than discarded because a callback that
            throws is a defect in the consumer, and one that failed silently
            would leave an operator with progress output that simply stopped
            partway and no reason for it.
    """

    results: tuple[CheckResult, ...]
    observer_failures: tuple[str, ...] = ()

    @property
    def passed(self) -> tuple[CheckResult, ...]:
        """The checks that ran and found nothing wrong.

        Returns:
            Those results, in order.
        """
        return tuple(result for result in self.results if result.passed)

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        """The checks that ran and found something wrong.

        Returns:
            Those results, in order.
        """
        return tuple(result for result in self.results if result.failed)

    @property
    def skipped(self) -> tuple[CheckResult, ...]:
        """The checks that did not run.

        Returns:
            Those results, in order.
        """
        return tuple(result for result in self.results if result.skipped)

    @property
    def ok(self) -> bool:
        """Whether the run found anything wrong.

        Skipped checks do not make a run negative. An operator with no
        groundstation configured has not been told their installation is
        broken; they have been told nothing about the groundstation, and the
        counts say how many checks that was. A caller that wants a complete
        diagnosis rather than a clean one asks for `skipped` to be empty.

        Returns:
            True when no check failed.
        """
        return not self.failures

    @property
    def first_failure(self) -> CheckResult | None:
        """The earliest broken link in the chain.

        Returns:
            The first failing result in registry order, or `None`.
        """
        failures = self.failures
        return failures[0] if failures else None

    #:= docs/specs/reachyctl/index.md#req-054-diagnosis-covers-the-whole-chain-and-names-the-failing-link
    #:% The doctor command MUST report the status of every link between the operator and
    #:% a working robot individually, and MUST identify which link is broken when one
    #:% is.
    def summary(self) -> str:
        """Say in one line what the run found, naming the broken link.

        Returns:
            A line naming the first failing check when one failed, and the
            counts otherwise. The per-check statuses are in `results`; this is
            the sentence that answers "which link is broken?" without the
            reader having to scan the table for it.
        """
        counts = (
            f"{len(self.passed)} passed, {len(self.failures)} failed, "
            f"{len(self.skipped)} skipped"
        )
        # Appended rather than allowed to replace the verdict: a callback that
        # threw says nothing about the robot, and the answer the operator asked
        # for still comes first.
        aside = (
            ""
            if not self.observer_failures
            else (
                f"; progress reporting itself failed "
                f"{len(self.observer_failures)} time(s)"
            )
        )
        broken = self.first_failure
        if broken is not None:
            return (
                f"the first broken link is {broken.identifier}: "
                f"{broken.summary} ({counts}){aside}"
            )
        if self.skipped:
            return f"nothing failed, but not everything was checked ({counts}){aside}"
        return f"every link is healthy ({counts}){aside}"


def counts_of(results: Sequence[CheckResult]) -> dict[str, int]:
    """Count results by outcome, with every outcome present.

    A caller building a structured document wants the same keys whatever the
    run found, so an outcome nothing produced is a zero rather than a missing
    field: a script that reads `failed` cannot be made to raise by a healthy
    installation.

    Args:
        results: What the run produced.

    Returns:
        One entry per `Outcome`, keyed by its value.
    """
    tally = dict.fromkeys((outcome.value for outcome in Outcome), 0)
    for result in results:
        tally[result.outcome.value] += 1
    return tally
