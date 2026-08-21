"""What a check needs of the world, expressed as the narrowest thing that works.

These are protocols rather than clients, and they were protocols before there
was anything to implement them: reaching a robot arrived with `reachyctl deploy`
and `reachyctl app` in change 0009, and the satellite itself in 0013. Writing a
robot transport here to have something for the daemon checks to call would have
meant 0009 finding one already written, in a package that has no business owning
it — so the checks are written against the shape of the answer, and whoever can
obtain that answer supplies it. `reachyctl` supplies it today; an Ansible
verification role supplies a different one.

The value objects carry a `complaint` beside the answer rather than raising,
because "the daemon did not respond" is an ordinary diagnosis and an exception
would make it an accident. An adapter that does raise is still safe: the runner
turns anything a probe throws into a failed result naming it.

**Nothing here carries a configuration value or a credential.** The effective
configuration crosses this seam as a mapping so a check can compare it, and the
check reports only which keys differ — never what they hold. A setting is
exactly where a credential lives, and REQ-059 is not satisfied by a rule that
holds until somebody puts a token in a settings file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "ApplicationState",
    "DaemonInfo",
    "GroundstationLink",
    "InstalledApplication",
    "Intent",
    "LinkReport",
    "ModelFileReport",
    "ModelFiles",
    "RobotDaemon",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class DaemonInfo:
    """Whether the robot's daemon answered, and what it said it is.

    Attributes:
        responding: Whether it answered at all.
        version: What it reported itself as. Empty when it did not answer.
        complaint: Why it did not, when it did not.
    """

    responding: bool
    version: str = ""
    complaint: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class InstalledApplication:
    """Whether the satellite is installed on the robot, and at what version.

    Attributes:
        installed: Whether the daemon knows about it.
        version: The installed version. Empty when nothing is installed.
        complaint: Why the answer could not be obtained, when it could not.
    """

    installed: bool
    version: str = ""
    complaint: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplicationState:
    """Whether the satellite is running on the robot.

    Attributes:
        running: Whether it is.
        detail: What the daemon says about it — the state name, or why it
            stopped. Free text from the robot, so a consumer scrubs it like any
            other string it did not write.
    """

    running: bool
    detail: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class LinkReport:
    """One session's worth of evidence about the groundstation.

    Three checks read this — that a session opened, that capabilities were
    negotiated, and how long a frame took to come back — and they share one
    report because they should share one session. Opening three would triple
    the cost of a diagnostic and measure three different moments, and the
    checks stay independent regardless: each reports its own outcome, and a
    session that never opened makes all three say so rather than making two of
    them disappear.

    Attributes:
        endpoint: Where the session was attempted, already redacted for
            printing.
        established: Whether a session was negotiated.
        offered: The capability names this side offered.
        agreed: The capability names both sides settled on.
        establishment_ms: How long opening the session took, in milliseconds.
            `None` when it never opened.
        round_trip_ms: How long one frame took to go out and come back, in
            milliseconds. `None` when nothing came back.
        complaint: Why no session was established, when none was.
        result_complaint: Why no result came back, when none did.
    """

    endpoint: str
    established: bool
    offered: tuple[str, ...] = ()
    agreed: tuple[str, ...] = ()
    establishment_ms: float | None = None
    round_trip_ms: float | None = None
    complaint: str = ""
    result_complaint: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelFileReport:
    """What is in a model directory, judged against what is pinned.

    `unavailable` is the difference between "these files are wrong" and "there
    was nothing here to judge them against", and the two are different facts
    about different machines. A control machine carrying the checks but not the
    groundstation package has nothing to verify against and is not in an error
    state; a machine that has the registry and a file that does not match it
    is. Collapsing the first into the second makes a provisioning run fail on a
    machine that was never meant to carry the service.

    Attributes:
        directory: Where it looked.
        unavailable: Why nothing could be judged, when nothing could. Empty
            when the registry was consulted, whatever it then found.
        verified: The names of the models whose file is present and hashes to
            the pinned digest.
        problems: One line per model that is absent, unreadable, or hashes to
            something else.
    """

    directory: str
    unavailable: str = ""
    verified: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Intent:
    """What the operator says the robot is supposed to be.

    The other half of "the effective configuration matches intent": the robot
    reports what is in force, and this is what was meant. Provisioning holds
    the authoritative declaration; a `doctor` run is handed the same values.

    Attributes:
        configuration: The settings that are supposed to be in force, by name.
            Compared by key: a check reports which names differ and never what
            any of them holds.
        announced_identity: The identity the satellite is supposed to announce
            to Home Assistant, or `None` when nothing declares one.
    """

    configuration: Mapping[str, str] = field(default_factory=dict)
    announced_identity: str | None = None


@runtime_checkable
class RobotDaemon(Protocol):
    """The robot, as the checks need it.

    Implemented against the robot's own remote-access and daemon interfaces by
    change 0009. Until then nothing binds it, and the checks that need it
    report themselves skipped rather than passing on no evidence.
    """

    async def ping(self) -> DaemonInfo:
        """Ask the daemon whether it is there and what it is.

        Returns:
            What it said.
        """
        ...

    async def installed_application(self) -> InstalledApplication:
        """Ask what version of the satellite is installed.

        Returns:
            What it said.
        """
        ...

    async def application_state(self) -> ApplicationState:
        """Ask whether the satellite is running.

        Returns:
            What it said.
        """
        ...

    async def effective_configuration(self) -> Mapping[str, str]:
        """Ask what configuration is actually in force.

        Returns:
            The settings by name. The values are compared and never reported:
            a setting is exactly where a credential ends up.
        """
        ...

    async def announced_identity(self) -> str:
        """Ask what identity the satellite announces to Home Assistant.

        Returns:
            The announced identity, or an empty string when the satellite
            announces none.
        """
        ...


@runtime_checkable
class GroundstationLink(Protocol):
    """One session to the groundstation, opened at most once and shared."""

    async def inspect(self) -> LinkReport:
        """Open a session if one has not been opened, and report what happened.

        Returns:
            The evidence. Repeated calls return the same report, so the three
            groundstation checks describe one session rather than three.
        """
        ...


@runtime_checkable
class ModelFiles(Protocol):
    """The model files in a deployed artifact, judged against what is pinned."""

    def inspect(self) -> ModelFileReport:
        """Read the directory and verify every registered model.

        Returns:
            What is there and what is wrong with it.
        """
        ...
