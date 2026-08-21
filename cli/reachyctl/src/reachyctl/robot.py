"""What this tool needs of a robot, expressed as the narrowest thing that works.

The reachyctl spec says the robot is reached over its remote-access and daemon
interfaces **in process** rather than by invoking command-line clients, so that
a failure arrives as a structured error a progress report can reflect rather
than as text to be parsed out of a subprocess. `reachyctl.ssh` is the
implementation of that; this module is the seam it sits behind, and everything
above it — `deploy`, `config`, `app` and the daemon client the shared checks run
against — is written against the seam.

The seam exists for two reasons and only the second is testing. The first is
that a command should be able to say *which* step failed and why without every
layer above the transport having learned what an SSH error looks like: a remote
command that exits non-zero is an ordinary outcome carrying a status and two
streams, and a link that is not there is a `RobotAccessError`, which the command
surface already knows costs `UNREACHABLE`. The second is that this repository
has no robot in it, and a rule that no test may require one — so the fake is not
a convenience, it is how the deploy sequence is exercised at all.

**A command is a sequence of arguments, never a shell line.** Nothing here
interpolates a value into a string that a shell then splits again: the transport
quotes what it is given, once, on the way out. That is what keeps a robot address
or a file name carrying a space from becoming two arguments, and it is what
makes a fake able to match on `argv` rather than on a rendering of it.
"""

from __future__ import annotations

import shlex

# Imported at run time rather than under `TYPE_CHECKING`: the PEP 695 alias
# below is lazy, so its right-hand side is evaluated on first access and a name
# that only ever existed for the type checker would raise `NameError` there.
# `reachy_checks.registry` settled the same question the same way; see the
# comment above `Probe` there.
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

