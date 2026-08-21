"""A robot that is not there, answering the commands this tool actually sends.

There is no robot in this repository and no test may require one, so the deploy
sequence, the configuration comparison and the application lifecycle are all
exercised against this. It is deliberately a *simulator* rather than a set of
canned answers: it holds a small filesystem, a systemd-shaped unit, an
environment and an installed-package list, and each command it is sent changes
that state the way the real one would.

That matters for two of this change's guarantees, and a stub returning fixed
strings could not express either.

**Restarting is what puts an environment change in force.** This fake re-reads
the managed drop-in into the effective environment when the daemon restarts, so
`config apply` really does have to write the file *and* restart before the
verification finds anything. `honours_restart=False` models the other case — the
robot whose configuration is on disk and silently inert — which is one of the two
failures the reachyctl spec's background names.

**An install can succeed into an environment nobody is reading.** `pip install`
here exits zero either way; `install_takes_effect=False` leaves the installed
version alone, which is exactly the deploy that looks identical to success at
every step. REQ-051's scenario is that case, and it cannot be written against a
fake that has no notion of where an install went.

Every command is recorded in `commands`, so a test can assert not only what
happened but what was *not* sent — which is how preview mode is proved to change
nothing.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from reachyctl.daemon import DaemonClient
from reachyctl.managed import parse_region
from reachyctl.robot import (
    CommandOutcome,
    RobotAccessError,
    RobotLayout,
    render,
)
from reachyctl.wheels import WheelError, describe_wheel

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence
    from pathlib import PurePosixPath

__all__ = [
    "DAEMON_DISTRIBUTION",
    "DROP_IN",
    "ROBOT",
    "FakeRemoteAccess",
    "FakeRobot",
    "daemon_for",
]

# An address in an RFC 5737 reserved range and a placeholder account, so nothing
# here can be anybody's — see the root AGENTS.md.
ROBOT: Final = "operator@192.0.2.10"

DAEMON_DISTRIBUTION: Final = "reachy-mini"

DROP_IN: Final = (
    "/etc/systemd/system/reachy-mini-daemon.service.d/10-reachy-managed.conf"
)

_SUDO: Final = ("sudo", "-n")


@dataclass
class FakeRobot:
    """The state a robot has, as far as this tool can see it.

    Attributes:
        active: Whether the daemon's unit is running.
        load_state: What systemd says about the unit being installed at all.
        exec_start: The interpreter in the unit's `ExecStart`, or empty when the
            unit cannot be read.
        files: The robot's filesystem, as far as this tool writes to it.
        packages: What is installed in the environment the daemon runs.
        environment: What the daemon is actually running with.
        app_running: Whether the application is running.
        app_detail: What the daemon says about it.
        honours_restart: Whether restarting re-reads the managed drop-in into
            the effective environment. False models a robot whose configuration
            is on disk and inert.
        install_succeeds: Whether `pip install` exits zero at all.
        install_takes_effect: Whether `pip install` changes what the daemon's
            environment holds. False models an install into an environment the
            daemon is not reading, which is REQ-051's scenario — and the reason
            it is separate from `install_succeeds` is that the whole difficulty
            of that scenario is that the install DID succeed.
        restart_succeeds: Whether restarting the daemon exits zero.
        start_succeeds: Whether asking the daemon to start the application
            actually starts it. Independent of the control's exit status, which
            is what makes a crash loop and a complaining control two different
            robots.
        stop_succeeds: The same, for stopping it.
        control_stdout: What the daemon's application control writes for a
            `status`, when it is not to write the JSON this tool reads. The
            empty default means the ordinary answer.
        metadata_stdout: What the interpreter's metadata query writes, when it
            is not to write the JSON this tool reads.
        journal_interrupts: Whether reading the journal ends the way an
            operator ends `--follow`.
        control_succeeds: Whether the control's start and stop verbs exit zero.
            It governs the exit STATUS only: what the application then does is
            `start_succeeds` and `stop_succeeds`. The two are separate because
            the interesting case is a control that complained while the thing it
            controls did exactly what was asked — which is the case a tool
            reading exit statuses gets wrong.
        modes: The mode each path was last `chmod`-ed to, so a test can assert
            the staging directory is narrowed rather than left as it was found.
        journal: The lines the robot's journal holds for the application.
        leaky: Whether a command that fails quotes the whole environment back in
            its complaint, values included. Robots and the tools on them do this
            — systemd echoes a unit's configuration, a Python traceback carries
            the arguments — and reachyctl REQ-059 is a promise that holds on
            exactly that path: text this tool did not write, arriving from
            somewhere nobody controls, on its way into a message.
        failing: Commands whose first word is in here exit non-zero.
    """

    active: bool = True
    load_state: str = "loaded"
    exec_start: str = "/opt/reachy/venv/bin/python"
    files: dict[str, str] = field(default_factory=dict)
    packages: dict[str, str] = field(
        default_factory=lambda: {DAEMON_DISTRIBUTION: "4.5.6"},
    )
    environment: dict[str, str] = field(default_factory=dict)
    app_running: bool = False
    app_detail: str = "inactive"
    honours_restart: bool = True
    install_succeeds: bool = True
    install_takes_effect: bool = True
    restart_succeeds: bool = True
    start_succeeds: bool = True
    stop_succeeds: bool = True
    control_stdout: str = ""
    metadata_stdout: str = ""
    journal_interrupts: bool = False
    control_succeeds: bool = True
    modes: dict[str, str] = field(default_factory=dict)
    journal: list[str] = field(default_factory=list)
    leaky: bool = False
    failing: set[str] = field(default_factory=set)

    @property
    def managed_region(self) -> str:
        """What the managed drop-in holds right now.

        Returns:
            Its content, or an empty string when it has never been written.
            This is the value a preview test snapshots before and compares
            after: the guarantee is that nothing happened, and only an
            after-state assertion tests that.
        """
        return self.files.get(DROP_IN, "")


class FakeRemoteAccess:
    """A `RemoteAccess` that runs commands against a `FakeRobot`.

    Attributes:
        robot: The state being operated on.
        commands: Every command sent, in order, rendered as it would be sent.
        connected: Whether anything was ever actually asked of the robot. A
            command that refused its arguments locally leaves this false, which
            is what reachyctl REQ-053's scenario asks to be able to observe.
        closed: Whether the link was let go of.
    """

    def __init__(
        self,
        robot: FakeRobot | None = None,
        observer: Callable[[Sequence[str]], None] | None = None,
    ) -> None:
        """Bind a link to a robot.

        Args:
            robot: The state to operate on. A default robot is a healthy one
                with nothing installed.
            observer: Called with each command as it is sent, before it runs.
                Used by the test that asserts the restart warning is written
                *before* the restart happens rather than beside it.
        """
        self.robot = robot if robot is not None else FakeRobot()
        self.commands: list[list[str]] = []
        self.connected = False
        self.closed = False
        self._observer = observer

    async def connect(self) -> None:
        """Open the link. Nothing to open, and it is recorded as opened."""
        self.connected = True

    async def run(self, command: Sequence[str]) -> CommandOutcome:
        """Run one command against the robot.

        Args:
            command: The arguments to run.

        Returns:
            What it did.
        """
        self.connected = True
        self.commands.append(list(command))
        if self._observer is not None:
            self._observer(command)
        return self._dispatch(list(command))

    async def upload(self, content: bytes, remote: PurePosixPath) -> None:
        """Put bytes on the robot.

        Args:
            content: What to write.
            remote: Where to write it.

        Raises:
            RobotAccessError: If the staging directory has not been made, which
                is what a real transfer would fail with and what makes the
                order of the transfer step's two commands matter.
        """
        self.connected = True
        self.commands.append(["<upload>", str(remote), str(len(content))])
        parent = str(remote.parent)
        if parent not in self.robot.files:
            message = f"no such directory on the robot: {parent}"
            raise RobotAccessError(message)
        self.robot.files[str(remote)] = content.decode(
            "utf-8", errors="surrogateescape"
        )

    async def stream(self, command: Sequence[str]) -> AsyncIterator[str]:
        """Run a command and yield its output a line at a time.

        Args:
            command: The arguments to run.

        Yields:
            Each line the robot's journal holds.
        """
        self.connected = True
        self.commands.append(list(command))
        for line in self.robot.journal:
            yield line
        if self.robot.journal_interrupts:
            # How an operator ends `--follow`: at the keyboard, part way
            # through a stream that was never going to end on its own.
            raise KeyboardInterrupt

    async def aclose(self) -> None:
        """Let the link go."""
        self.closed = True

    # --- what the robot does with each command -------------------------------

    def _dispatch(self, command: list[str]) -> CommandOutcome:
        """Work out what one command does to this robot.

        Args:
            command: The arguments, possibly behind `sudo -n`.

        Returns:
            What it did.
        """
        line = render(command)
        argv = (
            command[len(_SUDO) :]
            if tuple(command[: len(_SUDO)]) == _SUDO
            else list(command)
        )
        if argv and argv[0] in self.robot.failing:
            return CommandOutcome(
                command=line,
                exit_status=1,
                stdout="",
                stderr=f"{argv[0]}: this robot was told to refuse that",
            )
        for matches, handle in (
            (_is_show, self._show),
            (_is_systemctl_verb, self._systemctl),
            (_is_cat, self._cat),
            (_is_mkdir, self._mkdir),
            (_is_install, self._install),
            (_is_chmod, self._chmod),
            (_is_remove, self._remove),
            (_is_pip, self._pip),
            (_is_metadata, self._metadata),
            (_is_control, self._control),
        ):
            if matches(argv):
                return handle(line, argv)
        return CommandOutcome(
            command=line,
            exit_status=127,
            stdout="",
            stderr=f"this robot does not know the command {argv[0] if argv else ''}",
        )

    def _show(self, line: str, argv: list[str]) -> CommandOutcome:
        """Answer `systemctl show`.

        Args:
            line: The rendered command.
            argv: Its arguments.

        Returns:
            The properties asked for.
        """
        wanted = [
            argument.removeprefix("--property=")
            for argument in argv
            if argument.startswith("--property=")
        ]
        values = {
            "LoadState": self.robot.load_state,
            "ActiveState": "active" if self.robot.active else "inactive",
            "SubState": "running" if self.robot.active else "dead",
            "Environment": " ".join(
                shlex.quote(f"{name}={value}")
                for name, value in sorted(self.robot.environment.items())
            ),
            "ExecStart": (
                ""
                if not self.robot.exec_start
                else (
                    f"{{ path={self.robot.exec_start} ; "
                    f"argv[]={self.robot.exec_start} -m reachy_mini.daemon ; "
                    f"ignore_errors=no }}"
                )
            ),
        }
        bare = "--value" in argv
        found = [values.get(name, "") for name in wanted]
        stdout = (
            "\n".join(found)
            if bare
            else "\n".join(f"{name}={values.get(name, '')}" for name in wanted)
        )
        return CommandOutcome(command=line, exit_status=0, stdout=stdout, stderr="")

    def _leak(self) -> str:
        """Render what a leaky robot puts in a complaint.

        Returns:
            The whole environment, values included, as a robot's own tool would
            quote it back.
        """
        if not self.robot.leaky:
            return ""
        rendered = " ".join(
            f"{name}={value}" for name, value in sorted(self.robot.environment.items())
        )
        return f" (the unit's environment was {rendered})"

    def _systemctl(self, line: str, argv: list[str]) -> CommandOutcome:
        """Answer `systemctl restart` and `systemctl daemon-reload`.

        Args:
            line: The rendered command.
            argv: Its arguments.

        Returns:
            What it did. A restart re-reads the managed drop-in, which is what
            makes an environment change actually take effect here.
        """
        if argv[1] == "restart":
            if not self.robot.restart_succeeds:
                return CommandOutcome(
                    command=line,
                    exit_status=1,
                    stdout="",
                    stderr=f"Job for {argv[2]} failed.{self._leak()}",
                )
            self.robot.active = True
            if self.robot.honours_restart:
                self.robot.environment = dict(parse_region(self.robot.managed_region))
            # The daemon restarting takes the application down with it, which
            # is exactly why the command warns before doing it.
            self.robot.app_running = False
            self.robot.app_detail = "stopped by a daemon restart"
        return CommandOutcome(command=line, exit_status=0, stdout="", stderr="")

    def _cat(self, line: str, argv: list[str]) -> CommandOutcome:
        """Answer `cat`.

        Args:
            line: The rendered command.
            argv: Its arguments.

        Returns:
            The file, or a failure when there is none.
        """
        content = self.robot.files.get(argv[1])
        if content is None:
            return CommandOutcome(
                command=line,
                exit_status=1,
                stdout="",
                stderr=f"cat: {argv[1]}: No such file or directory",
            )
        return CommandOutcome(command=line, exit_status=0, stdout=content, stderr="")

    def _mkdir(self, line: str, argv: list[str]) -> CommandOutcome:
        """Answer `mkdir --parents`.

        Args:
            line: The rendered command.
            argv: Its arguments.

        Returns:
            Success. The directory is recorded so a transfer into it works.
        """
        self.robot.files.setdefault(argv[-1], "<directory>")
        return CommandOutcome(command=line, exit_status=0, stdout="", stderr="")

    def _chmod(self, line: str, argv: list[str]) -> CommandOutcome:
        """Answer `chmod`, which narrows the staging directory.

        Args:
            line: The rendered command.
            argv: Its arguments.

        Returns:
            What it did. The mode is recorded so a test can assert the staging
            directory is not left readable to everyone on the robot.
        """
        self.robot.modes[argv[-1]] = argv[-2]
        return CommandOutcome(command=line, exit_status=0, stdout="", stderr="")

    def _remove(self, line: str, argv: list[str]) -> CommandOutcome:
        """Answer `rm --force`, which is how a staged file is discarded.

        Args:
            line: The rendered command.
            argv: Its arguments.

        Returns:
            What it did. `--force` succeeds on a path that is not there, as the
            real one does.
        """
        self.robot.files.pop(argv[-1], None)
        return CommandOutcome(command=line, exit_status=0, stdout="", stderr="")

    def _install(self, line: str, argv: list[str]) -> CommandOutcome:
        """Answer `install`, which is how a staged file reaches `/etc`.

        Args:
            line: The rendered command.
            argv: Its arguments.

        Returns:
            What it did.
        """
        source, destination = argv[-2], argv[-1]
        if source not in self.robot.files:
            return CommandOutcome(
                command=line,
                exit_status=1,
                stdout="",
                stderr=f"install: cannot stat '{source}'",
            )
        self.robot.files[destination] = self.robot.files[source]
        return CommandOutcome(command=line, exit_status=0, stdout="", stderr="")

    def _pip(self, line: str, argv: list[str]) -> CommandOutcome:
        """Answer `python -m pip install`.

        Args:
            line: The rendered command.
            argv: Its arguments.

        Returns:
            Success, whether or not the install went anywhere the daemon reads.
            That is the point: the predecessor's failure exited zero here too.
        """
        staged = self.robot.files.get(argv[-1])
        if staged is None:
            return CommandOutcome(
                command=line,
                exit_status=1,
                stdout="",
                stderr=f"ERROR: {argv[-1]} does not exist",
            )
        if not self.robot.install_succeeds:
            return CommandOutcome(
                command=line,
                exit_status=1,
                stdout="",
                stderr="ERROR: Could not install packages due to an OSError",
            )
        try:
            wheel = describe_wheel(
                argv[-1].rsplit("/", 1)[-1],
                staged.encode("utf-8", errors="surrogateescape"),
            )
        except WheelError as error:
            # What pip does with a file that is not a wheel: refuse it, on the
            # robot, after the transfer. Modelled rather than raised, because a
            # remote command that failed is an outcome and not an exception.
            return CommandOutcome(
                command=line,
                exit_status=1,
                stdout="",
                stderr=f"ERROR: {error}",
            )
        if self.robot.install_takes_effect:
            self.robot.packages[wheel.distribution] = wheel.version
        return CommandOutcome(
            command=line,
            exit_status=0,
            stdout=f"Successfully installed {argv[-1].rsplit('/', 1)[-1]}",
            stderr="",
        )

    def _metadata(self, line: str, argv: list[str]) -> CommandOutcome:
        """Answer the interpreter's metadata query.

        Args:
            line: The rendered command.
            argv: Its arguments.

        Returns:
            One entry per distribution asked about.
        """
        names = argv[3:]
        found = {name: self.robot.packages.get(name, "") for name in names}
        return CommandOutcome(
            command=line,
            exit_status=0,
            stdout=self.robot.metadata_stdout or json.dumps(found),
            stderr="",
        )

    def _control(self, line: str, argv: list[str]) -> CommandOutcome:
        """Answer the daemon's application control.

        Args:
            line: The rendered command.
            argv: Its arguments.

        Returns:
            What it did.
        """
        verb = argv[3]
        if verb == "start":
            self.robot.app_running = self.robot.start_succeeds
            self.robot.app_detail = (
                "active" if self.robot.start_succeeds else "exited 1 on startup"
            )
        elif verb == "stop":
            self.robot.app_running = not self.robot.stop_succeeds
            self.robot.app_detail = (
                "stopped by an operator"
                if self.robot.stop_succeeds
                else "still running after a stop"
            )
        if verb != "status":
            return CommandOutcome(
                command=line,
                exit_status=0 if self.robot.control_succeeds else 1,
                stdout="",
                stderr=(
                    ""
                    if self.robot.control_succeeds
                    else f"the daemon refused to {verb} that application"
                ),
            )
        if self.robot.control_stdout:
            return CommandOutcome(
                command=line,
                exit_status=0,
                stdout=self.robot.control_stdout,
                stderr="",
            )
        return CommandOutcome(
            command=line,
            exit_status=0,
            stdout=json.dumps(
                {
                    "application": argv[-1],
                    "running": self.robot.app_running,
                    "detail": self.robot.app_detail,
                },
            ),
            stderr="",
        )


def _is_show(argv: list[str]) -> bool:
    """Say whether this is `systemctl show`.

    Args:
        argv: The command.

    Returns:
        True when it is.
    """
    return len(argv) > 1 and argv[0] == "systemctl" and argv[1] == "show"


def _is_systemctl_verb(argv: list[str]) -> bool:
    """Say whether this is another `systemctl` verb.

    Args:
        argv: The command.

    Returns:
        True when it is.
    """
    return len(argv) > 1 and argv[0] == "systemctl"


def _is_cat(argv: list[str]) -> bool:
    """Say whether this is `cat`.

    Args:
        argv: The command.

    Returns:
        True when it is.
    """
    return argv[:1] == ["cat"] and len(argv) > 1


def _is_mkdir(argv: list[str]) -> bool:
    """Say whether this is `mkdir`.

    Args:
        argv: The command.

    Returns:
        True when it is.
    """
    return bool(argv) and argv[0] == "mkdir"


def _is_install(argv: list[str]) -> bool:
    """Say whether this is `install`.

    Args:
        argv: The command.

    Returns:
        True when it is.
    """
    return bool(argv) and argv[0] == "install"


def _is_pip(argv: list[str]) -> bool:
    """Say whether this is a `pip install` through an interpreter.

    Args:
        argv: The command.

    Returns:
        True when it is.
    """
    return argv[1:4] == ["-m", "pip", "install"] and len(argv) > 4


def _is_metadata(argv: list[str]) -> bool:
    """Say whether this is the interpreter's metadata query.

    Args:
        argv: The command.

    Returns:
        True when it is.
    """
    return len(argv) > 2 and argv[1] == "-c" and "importlib.metadata" in argv[2]


def _is_control(argv: list[str]) -> bool:
    """Say whether this is the daemon's application control.

    Args:
        argv: The command.

    Returns:
        True when it is.
    """
    return len(argv) > 4 and argv[1] == "-m" and argv[2] != "pip"


def daemon_for(
    robot: FakeRobot | None = None,
    observer: Callable[[Sequence[str]], None] | None = None,
    layout: RobotLayout | None = None,
    complain: Callable[[str], None] | None = None,
) -> tuple[DaemonClient, FakeRemoteAccess]:
    """Build a daemon client over a robot that is not there.

    Args:
        robot: The state to operate on.
        observer: Called with each command as it is sent.
        layout: Where things are on the robot. The defaults are the real ones,
            so a test exercises the paths and unit names that ship.
        complain: Where the client says what is worth seeing and not worth
            failing over.

    Returns:
        The client and the link underneath it, so a test can assert on both what
        was reported and what was actually sent.
    """
    access = FakeRemoteAccess(robot, observer)
    client = DaemonClient(
        access,
        layout or RobotLayout(),
        elevate=True,
        complain=complain,
    )
    return client, access


def _is_chmod(argv: list[str]) -> bool:
    """Say whether this is `chmod`.

    Args:
        argv: The command.

    Returns:
        True when it is.
    """
    return len(argv) > 2 and argv[0] == "chmod"


def _is_remove(argv: list[str]) -> bool:
    """Say whether this is `rm`.

    Args:
        argv: The command.

    Returns:
        True when it is.
    """
    return len(argv) > 1 and argv[0] == "rm"
