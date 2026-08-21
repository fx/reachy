"""`provision`: the provisioning run, wrapped rather than reimplemented.

The reachyctl spec's "Division with provisioning" is explicit about what this
command is for: provisioning owns durable machine state, `reachyctl` operates a
robot already in that state, and the tool **wraps** the provisioning run so there
is one description of what a robot is. A second implementation of "apply the
declared configuration" living in this tool would be a second description, free
to drift from the one in version control — and the shared check registry, which
both the verification role and `doctor` run, would then be agreeing about the end
state of two different things.

**So this is deliberately thin, and it is the one command that shells out.**
Everything else here reaches the robot in process, because a failure should
arrive as a structured error rather than as text to be parsed out of a
subprocess. That reasoning does not apply to `ansible-playbook`: its output is
the report, it is what an operator would run by hand, and reproducing its
progress rendering inside this tool would make the wrapped run harder to read
than the unwrapped one. What this adds is the four things a wrapper is worth —
finding the playbook, spelling `--check` the way every other mutating command in
this tool spells preview, naming the removal path so it is discoverable, and
turning Ansible's exit status into this tool's.

**Nothing here takes a credential, and `--extra-vars` is a path or nothing.**
Ansible accepts `NAME=VALUE` and a JSON document there as well; this command
refuses both. An argument carrying a value is visible in the process list to
every user on the machine and lands in the shell history, and a declaration is
exactly where the groundstation credential lives — so the credential arrives the
way `reachyctl.credentials` insists every other one does, inside a file, which
here means one `ansible-vault encrypt` has encrypted.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from reachyctl.errors import ConfigurationError
from reachyctl.exits import ExitCode
from reachyctl.output import Report

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from reachyctl.output import Reporter

__all__ = [
    "DEFAULT_DIRECTORY",
    "DIRECTORY_VARIABLE",
    "PLAYBOOK",
    "REMOVAL_PLAYBOOK",
    "ProvisionPlan",
    "ansible_command",
    "checked_extra_vars",
    "checked_scope",
    "execute",
    "exit_code_for",
    "resolve_directory",
]

# Where the playbook is, relative to a checkout. It is not shipped inside the
# wheel: the declaration is version-controlled content belonging to whoever runs
# the robot, and a copy baked into a released artifact would be a second one.
DEFAULT_DIRECTORY: Final = Path("provisioning/ansible")

DIRECTORY_VARIABLE: Final = "REACHYCTL_PROVISIONING_DIR"

PLAYBOOK: Final = "site.yml"
REMOVAL_PLAYBOOK: Final = "remove.yml"

_EXECUTABLE: Final = "ansible-playbook"

# The one form `--extra-vars` may take here. Ansible also accepts `NAME=VALUE`
# and a JSON document, and both put whatever they carry into the process list —
# visible to every user on the machine — and into the shell history. A
# declaration is where the groundstation credential lives, so this tool takes a
# path and nothing else, exactly as `--credential-file` does and for exactly the
# same reason. An operator who wants an inline variable runs `ansible-playbook`
# themselves, which is a decision they make rather than one this tool makes for
# them.
_FILE_PREFIX: Final = "@"

# Ansible's own status for "a host was unreachable and nothing failed", and the
# one worth translating: a script that read it as "the robot is unhealthy" would
# page somebody about a network. Everything else — a failed host, an error, a
# parser problem, a bad option — is `FAILURE`, because the run happened and its
# answer was negative.
#
# The number is `TaskQueueManager.RUN_UNREACHABLE_HOSTS`, and those statuses are
# a **bit field**: `RUN_ERROR` is 1, `RUN_FAILED_HOSTS` is 2, this is 4 and
# `RUN_FAILED_BREAK_PLAY` is 8, so a run with both a failed host and an
# unreachable one exits 6. That is why this is an equality rather than a mask —
# a run where something failed produced a diagnosis, whatever else it also
# found, and only a run that reached nothing at all learned nothing.
_ANSIBLE_UNREACHABLE: Final = 4


@dataclass(frozen=True, slots=True, kw_only=True)
class ProvisionPlan:
    """What one `provision` run was asked to do.

    Attributes:
        directory: Where the playbook is.
        preview: Whether to report the changes and make none of them.
        remove: Whether to run the removal path rather than the apply.
        tags: The concerns to apply, or empty for all of them.
        limit: Which hosts to run against, or empty for the inventory's.
        inventory: An inventory to use, or `None` for the playbook's own.
        extra_vars: Values passed straight to Ansible. This is how a credential
            arrives — as `@path/to/vault.yml`, a path rather than a value.
        verbose: Whether to ask Ansible for its own verbose output.
    """

    directory: Path
    preview: bool = False
    remove: bool = False
    tags: tuple[str, ...] = ()
    limit: str = ""
    inventory: Path | None = None
    extra_vars: tuple[str, ...] = ()
    verbose: bool = False

    @property
    def playbook(self) -> str:
        """Say which playbook this run is of.

        Returns:
            The removal playbook when the removal path was asked for, and the
            apply otherwise.
        """
        return REMOVAL_PLAYBOOK if self.remove else PLAYBOOK


def resolve_directory(
    given: Path | None,
    environment: str = "",
    exists: Callable[[Path], bool] = Path.is_dir,
) -> Path:
    """Decide where the playbook is, and refuse to guess.

    Args:
        given: What `--directory` said, or `None`.
        environment: What the environment variable held, or an empty string.
        exists: How to check a directory. Injected so this is exercisable
            without a filesystem — no unit test in this repository performs
            input or output.

    Returns:
        The directory.

    Raises:
        ConfigurationError: If the directory named is not there. Nothing was
            contacted, so this is not a diagnosis of any robot; the message says
            which of the three sources named the path, because "not found" is
            unhelpful when the path came from an environment variable somebody
            set weeks ago.
    """
    if given is not None:
        return _existing(given, f"--directory {given}", exists)
    if environment:
        return _existing(
            Path(environment),
            f"{DIRECTORY_VARIABLE}={environment}",
            exists,
        )
    return _existing(
        DEFAULT_DIRECTORY,
        f"the default {DEFAULT_DIRECTORY}, relative to this directory",
        exists,
    )


def _existing(
    directory: Path,
    source: str,
    exists: Callable[[Path], bool],
) -> Path:
    """Insist a directory is there before a run is built around it.

    Args:
        directory: The path to check.
        source: How this path was arrived at, for the message.
        exists: How to check.

    Returns:
        The same path.

    Raises:
        ConfigurationError: If it is not a directory.
    """
    if not exists(directory):
        message = (
            f"there is no provisioning directory at {directory} ({source}). The "
            f"playbook is version-controlled content rather than something this "
            f"wheel ships, so run this from a checkout, pass --directory, or set "
            f"{DIRECTORY_VARIABLE}"
        )
        raise ConfigurationError(message)
    return directory


def checked_scope(*, remove: bool, tags: Sequence[str]) -> None:
    """Refuse a removal narrowed to one concern.

    `site.yml` carries per-concern tags because applying one concern without the
    others is what makes the playbook usable during development. `remove.yml`
    carries none, and deliberately: REQ-064 asks for a path that removes
    everything provisioning applied, and half a removal leaves a robot in a state
    nothing describes. Passing both would therefore select no task at all —
    Ansible would run, report nothing, and exit zero, which is the shape of a
    successful removal that removed nothing.

    Args:
        remove: Whether the removal path was asked for.
        tags: The concerns asked for.

    Raises:
        ConfigurationError: If both were given.
    """
    if remove and tags:
        message = (
            f"--remove takes no --tags. The removal path has no per-concern "
            f"tags, because removing one concern and leaving the others leaves "
            f"a robot in a state nothing describes; asking for "
            f"{', '.join(tags)} would select no task at all and the run would "
            f"report success having removed nothing"
        )
        raise ConfigurationError(message)


def checked_extra_vars(values: Sequence[str]) -> tuple[str, ...]:
    """Insist every `--extra-vars` is a path rather than a value.

    Args:
        values: What the operator wrote.

    Returns:
        The same values.

    Raises:
        ConfigurationError: If any of them is not a path. The offending value is
            **not** quoted back: the whole reason for refusing it is that it may
            be a credential, and a refusal that printed it would be the leak it
            exists to prevent. The position is enough to find it — the same
            choice `reachyctl.cli._assignments` makes for the same reason.
    """
    inline = [
        position
        for position, value in enumerate(values, start=1)
        # `@` on its own is the prefix and no path. Refused here rather than
        # forwarded, so the operator gets this command's message instead of
        # Ansible's, several seconds later.
        if not value.startswith(_FILE_PREFIX) or len(value) == len(_FILE_PREFIX)
    ]
    if inline:
        positions = ", ".join(str(position) for position in inline)
        message = (
            f"--extra-vars at position {positions} is not a path. This command "
            f"takes @path/to/vars.yml and nothing else, because a NAME=VALUE "
            f"argument is visible in the process list to every user on this "
            f"machine and lands in the shell history — and a declaration is "
            f"where the groundstation credential lives. Put the values in a "
            f"file, `ansible-vault encrypt` it if it holds a secret, and pass "
            f"@that. It is not quoted back here, for the same reason"
        )
        raise ConfigurationError(message)
    return tuple(values)


def ansible_command(plan: ProvisionPlan) -> list[str]:
    """Build the command this run is.

    Args:
        plan: What the run was asked to do.

    Returns:
        The arguments, in a stable order so that a run reported in the output is
        one an operator can paste. `--check` is how Ansible spells preview, and
        `--diff` goes with it: the point of a preview is seeing what would
        change, and the roles that carry a value censor their own diff.
    """
    command = [_EXECUTABLE, plan.playbook]
    if plan.preview:
        command += ["--check", "--diff"]
    if plan.inventory is not None:
        command += ["--inventory", str(plan.inventory)]
    if plan.tags:
        command += ["--tags", ",".join(plan.tags)]
    if plan.limit:
        command += ["--limit", plan.limit]
    for value in plan.extra_vars:
        command += ["--extra-vars", value]
    if plan.verbose:
        command.append("--verbose")
    return command


def exit_code_for(status: int) -> ExitCode:
    """Turn Ansible's exit status into this tool's.

    Args:
        status: What `ansible-playbook` exited with.

    Returns:
        `OK`, `UNREACHABLE` when every host was unreachable, and `FAILURE`
        otherwise. See `reachyctl.exits` on why the distinction matters to a
        script.
    """
    if status == 0:
        return ExitCode.OK
    if status == _ANSIBLE_UNREACHABLE:
        return ExitCode.UNREACHABLE
    return ExitCode.FAILURE


#:= docs/specs/provisioning/index.md#req-065-changes-are-previewable
#:% Provisioning MUST support reporting the changes a run would make without making
#:% any of them.
def execute(
    plan: ProvisionPlan,
    reporter: Reporter,
    run: Callable[[Sequence[str], Path], int] = lambda command, directory: (
        # Ansible's own output goes to standard ERROR, which is where every
        # other command here puts progress. Standard output carries the result
        # and nothing else, so `reachyctl provision --output json | jq` parses —
        # reachyctl REQ-058 — where inheriting Ansible's stdout would interleave
        # a play recap with the document. It is not captured and replayed
        # afterwards: a run against a robot on a slow link is minutes of
        # waiting, and a progress report that arrives all at once at the end is
        # not one.
        subprocess.run(  # noqa: S603 - argv is this module's own literals plus checked paths; no shell, and wrapping the playbook is the command
            command,
            cwd=directory,
            stdout=sys.stderr,
            check=False,
        ).returncode
    ),
    which: Callable[[str], str | None] = shutil.which,
) -> ExitCode:
    """Run the playbook and report what it did.

    Args:
        plan: What the run was asked to do.
        reporter: Where the result is written. Ansible's own progress goes
            straight to standard error, unwrapped and unscrubbed, because it is
            the report — and because nothing here holds a value to scrub it
            with: no option takes a credential, and one reaching the playbook
            does so inside a file Ansible itself does not echo.
        run: How to run it. Injected so the command this builds is exercisable
            without running Ansible — no unit test here performs input or
            output.
        which: How to find the executable. Injected for the same reason.

    Returns:
        The exit status.

    Raises:
        ConfigurationError: If `ansible-playbook` is not installed. Nothing was
            asked of any robot, so this is not a diagnosis of one and a script
            must not read it as a `FAILURE`.
    """
    if which(_EXECUTABLE) is None:
        message = (
            f"{_EXECUTABLE} is not on PATH. This command wraps the provisioning "
            f"run rather than reimplementing it, so it needs an Ansible control "
            f"machine; `just provision-lint` and `just provision-idempotency` "
            f"use the pinned one from this workspace's `provisioning` dependency "
            f"group"
        )
        raise ConfigurationError(message)
    command = ansible_command(plan)
    reporter.detail(f"running {' '.join(command)} in {plan.directory}")
    status = run(command, plan.directory)
    code = exit_code_for(status)
    # The report is emitted and the status is this function's own. `emit`
    # answers `OK` or `FAILURE` from `report.ok`, and `UNREACHABLE` — Ansible's
    # "every host was unreachable" — would otherwise be flattened into a generic
    # failure. A script reading that would page somebody about a robot when what
    # it learned was about a network, which is the distinction
    # `reachyctl.exits` exists to keep.
    reporter.emit(
        Report(
            command="provision",
            ok=code is ExitCode.OK,
            summary=_summary(plan, status),
            data={
                "playbook": plan.playbook,
                "directory": str(plan.directory),
                "preview": plan.preview,
                "remove": plan.remove,
                "tags": plan.tags,
                "ansible_status": status,
            },
        ),
    )
    return code


def _summary(plan: ProvisionPlan, status: int) -> str:
    """Say in one line what the run did.

    Args:
        plan: What the run was asked to do.
        status: What Ansible exited with.

    Returns:
        The line. Ansible's own recap is above it in the terminal and says which
        task failed; this says what was run and whether it worked.
    """
    what = "the removal path" if plan.remove else "the provisioning run"
    scope = f" ({', '.join(plan.tags)})" if plan.tags else ""
    if status == 0:
        return (
            f"{what}{scope} was a preview: the robot was not changed, and the "
            f"recap above says what would have been"
            if plan.preview
            else f"{what}{scope} completed and the robot verified its end state"
        )
    return f"{what}{scope} exited {status}; see the play recap above"