from reachyctl.errors import CommandError, ConfigurationError
from reachyctl.exits import ExitCode
from reachyctl.managed import (
    DEFAULT_DAEMON_UNIT,
    DEFAULT_DROP_IN_NAME,
    drop_in_directory,
    drop_in_path,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path, PurePosixPath

__all__ = [
    "DEFAULT_APPLICATION",
    "DEFAULT_DAEMON_CONTROL",
    "DEFAULT_DAEMON_DISTRIBUTION",
    "DEFAULT_PYTHON",
    "DEFAULT_SSH_PORT",
    "DEFAULT_STAGING",
    "Closer",
    "CommandOutcome",
    "RemoteAccess",
    "RobotAccessError",
    "RobotLayout",
    "RobotTarget",
    "closing",
    "parse_robot",
    "render",
]

# What a command calls to let the link go. It is awaited inside the same event
# loop the run used, because a connection opened on one loop cannot be closed
# from another — and a command that ended in a failure is exactly the one that
# would otherwise leave a channel open.
type Closer = Callable[[], Awaitable[None]]


async def closing[T](work: Awaitable[T], close: Closer | None) -> T:
    """Await something and let the link go afterwards, whatever happened.

    Args:
        work: What the command is doing.
        close: How to let the link go, or `None` when there is nothing to let
            go of.

    Returns:
        Whatever the work produced.
    """
    try:
        return await work
    finally:
        if close is not None:
            await close()


DEFAULT_SSH_PORT: Final = 22

# The port range, named so the refusal below can quote its own bounds rather
# than repeating two numbers a reader has to trust.
_LOWEST_PORT: Final = 1
_HIGHEST_PORT: Final = 65535

# The distribution this repository deploys to a robot by default. A public
# identifier of this project's own artifact, not anybody's environment.
DEFAULT_APPLICATION: Final = "reachy-mini-ha-satellite"

# The vendor's daemon distribution, whose version `ping` reports.
DEFAULT_DAEMON_DISTRIBUTION: Final = "reachy-mini"

# The module the daemon's application control is reached through, run under the
# robot's application environment. This is the one name in this file that is
# provisional: it is what the daemon is expected to expose, it is behind a field
# rather than written into a command so that meeting a robot that spells it
# differently is a `--daemon-control` away, and confirming it is the first thing
# the deferred hardware session does.
DEFAULT_DAEMON_CONTROL: Final = "reachy_mini.apps"

# The application environment's interpreter, used only when the daemon's own
# unit does not say which one it runs. See `reachyctl.daemon`, which prefers the
# answer it gets from the daemon over this one, because installing into an
# environment the daemon is not using is the failure REQ-051 exists to catch.
DEFAULT_PYTHON: Final = "/opt/reachy/venv/bin/python"

# Where a transfer lands before it is installed. Under `/var/tmp` rather than
# `/tmp`: a wheel is large enough that a `tmpfs` on a device with a gigabyte of
# memory is the wrong place for it, and `/var/tmp` survives a reboot mid-deploy
# rather than leaving an install step pointing at nothing.
# S108: the rule is about this process writing to a world-writable directory on
# the machine it is running on. This is a path on the ROBOT, sent to `mkdir` and
# `install` over a link, and the reason it is under `/var/tmp` is above. The
# staged file is moved into place with an explicit mode and owner rather than
# being read back from where it landed.
DEFAULT_STAGING: Final = "/var/tmp/reachyctl"  # noqa: S108


class RobotAccessError(CommandError):
    """The robot could not be reached, or the link broke while it was in use.

    Not a diagnosis: nothing has been learned about the robot, so a script
    reading this as "the robot is unhealthy" would be reporting on its own
    network. It costs `UNREACHABLE`, exactly as a groundstation that is not
    there does.
    """

    exit_code: ExitCode = ExitCode.UNREACHABLE


@dataclass(frozen=True, slots=True, kw_only=True)
class RobotTarget:
    """Where the robot is and how to get onto it.

    No default holds an address. Every field that could name somebody's robot
    is supplied at run time, from an argument or from the environment, and this
    repository is public — see the root `AGENTS.md`.

    Attributes:
        host: The robot's address or name.
        user: The account to connect as.
        port: The SSH port.
        identity_file: A private key to offer, or `None` to use the agent and
            the default identities.
        known_hosts: A host-key file to verify against, or `None` for the
            client's own default. There is deliberately no option that turns
            verification off.
        elevate: Whether privileged commands are prefixed with `sudo -n`.
            Non-interactive: a `sudo` that stopped to ask for a password over a
            link with no terminal would hang a deploy rather than fail it.
        timeout: How long to wait for the connection, in seconds.
    """

    host: str
    user: str
    port: int = DEFAULT_SSH_PORT
    identity_file: Path | None = None
    known_hosts: Path | None = None
    elevate: bool = True
    timeout: float = 30.0

    def describe(self) -> str:
        """Say where this is, for a progress line.

        Returns:
            The account and address as an operator wrote them. Nothing is
            invented and nothing is hidden: an operator watching a deploy needs
            to know which robot it is talking to, and a credential never
            reaches this type — the key is a path and the host key is a path.
        """
        return f"{self.user}@{self.host}:{self.port}"


@dataclass(frozen=True, slots=True, kw_only=True)
class RobotLayout:
    """Where things are on the robot, and what they are called.

    Every field has a default, and each default is this stack's own convention
    rather than an observation of anybody's machine. They are fields rather
    than constants because the robot is a device this repository does not own:
    a vendor image that names its unit differently should cost an option, not a
    release.

    Attributes:
        daemon_unit: The systemd unit that carries the environment, and the one
            a configuration change restarts.
        drop_in_name: The managed drop-in's file name inside that unit's
            drop-in directory.
        application: The distribution name of the application being operated.
        daemon_distribution: The daemon's own distribution, whose version
            `ping` reports.
        daemon_control: The module the daemon's application control is reached
            through. See `DEFAULT_DAEMON_CONTROL`.
        python: The application environment's interpreter, used only as a
            fallback — see `DEFAULT_PYTHON`.
        staging: Where a transferred wheel lands before it is installed.
    """

    daemon_unit: str = DEFAULT_DAEMON_UNIT
    drop_in_name: str = DEFAULT_DROP_IN_NAME
    application: str = DEFAULT_APPLICATION
    daemon_distribution: str = DEFAULT_DAEMON_DISTRIBUTION
    daemon_control: str = DEFAULT_DAEMON_CONTROL
    python: str = DEFAULT_PYTHON
    staging: str = DEFAULT_STAGING

    @property
    def drop_in(self) -> str:
        """Where the managed region is written.

        Returns:
            The absolute path of the drop-in this tool owns in full.
        """
        return drop_in_path(self.daemon_unit, self.drop_in_name)

    @property
    def drop_in_directory(self) -> str:
        """Where that drop-in's directory is.

        Returns:
            The absolute path of the unit's drop-in directory. Other drop-ins
            in it belong to whoever put them there.
        """
        return drop_in_directory(self.daemon_unit)


@dataclass(frozen=True, slots=True, kw_only=True)
class CommandOutcome:
    """What a remote command did.

    A non-zero status is an outcome and not an exception, because most of them
    are answers: `systemctl show` on a unit that is not installed, a `cat` of a
    drop-in that has never been written. What *is* an exception is not being
    able to run the command at all, and that is `RobotAccessError`.

    Attributes:
        command: What was run, rendered for a message. Arguments only — no
            value this tool holds is ever interpolated into it.
        exit_status: What the remote command exited with.
        stdout: What it wrote to standard output.
        stderr: What it wrote to standard error. Free text from the robot, so a
            consumer scrubs it like any other string it did not write.
    """

    command: str
    exit_status: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """Whether the command succeeded.

        Returns:
            True when it exited zero.
        """
        return self.exit_status == 0

    def complaint(self) -> str:
        """Say why this command is not an answer, quoting the robot verbatim.

        **Verbatim, and that is not laziness — it is reachyctl REQ-059.** This
        text was written by something on the robot, and a setting's value is
        exactly the sort of thing a tool there quotes back: systemd echoes a
        unit's configuration, a Python traceback carries its arguments. The
        redactor that removes a known secret matches the *value*, so anything
        that rewrites this text before the redactor sees it — truncating it to
        its first line, joining its lines with a separator, cutting it to a
        length — can split a secret in half and the redactor then matches
        nothing and reports success while the secret goes out in pieces.

        An earlier version of this method took the first line, and the test
        that guards this requirement caught it: a credential containing a
        newline was cut at the newline and the first half was printed. The
        first line is more readable and it is the wrong trade. What makes the
        output readable is the rendering, which escapes a line break *after*
        scrubbing — see `reachyctl.output`.

        Returns:
            The command, its status, and everything it said, unmodified.
        """
        reason = self.stderr or self.stdout or "it said nothing"
        return f"`{self.command}` exited {self.exit_status}: {reason}"


def parse_robot(
    text: str,
    *,
    identity_file: Path | None = None,
    known_hosts: Path | None = None,
    elevate: bool = True,
    timeout: float = 30.0,
) -> RobotTarget:
    """Read `user@host`, `user@host:port` or `user@[v6]:port` into a target.

    **The authority is taken apart once and never rebuilt.** An IPv6 literal
    carries colons of its own, so a parser that splits on the last colon turns
    `user@2001:db8::1` into a host of `2001:db8:` and a port of `1`, and a
    renderer that joins the pieces back together without brackets produces an
    address that resolves to nothing. Both halves are handled here explicitly:
    a bracketed authority keeps its brackets off the host and its port outside
    them, and an unbracketed authority with more than one colon is an address
    rather than an address and a port.

    Args:
        text: What the operator wrote.
        identity_file: A private key to offer.
        known_hosts: A host-key file to verify against.
        elevate: Whether privileged commands are prefixed with `sudo -n`.
        timeout: How long to wait for the connection.

    Returns:
        The target.

    Raises:
        ConfigurationError: If it is not an address this can be read from.
            Nothing was contacted, so this is not a diagnosis of anything, and
            the message quotes only what the operator typed.
    """
    user, separator, authority = text.rpartition("@")
    if not separator or not user or not authority:
        message = (
            f"{text!r} is not a robot address: write it as user@host, "
            f"user@host:port, or user@[ipv6]:port. The account is not "
            f"defaulted, because a default account name is an identifier "
            f"belonging to somebody's environment"
        )
        raise ConfigurationError(message)
    host, port = _authority(authority, text)
    return RobotTarget(
        host=host,
        user=user,
        port=port,
        identity_file=identity_file,
        known_hosts=known_hosts,
        elevate=elevate,
        timeout=timeout,
    )


def _authority(authority: str, text: str) -> tuple[str, int]:
    """Split the host from the port, IPv6 included.

    Args:
        authority: Whatever followed the account.
        text: The whole address, for the message.

    Returns:
        The host and the port.

    Raises:
        ConfigurationError: If the authority is malformed, or the port is not a
            number in range.
    """
    if authority.startswith("["):
        closing = authority.find("]")
        if closing < 0:
            message = f"{text!r} opens a bracketed address and never closes it"
            raise ConfigurationError(message)
        host = authority[1:closing]
        remainder = authority[closing + 1 :]
        if not remainder:
            return host, DEFAULT_SSH_PORT
        if not remainder.startswith(":"):
            message = (
                f"{text!r} has {remainder!r} after the bracketed address; only "
                f"a ':port' may follow one"
            )
            raise ConfigurationError(message)
        return host, _port(remainder[1:], text)
    if authority.count(":") > 1:
        # More than one colon and no brackets is an IPv6 literal written
        # without a port. Splitting it would invent one.
        return authority, DEFAULT_SSH_PORT
    host, separator, port = authority.partition(":")
    if not host:
        message = f"{text!r} names no host"
        raise ConfigurationError(message)
    return host, _port(port, text) if separator else DEFAULT_SSH_PORT


def _port(value: str, text: str) -> int:
    """Read a port number.

    Args:
        value: What followed the colon.
        text: The whole address, for the message.

    Returns:
        The port.

    Raises:
        ConfigurationError: If it is not a number between 1 and 65535.
    """
    try:
        port = int(value, 10)
    except ValueError as error:
        message = f"{text!r} has {value!r} where a port number belongs"
        raise ConfigurationError(message) from error
    if not _LOWEST_PORT <= port <= _HIGHEST_PORT:
        message = (
            f"{text!r} has port {port}, which is outside {_LOWEST_PORT}-{_HIGHEST_PORT}"
        )
        raise ConfigurationError(message)
    return port


def render(command: Sequence[str]) -> str:
    """Render a command for a message, exactly as it will be run.

    Args:
        command: The arguments.

    Returns:
        The command as a shell line, quoted. This is for reading, and it is
        also what the transport sends — one rendering, so a message quoting a
        command quotes the command that ran.
    """
    return shlex.join(command)


@runtime_checkable
class RemoteAccess(Protocol):
    """The robot's remote-access interface, as this tool needs it."""

    async def connect(self) -> None:
        """Open the link, before anything is asked over it.

        Every robot-facing command calls this first, and the reason is not
        tidiness. A command's steps are asked through the shared check
        registry, and `reachy_checks.run_check` turns *anything* a probe raises
        into a failed result — which is right for a diagnosis and wrong for an
        operation: a robot that could not be reached has told us nothing, and
        `reachyctl.exits` reserves `UNREACHABLE` for exactly that. Worse, an
        `app stop` whose first question failed that way reads the answer as
        "already stopped" and reports success over a robot it never contacted.

        Opening the link explicitly puts that failure where it belongs, above
        the checks, as a `RobotAccessError`. It costs no extra round trip: the
        connection is opened once and every step then uses it.

        Raises:
            RobotAccessError: If the robot cannot be reached.
        """
        ...

    async def run(self, command: Sequence[str]) -> CommandOutcome:
        """Run one command and wait for it to finish.

        Args:
            command: The arguments to run. Quoted by the transport, once.

        Returns:
            What it did.

        Raises:
            RobotAccessError: If the command could not be run at all.
        """
        ...

    async def upload(self, content: bytes, remote: PurePosixPath) -> None:
        """Put bytes on the robot at a path.

        Args:
            content: What to write.
            remote: Where to write it.

        Raises:
            RobotAccessError: If the transfer could not be made.
        """
        ...

    def stream(self, command: Sequence[str]) -> AsyncIterator[str]:
        """Run a command and yield its output a line at a time.

        Args:
            command: The arguments to run.

        Returns:
            The lines, as they arrive. A command that never ends — following a
            journal — is stopped by abandoning the iterator.
        """
        ...

    async def aclose(self) -> None:
        """Let the link go."""
        ...
