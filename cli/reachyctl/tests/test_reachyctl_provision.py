"""`provision`: what the wrapper builds, and what it makes of what came back.

The command runs `ansible-playbook`, so what is exercised here is everything
around that: the command it assembles, where it looks for the playbook, and the
translation from Ansible's exit status to this tool's. The run itself is
injected, because no unit test in this repository performs input or output — and
because the thing being wrapped has its own gate, `just provision-idempotency`,
which applies the playbook twice against a container and fails on any change.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from reachyctl_support import reporter_for

from reachyctl.errors import ConfigurationError
from reachyctl.exits import ExitCode
from reachyctl.provision import (
    DEFAULT_DIRECTORY,
    DIRECTORY_VARIABLE,
    PLAYBOOK,
    REMOVAL_PLAYBOOK,
    ProvisionPlan,
    ansible_command,
    checked_extra_vars,
    checked_scope,
    execute,
    exit_code_for,
    resolve_directory,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

DIRECTORY: Final = Path("provisioning/ansible")


def plan(**overrides: object) -> ProvisionPlan:
    """Build a plan, with a plain apply as the default.

    Args:
        overrides: Whatever the case is about.

    Returns:
        The plan.
    """
    fields: dict[str, object] = {"directory": DIRECTORY}
    fields.update(overrides)
    return ProvisionPlan(**fields)  # type: ignore[arg-type]  # keyword-only fields, built per case


def test_an_apply_is_the_playbook_and_nothing_else() -> None:
    """A wrapper that added flags nobody asked for would be a wrapper nobody trusts."""
    assert ansible_command(plan()) == ["ansible-playbook", PLAYBOOK]


def test_preview_is_ansibles_own_check_mode() -> None:
    """REQ-065 is a mode the tool already has, not one this command implements."""
    assert ansible_command(plan(preview=True)) == [
        "ansible-playbook",
        PLAYBOOK,
        "--check",
        "--diff",
    ]


def test_the_removal_path_is_the_other_playbook() -> None:
    """REQ-064's supported path, named so it is discoverable from the tool."""
    assert ansible_command(plan(remove=True))[1] == REMOVAL_PLAYBOOK


def test_tags_are_joined_the_way_ansible_reads_them() -> None:
    """One option, repeatable, because that is how every other list option here reads."""
    command = ansible_command(plan(tags=("daemon_env", "verify")))

    assert command[-2:] == ["--tags", "daemon_env,verify"]


def test_an_inventory_a_limit_and_extra_vars_are_passed_through() -> None:
    """Including the one that carries a credential, which arrives as a path."""
    command = ansible_command(
        plan(
            inventory=Path("inventory.ini"),
            limit="one-robot",
            extra_vars=("@vault.yml", "@application.yml"),
        ),
    )

    assert "--inventory" in command
    assert command[command.index("--inventory") + 1] == "inventory.ini"
    assert command[command.index("--limit") + 1] == "one-robot"
    assert command.count("--extra-vars") == 2
    assert "@vault.yml" in command
    assert "@application.yml" in command


def test_a_verbose_run_asks_ansible_for_its_own_detail() -> None:
    """Ansible's output is the report, so `--verbose` belongs to Ansible."""
    assert "--verbose" in ansible_command(plan(verbose=True))


def test_the_directory_given_on_the_command_line_wins() -> None:
    """Three sources, and the message says which one named a path that is not there."""
    assert resolve_directory(Path("elsewhere"), "", lambda _: True) == Path("elsewhere")


def test_the_environment_is_consulted_when_nothing_was_given() -> None:
    """So an operator working in one checkout sets it once."""
    assert resolve_directory(None, "from/env", lambda _: True) == Path("from/env")


def test_the_default_is_where_a_checkout_keeps_the_playbook() -> None:
    """The wheel does not ship it: the declaration is version-controlled content."""
    assert resolve_directory(None, "", lambda _: True) == DEFAULT_DIRECTORY


