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
what version of the application is installed, this client resolves which
interpreter owns the environment the daemon's packages are installed in, and
asks *that* one. Installing into a configured path and then verifying against
the same configured path would agree with itself no matter which environment the
daemon was really using, which is the shape of the original failure rather than
a check on it.

**The unit's start program is not that interpreter, and assuming it was one
started a second daemon.** On ReachyMiniOS v0.2.3 the unit starts a shell launcher out
of the daemon's own virtual environment; an earlier version of this module read
that path as an interpreter and ran it with `-c '<python source>'`, which
launched a second daemon that contended with the first for its network port, its
serial device and its camera. `reachyctl.interpreters` derives the candidates
instead — from what the operator supplied, from the environment the unit
declares, and from the environment the start program is installed in — and this
client confirms one of them before any Python source goes near it. Nothing
whose file name does not claim to be an interpreter is ever executed at all.
When nothing resolves, that is `InterpreterResolutionError` and not a fallback:
a path that might not be an interpreter is exactly what produced the second
daemon.

**There are two environments, and asking the wrong one is a false answer
rather than a failed one.** The daemon runs out of one and, on the released
image, installs applications into a sibling of it — so the daemon's own version
is read through `interpreter` and the application's through
`application_interpreter`, and a wheel is installed through the same one its
version is read back from. Asking the daemon's environment for the application
reports a robot that is running the satellite as not having it installed, which
is worse than an error: an operator acts on it by installing what is already
there.

**There are two application-control interfaces, and the robot has one of them.**
The released image serves its control over the daemon's own HTTP API; the
container target the provisioning gate runs against implements the control
module change 0009 recorded as provisional. Both are asked, API first, and
neither is a workaround for the other — a stock robot needs no flag, and a robot
serving neither fails naming both rather than reporting the application stopped.
Every request goes through an interpreter this client has already proven, so
neither route can reach the unit's start program.

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
from reachyctl.interpreters import (
    application_candidates,
    candidates,
    names_an_interpreter,
)
from reachyctl.managed import MalformedRegionError, parse_region
from reachyctl.robot import CommandOutcome, RobotAccessError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping, Sequence

    from reachyctl.interpreters import Candidate
    from reachyctl.robot import RemoteAccess, RobotLayout

__all__ = ["DaemonClient", "DaemonControlError", "InterpreterResolutionError"]

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

# One request to the daemon's own HTTP API, run through an interpreter this
# client has already confirmed. It is Python source, so it goes only to a proven
# interpreter — never to the unit's start program, which is the whole of
# REQ-106 — and it needs nothing installed on the robot beyond that interpreter:
# a `curl` this image happened not to ship would be one more thing to be wrong
# about a machine we do not own.
#
# The two failure statuses are the point of it. A daemon that ANSWERED with an
# error is a daemon whose control interface is there and refused, and falling
# back to another interface would replace the reason with a second, unrelated
# one. A daemon that could not be reached at all is an image that does not serve
# this interface, and that is what the fallback is for.
_API_SCRIPT: Final = (
    "import sys, urllib.error, urllib.request\n"
    "method, url = sys.argv[1], sys.argv[2]\n"
    "request = urllib.request.Request(url, method=method)\n"
    "try:\n"
    "    with urllib.request.urlopen(request, timeout=10) as answer:\n"
    "        sys.stdout.write(answer.read().decode('utf-8') or 'null')\n"
    "except urllib.error.HTTPError as error:\n"
    "    sys.stderr.write(f'{error.code} {error.reason}')\n"
    "    raise SystemExit(3) from None\n"
    "except OSError as error:\n"
    "    sys.stderr.write(str(error))\n"
    "    raise SystemExit(7) from None\n"
)

# What the daemon answered with an error status.
_API_REFUSED: Final = 3

# What the daemon's API could not be reached at all.
_API_UNREACHABLE: Final = 7

# The daemon's application-control endpoints, under `RobotLayout.daemon_api`.
_APPS: Final = "/api/apps"

# The one state in the daemon's application-state vocabulary that means the
# application is up. `starting`, `stopping`, `done` and `error` are the others,
# and none of them is running — a check that treated `starting` as running
# would pass over an application that never finishes starting.
_RUNNING: Final = "running"

# systemd renders a command as `{ path=/usr/bin/x ; argv[]=... ; ... }`, one
# such block per `ExecStart=` the unit declares. The first block's path is the
# program the daemon is STARTED by, which is the daemon's entry point and not
# necessarily an interpreter — see `reachyctl.interpreters`.
_EXEC_PATH: Final = re.compile(r"path=(\S+)")

