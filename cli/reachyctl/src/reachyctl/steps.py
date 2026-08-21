"""A sequence of verifiable steps, reported as it happens.

`deploy`, `config apply` and `app` are all the same shape: several things done
in order against a robot, on a link where each one costs a visible fraction of a
second, with the last one deciding whether the whole thing worked. This module
is that shape, once.

Two things it makes true that a loop of `await` calls would not.

**Progress is live, and it is the same progress the report carries.** A step
announces itself before it runs and records what it did afterwards, so an
operator watching a slow deploy sees where it is, and the structured output at
the end lists exactly the same steps — reachyctl REQ-058 asks for a machine
readable result, and a result that omitted the steps a person watched would be
two accounts of one run.

**A planned step is not a done step.** Preview mode runs the steps that only
read and records the rest as `planned`, which is what REQ-052 means by reporting
the changes it would make and making none of them. The distinction is in the
data rather than in a sentence, so a script can tell a preview from a run
without parsing prose, and a test can assert that nothing was done by asserting
that nothing says it was.

Nothing here reaches the robot or decides what a step means. Steps are supplied
by the command; this records them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from reachyctl.output import Reporter

__all__ = ["StepLog", "StepOutcome", "StepResult"]


class StepOutcome(StrEnum):
    """How one step of a sequence ended.

    Attributes:
        DONE: It ran and did what it was for.
        PLANNED: It was not run, because this was a preview. Something would
            have changed and nothing did.
        SKIPPED: It was not run because it had nothing to do — a setting
            already in force, an application already stopped.
        FAILED: It ran and did not do what it was for.
    """

    DONE = "done"
    PLANNED = "planned"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True, kw_only=True)
class StepResult:
    """One step, and what became of it.

    Attributes:
        name: What the step is called. Stable and greppable, like a check's
            identifier, because these are what an operator reads back to
            somebody over a telephone.
        outcome: How it ended.
        detail: One line naming what actually happened — the version that was
            installed, the number of settings that changed, the reason it
            failed.
    """

    name: str
    outcome: StepOutcome
    detail: str

    @property
    def failed(self) -> bool:
        """Whether this step ran and did not work.

        Returns:
            True when the outcome is `FAILED`.
        """
        return self.outcome is StepOutcome.FAILED


@dataclass(slots=True)
class StepLog:
    """The steps of one run, recorded as they happen and reported as they go.

    Attributes:
        reporter: Where progress is written. Every line goes through it, so
            every line is scrubbed — see `reachyctl.output`.
        results: What has happened so far, in order.
    """

    reporter: Reporter
    results: list[StepResult] = field(default_factory=list)

    def begin(self, name: str, detail: str) -> None:
        """Say that a step is starting.

        Args:
            name: The step's name.
            detail: What it is about to do.
        """
        self.reporter.note(f"{name}: {detail}")

    def done(self, name: str, detail: str) -> StepResult:
        """Record a step that did what it was for.

        Args:
            name: The step's name.
            detail: What it did.

        Returns:
            The result.
        """
        return self._record(name, StepOutcome.DONE, detail)

    def planned(self, name: str, detail: str) -> StepResult:
        """Record a step a preview did not run.

        Args:
            name: The step's name.
            detail: What it would have done.

        Returns:
            The result.
        """
        return self._record(name, StepOutcome.PLANNED, detail)

    def skipped(self, name: str, detail: str) -> StepResult:
        """Record a step that had nothing to do.

        Args:
            name: The step's name.
            detail: Why there was nothing to do.

        Returns:
            The result.
        """
        return self._record(name, StepOutcome.SKIPPED, detail)

    def failed(self, name: str, detail: str) -> StepResult:
        """Record a step that did not do what it was for.

        Args:
            name: The step's name.
            detail: What went wrong.

        Returns:
            The result.
        """
        return self._record(name, StepOutcome.FAILED, detail)

    def _record(self, name: str, outcome: StepOutcome, detail: str) -> StepResult:
        """Record one step and say so.

        Args:
            name: The step's name.
            outcome: How it ended.
            detail: What happened.

        Returns:
            The result.
        """
        result = StepResult(name=name, outcome=outcome, detail=detail)
        self.results.append(result)
        self.reporter.note(f"{name}: {outcome.value} — {detail}")
        return result

    @property
    def ok(self) -> bool:
        """Whether every step that ran did what it was for.

        Returns:
            True when nothing failed. A preview in which every mutating step is
            `planned` is a successful preview, which is what makes a preview
            usable in a script.
        """
        return not any(result.failed for result in self.results)

    @property
    def rows(self) -> tuple[Mapping[str, object], ...]:
        """The steps, shaped for a report.

        Returns:
            One row per step, in the order they happened.
        """
        return tuple(
            {
                "step": result.name,
                "status": result.outcome.value,
                "detail": result.detail,
            }
            for result in self.results
        )
