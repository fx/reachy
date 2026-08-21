"""What Ansible actually loads: the filter names, and the edges behind them.

Two things live here. The first is the seam between the playbook and the Python:
Ansible reaches these modules by the names in each `FilterModule.filters()`, and
a rename that a role template does not follow is a run that fails at templating
rather than a test that fails here — so every `| reachy_…` a role or playbook
writes is checked against what the plugins register.

The second is the handful of paths a run against a healthy container never
reaches: a robot whose managed region something else wrote, a declaration YAML
typed as a number, a unit that declares no command. Those are the paths that
matter when something is wrong, which is the only time anybody reads the output.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

import reachy_app
import reachy_managed
import reachy_verify

ANSIBLE: Final = Path(__file__).resolve().parents[1] / "ansible"

# A filter invocation in a template: `| reachy_something`. The roles use no other
# prefix, and Ansible's own filters are not this test's business.
_FILTER_USE: Final = re.compile(r"\|\s*(reachy_[a-z_]+)")


def registered() -> dict[str, object]:
    """List every filter the three plugins expose.

    Returns:
        The filters by the name a template writes.
    """
    return {
        **reachy_managed.FilterModule().filters(),
        **reachy_app.FilterModule().filters(),
        **reachy_verify.FilterModule().filters(),
    }


@pytest.mark.filesystem  # reads the playbooks; the names in them are the contract
def test_every_filter_a_role_writes_is_one_the_plugins_register() -> None:
    """A rename Ansible only discovers at templating time is a run that failed on a robot."""
    exposed = set(registered())
    used: dict[str, str] = {}
    for source in sorted(ANSIBLE.rglob("*.yml")):
        for name in _FILTER_USE.findall(source.read_text(encoding="utf-8")):
            used[name] = str(source.relative_to(ANSIBLE))

    assert used, "the roles must reach their Python through filters"
    unknown = {name: where for name, where in used.items() if name not in exposed}
    assert not unknown, f"filters no plugin registers: {unknown}"


@pytest.mark.filesystem  # reads the playbooks; see above
def test_every_filter_the_plugins_register_is_one_something_writes() -> None:
    """An unused filter is a second way to do something, waiting to disagree with the first."""
    used = {
        name
        for source in ANSIBLE.rglob("*.yml")
        for name in _FILTER_USE.findall(source.read_text(encoding="utf-8"))
    }

    assert set(registered()) - used == set()


@pytest.mark.parametrize(
    "content",
    [
        # No markers at all: something else entirely wrote this file.
        '[Service]\nEnvironment="A_SETTING=1"\n',
        # A begin with no end.
        f"{reachy_managed.BEGIN_MARKER}\n",
        # Both, in the wrong order.
        f"{reachy_managed.END_MARKER}\n{reachy_managed.BEGIN_MARKER}\n",
        # Two begins.
        f"{reachy_managed.BEGIN_MARKER}\n{reachy_managed.BEGIN_MARKER}\n"
        f"{reachy_managed.END_MARKER}\n",
    ],
)
def test_a_region_whose_markers_are_wrong_is_unreadable_rather_than_empty(
    content: str,
) -> None:
    """Reading somebody else's file as ours loses their content on the next apply.

    Args:
        content: The file to refuse.
    """
    state = reachy_managed.region_state(present=True, content=content)

    assert state["state"] == reachy_managed.UNREADABLE
    assert "not readable" in state["complaint"]


def test_a_setting_assigned_twice_makes_the_region_unreadable() -> None:
    """Taking either one silently discards a value, and the next apply writes the survivor."""
    content = (
        f"{reachy_managed.HEADER}{reachy_managed.SECTION}\n"
        f"{reachy_managed.BEGIN_MARKER}\n"
        f'Environment="A_SETTING=1"\n'
        f'Environment="A_SETTING=2"\n'
        f"{reachy_managed.END_MARKER}\n"
    )

    state = reachy_managed.region_state(present=True, content=content)

    assert state["state"] == reachy_managed.UNREADABLE
    assert "assigned twice" in state["complaint"]
    # The line number, and neither of the two values.
    assert "1" not in state["complaint"].replace("line 9", "")


@pytest.mark.parametrize(
    ("declared", "written"),
    [
        ({"REACHY_SATELLITE_FRAME_INTERVAL_MS": 100}, "100"),
        ({"REACHY_SATELLITE_RESULT_STALENESS_SECONDS": 0.5}, "0.5"),
    ],
)
def test_a_number_typed_by_yaml_is_written_as_text(
    declared: dict[str, object],
    written: str,
) -> None:
    """A settings file is YAML, and a systemd environment holds text.

    Refusing a bare `100` would mean an operator quoting every number in their
    declaration and finding out which ones from a `TypeError`.

    Args:
        declared: The declaration as YAML made it.
        written: What should reach the robot.
    """
    accepted = reachy_managed.validated_settings(declared)

    assert accepted["ok"]
    assert next(iter(accepted["settings"].values())) == written


@pytest.mark.parametrize(("declared", "written"), [(True, "true"), (False, "false")])
def test_a_boolean_typed_by_yaml_is_written_the_way_the_robot_reads_one(
    declared: bool,
    written: str,
) -> None:
    """A YAML `true` is a boolean, and the vocabulary's spelling is the robot's.

    Args:
        declared: The declaration as YAML made it.
        written: What should reach the robot.
    """
    # No boolean setting is declared today, so this exercises the coercion
    # through a name the vocabulary refuses — which is the point: the value
    # reached the validator as text, and the refusal is about the name.
    accepted = reachy_managed.validated_settings({"REACHY_NOT_A_SETTING": declared})

    assert not accepted["ok"]
    assert written not in accepted["complaint"]


@pytest.mark.parametrize("value", [None, ["a", "list"], {"a": "mapping"}])
def test_a_value_with_no_text_form_is_refused_rather_than_guessed_at(
    value: object,
) -> None:
    """Guessing would write something nobody meant into a file nobody reads by hand.

    Args:
        value: What the declaration held.
    """
    refused = reachy_managed.validated_settings({"REACHY_SATELLITE_LOG_LEVEL": value})

    assert not refused["ok"]
    assert "no text form" in refused["complaint"]
    assert "REACHY_SATELLITE_LOG_LEVEL" in refused["complaint"]


def test_the_interpreter_comes_from_the_unit_rather_than_the_configured_path() -> None:
    """Verifying against the path you installed into agrees with itself either way."""
    exec_start = (
        "{ path=/opt/reachy/venv/bin/python ; argv[]=/opt/reachy/venv/bin/python "
        "-m reachy_mini.daemon ; ignore_errors=no }"
    )

    assert reachy_app.interpreter(exec_start, "/configured/python") == (
        "/opt/reachy/venv/bin/python"
    )


def test_a_unit_that_declares_no_command_falls_back_to_the_configured_path() -> None:
    """A unit that is not installed reports an empty property, which is a real answer."""
    assert reachy_app.interpreter("", "/configured/python") == "/configured/python"


def test_no_application_declared_withholds_the_two_checks_with_no_subject() -> None:
    """Running them would report a robot broken for not carrying what nobody asked for."""
    run = reachy_verify.check_run(
        {
            "unit": "reachy-mini-daemon.service",
            "application": "",
            "daemon_distribution": "reachy-mini",
            "properties": "LoadState=loaded\nActiveState=active\nEnvironment=\n",
            "versions": '{"reachy-mini": "1.9.0"}',
            "status": "",
            "status_complaint": "",
        },
        None,
    )

    assert run["ok"]
    assert [row["check"] for row in run["not_asked"]] == [
        "application.installed",
        "application.running",
    ]
    assert reachy_verify.NO_APPLICATION_DECLARED in run["not_asked"][0]["reason"]
    performed = {row["check"] for row in run["results"]}
    assert "application.installed" not in performed
    assert "daemon.reachable" in performed


def test_a_region_whose_header_is_wrong_fails_the_closed_form_check() -> None:
    """Enumerating the ways a file can be wrong is a list that is never finished.

    Every line here is one this format writes; what is not is the header above
    them. Nothing above the round trip notices, which is exactly why the round
    trip is there — "this renderer could have produced this file" is the
    property, and the renderer is the only thing that knows it.
    """
    content = (
        "# somebody else's header\n"
        f"{reachy_managed.SECTION}\n"
        f"{reachy_managed.BEGIN_MARKER}\n"
        'Environment="A_SETTING=1"\n'
        f"{reachy_managed.END_MARKER}\n"
    )

    state = reachy_managed.region_state(present=True, content=content)

    assert state["state"] == reachy_managed.UNREADABLE
    assert "byte for byte" in state["complaint"]


def test_an_environment_entry_that_is_not_an_assignment_is_ignored() -> None:
    """The whole environment is one line, and shell splitting is the closest parse."""
    assert reachy_verify.split_environment('bare "A=1"') == {"A": "1"}


@pytest.mark.parametrize("status", ["not json", '"a string"', "[1, 2]"])
def test_a_control_answering_with_something_that_is_not_an_object_is_no_answer(
    status: str,
) -> None:
    """A claim that the application is stopped would be an answer it never gave.

    Args:
        status: What the daemon's control printed.
    """
    run = reachy_verify.check_run(
        {
            "unit": "reachy-mini-daemon.service",
            "application": "an-application",
            "daemon_distribution": "reachy-mini",
            "properties": "LoadState=loaded\nActiveState=active\nEnvironment=\n",
            "versions": '{"reachy-mini": "1.9.0", "an-application": "1.0"}',
            "status": status,
            "status_complaint": "",
        },
        None,
    )

    detail = next(
        str(row["detail"])
        for row in run["results"]
        if row["check"] == "application.running"
    )
    assert "not a JSON object" in detail
    assert status not in detail


@pytest.mark.parametrize("payload", ["", "   ", "not json", '["a", "list"]'])
def test_a_version_answer_that_cannot_be_read_says_nothing_is_installed(
    payload: str,
) -> None:
    """Which the installed-application check reports, with a remediation and a command.

    Args:
        payload: What the interpreter printed.
    """
    robot = reachy_verify.daemon_from(
        {
            "unit": "reachy-mini-daemon.service",
            "application": "an-application",
            "daemon_distribution": "reachy-mini",
            "properties": "LoadState=loaded\nActiveState=active\n",
            "versions": payload,
            "status": "",
        },
    )

    assert not robot.application.installed


@pytest.mark.parametrize(
    ("declared", "in_a_wheel_name"),
    [
        ("example-tool", "example_tool"),
        ("example.tool", "example_tool"),
        ("Example_Tool", "example__TOOL"),
        ("reachy-mini-ha-satellite", "reachy_mini_ha_satellite"),
    ],
)
def test_a_declaration_and_a_wheel_name_compare_equal_after_normalising(
    declared: str,
    in_a_wheel_name: str,
) -> None:
    """A wheel's file name carries the escaped form, so neither spells the other.

    `example.tool` and `example-tool` both arrive as `example_tool`. Comparing
    either against the declaration verbatim would refuse a wheel for the
    distribution the role was told to install.

    Args:
        declared: The name as a declaration spells it.
        in_a_wheel_name: The name as a wheel's file name spells it.
    """
    release = reachy_app.wheel_release(f"{in_a_wheel_name}-1.0-py3-none-any.whl")

    assert release["ok"]
    assert release["distribution"] == reachy_app.distribution_name(declared)


def test_a_local_version_is_read_from_the_name_exactly_as_it_is_spelled() -> None:
    """A local version appears verbatim, because a wheel name carries a real version.

    `packaging` refuses a wheel whose version segment is not a valid PEP 440
    version — `example_tool-1.0_local-…` is rejected outright, and pip refuses to
    install it — so no tool produces the escaped spelling and this does not have
    to decode one. What it reports is still not the authority: the role reads
    `.dist-info/METADATA` out of the staged wheel for every decision it makes,
    because a file name is a claim and the metadata is what pip records.
    """
    release = reachy_app.wheel_release("example_tool-1.0+local-py3-none-any.whl")

    assert release["ok"]
    assert release["distribution"] == "example-tool"
    assert release["version"] == "1.0+local"
