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

**A robot has two environments and one of the two control interfaces.** The
daemon runs out of one virtual environment and, on the released image, installs
applications into a sibling of it — so `app_packages` is what the application
environment holds and `packages` is what the daemon's own does, and a robot that
keeps a single environment for both simply leaves `app_packages` unset. The
released image serves its application control over an HTTP API on the robot;
the container target the provisioning gate runs against implements the control
module instead. `daemon_api` says which of the two this robot has, so both
routes are exercised without either being the only one that is ever tested.

**Running the unit's start program starts a second daemon.** `interpreters` says
which paths on this robot are really interpreters, and anything else the unit
starts is a launcher: sending it a command records the run in `wrapper_runs` and
answers the way the real one did on ReachyMiniOS v0.2.3 — a second daemon that
found the port already bound and died. A test proves REQ-106 by asserting that
list is empty, which is an assertion about the robot rather than about the
arguments a tool happened to build.

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
    DEFAULT_APPLICATION,
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
    "DAEMON_INTERPRETER",
    "DROP_IN",
    "ROBOT",
    "STOCK_INTERPRETER",
    "STOCK_LAUNCHER",
    "FakeRemoteAccess",
    "FakeRobot",
    "applied_settings",
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

# The interpreter a default robot's daemon environment is built around, and the
# version it answers `-V` with.
DAEMON_INTERPRETER: Final = "/opt/reachy/venv/bin/python"
_INTERPRETER_VERSION: Final = "3.12.3"

# What the stock image's unit actually starts: a shell launcher living inside the
# daemon's own environment, three directories below its `site-packages`. Not a
# path from anybody's robot — it is the layout the released image ships, quoted
# here so the resolution can be tested against the shape that broke it.
STOCK_LAUNCHER: Final = (
    "/venvs/mini_daemon/lib/python3.12/site-packages/reachy_mini/daemon/app/"
    "services/wireless/launcher.sh"
)
STOCK_INTERPRETER: Final = "/venvs/mini_daemon/bin/python"

# And the environment that image installs applications into: a SIBLING of the
# daemon's own, which is what makes asking the daemon's interpreter what version
# of the application is installed the wrong question on a real robot.
STOCK_APPLICATIONS: Final = "/venvs/apps_venv/bin/python"


