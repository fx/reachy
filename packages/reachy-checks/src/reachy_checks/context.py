"""What a run has to work with, and what it says when it has not got it.

A check declares what it needs — a robot connection, a groundstation session, a
model directory, a statement of intent — and the runner compares that against
what the caller supplied. A check whose needs are not met is skipped, and the
skip line says which need was absent.

Why the reason is supplied by the caller rather than written here: absence
means different things to different callers. `reachyctl doctor` has no robot
connection today because nothing can open one until change 0009 lands, which an
operator should be told plainly rather than left to infer from a blank. An
Ansible verification role that reached the robot and then lost it means
something else entirely. Neither reason belongs to the registry, so the
registry holds a neutral default and the caller replaces it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from reachy_checks.ports import GroundstationLink, Intent, ModelFiles, RobotDaemon

__all__ = ["CheckContext", "MissingResourceError", "Requirement"]


class Requirement(StrEnum):
    """Something a check needs before it can say anything.

    Attributes:
        DAEMON: A connection to the robot's daemon.
        GROUNDSTATION: A groundstation endpoint and a credential for it.
        MODELS: A directory of model files to verify.
        INTENT: A statement of what the robot is supposed to be.
    """

    DAEMON = "daemon"
    GROUNDSTATION = "groundstation"
    MODELS = "models"
    INTENT = "intent"

    @property
    def absent(self) -> str:
        """Say what it means that this was not supplied.

        Returns:
            A neutral line, for a caller that supplied no reason of its own.
        """
        return _ABSENT[self]


_ABSENT: Mapping[Requirement, str] = {
    Requirement.DAEMON: "no connection to the robot's daemon was supplied",
    Requirement.GROUNDSTATION: "no groundstation endpoint and credential were supplied",
    Requirement.MODELS: "no model directory was supplied",
    Requirement.INTENT: "nothing declares what this robot is supposed to be",
}


class MissingResourceError(RuntimeError):
    """A probe asked for something its check did not declare it needs.

    Raised rather than returned, because it is a mistake in the registry and
    not a diagnosis of anything: the runner skips a check whose requirements
    are unmet, so a probe reaching for a resource can only fail this way if its
    declaration and its body disagree.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class CheckContext:
    """Everything one run of the checks was given.

    Attributes:
        daemon: The robot's daemon, when one can be reached.
        groundstation: The groundstation session, when one is configured.
        models: The model directory, when there is one to look at.
        intent: What the robot is supposed to be, when something declares it.
        unavailable: Why a resource is absent, by resource. A caller that
            supplies a reason gets it in the skip line instead of the neutral
            default.
    """

    daemon: RobotDaemon | None = None
    groundstation: GroundstationLink | None = None
    models: ModelFiles | None = None
    intent: Intent | None = None
    unavailable: Mapping[Requirement, str] = field(default_factory=dict)

    @property
    def available(self) -> frozenset[Requirement]:
        """What this run actually has.

        Returns:
            Every requirement a resource was supplied for.
        """
        supplied = {
            Requirement.DAEMON: self.daemon,
            Requirement.GROUNDSTATION: self.groundstation,
            Requirement.MODELS: self.models,
            Requirement.INTENT: self.intent,
        }
        return frozenset(
            requirement
            for requirement, resource in supplied.items()
            if resource is not None
        )

    def missing(self, requirements: Sequence[Requirement]) -> tuple[Requirement, ...]:
        """Say which of a check's requirements are not met.

        Args:
            requirements: What the check declared it needs.

        Returns:
            Those that were not supplied, in the order declared.
        """
        available = self.available
        return tuple(
            requirement for requirement in requirements if requirement not in available
        )

    def explain(self, requirements: Sequence[Requirement]) -> str:
        """Say why a check was skipped.

        Args:
            requirements: The requirements that were not met.

        Returns:
            One line, joining the reason for each absent resource.
        """
        return "; ".join(
            self.unavailable.get(requirement, requirement.absent)
            for requirement in requirements
        )

    def require_daemon(self) -> RobotDaemon:
        """Take the robot connection, insisting there is one.

        Returns:
            The daemon.

        Raises:
            MissingResourceError: If the check did not declare it needs one.
        """
        if self.daemon is None:
            raise MissingResourceError(_undeclared(Requirement.DAEMON))
        return self.daemon

    def require_groundstation(self) -> GroundstationLink:
        """Take the groundstation link, insisting there is one.

        Returns:
            The link.

        Raises:
            MissingResourceError: If the check did not declare it needs one.
        """
        if self.groundstation is None:
            raise MissingResourceError(_undeclared(Requirement.GROUNDSTATION))
        return self.groundstation

    def require_models(self) -> ModelFiles:
        """Take the model directory, insisting there is one.

        Returns:
            The model files.

        Raises:
            MissingResourceError: If the check did not declare it needs one.
        """
        if self.models is None:
            raise MissingResourceError(_undeclared(Requirement.MODELS))
        return self.models

    def require_intent(self) -> Intent:
        """Take the declared intent, insisting there is one.

        Returns:
            The intent.

        Raises:
            MissingResourceError: If the check did not declare it needs one.
        """
        if self.intent is None:
            raise MissingResourceError(_undeclared(Requirement.INTENT))
        return self.intent


def _undeclared(requirement: Requirement) -> str:
    """Say that a probe reached for something its check did not declare.

    Args:
        requirement: What it reached for.

    Returns:
        The message, naming the fix, because the fix is in this package.
    """
    return (
        f"a probe asked for {requirement.value}, which its check does not "
        f"declare in `requires`; add it there so the runner can skip the check "
        f"rather than run it against nothing"
    )