# The flag a candidate is asked to identify itself with, and what an interpreter
# answers. A flag, deliberately, and never source: the point of the whole
# resolution is that nothing unproven is handed a program to run, and `-V` asks
# a question no interpreter can misread and no launcher is given the chance to.
#
# The answer has to be the WHOLE of what came back, on ONE stream, and a
# COMPLETE version. This step is the one that ESTABLISHES what everything after
# it assumes, so it has to be something a program cannot pass by accident, and
# each weaker form leaves a gap the next one has to close: the exit status alone
# admits anything that exits zero, the word alone admits a banner, a
# version-shaped PREFIX admits `Python 3.12.3 - wrapper usage: ...`, whichever
# stream happens to be non-empty admits a program that prints the version on one
# and announces itself on the other, a CONCATENATION of the two admits a version
# split across them, and an OPTIONAL minor and patch admit the eight characters
# `Python 3`, which any wrapper can print by accident.
#
# So the pattern is the shape `-V` actually produces and nothing else: major,
# minor and patch, and the release-level suffix CPython appends to a
# pre-release — `Python 3.12.3`, `Python 3.13.0rc1`. Anything an interpreter
# would not print is refused, and refusal is safe: the next candidate is tried,
# and a robot where none answers gets a named error rather than a program handed
# Python source on the strength of two characters.
_VERSION_FLAG: Final = "-V"
_VERSION_ANSWER: Final = re.compile(r"Python \d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?")

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