@dataclass
class FakeRobot:
    """The state a robot has, as far as this tool can see it.

    Attributes:
        active: Whether the daemon's unit is running.
        load_state: What systemd says about the unit being installed at all.
        exec_start: The program in the unit's `ExecStart`, or empty when the
            unit declares none. It is the daemon's entry point, which on some
            images is an interpreter and on the stock one is a launcher.
        interpreters: Every path on this robot that answers `-V`, and what it
            says after the word `Python`. Anything else asked for its version
            says there is no such file. Anything but a bare version models the
            impostor — an empty string, or a version with a banner or a usage
            line after it — which is not an interpreter and must not be treated
            as one.
        noisy_interpreters: Paths that also write to standard error, and what
            they write there. `-V` makes CPython write one bare version to one
            stream and nothing to the other, so anything that speaks on both is
            an impostor — whether the second stream carries a launcher's banner
            or the rest of a version split in half.
        wrapper_runs: Every command sent to the unit's start program while that
            program is not an interpreter. Each one started a second daemon, so
            a test asserting REQ-106 asserts this is empty.
        files: The robot's filesystem, as far as this tool writes to it.
        packages: What is installed in the environment the daemon runs.
        environments: What each named interpreter's environment holds, for a
            robot with more than one. An interpreter not in here answers from
            `packages`, which is what an image keeping a single environment for
            the daemon and its applications does — and what the container
            target the provisioning gate uses is.
        daemon_api: Whether this robot serves the daemon's own HTTP API. False
            models an image that does not, on which the control module is the
            only interface, and is the default because that is the shape every
            test written before the API existed assumes.
        api_stdout: What the daemon's API writes for a status request, when it
            is not to write the status document this tool reads.
        api_refuses: Whether the API answers every request with an error
            status. A daemon that answered and refused is not a daemon with no
            API, and the two must not be treated alike.
        version_answers: How many times an interpreter answers `-V` before this
            robot reports it gone. `None` is always, which is every ordinary
            robot; a number models an environment that changes under the client
            between one question and the next, which is the case this module
            refuses to memoise against.
        current_app: Which application the daemon's API reports as the current
            one. Not always the one being asked about: a robot running
            something else is a robot this application is not running on.
        app_state: What the API calls the current application's state, when it
            is not to be derived from `app_running`. `starting` and `error` are
            the interesting ones, because neither is running.
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
        control_runs: Whether the control module is there at all. False is the
            released image: `reachy_mini.apps` has no `__main__`, so a robot
            with no HTTP API and no module has neither interface, which is the
            case whose failure has to name both.
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
    exec_start: str = DAEMON_INTERPRETER
    interpreters: dict[str, str] = field(
        default_factory=lambda: {DAEMON_INTERPRETER: _INTERPRETER_VERSION},
    )
    noisy_interpreters: dict[str, str] = field(default_factory=dict)
    wrapper_runs: list[list[str]] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)
    packages: dict[str, str] = field(
        default_factory=lambda: {DAEMON_DISTRIBUTION: "4.5.6"},
    )
    environments: dict[str, dict[str, str]] = field(default_factory=dict)
    daemon_api: bool = False
    api_stdout: str = ""
    api_refuses: bool = False
    version_answers: int | None = None
    current_app: str = DEFAULT_APPLICATION
    app_state: str = ""
    environment: dict[str, str] = field(default_factory=dict)
    app_running: bool = False
    app_detail: str = "inactive"
    honours_restart: bool = True
    install_succeeds: bool = True
    install_takes_effect: bool = True
    restart_succeeds: bool = True
    start_succeeds: bool = True
    stop_succeeds: bool = True
    control_runs: bool = True
    control_stdout: str = ""
    metadata_stdout: str = ""
    journal_interrupts: bool = False
    control_succeeds: bool = True
    modes: dict[str, str] = field(default_factory=dict)
    journal: list[str] = field(default_factory=list)
    leaky: bool = False
    failing: set[str] = field(default_factory=set)

    @property
    def managed_region(self) -> str | None:
        """What the managed drop-in holds right now.

        Returns:
            Its content, or `None` when there is no such file. `None` and `""`
            are different robots — a drop-in that was never written and one
            something emptied — and this models that distinction rather than
            flattening it, because flattening it is the defect the tool was
            fixed for.

            This is also the value a preview test snapshots before and compares
            after: the guarantee is that nothing happened, and only an
            after-state assertion tests that.
        """
        return self.files.get(DROP_IN)


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
        if self._is_launcher(argv):
            return self._launch(line, argv)
        if _is_version(argv):
            # Answered before `failing` is consulted, because the two model
            # different things. `interpreters` is what this robot HAS; `failing`
            # is a program refusing the work it was asked to do, and being asked
            # what you are is not work. A test that wants a path which is not an
            # interpreter says so by leaving it out of `interpreters`.
            return self._version(line, argv)
        if argv and argv[0] in self.robot.failing:
            return CommandOutcome(
                command=line,
                exit_status=1,
                stdout="",
                stderr=f"{argv[0]}: this robot was told to refuse that",
            )
        for matches, handle in (
            (_is_api, self._api),
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

    def _is_launcher(self, argv: list[str]) -> bool:
        """Say whether this command runs the unit's start program.

        Args:
            argv: The command.

        Returns:
            True when the program being run is what the unit starts and that
            program is not one of this robot's interpreters — which is the whole
            of the stock image's problem.
        """
        return bool(
            argv
            and self.robot.exec_start
            and argv[0] == self.robot.exec_start
            and argv[0] not in self.robot.interpreters,
        )

    def _launch(self, line: str, argv: list[str]) -> CommandOutcome:
        """Run the unit's start program, which starts a second daemon.

        Args:
            line: The rendered command.
            argv: Its arguments.

        Returns:
            What the real one did on ReachyMiniOS v0.2.3: the launcher ignored
            everything it was passed, started a daemon, found the first one
            already holding the port and the serial device, and died.
        """
        self.robot.wrapper_runs.append(argv)
        return CommandOutcome(
            command=line,
            exit_status=1,
            stdout="",
            stderr=(
                "ERROR: [Errno 98] error while attempting to bind on address "
                "('0.0.0.0', 8000): address already in use"
            ),
        )

    def _version(self, line: str, argv: list[str]) -> CommandOutcome:
        """Answer a candidate interpreter asked to identify itself.

        Args:
            line: The rendered command.
            argv: Its arguments.

        Returns:
            A version line when this robot really has an interpreter there, and
            what a shell says about a path that is not there when it does not.
        """
        version = self.robot.interpreters.get(argv[0])
        if self.robot.version_answers is not None:
            if self.robot.version_answers <= 0:
                version = None
            self.robot.version_answers -= 1
        if version is None:
            return CommandOutcome(
                command=line,
                exit_status=127,
                stdout="",
                stderr=f"{argv[0]}: No such file or directory",
            )
        return CommandOutcome(
            command=line,
            exit_status=0,
            stdout=f"Python {version}\n",
            stderr=self.robot.noisy_interpreters.get(argv[0], ""),
        )

    def _api(self, line: str, argv: list[str]) -> CommandOutcome:
        """Answer a request to the daemon's own HTTP API.

        The exit statuses are the contract, not the bodies: 7 says this robot
        serves no such API and the client should try the other interface, 3
        says the API answered and refused, and 0 says it answered.

        Args:
            line: The rendered command.
            argv: Its arguments, ending in the method and the URL.

        Returns:
            What the request did.
        """
        method, url = argv[-2], argv[-1]
        if not self.robot.daemon_api:
            return CommandOutcome(
                command=line,
                exit_status=7,
                stdout="",
                stderr="[Errno 111] Connection refused",
            )
        if self.robot.api_refuses:
            return CommandOutcome(
                command=line,
                exit_status=3,
                stdout="",
                stderr="503 Service Unavailable",
            )
        path = url.partition("://")[2].partition("/")[2]
        if method == "GET" and path.endswith("current-app-status"):
            return CommandOutcome(
                command=line,
                exit_status=0,
                stdout=self.robot.api_stdout or self._app_status(),
                stderr="",
            )
        if method == "POST" and "/start-app/" in path:
            self.robot.current_app = path.rpartition("/start-app/")[2]
            self.robot.app_running = self.robot.start_succeeds
            self.robot.app_detail = (
                "active" if self.robot.start_succeeds else "exited 1 on startup"
            )
            return self._api_verb(line)
        if method == "POST" and path.endswith("stop-current-app"):
            self.robot.app_running = not self.robot.stop_succeeds
            self.robot.app_detail = (
                "stopped by an operator"
                if self.robot.stop_succeeds
                else "still running after a stop"
            )
            return self._api_verb(line)
        return CommandOutcome(
            command=line,
            exit_status=3,
            stdout="",
            stderr="404 Not Found",
        )

    def _api_verb(self, line: str) -> CommandOutcome:
        """Answer a request that asked the daemon to do something.

        Args:
            line: The rendered command.

        Returns:
            What it did. A daemon that refused answers with an error STATUS,
            which is not the same as having no API at all.
        """
        if not self.robot.control_succeeds:
            return CommandOutcome(
                command=line,
                exit_status=3,
                stdout="",
                stderr="400 Bad Request",
            )
        return CommandOutcome(
            command=line,
            exit_status=0,
            stdout=self._app_status(),
            stderr="",
        )

    def _app_status(self) -> str:
        """Render what the daemon's API says about the current application.

        Returns:
            The status document, or `null` when the daemon is running nothing
            — which is what the real endpoint answers and is a different fact
            from an application that is installed and stopped.
        """
        if not self.robot.app_running and not self.robot.app_state:
            return "null"
        state = self.robot.app_state or (
            "running" if self.robot.app_running else "done"
        )
        return json.dumps(
            {
                "info": {"name": self.robot.current_app, "source_kind": "installed"},
                "state": state,
                "error": None,
            },
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
                # A robot with no drop-in restarts into an empty environment;
                # one whose drop-in something emptied is a robot systemd would
                # refuse to read, and the tool refuses it too rather than
                # guessing — so the fake does not guess either.
                region = self.robot.managed_region
                self.robot.environment = (
                    {} if region is None else dict(parse_region(region))
                )
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
        installed = self.robot.environments.get(argv[0], self.robot.packages)
        found = {name: installed.get(name, "") for name in names}
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
        if not self.robot.control_runs:
            return CommandOutcome(
                command=line,
                exit_status=1,
                stdout="",
                stderr=f"{argv[0]}: No module named {argv[2]}.__main__",
            )
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


def _is_version(argv: list[str]) -> bool:
    """Say whether this asks a candidate interpreter to identify itself.

    Args:
        argv: The command.

    Returns:
        True when it is `<path> -V` and nothing else. A flag and no source,
        which is the point of it.
    """
    return argv[1:] == ["-V"]


def _is_api(argv: list[str]) -> bool:
    """Say whether this is a request to the daemon's own HTTP API.

    Args:
        argv: The command.

    Returns:
        True when it is the client's request script, run through an
        interpreter. Told apart from the metadata query by what the source
        imports, which is what the two are actually distinguished by.
    """
    return len(argv) > 2 and argv[1] == "-c" and "urllib" in argv[2]


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


def applied_settings(robot: FakeRobot) -> dict[str, str]:
    """Read back what an apply left in the robot's managed drop-in.

    Asserts the file is *there* before parsing it, which is the distinction the
    tool itself makes: an absent drop-in and an empty one are different robots,
    and a test asserting on "the settings" has to say which of the two it
    expects to be looking at. A test that means "nothing was applied" asserts
    `robot.managed_region is None` instead.

    Args:
        robot: The robot to read.

    Returns:
        The settings the drop-in carries.
    """
    region = robot.managed_region
    assert region is not None, "the managed drop-in was never written"
    return parse_region(region)
