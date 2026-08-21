"""The managed daemon drop-in, written from the playbook's side.

This is the **second** implementation of the file described by
[`docs/ops/managed-daemon-environment.md`](../../../../docs/ops/managed-daemon-environment.md).
The first is `reachyctl.managed`, and that document opens by saying so: two
independent implementations write one file on the robot, and the document is
what they are both written against. Neither imports the other — a shared helper
would make "the two agree" true by construction and untestable, and it would put
the whole `reachyctl` distribution, its transport and its terminal renderer on
the machine that runs the playbook, which is exactly what reachyctl REQ-056's
design note refuses for the check registry.

What ties them together instead is a test. `provisioning/tests/` renders the same
declarations through both implementations and compares the bytes, and it parses
each one's output with the other's reader. So a change to either side that this
side does not follow is a red run rather than two tools quietly reverting each
other's writes on a robot.

**The whole file is owned.** Every line is written by whichever side applied
last; nothing is preserved, merged with or appended to. That is provisioning
REQ-063, and it is what makes a setting withdrawn from the declaration leave the
robot rather than linger. Pruning is therefore not a step in the role — it is
what rendering the whole file *is*.

**Absent, empty and unreadable are three states.** `region_state` answers with
the one it found, and an existing empty file belongs with "something else wrote
this", not with "nothing has been applied": this format never writes an empty
file, because withdrawing every setting still writes the header, `[Service]` and
both markers. Collapsing the two would let the next apply silently replace a file
somebody else is maintaining.

**A reader accepts only what it could have written.** `region_state` parses the
region and then re-renders it, and refuses the file unless the result is what it
was given, byte for byte. Enumerating the ways a file can be wrong is a list that
is never finished; "this renderer could have produced this" is the property, and
the renderer is the only thing that knows it.

Nothing here renders a value into a message. A setting is exactly where a
credential ends up — `REACHY_GROUNDSTATION_CREDENTIAL` is marked `secret` in the
vocabulary — so the complaints below name a line number or a setting name and
stop there, and the tasks that pass a rendered region between them carry
`no_log`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final

from reachy_contracts import SettingError, validate_settings

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

__all__ = [
    "BEGIN_MARKER",
    "DEFAULT_DAEMON_UNIT",
    "DEFAULT_DROP_IN_NAME",
    "END_MARKER",
    "HEADER",
    "SECTION",
    "FilterModule",
    "drop_in_directory",
    "drop_in_path",
    "region_state",
    "render_region",
    "settings_change",
    "validated_settings",
]

# The unit is the daemon, not the application. The application inherits its
# environment from the daemon, which is also why putting a change in force
# requires the daemon to restart — see the provisioning spec's "Daemon
# environment" section. The role encodes both facts rather than documenting
# them: the drop-in path below is derived from this name, and the write notifies
# a handler that restarts this unit.
DEFAULT_DAEMON_UNIT: Final = "reachy-mini-daemon.service"

# systemd applies drop-ins in name order. `10-` leaves room both before and
# after for anything the vendor's image or an operator puts alongside it.
DEFAULT_DROP_IN_NAME: Final = "10-reachy-managed.conf"

BEGIN_MARKER: Final = "# >>> reachy managed environment >>>"
END_MARKER: Final = "# <<< reachy managed environment <<<"

SECTION: Final = "[Service]"

# Five lines, byte for byte as the contract document prints them. It says the
# one thing an operator who opens the file needs to know before editing it,
# which is that editing it is futile.
HEADER: Final = (
    "# This file is generated and is owned in full by the Reachy tooling.\n"
    "# `reachyctl config apply` and the Ansible daemon_env role both rewrite it\n"
    "# whole, so an edit made here by hand is lost on the next apply, and a\n"
    "# setting removed from the declaration is removed from the robot rather\n"
    "# than left behind. See docs/ops/managed-daemon-environment.md.\n"
)

# `Environment="NAME=value"`, with the whole assignment inside one pair of double
# quotes, and the value written the way `_escape` writes one: anything that is
# neither a quote nor a backslash, or one of the two escapes this format
# produces. Deliberately not a permissive `(.*)` — a pattern that accepted an
# unbalanced quote or an escape systemd reads differently would report a line
# this format could never have written as a setting the role owns, after which
# the next run rewrites somebody else's file.
_ENVIRONMENT_LINE: Final = re.compile(
    r'^Environment="([^="\\]+)=((?:[^"\\]|\\["\\])*)"$',
)

# The two escapes `_escape` writes, and the only two the pattern above admits.
_ESCAPED: Final = re.compile(r'\\(["\\])')

# What `region_state` answers with. Three states, because absent, ours and
# somebody else's call for three different actions.
ABSENT: Final = "absent"
MANAGED: Final = "managed"
UNREADABLE: Final = "unreadable"


def drop_in_directory(unit: str = DEFAULT_DAEMON_UNIT) -> str:
    """Say where a unit's drop-ins live.

    Args:
        unit: The systemd unit carrying the environment.

    Returns:
        The absolute directory path. Other drop-ins in it belong to whoever put
        them there; this role neither reads nor writes them.
    """
    return f"/etc/systemd/system/{unit}.d"


def drop_in_path(
    unit: str = DEFAULT_DAEMON_UNIT,
    name: str = DEFAULT_DROP_IN_NAME,
) -> str:
    """Say where the managed region is written.

    Args:
        unit: The systemd unit carrying the environment.
        name: The drop-in's file name.

    Returns:
        The absolute file path. `reachyctl config apply` writes this same path;
        see the module documentation.
    """
    return f"{drop_in_directory(unit)}/{name}"


def render_region(settings: Mapping[str, str]) -> str:
    """Render the whole file for a declaration.

    Args:
        settings: What is to be in force, by name. Already validated —
            `validated_settings` is what refuses a value carrying a control
            character, and a line break in a value would end the directive it is
            on and turn the remainder into a directive of its own.

    Returns:
        The file's complete content, ending in a newline. Settings appear in
        name order, which is what makes two applies of one declaration produce
        identical bytes — provisioning REQ-060 as a property of the format
        rather than something each implementation has to remember.
    """
    lines = [HEADER, f"{SECTION}\n", f"{BEGIN_MARKER}\n"]
    lines.extend(
        f'Environment="{name}={_escape(settings[name])}"\n' for name in sorted(settings)
    )
    lines.append(f"{END_MARKER}\n")
    return "".join(lines)


def region_state(present: bool, content: str = "") -> dict[str, Any]:
    """Say which of the three states the file on the robot is in.

    Total rather than raising, because the caller is a playbook: a filter that
    threw would surface as a templating traceback, where a record lets the role
    fail with the sentence an operator needs and lets `--check` report the same
    thing without stopping.

    Args:
        present: Whether the file exists. The caller's distinction to make and
            not one visible here: an absent file and an existing empty one both
            arrive as an empty string, and they are opposite facts.
        content: The file's content, when it exists.

    Returns:
        A record carrying `state`, the `settings` read back when the region is
        ours, and a `complaint` naming what is wrong when it is not. The
        complaint quotes no value: a line inside the region may hold a
        credential, so a line number is what it reports instead.
    """
    if not present:
        return {"state": ABSENT, "settings": {}, "complaint": ""}
    if not content.strip():
        return _unreadable(
            "the file is there and is empty. This format never writes an empty "
            "file — withdrawing every setting still writes the header and both "
            "markers — so something other than the Reachy tooling has emptied it",
        )
    # `split`, not `splitlines`: the latter also breaks on a carriage return, a
    # form feed and three Unicode separators, none of which this format emits, so
    # a file written with any of them would be read as one of ours. The round
    # trip at the end would catch it regardless; splitting strictly is what makes
    # the complaint say which line is wrong rather than only that the file is.
    lines = content.split("\n")
    begins = [index for index, line in enumerate(lines) if line == BEGIN_MARKER]
    ends = [index for index, line in enumerate(lines) if line == END_MARKER]
    if len(begins) != 1 or len(ends) != 1 or begins[0] > ends[0]:
        return _unreadable(
            f"expected exactly one {BEGIN_MARKER!r} followed by exactly one "
            f"{END_MARKER!r}, and found {len(begins)} and {len(ends)}",
        )
    settings: dict[str, str] = {}
    for offset, line in enumerate(lines[begins[0] + 1 : ends[0]], begins[0] + 2):
        match = _ENVIRONMENT_LINE.match(line)
        if match is None:
            return _unreadable(
                f"line {offset} is not an Environment assignment this format "
                f"writes. The line itself is not quoted back, because a line "
                f"inside this region holds a setting's value",
            )
        name = match.group(1)
        if name in settings:
            return _unreadable(
                f"{name} is assigned twice, at line {offset} and earlier. This "
                f"format writes one line per setting, and taking either one "
                f"would silently discard a value",
            )
        settings[name] = _unescape(match.group(2))
    if render_region(settings) != content:
        # Everything above says why a particular line is not ours. This says the
        # file as a whole is not, which is the property that actually matters and
        # the only one that cannot be got wrong by omission. It quotes nothing:
        # the difference could be in a value.
        return _unreadable(
            "it is not byte for byte what this format writes, so re-rendering "
            "what was read does not reproduce it",
        )
    return {"state": MANAGED, "settings": settings, "complaint": ""}


def validated_settings(declared: Mapping[str, object]) -> dict[str, Any]:
    """Check a declaration against the shared vocabulary, before anything is written.

    The vocabulary is `reachy_contracts.ROBOT_SETTINGS`, which is where every
    setting the robot understands is declared once. Validating here means a value
    the robot would refuse costs no write and no daemon restart, which is
    reachyctl REQ-053 applied on this side of the same contract.

    Args:
        declared: The settings by name, as the inventory and group variables
            supplied them — so a value may be whatever YAML made of it, not
            necessarily a string.

    Returns:
        A record carrying `ok`, the accepted `settings` normalised the way they
        will be written, and a `complaint` naming every offending setting and its
        constraint. The complaint quotes no value — that is the contracts
        package's rule and this only passes it on.
    """
    written: dict[str, str] = {}
    unwritable: list[str] = []
    for name, value in declared.items():
        # YAML types a bare `100` as an integer and a bare `true` as a boolean,
        # and a settings file is written in YAML. A systemd environment holds
        # text, so these are rendered here rather than refused: the alternative
        # is an operator quoting every number in their declaration and finding
        # out which ones from a `TypeError`. A value with no obvious text form —
        # a list, a mapping, a null — is refused, because guessing at one would
        # write something nobody meant.
        if isinstance(value, bool):
            written[str(name)] = "true" if value else "false"
        elif isinstance(value, str | int | float):
            written[str(name)] = str(value)
        else:
            unwritable.append(str(name))
    if unwritable:
        return {
            "ok": False,
            "settings": {},
            "complaint": (
                f"{', '.join(sorted(unwritable))} hold(s) a value with no text "
                f"form; a systemd environment holds text, so a setting takes a "
                f"string, a number or a boolean"
            ),
        }
    try:
        accepted = validate_settings(written)
    except SettingError as error:
        return {"ok": False, "settings": {}, "complaint": str(error)}
    return {"ok": True, "settings": accepted, "complaint": ""}


def settings_change(
    desired: Mapping[str, str],
    current: Mapping[str, str],
) -> dict[str, Any]:
    """Say what converging on a declaration would do, in names only.

    The write itself is a whole-file copy, so this decides nothing — the module
    that owns the file owns the outcome. What it produces is the line an operator
    reads, in `--check` and in an ordinary run alike, and the reason it exists as
    a filter rather than as a `debug` of the two dictionaries is that printing
    either of them would print a credential.

    Args:
        desired: What the declaration says should be in force.
        current: What the managed region carries now.

    Returns:
        A record carrying `added`, `changed`, `removed` and `unchanged` as name
        tuples in name order, `changes` saying whether any of the first three is
        non-empty, and a one-line `summary`.
    """
    added = sorted(set(desired) - set(current))
    removed = sorted(set(current) - set(desired))
    shared = sorted(set(desired) & set(current))
    changed = [name for name in shared if desired[name] != current[name]]
    unchanged = [name for name in shared if desired[name] == current[name]]
    changes = bool(added or changed or removed)
    return {
        "added": added,
        "changed": changed,
        "removed": removed,
        "unchanged": unchanged,
        "changes": changes,
        "summary": (
            f"{len(added)} to add, {len(changed)} to change, "
            f"{len(removed)} to remove, {len(unchanged)} already in place"
            if changes
            else f"the managed region already declares all {len(unchanged)} setting(s)"
        ),
    }


def _unreadable(reason: str) -> dict[str, Any]:
    """Report a file this format did not write.

    Args:
        reason: What is wrong with it.

    Returns:
        The record, with the sentence the role fails on. The path is added by
        the role, which is the layer that knows which robot and which file.
    """
    return {
        "state": UNREADABLE,
        "settings": {},
        "complaint": (
            f"the managed region is not readable: {reason}. Something other "
            f"than the Reachy tooling has written this file, and this role owns "
            f"what it writes in full — converging it would replace their content"
        ),
    }


def _escape(value: str) -> str:
    """Make a value safe inside a double-quoted systemd assignment.

    systemd reads a backslash inside a quoted string as an escape and a double
    quote as the end of the string, so both are escaped — the backslash first,
    or the escape this adds would itself be escaped by the pass that follows.
    Nothing else is escaped.

    Args:
        value: The setting's value.

    Returns:
        The value as it appears between the quotes.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _unescape(value: str) -> str:
    """Read a value back out of a double-quoted systemd assignment.

    Total rather than defensive: `_ENVIRONMENT_LINE` has already refused any line
    whose backslashes are not the two escapes `_escape` writes, so there is no
    dangling escape to decide what to do with.

    Args:
        value: What appeared between the quotes.

    Returns:
        The setting's value.
    """
    return _ESCAPED.sub(r"\1", value)


class FilterModule:
    """Expose this module's functions to Jinja, which is how the roles reach them."""

    def filters(self) -> dict[str, Callable[..., Any]]:
        """List the filters this plugin provides.

        Returns:
            The filters by the name a template writes.
        """
        return {
            "reachy_drop_in_directory": drop_in_directory,
            "reachy_drop_in_path": drop_in_path,
            "reachy_managed_region": render_region,
            "reachy_region_state": region_state,
            "reachy_settings_change": settings_change,
            "reachy_validated_settings": validated_settings,
        }