class InterpreterResolutionError(RobotAccessError):
    """No interpreter could be resolved for the daemon's environment.

    Its own type for the same reason `DaemonControlError` is: the robot
    answered, and what it said does not let this tool operate it. There is
    deliberately no fallback behind this error. Falling back to a configured
    path means installing into an environment the daemon may not be using, which
    is the failure reachyctl REQ-051 exists to catch; falling back to the unit's
    start program means running a launcher with Python arguments, which is the
    failure that started a second daemon. The message names every path that was
    tried and why, and it names `--python`, which is the answer an operator can
    always give.
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
        versions = await self.installed_versions(
            await self.interpreter(),
            self._layout.daemon_distribution,
        )
        return DaemonInfo(
            responding=True,
            version=versions.get(self._layout.daemon_distribution, ""),
        )

    async def installed_application(self) -> InstalledApplication:
        """Ask what version of the application the robot has, where it keeps it.

        **The daemon's environment and the application's are not the same
        question, and on the released image they are not the same directory.**
        ReachyMiniOS runs the daemon out of one virtual environment and installs
        applications into a sibling of it, so this asks
        `application_interpreter` rather than `interpreter`. Asking the daemon's
        own would report a robot that is running the application as not having
        it installed, which is a false negative and worse than the error it
        replaced: an operator acts on it by installing something that is
        already there.

        Returns:
            Whether it is installed and at what version, read through the
            interpreter that owns the environment the daemon puts applications
            in. A complaint names that environment, because "not installed" is
            only useful with "and here is where I looked".
        """
        python = await self.application_interpreter()
        versions = await self.installed_versions(python, self._layout.application)
        version = versions.get(self._layout.application, "")
        if not version:
            return InstalledApplication(
                installed=False,
                complaint=(
                    f"{self._layout.application} is not installed in the "
                    f"environment the daemon runs applications from, which on "
                    f"this robot is the one {python} owns"
                ),
            )
        return InstalledApplication(installed=True, version=version)

    async def application_state(self) -> ApplicationState:
        """Ask the daemon whether it is running the application.

        Asked of the daemon's own HTTP API first, because that is the interface
        the released image actually serves, and of the control module second —
        see `_reach_control` for why there are two and why neither is a
        workaround for the other.

        Returns:
            Whether it is, with whatever the daemon said about it. `running` is
            false only when the daemon said so, and the detail says which
            application is running when it is not this one.

        Raises:
            DaemonControlError: If neither interface could be reached, or one
                answered with something this tool cannot read. Returning "not
                running" for either would make `app stop` report an application
                it never asked about as already stopped, and exit zero.
            InterpreterResolutionError: If there is no interpreter to reach
                either through. Both need one, and a robot whose environment
                cannot be resolved has said nothing about its application
                either way.
        """
        answered = await self._api("GET", f"{_APPS}/current-app-status")
        if not _unreachable(answered):
            return self._read_status(answered)
        outcome = await self._control("status", "--json")
        if not outcome.ok:
            # Both interfaces named, because a robot serving neither is the one
            # an operator has the hardest time diagnosing: told only about the
            # module they would look for a module, and the reason the API was
            # not there is the other half of the answer.
            message = (
                f"neither of the daemon's application-control interfaces "
                f"answered. Its API at {self._layout.daemon_api} could not be "
                f"reached ({answered.complaint()}), and its control module "
                f"could not be run: {outcome.complaint()}"
            )
            raise DaemonControlError(message)
        report = self._decode(outcome)
        running = report.get("running")
        detail = report.get("detail")
        return ApplicationState(
            running=running is True,
            detail=detail if isinstance(detail, str) else "",
        )

    def _read_status(self, outcome: CommandOutcome) -> ApplicationState:
        """Read what the daemon's API said about the application it is running.

        The endpoint answers about the CURRENT application rather than about the
        one asked for, so a robot running something else is a robot on which
        this application is not running — and the detail says which, because an
        operator whose satellite was displaced by another application needs to
        be told that rather than left with "not running".

        Args:
            outcome: What the request did.

        Returns:
            Whether this application is running, and what the daemon said.

        Raises:
            DaemonControlError: If the API answered with an error status, or
                with something this tool cannot read.
        """
        if outcome.exit_status == _API_REFUSED:
            message = (
                f"the daemon's application API refused the request: "
                f"{outcome.complaint()}"
            )
            raise DaemonControlError(message)
        if not outcome.ok:
            message = (
                f"the daemon's application API could not be asked: "
                f"{outcome.complaint()}"
            )
            raise DaemonControlError(message)
        try:
            decoded = json.loads(outcome.stdout)
        except ValueError as error:
            message = (
                f"the daemon's application API answered "
                f"{_APPS}/current-app-status with something that is not JSON"
            )
            raise DaemonControlError(message) from error
        if decoded is None:
            return ApplicationState(
                running=False,
                detail="the daemon is running no application",
            )
        if not isinstance(decoded, dict):
            message = (
                f"the daemon's application API answered "
                f"{_APPS}/current-app-status with a {type(decoded).__name__} "
                f"rather than an object"
            )
            raise DaemonControlError(message)
        state = decoded.get("state")
        detail = state if isinstance(state, str) else "in a state it did not name"
        failure = decoded.get("error")
        if isinstance(failure, str) and failure:
            detail = f"{detail}: {failure}"
        info = decoded.get("info")
        current = info.get("name") if isinstance(info, dict) else None
        if current != self._layout.application:
            named = current if isinstance(current, str) and current else "something"
            return ApplicationState(
                running=False,
                detail=f"the daemon is running {named} instead, {detail}",
            )
        return ApplicationState(running=state == _RUNNING, detail=detail)

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
        return _environment(outcome.stdout)

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

    #:= docs/specs/stock-robot-installation/index.md#req-105-the-daemon-s-start-program-is-not-assumed-to-be-an-interpreter
    #:% `reachyctl` MUST NOT execute the program named by a robot's daemon service unit
    #:% as though it were a Python interpreter.
    #
    #:= docs/specs/stock-robot-installation/index.md#req-106-diagnosis-and-deployment-start-no-second-daemon
    #:% `reachyctl` MUST NOT start another instance of the robot's daemon, or take any
    #:% device, port or lock from the running one, as a side effect of diagnosing or
    #:% deploying.
    async def interpreter(self) -> str:
        """Resolve the interpreter that owns the daemon's package environment.

        One round trip reads the unit's start program and the environment it
        declares; `reachyctl.interpreters` turns those into candidates, and each
        is asked to identify itself before it is trusted. The unit's start
        program is a candidate only when its file name says it is an
        interpreter, so a shell launcher is never run — see the module
        documentation for the robot that made this necessary.

        Returns:
            The first candidate that answered as an interpreter.

        Raises:
            InterpreterResolutionError: If nothing did. Named and remediable:
                the message lists what was tried and why, says what was
                deliberately not run, and names `--python`.
            RobotAccessError: If the unit could not be read at all.
        """
        properties = await self._show(
            self._layout.daemon_unit,
            "ExecStart",
            "Environment",
        )
        found = _EXEC_PATH.search(properties.get("ExecStart", ""))
        exec_start = found.group(1) if found is not None else ""
        considered = candidates(
            configured=self._layout.python,
            exec_start=exec_start,
            environment=_environment(properties.get("Environment", "")),
        )
        for candidate in considered:
            if await self._identifies_as_an_interpreter(candidate.path):
                return candidate.path
        raise InterpreterResolutionError(
            _unresolved(
                self._layout.daemon_unit,
                exec_start,
                self._layout.python,
                considered,
            ),
        )

    async def application_interpreter(self) -> str:
        """Resolve the interpreter of the environment applications are in.

        A second question, and on the released image a second answer: the
        daemon runs out of one virtual environment and installs applications
        into a sibling of it. `reachyctl.interpreters.application_candidates`
        derives both possibilities from where the daemon's own interpreter
        turned out to be, and each is confirmed the same way.

        Returns:
            The first candidate that answered as an interpreter. The daemon's
            own is always the last one tried, so an image that keeps a single
            environment for both resolves to exactly what it did before.

        Raises:
            InterpreterResolutionError: If none answered — which means the
                daemon's own interpreter stopped answering between one question
                and the next, since it is always in the list.
            RobotAccessError: If the unit could not be read at all.
        """
        daemon = await self.interpreter()
        considered = application_candidates(daemon)
        for candidate in considered:
            if await self._identifies_as_an_interpreter(candidate.path):
                return candidate.path
        message = (
            f"could not resolve the Python interpreter of the environment "
            f"{self._layout.daemon_unit} runs applications from. Tried "
            + "; ".join(candidate.describe() for candidate in considered)
            + ". Name the interpreter with --python"
        )
        raise InterpreterResolutionError(message)

    async def _identifies_as_an_interpreter(self, path: str) -> bool:
        """Ask a candidate to say what it is.

        `-V` and not source: this is the step that *establishes* the thing
        everything downstream assumes, so it cannot itself assume it. The answer
        is read rather than the exit status, because a program can exit zero
        having done something else entirely, and only the version makes it an
        interpreter.

        Args:
            path: The candidate. `reachyctl.interpreters.candidates` has already
                established that its file name is one CPython gives an
                interpreter — every candidate, an operator's `--python`
                included — so this is never the first thing to look at the name.
                What that gate cannot see is a rename, which is why this step
                exists and why what it runs is a flag rather than source.

        Returns:
            True when it spoke on exactly one stream and everything it said
            there is a version — `Python 3.12.3`, which is exactly what `-V`
            makes CPython print. A path that is not there, is not executable,
            said one word more, or said something on both streams is not an
            interpreter, and the next candidate is tried.
        """
        outcome = await self._run([path, _VERSION_FLAG])
        if not outcome.ok:
            return False
        # ONE stream says the whole version and the other says nothing. Which
        # one is not fixed here — Python 3 writes to standard output and Python
        # 2 wrote to standard error, and a robot is not this tool's choice of
        # interpreter — but that there is exactly one is. Preferring whichever
        # stream is non-empty would admit a program that prints the version on
        # one and a launcher's banner on the other; concatenating them would
        # admit a version split across the two. Neither is what `-V` does.
        spoken = [
            stream
            for stream in (outcome.stdout.strip(), outcome.stderr.strip())
            if stream
        ]
        return len(spoken) == 1 and _VERSION_ANSWER.fullmatch(spoken[0]) is not None

    async def installed_versions(
        self,
        python: str,
        *distributions: str,
    ) -> dict[str, str]:
        """Ask one of the robot's environments what versions it holds.

        The interpreter is an argument rather than resolved here, because there
        are two environments and the caller is the one that knows which question
        it is asking: the daemon's own for the daemon's version, and the one
        applications are installed into for the application's.

        Args:
            python: The interpreter that owns the environment to ask, already
                confirmed to be one.
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
        """Install a wheel into the environment the daemon runs applications from.

        The same environment `installed_application` reads back, and that is
        not a detail: installing into one and verifying against another is the
        failure reachyctl REQ-051 exists to catch, arriving by the door the two
        environments open.

        Args:
            wheel: Where the wheel is on the robot.

        Returns:
            What the install did. A failed install is returned rather than
            raised, because the deploy step sequence reports it as the step
            that failed — and because the install exiting zero is exactly the
            thing this change refuses to treat as success.
        """
        python = await self.application_interpreter()
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
            What the daemon's control did, through whichever interface this
            robot serves.
        """
        answered = await self._api(
            "POST",
            f"{_APPS}/start-app/{self._layout.application}",
        )
        if not _unreachable(answered):
            return answered
        return await self._control("start")

    async def stop_application(self) -> CommandOutcome:
        """Ask the daemon to stop the application.

        The API endpoint stops whatever is running rather than a named
        application, which is what the daemon offers: it runs one at a time.

        Returns:
            What the daemon's control did, through whichever interface this
            robot serves.
        """
        answered = await self._api("POST", f"{_APPS}/stop-current-app")
        if not _unreachable(answered):
            return answered
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

    async def _api(self, method: str, path: str) -> CommandOutcome:
        """Ask the daemon's own HTTP API, if this robot serves one.

        **There are two application-control interfaces and neither is a
        workaround for the other.** This one is what the released image serves
        and what a stock robot has; `_control`'s module is what change 0009
        recorded as provisional, what the provisioning roles use, and what the
        container target the idempotency gate runs against implements. A robot
        has one or the other, so the client asks for both rather than making an
        operator tell it which — that is the same seam fixed once, not a
        per-command special case.

        Args:
            method: The HTTP method.
            path: The endpoint, below `RobotLayout.daemon_api`.

        Returns:
            What the request did. `_unreachable` reads it for whether this robot
            serves such an API at all, which is the caller's signal to try the
            other interface. An error STATUS is not that signal: a daemon that
            answered and refused has an API, and replacing its reason with a
            second interface's unrelated failure is how an operator ends up
            debugging the wrong thing.

        Raises:
            InterpreterResolutionError: If no interpreter could be resolved to
                make the request through.
        """
        python = await self.interpreter()
        return await self._run(
            [python, "-c", _API_SCRIPT, method, f"{self._layout.daemon_api}{path}"],
        )

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


def _unreachable(outcome: CommandOutcome) -> bool:
    """Say whether the daemon's API is absent rather than unhappy.

    Args:
        outcome: What the request did.

    Returns:
        True only when the request could not reach anything at all, which is
        the one answer that means "this robot serves the other interface". Every
        other failure is a failure of an API that exists.
    """
    return outcome.exit_status == _API_UNREACHABLE


def _environment(text: str) -> dict[str, str]:
    """Read the environment out of systemd's rendering of it.

    systemd prints the whole environment on one line, quoting an assignment that
    needs it. Splitting it the way a shell would is the closest available parse
    and not an exact one: systemd's own escaping is its own, and a value
    carrying something it escapes differently would come back subtly wrong. It
    is what there is — `systemctl show` offers no structured output — and the
    managed region itself is read from the file, where the format is this
    repository's own.

    Args:
        text: What systemd printed for the `Environment` property.

    Returns:
        The settings by name.
    """
    settings: dict[str, str] = {}
    for assignment in shlex.split(text.strip()):
        name, separator, value = assignment.partition("=")
        if separator:
            settings[name] = value
    return settings


def _unresolved(
    unit: str,
    exec_start: str,
    configured: str | None,
    considered: Sequence[Candidate],
) -> str:
    """Say why no interpreter could be resolved, and what to do about it.

    Args:
        unit: The daemon's unit, so the operator knows which robot and which
            service this is about.
        exec_start: The program that unit starts, or an empty string when it
            declares none.
        configured: What `--python` named, so an operator whose own answer was
            refused is told that rather than left looking for it in a list it
            is not in.
        considered: Every candidate that was tried, in the order they were.

    Returns:
        The message. It names the paths and their reasons rather than only
        counting them, says out loud what was deliberately *not* run, and ends
        with the answer an operator can always give.
    """
    tried = (
        "Tried " + "; ".join(candidate.describe() for candidate in considered) + "."
        if considered
        else "Nothing on this robot suggested one."
    )
    if not exec_start:
        withheld = f"The unit {unit} declares no start program to derive one from."
    elif names_an_interpreter(exec_start):
        withheld = f"The unit {unit} starts {exec_start}."
    else:
        withheld = (
            f"The unit {unit} starts {exec_start}, which was not run: a unit's "
            f"start program is the daemon's entry point, and only on some "
            f"images is that also an interpreter. Running it with Python "
            f"arguments starts a second daemon that competes with the first for "
            f"its port, its serial device and its camera."
        )
    if configured and not names_an_interpreter(configured):
        refused = (
            f" The path --python names, {configured}, was not run either: this "
            f"tool only ever executes a program whose file name is one CPython "
            f"gives an interpreter, which is what stops a launcher being run at "
            f"all. Point --python at the interpreter itself, or at a link to it "
            f"named python."
        )
    else:
        refused = ""
    return (
        f"could not resolve the Python interpreter of the environment {unit} "
        f"runs. {withheld}{refused} {tried} Name the interpreter with --python"
    )


def _substate(properties: Mapping[str, str]) -> str:
    """Add systemd's finer-grained state to a complaint, when there is one.

    Args:
        properties: What `systemctl show` reported.

    Returns:
        The sub-state in parentheses, or an empty string.
    """
    substate = properties.get("SubState", "")
    return f" ({substate})" if substate else ""
