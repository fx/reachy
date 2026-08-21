"""Fakes for the world the checks ask about.

Nothing here fakes a check. What is faked is what a check reaches through: a
robot daemon that answers whatever a test needs it to, a groundstation link
that reports one canned session, and a model directory that is or is not in
order. The registry, the runner and the probes are always the real ones.

There is one of each rather than a fake per scenario, and each is configured by
what it should answer, because every check has to be exercised in the state
where it passes, the state where it fails and the state where it is skipped —
and a fake per state would be three fakes that could each drift from the port.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from reachy_checks import (
    ApplicationState,
    CheckContext,
    DaemonInfo,
    InstalledApplication,
    Intent,
    LinkReport,
    ModelFileReport,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "CREDENTIAL",
    "ENDPOINT",
    "FakeDaemon",
    "FakeLink",
    "FakeModelFiles",
    "healthy_context",
    "healthy_daemon",
    "healthy_link",
]

# A placeholder credential. Not anybody's, and never a real one — see the root
# AGENTS.md on what may enter a tracked file in a public repository.
CREDENTIAL: Final = "example-credential"

# An address in an RFC 5737 reserved range, so nothing here can be anybody's.
ENDPOINT: Final = "ws://192.0.2.10:8000/v1/session"


class FakeDaemon:
    """A robot daemon that answers exactly what a test needs it to.

    Attributes:
        calls: The names of the methods that were called, in order, so a test
            can assert that a check asked what it claims to ask.
    """

    def __init__(
        self,
        *,
        info: DaemonInfo | None = None,
        installed: InstalledApplication | None = None,
        state: ApplicationState | None = None,
        configuration: Mapping[str, str] | None = None,
        identity: str = "",
        raises: Exception | None = None,
    ) -> None:
        """Describe what this daemon will say.

        Args:
            info: What `ping` answers.
            installed: What `installed_application` answers.
            state: What `application_state` answers.
            configuration: What `effective_configuration` answers.
            identity: What `announced_identity` answers.
            raises: Raised by every method instead of answering, which is how
                an adapter that fell over is modelled.
        """
        self._info = info or DaemonInfo(responding=True, version="daemon 1.2.3")
        self._installed = installed or InstalledApplication(
            installed=True,
            version="0.1.0",
        )
        self._state = state or ApplicationState(running=True, detail="active")
        self._configuration = dict(configuration or {})
        self._identity = identity
        self._raises = raises
        self.calls: list[str] = []

    def _record(self, name: str) -> None:
        """Note a call and fail it if this daemon is meant to.

        Args:
            name: Which method was called.

        Raises:
            Exception: Whatever this daemon was told to raise.
        """
        self.calls.append(name)
        if self._raises is not None:
            raise self._raises

    async def ping(self) -> DaemonInfo:
        """Answer whether the daemon is there.

        Returns:
            What this fake was told to say.
        """
        self._record("ping")
        return self._info

    async def installed_application(self) -> InstalledApplication:
        """Answer what is installed.

        Returns:
            What this fake was told to say.
        """
        self._record("installed_application")
        return self._installed

    async def application_state(self) -> ApplicationState:
        """Answer whether the application is running.

        Returns:
            What this fake was told to say.
        """
        self._record("application_state")
        return self._state

    async def effective_configuration(self) -> Mapping[str, str]:
        """Answer what configuration is in force.

        Returns:
            What this fake was told to say.
        """
        self._record("effective_configuration")
        return dict(self._configuration)

    async def announced_identity(self) -> str:
        """Answer what identity the satellite announces.

        Returns:
            What this fake was told to say.
        """
        self._record("announced_identity")
        return self._identity


class FakeLink:
    """A groundstation link that reports one canned session.

    Attributes:
        inspections: How many times it was asked. The three groundstation
            checks share one session, and this is what proves it.
    """

    def __init__(self, report: LinkReport) -> None:
        """Describe what this link will report.

        Args:
            report: The evidence to hand back.
        """
        self._report = report
        self.inspections = 0

    async def inspect(self) -> LinkReport:
        """Report the session.

        Returns:
            The canned report.
        """
        self.inspections += 1
        return self._report


class FakeModelFiles:
    """A model directory that is or is not in order."""

    def __init__(self, report: ModelFileReport) -> None:
        """Describe what this directory will report.

        Args:
            report: What is there and what is wrong with it.
        """
        self._report = report

    def inspect(self) -> ModelFileReport:
        """Report the directory.

        Returns:
            The canned report.
        """
        return self._report


def healthy_daemon() -> FakeDaemon:
    """Build a daemon on which every robot-side check passes.

    Returns:
        The daemon, announcing the identity `healthy_context` declares.
    """
    return FakeDaemon(
        configuration={"wake_word": "okay nabu", "detection_source": "groundstation"},
        identity="reachy-mini-example",
    )


def healthy_link() -> FakeLink:
    """Build a link on which every groundstation check passes.

    Returns:
        The link.
    """
    return FakeLink(
        LinkReport(
            endpoint=ENDPOINT,
            established=True,
            offered=("face", "gesture"),
            agreed=("face",),
            establishment_ms=42.0,
            round_trip_ms=17.5,
        ),
    )


def healthy_context() -> CheckContext:
    """Build a context on which every check passes.

    Returns:
        The context, with every resource supplied and in order.
    """
    return CheckContext(
        daemon=healthy_daemon(),
        groundstation=healthy_link(),
        models=FakeModelFiles(
            ModelFileReport(
                directory="/opt/reachy/models",
                verified=("face_detection_yunet",),
            ),
        ),
        intent=Intent(
            configuration={
                "wake_word": "okay nabu",
                "detection_source": "groundstation",
            },
            announced_identity="reachy-mini-example",
        ),
    )