def test_a_directory_that_is_not_there_is_refused_and_says_which_source_named_it() -> (
    None
):
    """A bare "not found" is unhelpful for a path a variable set weeks ago named."""
    with pytest.raises(ConfigurationError, match=DIRECTORY_VARIABLE):
        resolve_directory(None, "from/env", lambda _: False)


def test_a_missing_default_directory_names_the_three_ways_out() -> None:
    """An operator running this outside a checkout has three things they can do."""
    with pytest.raises(ConfigurationError, match="--directory"):
        resolve_directory(None, "", lambda _: False)


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (0, ExitCode.OK),
        (1, ExitCode.FAILURE),
        (2, ExitCode.FAILURE),
        (3, ExitCode.UNREACHABLE),
        (4, ExitCode.FAILURE),
        (99, ExitCode.FAILURE),
    ],
)
def test_ansibles_status_becomes_one_a_script_here_already_understands(
    status: int,
    code: ExitCode,
) -> None:
    """Unreachable is the one worth translating: it is not a diagnosis of the robot.

    Args:
        status: What `ansible-playbook` exited with.
        code: What this tool should exit with.
    """
    assert exit_code_for(status) == code


def test_a_run_that_worked_reports_what_it_ran() -> None:
    """The recap is Ansible's; the line this adds says which playbook and where."""
    reporter, streams = reporter_for()
    ran: list[Sequence[str]] = []

    def record(command: Sequence[str], directory: Path) -> int:
        """Remember what was run rather than running it.

        Args:
            command: The arguments the wrapper built.
            directory: Where it would have run them.

        Returns:
            The status a successful run exits with.
        """
        del directory
        ran.append(command)
        return 0

    code = execute(
        plan(tags=("daemon_env",)),
        reporter,
        run=record,
        which=lambda _: "/usr/bin/ansible-playbook",
    )

    assert code is ExitCode.OK
    assert ran == [["ansible-playbook", PLAYBOOK, "--tags", "daemon_env"]]
    assert "daemon_env" in streams.result


def test_a_preview_makes_no_change_and_says_so() -> None:
    """REQ-065 from the wrapper's side, asserted as an after-state.

    Deliberately not "the summary said preview": a command that printed a
    perfect plan and then applied it anyway would pass that. What is asserted is
    that the run the wrapper actually launched could not have changed anything —
    it carried `--check`, and the stand-in robot below records whether it was
    ever asked to converge.
    """
    reporter, streams = reporter_for()
    robot = {"converged": False}

    def record(command: Sequence[str], directory: Path) -> int:
        """Converge the stand-in robot unless the run was a preview.

        Args:
            command: The arguments the wrapper built.
            directory: Where it would have run them.

        Returns:
            The status a successful run exits with.
        """
        del directory
        if "--check" not in command:
            robot["converged"] = True
        return 0

    execute(
        plan(preview=True),
        reporter,
        run=record,
        which=lambda _: "/usr/bin/ansible-playbook",
    )

    assert robot["converged"] is False
    assert "was not changed" in streams.result


def test_a_run_that_is_not_a_preview_does_converge_the_robot() -> None:
    """The other half of the pair: the preview assertion above must be able to fail."""
    robot = {"converged": False}

    def record(command: Sequence[str], directory: Path) -> int:
        """Converge the stand-in robot unless the run was a preview.

        Args:
            command: The arguments the wrapper built.
            directory: Where it would have run them.

        Returns:
            The status a successful run exits with.
        """
        del directory
        if "--check" not in command:
            robot["converged"] = True
        return 0

    execute(
        plan(),
        reporter_for()[0],
        run=record,
        which=lambda _: "/usr/bin/ansible-playbook",
    )

    assert robot["converged"] is True


