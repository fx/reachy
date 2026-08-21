"""Reading a robot's address, and what a remote command's outcome says.

The address parsing has one case that is easy to get wrong and expensive when it
is: an IPv6 literal carries colons of its own, so a parser that splits on the
last colon turns an address into a different address and a port. That case has
its own tests here, both bracketed and bare.

Every address below is in an RFC 5737 or RFC 3849 reserved range and every
account is a placeholder — see the root `AGENTS.md`.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reachyctl.errors import ConfigurationError
from reachyctl.exits import ExitCode
from reachyctl.robot import (
    DEFAULT_SSH_PORT,
    CommandOutcome,
    RobotAccessError,
    RobotLayout,
    parse_robot,
    render,
)


def test_an_address_with_no_port_uses_the_default() -> None:
    """The ordinary case, so the ones below are variations of something."""
    target = parse_robot("operator@192.0.2.10")

    assert target.user == "operator"
    assert target.host == "192.0.2.10"
    assert target.port == DEFAULT_SSH_PORT
    assert target.describe() == "operator@192.0.2.10:22"


def test_an_address_with_a_port_reads_both() -> None:
    """A robot behind a forwarded port is the reason this option exists."""
    target = parse_robot("operator@192.0.2.10:2222")

    assert target.host == "192.0.2.10"
    assert target.port == 2222


def test_a_bare_ipv6_address_is_not_split_into_a_host_and_a_port() -> None:
    """More than one colon and no brackets is an address, not an address and a port.

    Splitting on the last colon here would produce the host `2001:db8::` and the
    port `1`, which resolves to nothing and reports a connection failure that
    names an address the operator never typed.
    """
    target = parse_robot("operator@2001:db8::1")

    assert target.host == "2001:db8::1"
    assert target.port == DEFAULT_SSH_PORT


def test_a_bracketed_ipv6_address_keeps_its_brackets_off_the_host() -> None:
    """The brackets belong to the notation, not to the address."""
    target = parse_robot("operator@[2001:db8::1]:2222")

    assert target.host == "2001:db8::1"
    assert target.port == 2222


def test_a_bracketed_ipv6_address_with_no_port_still_reads() -> None:
    """The other half of the bracketed branch."""
    target = parse_robot("operator@[2001:db8::1]")

    assert target.host == "2001:db8::1"
    assert target.port == DEFAULT_SSH_PORT


def test_a_bracket_that_is_never_closed_is_refused() -> None:
    """Rather than being read as a host whose name starts with a bracket."""
    with pytest.raises(ConfigurationError, match="never closes it"):
        parse_robot("operator@[2001:db8::1")


def test_anything_but_a_port_after_a_bracketed_address_is_refused() -> None:
    """A trailing fragment means the address was not what it looked like."""
    with pytest.raises(ConfigurationError, match="only a ':port'"):
        parse_robot("operator@[2001:db8::1]/session")


def test_an_address_with_no_account_is_refused() -> None:
    """Defaulting one would put somebody's account name in a tracked file."""
    with pytest.raises(ConfigurationError, match="user@host"):
        parse_robot("192.0.2.10")


def test_an_address_with_an_empty_account_is_refused() -> None:
    """`@host` is a typo, not an account."""
    with pytest.raises(ConfigurationError, match="user@host"):
        parse_robot("@192.0.2.10")


def test_an_address_with_no_host_is_refused() -> None:
    """`operator@:22` names a port and nothing to connect to."""
    with pytest.raises(ConfigurationError, match="names no host"):
        parse_robot("operator@:22")


def test_a_port_that_is_not_a_number_is_refused() -> None:
    """And the message quotes only what the operator typed."""
    with pytest.raises(ConfigurationError, match="where a port number belongs"):
        parse_robot("operator@192.0.2.10:ssh")


@pytest.mark.parametrize("port", ["0", "65536"])
def test_a_port_outside_the_range_is_refused(port: str) -> None:
    """Both ends, because each is a different comparison.

    Args:
        port: The port to refuse.
    """
    with pytest.raises(ConfigurationError, match="outside 1-65535"):
        parse_robot(f"operator@192.0.2.10:{port}")


def test_the_options_that_are_not_the_address_are_carried_through() -> None:
    """They are what an operator with a non-default setup passes."""
    target = parse_robot(
        "operator@192.0.2.10",
        identity_file=Path("/keys/robot"),
        known_hosts=Path("/keys/known_hosts"),
        elevate=False,
    )

    assert target.identity_file == Path("/keys/robot")
    assert target.known_hosts == Path("/keys/known_hosts")
    assert target.elevate is False


def test_a_command_is_rendered_quoted_exactly_as_it_is_sent() -> None:
    """One rendering, so a message quoting a command quotes the command that ran."""
    assert render(["systemctl", "show", "a unit.service"]) == (
        "systemctl show 'a unit.service'"
    )


def test_a_failed_command_quotes_everything_the_robot_said_verbatim() -> None:
    """Nothing is truncated, joined or shortened, and that is REQ-059 rather than style.

    A secret can be anywhere in text a robot wrote, and every one of those
    transformations can cut one in half — after which the redactor matches
    nothing and reports success while half the secret goes out. What makes the
    result readable is the rendering, which escapes a line break after
    scrubbing.
    """
    outcome = CommandOutcome(
        command="cat /etc/missing",
        exit_status=1,
        stdout="",
        stderr="cat: /etc/missing: No such file or directory\nand more\n",
    )

    assert outcome.ok is False
    assert outcome.complaint() == (
        "`cat /etc/missing` exited 1: cat: /etc/missing: No such file or "
        "directory\nand more\n"
    )


def test_a_failed_command_that_said_nothing_still_complains_readably() -> None:
    """A status with nothing after it is a message nobody can act on."""
    outcome = CommandOutcome(command="true", exit_status=1, stdout="", stderr="")

    assert "it said nothing" in outcome.complaint()


def test_a_command_that_only_wrote_to_standard_output_is_quoted_from_there() -> None:
    """Some tools report their reason on the wrong stream, and it is still the reason."""
    outcome = CommandOutcome(
        command="install x y",
        exit_status=1,
        stdout="cannot stat 'x'",
        stderr="",
    )

    assert "cannot stat" in outcome.complaint()


def test_being_unable_to_reach_a_robot_costs_an_unreachable_status() -> None:
    """Nothing has been learned about the robot, so it is not a diagnosis."""
    assert RobotAccessError("").exit_code is ExitCode.UNREACHABLE


def test_the_layout_derives_its_paths_from_the_unit_it_is_given() -> None:
    """So a vendor image naming its unit differently costs an option."""
    layout = RobotLayout(daemon_unit="other.service")

    assert layout.drop_in_directory == "/etc/systemd/system/other.service.d"
    assert layout.drop_in == (
        "/etc/systemd/system/other.service.d/10-reachy-managed.conf"
    )


def test_an_ipv6_address_is_rendered_so_it_can_be_read_back() -> None:
    """`describe` is the one place the authority is rebuilt, so it puts the brackets back.

    Every `RobotAccessError` message quotes this string. Unbracketed it reads as
    `operator@2001:db8::1:22`, which is an address nobody can paste into another
    command.
    """
    assert parse_robot("operator@2001:db8::1").describe() == "operator@[2001:db8::1]:22"
    assert (
        parse_robot("operator@[2001:db8::1]:2222").describe()
        == "operator@[2001:db8::1]:2222"
    )
    assert parse_robot("operator@192.0.2.10").describe() == "operator@192.0.2.10:22"
