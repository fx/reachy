"""The robot's remote-access interface, spoken in process.

The reachyctl spec asks for the robot to be reached "in process rather than by
invoking command-line clients, so that failures arrive as structured errors that
progress reporting can reflect rather than as text to be parsed out of a
subprocess". `asyncssh` is what makes that possible: it is a pure-Python SSH
implementation, so a command's exit status, its two streams and the reason a
connection failed all arrive as values rather than as something to scrape off a
child process's output. It also brings a real SSH **server**, which is why the
integration test beside this module drives the real transport in process rather
than describing what it would do.

**One connection per command run.** Every step of a deploy — transfer, install,
restart, start, verify — goes over the same connection, opened on first use and
closed on the way out. The link is measured at 100-170 ms idle with 700 ms
spikes, and an SSH handshake is several round trips, so reconnecting per step
would spend more time authenticating than working.

**Host keys are verified, and there is no option that stops them being.**
`--known-hosts` names a file to verify against; without it the client's own
default locations are used. An "insecure" switch would be a switch somebody
leaves on, on the one link that carries everything this tool does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import asyncssh

from reachyctl.robot import CommandOutcome, RobotAccessError, render

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import PurePosixPath

    from reachyctl.robot import RobotTarget

__all__ = ["SshAccess"]


def _text(value: object) -> str:
    """Read one of a completed process's streams as text.

    `asyncssh` types a stream as `str` or `bytes` depending on whether an
    encoding was configured, and this client configures one — but the
    annotation covers both, so strict typing needs the narrowing to be written
    down rather than assumed.

    Args:
        value: What the library handed back.

    Returns:
        The stream as text. Bytes are decoded leniently: this is a robot's
        output on its way into a message, and a byte sequence that is not valid
        UTF-8 should cost a replacement character rather than an exception on
        top of whatever already went wrong.
    """
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return "" if value is None else str(value)


class SshAccess:
    """A robot reached over SSH, with the connection opened when first needed."""

    def __init__(self, target: RobotTarget) -> None:
        """Prepare access to a robot. Nothing is connected yet.

        Connecting lazily is what makes reachyctl REQ-053 observable rather
        than merely intended: a command validates what it was given first, and
        a value the robot would refuse costs no connection at all — not a
        connection that is opened and then not used.

        Args:
            target: Where the robot is and how to get onto it.
        """
        self._target = target
        self._connection: asyncssh.SSHClientConnection | None = None

    async def _connect(self) -> asyncssh.SSHClientConnection:
        """Open the connection, or hand back the one already open.

        Returns:
            The connection.

        Raises:
            RobotAccessError: If the robot cannot be reached, refuses the
                credentials offered, or presents a host key that does not
                verify. The message names the robot and the reason and quotes
                nothing that was read from it.
        """
        if self._connection is not None:
            return self._connection
        options: dict[str, Any] = {
            "username": self._target.user,
            "port": self._target.port,
            "connect_timeout": self._target.timeout,
        }
        if self._target.identity_file is not None:
            options["client_keys"] = [str(self._target.identity_file)]
        if self._target.known_hosts is not None:
            # Passed only when the operator named a file. `asyncssh` reads its
            # own default locations when the argument is left alone, and takes
            # `None` to mean "do not verify the host key at all" — so passing
            # the absent case through would turn an unset option into a
            # disabled check.
            options["known_hosts"] = str(self._target.known_hosts)
        try:
            self._connection = await asyncssh.connect(self._target.host, **options)
        except (OSError, asyncssh.Error) as error:
            message = (
                f"cannot reach the robot at {self._target.describe()}: "
                f"{type(error).__name__}: {error}"
            )
            raise RobotAccessError(message) from error
        return self._connection

    async def connect(self) -> None:
        """Open the link, before anything is asked over it.

        Raises:
            RobotAccessError: If the robot cannot be reached.
        """
        await self._connect()

    async def run(self, command: Sequence[str]) -> CommandOutcome:
        """Run one command and wait for it to finish.

        Args:
            command: The arguments to run.

        Returns:
            Its status and both streams.

        Raises:
            RobotAccessError: If the command could not be run at all — the link
                is not there, or it broke while the command was in flight.
        """
        line = render(command)
        connection = await self._connect()
        try:
            completed = await connection.run(line, check=False)
        except (OSError, asyncssh.Error) as error:
            message = (
                f"the link to {self._target.describe()} failed while running "
                f"`{line}`: {type(error).__name__}: {error}"
            )
            raise RobotAccessError(message) from error
        return CommandOutcome(
            command=line,
            # `exit_status` is `None` when the remote end was killed by a
            # signal and reported that instead. There is no status to report,
            # and treating an absent one as zero would call a killed process a
            # success — which on the restart step is exactly the "looks
            # identical to success" failure this change exists to remove.
            exit_status=(
                completed.exit_status if completed.exit_status is not None else -1
            ),
            stdout=_text(completed.stdout),
            stderr=_text(completed.stderr),
        )

    async def upload(self, content: bytes, remote: PurePosixPath) -> None:
        """Put bytes on the robot at a path.

        Args:
            content: What to write.
            remote: Where to write it.

        Raises:
            RobotAccessError: If the transfer could not be made.
        """
        connection = await self._connect()
        try:
            async with (
                connection.start_sftp_client() as sftp,
                sftp.open(str(remote), "wb") as handle,
            ):
                await handle.write(content)
        except (OSError, asyncssh.Error) as error:
            message = (
                f"could not write {remote} on {self._target.describe()}: "
                f"{type(error).__name__}: {error}"
            )
            raise RobotAccessError(message) from error

    async def stream(self, command: Sequence[str]) -> AsyncIterator[str]:
        """Run a command and yield its output a line at a time.

        Args:
            command: The arguments to run.

        Yields:
            Each line, without its terminator, as it arrives. A command that
            never ends is stopped by abandoning the iterator, which closes the
            channel.

        Raises:
            RobotAccessError: If the command could not be started, or the link
                broke while it was running.
        """
        line = render(command)
        connection = await self._connect()
        try:
            async with connection.create_process(line) as process:
                async for chunk in process.stdout:
                    yield _text(chunk).rstrip("\r\n")
        except (OSError, asyncssh.Error) as error:
            message = (
                f"the link to {self._target.describe()} failed while streaming "
                f"`{line}`: {type(error).__name__}: {error}"
            )
            raise RobotAccessError(message) from error

    async def aclose(self) -> None:
        """Let the connection go.

        Safe to call when nothing was ever opened, which is the ordinary case
        for a command that refused its arguments before contacting anything.
        """
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()
            await connection.wait_closed()