def test_a_run_against_a_robot_that_is_not_there_exits_unreachable() -> None:
    """Not FAILURE: nothing was learned about the robot, so it is not a diagnosis.

    `Reporter.emit` answers OK or FAILURE from the report alone, so a command
    that returned what it emitted would flatten this into a generic failure and
    a monitor would page somebody about a robot when what it learned was about a
    network.
    """
    reporter, streams = reporter_for()

    code = execute(
        plan(),
        reporter,
        run=lambda _command, _directory: 3,
        which=lambda _: "/usr/bin/ansible-playbook",
    )

    assert code is ExitCode.UNREACHABLE
    assert "exited 3" in streams.result


@pytest.mark.parametrize(
    "value",
    [
        "reachy_groundstation_credential=example-secret",
        "reachy_app_distribution=example",
        '{"reachy_groundstation_credential": "example-secret"}',
        "",
    ],
)
def test_extra_vars_takes_a_path_and_refuses_a_value(value: str) -> None:
    """An argument carrying a value is in the process list and the shell history.

    Args:
        value: What the operator wrote.
    """
    with pytest.raises(ConfigurationError, match="not a path") as refusal:
        checked_extra_vars([value])

    # The refusal names the position and never the value, because the reason for
    # refusing it is that it may be a credential.
    assert value not in str(refusal.value) or not value


def test_extra_vars_accepts_a_variables_file() -> None:
    """Which is how a credential reaches the playbook: inside a file, vault-encrypted."""
    assert checked_extra_vars(["@vault.yml", "@more.yml"]) == (
        "@vault.yml",
        "@more.yml",
    )


def test_every_offending_extra_var_is_reported_at_once() -> None:
    """One refusal per invocation is one invocation per mistake."""
    with pytest.raises(ConfigurationError, match="position 1, 3"):
        checked_extra_vars(["a=1", "@vault.yml", "b=2"])


def test_a_failed_run_exits_non_zero_and_points_at_the_recap() -> None:
    """Ansible already said which task failed; repeating it here would be a second copy."""
    reporter, streams = reporter_for()

    code = execute(
        plan(),
        reporter,
        run=lambda _command, _directory: 2,
        which=lambda _: "/usr/bin/ansible-playbook",
    )

    assert code is ExitCode.FAILURE
    assert "exited 2" in streams.result


def test_the_run_happens_in_the_directory_the_playbook_is_in() -> None:
    """`ansible.cfg` is read from the working directory, so this is not incidental."""
    seen: list[Path] = []

    def record(command: Sequence[str], directory: Path) -> int:
        """Remember where the run would have happened.

        Args:
            command: The arguments the wrapper built.
            directory: Where it would have run them.

        Returns:
            The status a successful run exits with.
        """
        del command
        seen.append(directory)
        return 0

    execute(
        plan(),
        reporter_for()[0],
        run=record,
        which=lambda _: "/usr/bin/ansible-playbook",
    )

    assert seen == [DIRECTORY]


def test_no_ansible_on_the_path_is_a_configuration_failure() -> None:
    """Nothing was asked of any robot, so a monitor must not read it as unhealthy."""
    with pytest.raises(ConfigurationError, match="ansible-playbook"):
        execute(
            plan(),
            reporter_for()[0],
            run=lambda _command, _directory: 0,
            which=lambda _: None,
        )


def test_the_removal_path_refuses_to_be_narrowed_to_one_concern() -> None:
    """`remove.yml` has no tags, so both together would select no task and exit zero.

    A run that reported success having removed nothing is worse than a refusal,
    which is why this is checked before Ansible is reached rather than left to
    produce an empty recap.
    """
    with pytest.raises(ConfigurationError, match="takes no --tags"):
        checked_scope(remove=True, tags=("daemon_env",))


def test_tags_and_a_removal_are_each_fine_on_their_own() -> None:
    """The refusal is about the pair; neither is a problem by itself."""
    checked_scope(remove=True, tags=())
    checked_scope(remove=False, tags=("daemon_env",))
