"""The robot's daemon, asked the questions this tool needs answered.

This is the adapter `reachy_checks.RobotDaemon` was written against and left
unbound in change 0008. It is also everything `deploy`, `config` and `app` do to
a robot, which is deliberate: one object knows which command answers which
question, so a check and a deploy step cannot end up asking two different things
and calling both of them "is the application running".

**Nothing here is memoised, and that is the whole design.** The failure this
change exists to remove is a package that installed successfully into an
environment the running daemon was not using — an answer that was true a moment
ago and is not true now. A cache would make a deploy's verification step capable
of returning the value it read before the restart, which is precisely the
outcome that looks identical to success. Every method asks the robot.

**The interpreter is the daemon's, not a path this tool assumed.** Before asking
what version of the application is installed, this client asks systemd which
interpreter the daemon actually runs, and asks *that* one. Installing into a
configured path and then verifying against the same configured path would agree
with itself no matter which environment the daemon was really using, which is
the shape of the original failure rather than a check on it. The configured path
is a fallback for a unit whose `ExecStart` cannot be read, and it says so.

**A question that could not be asked is not an answer.** A method here either
returns what the robot said or raises. None of them returns an empty mapping, an
empty version or an empty file to mean "the command failed", because a caller
cannot tell that apart from "there is nothing there" — and the whole of this
change is about refusing to treat an absent answer as a good one. "Not
installed" and "no such drop-in" are real answers and are returned as such; a
command that could not run at all is a `RobotAccessError`, which the command
surface already knows costs `UNREACHABLE`.

**No method reports a configuration value.** `effective_configuration` returns
the settings so a caller can compare them; whether any of them is rendered is
the caller's decision, and `reachyctl.configure` renders a secret setting as set
or unset. See `reachy_checks.probes` on why that rule is written down twice.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import replace
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Final

from reachy_checks import ApplicationState, DaemonInfo, InstalledApplication
from reachyctl.managed import MalformedRegionError, parse_region
from reachyctl.robot import CommandOutcome, RobotAccessError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping, Sequence

    from reachyctl.robot import RemoteAccess, RobotLayout

__all__ = ["DaemonClient", "DaemonControlError"]

# Asked of the robot's own interpreter, so the answer is what that environment
# holds rather than what a wheel's file name claims. One round trip answers for
# every distribution named, because the link is slow enough that two would be
# noticed.
_METADATA_SCRIPT: Final = (
    "import json, sys\n"
    "from importlib.metadata import PackageNotFoundError, version\n"
    "found = {}\n"
    "for name in sys.argv[1:]:\n"
    "    try:\n"
    "        found[name] = version(name)\n"
    "    except PackageNotFoundError:\n"
    "        found[name] = ''\n"
    "sys.stdout.write(json.dumps(found))\n"
)

# systemd renders a command as `{ path=/usr/bin/x ; argv[]=... ; ... }`, one
# such block per `ExecStart=` the unit declares. The first block's path is the
# interpreter the daemon runs.
_EXEC_PATH: Final = re.compile(r"path=(\S+)")

# systemd's own spelling for "this unit is running".
_ACTIVE: Final = "active"

# What a POSIX tool says when a path is not there. Matched rather than inferred
# from the exit status, because `cat` exits 1 for a file that is absent and for
# one it may not read, and those are different facts about the robot. A robot
# whose tools speak another language falls through to the error, which is the
# safe direction: it says the file could not be read rather than silently
# treating it as never written.
_NOT_FOUND: Final = re.compile(r"No such file or directory", re.IGNORECASE)


class DaemonControlError(RobotAccessError):
    """The daemon's application control answered with something unreadable.

    Its own type because it is a different fault from the link being down: the
    robot answered, and what it said is not what this tool knows how to read.
    The most likely cause is a daemon whose control module is spelled
    differently from `RobotLayout.daemon_control`, which is an option away.
    """


class DaemonClient:
    """The robot's daemon, reached over a remote-access link."""

    def __init__(
        self,
        access: RemoteAccess,
        layout: RobotLayout,
        *,
        elevate: bool = True,
        complain: Callable[[str], None] | None = None,
    ) -> None:
        """Bind a client to one robot.

        Args:
            access: How commands reach it.
            layout: Where things are on it and what they are called.
            elevate: Whether privileged commands are prefixed with `sudo -n`.
            complain: Where to say something that is worth an operator seeing
                and is not worth failing a command over. The command surface
                passes the reporter's own progress line, so it is scrubbed like
                everything else; without one, such a line is dropped.
        """
        self._access = access
        self._layout = layout
        self._elevate = elevate
        self._complain_to = complain

    def _complain(self, message: str) -> None:
        """Say something an operator should see that does not fail a command.

        Args:
            message: What to say.
        """
        if self._complain_to is not None:
            self._complain_to(message)

    def for_application(self, application: str) -> DaemonClient:
        """Bind a client to the same robot, about a different application.

        `deploy` uses this the moment it knows what the wheel carries. The
        alternative — verifying whatever `--application` happened to say while
        installing whatever the wheel happened to hold — is a deploy that
        reports success because *some other* application is at the version the
        wheel declares. That is the shape of the failure this whole tool is
        written against, arriving by a different door.

        Args:
            application: The distribution the daemon controls and this client
                asks about.

        Returns:
            A client over the same link, with the same everything else.
        """
        return DaemonClient(
            self._access,
            replace(self._layout, application=application),
            elevate=self._elevate,
            complain=self._complain_to,
        )

    @property
    def layout(self) -> RobotLayout:
        """Where things are on this robot.

        Returns:
            The layout this client was built with, so a caller reporting what
            it did can name the paths it used.
        """
        return self._layout

    async def connect(self) -> None:
        """Open the link, so a robot that is not there is reported as one.

        Called first by every command that operates a robot. See
        `RemoteAccess.connect` for why it is explicit rather than left to the
        first question.

        Raises:
            RobotAccessError: If the robot cannot be reached.
        """
        await self._access.connect()

    # --- the RobotDaemon protocol the shared checks are written against ------

    async def ping(self) -> DaemonInfo:
        """Ask the daemon whether it is there and what it is.

        Returns:
            Whether its unit is active, and the daemon distribution's version
            when it is. A unit that is not loaded and a unit that is loaded and
            stopped are different faults and the complaint says which.
        """
        properties = await self._show(
            self._layout.daemon_unit,
            "LoadState",
            "ActiveState",
            "SubState",
        )
        load = properties.get("LoadState", "")
        active = properties.get("ActiveState", "")
        if load and load != "loaded":
            return DaemonInfo(
                responding=False,
                complaint=(
                    f"the unit {self._layout.daemon_unit} is {load}; the daemon "
                    f"is not installed on this robot"
                ),
            )
        if active != _ACTIVE:
            return DaemonInfo(
                responding=False,
                complaint=(
                    f"the unit {self._layout.daemon_unit} is "
                    f"{active or 'not reporting a state'}"
                    f"{_substate(properties)}"
                ),
            )
        versions = await self.installed_versions(self._layout.daemon_distribution)
        return DaemonInfo(
            responding=True,
            version=versions.get(self._layout.daemon_distribution, ""),
        )

    async def installed_application(self) -> InstalledApplication:
        """Ask what version of the application the daemon's environment holds.

        Returns:
            Whether it is installed and at what version, read through the
            interpreter the daemon itself runs.
        """
        versions = await self.installed_versions(self._layout.application)
        version = versions.get(self._layout.application, "")
        if not version:
            return InstalledApplication(
                installed=False,
                complaint=(
                    f"{self._layout.application} is not installed in the "
                    f"environment the daemon runs"
                ),
            )
        return InstalledApplication(installed=True, version=version)

    async def application_state(self) -> ApplicationState:
        """Ask the daemon whether it is running the application.

        Returns:
            Whether it is, with whatever the daemon said about it. `running` is
            false only when the daemon said so.

        Raises:
            DaemonControlError: If the control could not be run, or answered
                with something this tool cannot read. Returning "not running"
                for either would make `app stop` report an application it never
                asked about as already stopped, and exit zero.
        """
        outcome = await self._control("status", "--json")
        if not outcome.ok:
            message = (
                f"the daemon's application control could not be run: "
                f"{outcome.complaint()}"
            )
            raise DaemonControlError(message)
        report = self._decode(outcome)
        running = report.get("running")
        detail = report.get("detail")
        return ApplicationState(
            running=running is True,
            detail=detail if isinstance(detail, str) else "",
        )

    async def effective_configuration(self) -> Mapping[str, str]:
        """Ask systemd what environment the daemon is actually running with.

        This is the *effective* environment rather than the managed region:
        everything the unit ended up with, whichever drop-in or unit file put
        it there. That is what makes the effective-configuration check able to
        catch a setting that is declared and silently not in force, which is
        one of the two failures the reachyctl spec's background names.

        Returns:
            The settings by name. Values are returned so a caller can compare
            them and are not for printing — see the module documentation.

        Raises:
            RobotAccessError: If the environment could not be read. An empty
                mapping would mean "this robot has no settings", which is a
                different robot from one that did not answer: `config diff`
                would report every declared setting as missing, and an apply's
                verification would fail a change that had worked. The message
                quotes nothing the robot wrote — this is the read that teaches
                the redactor what to scrub.
        """
        outcome = await self._run(
            [
                "systemctl",
                "show",
                self._layout.daemon_unit,
                "--property=Environment",
                "--value",
            ],
        )
        if not outcome.ok:
            # `status_only`, not `complaint`: this is the read whose answer
            # teaches the redactor which values on this robot are secret, so its
            # own output is the one thing nothing could scrub. See
            # `CommandOutcome.status_only`.
            message = (
                f"could not read the environment of {self._layout.daemon_unit}: "
                f"{outcome.status_only()}"
            )
            raise RobotAccessError(message)
        settings: dict[str, str] = {}
        # systemd prints the whole environment on one line, quoting an
        # assignment that needs it. Splitting it the way a shell would is the
        # closest available parse and not an exact one: systemd's own escaping
        # is its own, and a value carrying something it escapes differently
        # would come back subtly wrong. It is what there is — `systemctl show`
        # offers no structured output — and the managed region itself is read
        # from the file, where the format is this repository's own.
        for assignment in shlex.split(outcome.stdout.strip()):
            name, separator, value = assignment.partition("=")
            if separator:
                settings[name] = value
        return settings

    async def announced_identity(self) -> str:
        """Ask what identity the satellite announces to Home Assistant.

        The identity is a setting, so it is read from the effective
        environment rather than through a second interface. It is also the one
        setting whose value is reported verbatim: it is a device name whose
        whole purpose is to be recognisable, and it is what an operator has to
        compare against Home Assistant's device list.

        Returns:
            The announced identity, or an empty string when nothing sets one.
        """
        settings = await self.effective_configuration()
        return settings.get("REACHY_HOME_ASSISTANT_IDENTITY", "")

    # --- what the operating commands need ------------------------------------

    async def interpreter(self) -> str:
        """Ask systemd which interpreter the daemon runs.

        Returns:
            The path in the unit's first `ExecStart`. The configured fallback is
            used only when the unit declares none — a unit that is not installed
            reports an empty property — and never to paper over a command that
            failed. See the module documentation for why asking rather than
            assuming is the point.

        Raises:
            RobotAccessError: If the unit could not be read at all.
        """
        properties = await self._show(self._layout.daemon_unit, "ExecStart")
        found = _EXEC_PATH.search(properties.get("ExecStart", ""))
        return found.group(1) if found is not None else self._layout.python

    async def installed_versions(self, *distributions: str) -> dict[str, str]:
        """Ask the daemon's environment what versions it holds.

        Args:
            distributions: The distribution names to look up.

        Returns:
            One entry per name, empty where nothing is installed. That is a real
            answer about a real environment.

        Raises:
            RobotAccessError: If the environment could not be asked, or answered
                with something unreadable. Answering "nothing is installed"
                would make a deploy's verification report the exact failure it
                exists to detect — a version that is not there — for a robot
                that simply did not answer.
        """
        python = await self.interpreter()
        outcome = await self._run([python, "-c", _METADATA_SCRIPT, *distributions])
        if not outcome.ok:
            message = (
                f"could not ask {python} what it has installed: {outcome.complaint()}"
            )
            raise RobotAccessError(message)
        try:
            decoded = json.loads(outcome.stdout)
        except ValueError as error:
            message = (
                f"{python} answered the version query with something that is not JSON"
            )
            raise RobotAccessError(message) from error
        if not isinstance(decoded, dict):
            message = (
                f"{python} answered the version query with a "
                f"{type(decoded).__name__} rather than an object"
            )
            raise RobotAccessError(message)
        return {name: str(decoded.get(name, "") or "") for name in distributions}

    async def read_managed_region(self) -> str | None:
        """Read the drop-in this tool owns in full.

        **Three states, three answers**, and collapsing any two of them is how a
        file this tool did not write gets silently replaced. The file is not
        there: nothing has been applied, and the next apply proceeds. The file
        is there: its content comes back, whatever is in it — including
        nothing, which `reachy_checks`-style callers must treat as a file
        rather than as an absence, because this format never writes an empty
        one. The file is there and cannot be read: that is a fault and is
        raised.

        Returns:
            The file's content, or `None` when there is no file. `None` and
            `""` are different robots and the type says so.

        Raises:
            RobotAccessError: If the file is there and could not be read — a
                permission that is wrong, a directory where a file belongs.
                Treating that as "never written" would make the next apply
                report every setting as new and then overwrite whatever is
                actually in the file.
        """
        outcome = await self._run(["cat", self._layout.drop_in])
        if outcome.ok:
            return outcome.stdout
        if _NOT_FOUND.search(outcome.stderr):
            return None
        message = f"could not read {self._layout.drop_in}: {outcome.complaint()}"
        raise RobotAccessError(message)

    async def read_managed_settings(self) -> dict[str, str]:
        """Read back the settings the managed region carries.

        Returns:
            The settings by name. A robot with no drop-in carries none, which is
            a robot nothing has been applied to rather than an error.

        Raises:
            MalformedRegionError: If the file is there and is not one this
                tooling wrote — including one that is there and empty, which
                this format never produces. The path is named, because the
                operator's next question is which file to look at. Deciding what
                to *do* about it belongs to the command.
        """
        content = await self.read_managed_region()
        if content is None:
            return {}
        try:
            return parse_region(content)
        except MalformedRegionError as error:
            message = f"{error}. The file is {self._layout.drop_in}"
            raise MalformedRegionError(message) from error

    async def write_managed_region(self, content: str) -> None:
        """Replace the managed drop-in with new content, and reload systemd.

        The write is staged and then copied into place with `install`, rather
        than written to `/etc` directly: the staging area is somewhere the
        connecting account can write, and `install` is what sets the mode and
        the ownership in the same step that puts the file where systemd reads
        it. A half-written drop-in is a daemon that will not start.

        **The staged copy is removed afterwards, always.** `install` copies, so
        without this the whole region — including whatever a setting marked
        secret holds — would be left sitting in the staging directory, on the
        robot, indefinitely. It is removed in a `finally`, because the paths
        that fail are exactly the ones that would otherwise leave it there.

        Args:
            content: The whole file, as `reachyctl.managed.render_region`
                produced it.

        Raises:
            RobotAccessError: If any step of the write failed. The message
                names the step and quotes no setting value.
        """
        staged = await self.stage(content.encode("utf-8"), "managed.conf")
        try:
            await self._expect(
                self._privileged(
                    ["mkdir", "--parents", self._layout.drop_in_directory],
                ),
                "could not create the drop-in directory",
            )
            await self._expect(
                self._privileged(
                    [
                        "install",
                        "--mode=0644",
                        "--owner=root",
                        "--group=root",
                        str(staged),
                        self._layout.drop_in,
                    ],
                ),
                "could not install the managed drop-in",
            )
            await self._expect(
                self._privileged(["systemctl", "daemon-reload"]),
                "could not reload systemd after writing the managed drop-in",
            )
        finally:
            await self.discard(staged)

    async def stage(self, content: bytes, name: str) -> PurePosixPath:
        """Put bytes somewhere on the robot the connecting account can write.

        The directory is created and then narrowed to the connecting account,
        because what passes through it includes the managed region and a
        setting is exactly where a credential lives. `chmod` runs on every call
        rather than only on creation: `mkdir --parents` leaves an existing
        directory's mode alone, so a staging directory made by something else,
        or by an older version of this tool, would keep whatever mode it had.

        Args:
            content: What to write.
            name: The file name to give it inside the staging directory.

        Returns:
            Where it landed. The caller removes it when it is done with it —
            `write_managed_region` and `reachyctl.deploy` both do.

        Raises:
            RobotAccessError: If the staging directory could not be made or
                narrowed, or the transfer failed.
        """
        await self._expect(
            ["mkdir", "--parents", self._layout.staging],
            "could not create the staging directory",
        )
        await self._expect(
            ["chmod", "0700", self._layout.staging],
            "could not narrow the staging directory to this account",
        )
        destination = PurePosixPath(self._layout.staging) / name
        await self._access.upload(content, destination)
        return destination

    async def discard(self, staged: PurePosixPath) -> None:
        """Remove something this tool staged on the robot.

        Best effort, and deliberately so: it is called from the `finally` of a
        step that may already be failing, and a robot that cannot delete a
        temporary file is not a reason to replace the failure an operator needs
        to read with one about tidying up. What it must not do is leave the
        file there quietly, so a refusal is written to the progress stream.

        It is called from the `finally` of steps that may already be failing,
        including ones that failed because the link broke — so it swallows a
        link failure too. Letting one out would replace the reason the deploy
        failed with a message about tidying up, which is the wrong sentence for
        an operator to be reading.

        Args:
            staged: What to remove.
        """
        try:
            outcome = await self._run(["rm", "--force", str(staged)])
        except RobotAccessError as error:
            self._complain(f"could not remove {staged} from the robot: {error}")
            return
        if not outcome.ok:
            # Nothing is raised, and nothing is silent either.
            self._complain(
                f"could not remove {staged} from the robot: {outcome.complaint()}",
            )

    async def install_wheel(self, wheel: PurePosixPath) -> CommandOutcome:
        """Install a wheel into the environment the daemon runs.

        Args:
            wheel: Where the wheel is on the robot.

        Returns:
            What the install did. A failed install is returned rather than
            raised, because the deploy step sequence reports it as the step
            that failed — and because the install exiting zero is exactly the
            thing this change refuses to treat as success.
        """
        python = await self.interpreter()
        return await self._run(
            self._privileged(
                [python, "-m", "pip", "install", "--upgrade", str(wheel)],
            ),
        )

    async def restart_daemon(self) -> CommandOutcome:
        """Restart the daemon, which is what puts an environment change in force.

        Returns:
            What the restart did.
        """
        return await self._run(
            self._privileged(["systemctl", "restart", self._layout.daemon_unit]),
        )

    async def start_application(self) -> CommandOutcome:
        """Ask the daemon to start the application.

        Returns:
            What the daemon's control did.
        """
        return await self._control("start")

    async def stop_application(self) -> CommandOutcome:
        """Ask the daemon to stop the application.

        Returns:
            What the daemon's control did.
        """
        return await self._control("stop")

    def journal(
        self,
        *,
        lines: int,
        follow: bool,
        since: str = "",
    ) -> AsyncIterator[str]:
        """Read the robot's journal, filtered to the application.

        The filter is a journal field match rather than a search of the text,
        so a line mentioning the application in passing is not a line the
        application wrote. It is combined with the daemon's unit, because the
        application runs as a child of the daemon and everything it writes is
        recorded against that unit.

        Args:
            lines: How many past lines to show before anything new.
            follow: Whether to keep the stream open and yield lines as they
                arrive.
            since: A journal time expression to start from, or empty for none.

        Returns:
            The lines, as they arrive.
        """
        command = [
            "journalctl",
            "--unit",
            self._layout.daemon_unit,
            "--output",
            "short-iso",
            "--no-pager",
            "--lines",
            str(lines),
        ]
        if since:
            command += ["--since", since]
        if follow:
            command.append("--follow")
        command.append(f"SYSLOG_IDENTIFIER={self._layout.application}")
        return self._access.stream(self._privileged(command))

    # --- the plumbing --------------------------------------------------------

    def _privileged(self, command: Sequence[str]) -> list[str]:
        """Prefix a command that needs root, when this robot needs it prefixed.

        Args:
            command: The arguments to run.

        Returns:
            The same arguments, behind a non-interactive `sudo` when elevation
            is on. Non-interactive on purpose: a `sudo` that stopped to ask for
            a password over a link with no terminal would hang a deploy at the
            step that restarts the daemon.
        """
        if not self._elevate:
            return list(command)
        return ["sudo", "-n", *command]

    async def _control(self, verb: str, *arguments: str) -> CommandOutcome:
        """Run one of the daemon's application-control verbs.

        Args:
            verb: What to ask for.
            arguments: Anything the verb takes before the application's name.

        Returns:
            What it did.
        """
        python = await self.interpreter()
        return await self._run(
            [
                python,
                "-m",
                self._layout.daemon_control,
                verb,
                *arguments,
                self._layout.application,
            ],
        )

    def _decode(self, outcome: CommandOutcome) -> Mapping[str, object]:
        """Read a JSON object the daemon's control printed.

        Args:
            outcome: What the command did.

        Returns:
            The object.

        Raises:
            DaemonControlError: If what came back is not a JSON object. Nothing
                the robot printed is quoted: this is the daemon's own output and
                the daemon's output is where a setting's value would be.
        """
        try:
            decoded = json.loads(outcome.stdout)
        except ValueError as error:
            message = (
                f"the daemon's application control answered `{outcome.command}` "
                f"with something that is not JSON. Check that "
                f"{self._layout.daemon_control} is the module this daemon "
                f"exposes"
            )
            raise DaemonControlError(message) from error
        if not isinstance(decoded, dict):
            message = (
                f"the daemon's application control answered `{outcome.command}` "
                f"with a {type(decoded).__name__} rather than an object"
            )
            raise DaemonControlError(message)
        return decoded

    async def _show(self, unit: str, *properties: str) -> dict[str, str]:
        """Ask systemd for some of a unit's properties.

        Args:
            unit: The unit to ask about.
            properties: Which properties to ask for.

        Returns:
            The properties by name. A unit that is not installed answers with
            empty values rather than failing, so an empty answer here means what
            it says.

        Raises:
            RobotAccessError: If the command could not be run. Returning empty
                properties would be indistinguishable from a unit that has none,
                and `ping` would then report a healthy robot as merely quiet.
        """
        outcome = await self._run(
            ["systemctl", "show", unit, *(f"--property={name}" for name in properties)],
        )
        if not outcome.ok:
            message = (
                f"could not read {', '.join(properties)} of {unit}: "
                f"{outcome.complaint()}"
            )
            raise RobotAccessError(message)
        found: dict[str, str] = {}
        for line in outcome.stdout.splitlines():
            name, separator, value = line.partition("=")
            if separator:
                found[name] = value
        return found

    async def _run(self, command: Sequence[str]) -> CommandOutcome:
        """Run one command on the robot.

        Args:
            command: The arguments to run.

        Returns:
            What it did.
        """
        return await self._access.run(command)

    async def _expect(self, command: Sequence[str], complaint: str) -> CommandOutcome:
        """Run a command that has to succeed.

        Args:
            command: The arguments to run.
            complaint: What to say if it does not.

        Returns:
            What it did.

        Raises:
            RobotAccessError: If it did not succeed.
        """
        outcome = await self._run(command)
        if not outcome.ok:
            raise RobotAccessError(f"{complaint}: {outcome.complaint()}")
        return outcome


def _substate(properties: Mapping[str, str]) -> str:
    """Add systemd's finer-grained state to a complaint, when there is one.

    Args:
        properties: What `systemctl show` reported.

    Returns:
        The sub-state in parentheses, or an empty string.
    """
    substate = properties.get("SubState", "")
    return f" ({substate})" if substate else ""
