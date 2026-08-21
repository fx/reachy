"""The managed region of the robot's daemon environment: where it is, and what it is.

This module is one half of a contract with change 0010. Provisioning REQ-063
says the managed configuration converges to exactly what is declared, including
removing what is no longer declared, and the Ansible `daemon_env` role owns the
same region from the other side. Two implementations writing the same file have
to agree about it byte for byte or they will fight over it — one apply reverting
the other's, forever — so the shape is declared here once and quoted verbatim in
[`docs/ops/managed-daemon-environment.md`](../../../../docs/ops/managed-daemon-environment.md),
which is the document the Ansible side is written against. A contract test
renders this module's output and compares it with the block in that document, so
the two cannot drift.

**The file is fully owned.** Every line of it is written by whichever side
applied last; nothing in it is preserved, merged or appended to. That is the
whole of REQ-063: a drop-in that is added to but never pruned means removing a
setting from the declaration leaves it on the robot, and the robot then diverges
from the file that is supposed to describe it. Other drop-ins in the same `.d/`
directory belong to whoever put them there and are never read or written here.

**The markers are not how ownership is decided — they are how the region is
read.** Ownership is the file. The markers exist so that a person who opens the
file can see where the generated environment starts and stops, and so that a
reader can find the `Environment=` lines without having to understand the rest of
the file's syntax. A file whose markers are missing or unpaired is reported as
unreadable rather than silently treated as empty, because "empty" and "somebody
edited this by hand" call for different actions.

**A percent is escaped, and that one is not about quoting.** systemd performs
specifier expansion on an `Environment=` value, so `%H` becomes the host name and
an *unknown* specifier makes systemd drop the whole assignment without failing
the unit. A percent-encoded URL is therefore not a setting that arrives wrong, it
is a setting that does not arrive, on a robot that started cleanly — which is the
silently-inert configuration this tool exists to catch. `_escape` writes `%%`.

**Settings are written in name order.** Two applies of the same declaration
therefore produce byte-identical files, which is what makes provisioning REQ-060
— a second run changes nothing — a property of the format rather than something
each implementation has to remember.

**Only what the writer writes is read back, and that is checked in closed
form.** `parse_region` reads the settings and then re-renders them: unless the
result is the file it was given, byte for byte, the file is refused. Three
separate attempts at enumerating what to reject — an escape this format does not
produce, a setting assigned twice, a line ending it does not emit — each closed
one hole and left the next, because the property being checked is not a list of
mistakes. It is "this renderer could have produced this file", and the renderer
is the only thing that knows.

That matters because of what happens next: a region read as ours is a region the
next apply **rewrites**. Reading somebody else's file as ours loses their
content, and reading a region with two lines for one setting as ours loses one of
the values. The specific checks below survive because they say *why* a file is
not ours, which is what an operator needs; the round trip is what makes the
answer complete.

It also puts a real obligation on change 0010: the Ansible role writes this same
file, and a role whose output differs by so much as a line ending produces a
region `reachyctl` refuses. That is the intended direction — the two agreeing
byte for byte is the whole point, and a check that quietly tolerated a
disagreement would let them fight over the file instead.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "BEGIN_MARKER",
    "DEFAULT_DAEMON_UNIT",
    "DEFAULT_DROP_IN_NAME",
    "END_MARKER",
    "HEADER",
    "MalformedRegionError",
    "drop_in_directory",
    "drop_in_path",
    "parse_region",
    "render_region",
]

# The unit the environment belongs to. Provisioning's spec is explicit about
# why it is the daemon rather than the application: the application inherits its
# environment from the daemon, which is also why applying a change requires the
# daemon to restart.
DEFAULT_DAEMON_UNIT: Final = "reachy-mini-daemon.service"

# The drop-in's file name. The numeric prefix is systemd's ordering convention —
# drop-ins are applied in name order — and `10-` leaves room both before and
# after for anything an operator or the vendor's image puts alongside it.
DEFAULT_DROP_IN_NAME: Final = "10-reachy-managed.conf"

BEGIN_MARKER: Final = "# >>> reachy managed environment >>>"
END_MARKER: Final = "# <<< reachy managed environment <<<"

# Written at the top of every generated file. It says what an operator who opens
# the file needs to know before editing it, which is that editing it is futile.
HEADER: Final = (
    "# This file is generated and is owned in full by the Reachy tooling.\n"
    "# `reachyctl config apply` and the Ansible daemon_env role both rewrite it\n"
    "# whole, so an edit made here by hand is lost on the next apply, and a\n"
    "# setting removed from the declaration is removed from the robot rather\n"
    "# than left behind. See docs/ops/managed-daemon-environment.md.\n"
)

_SECTION: Final = "[Service]"

# `Environment="NAME=value"`, with the whole assignment inside one pair of double
# quotes, and the value written the way `_escape` writes one: any character that
# is not a quote, a backslash or a percent, or one of the three escapes this
# format produces. Deliberately not `(.*)`. A permissive value pattern accepts
# lines this format could never have written — an unbalanced quote, an escape
# systemd reads differently, a bare percent systemd would expand as a specifier —
# and `parse_region` would then report them as settings this tool owns, after
# which the next apply rewrites somebody else's file. Every line this accepts is
# a line this module could have produced, which is what makes "the region is
# ours" a check rather than an assumption.
# The three escapes `_escape` writes, and the only three `_ENVIRONMENT_LINE`
# admits. A lone `%` is deliberately not among them: see `_escape`.
_ESCAPED: Final = re.compile(r'\\(["\\])|%(%)')

_ENVIRONMENT_LINE: Final = re.compile(
    r'^Environment="([^="\\%]+)=((?:[^"\\%]|\\["\\]|%%)*)"$',
)


class MalformedRegionError(ValueError):
    """The file on the robot is not one this format can read.

    Raised rather than returning an empty region, because the two call for
    different actions: an absent file means nothing has been applied yet, and a
    file whose markers are missing or unpaired means something else is writing
    it. Silently rewriting the second is how one tool's apply starts reverting
    another's.
    """


def drop_in_directory(unit: str = DEFAULT_DAEMON_UNIT) -> str:
    """Say where a unit's drop-ins live.

    Args:
        unit: The systemd unit carrying the environment.

    Returns:
        The absolute directory path.
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
        The absolute file path. This is the path change 0010's Ansible role
        writes too; see the module documentation.
    """
    return f"{drop_in_directory(unit)}/{name}"


def render_region(settings: Mapping[str, str]) -> str:
    """Render the whole file for a declaration.

    Args:
        settings: The settings that are to be in force, by name. Already
            validated: this function does not judge values, it writes them, and
            a value carrying a line break would end the directive it is on.
            `reachy_contracts.validate_settings` is what refuses one.

    Returns:
        The file's complete content, ending in a newline. Settings appear in
        name order, so applying the same declaration twice produces identical
        bytes.
    """
    lines = [HEADER, f"{_SECTION}\n", f"{BEGIN_MARKER}\n"]
    lines.extend(
        f'Environment="{name}={_escape(settings[name])}"\n' for name in sorted(settings)
    )
    lines.append(f"{END_MARKER}\n")
    return "".join(lines)


def parse_region(content: str) -> dict[str, str]:
    """Read back the settings a rendered file carries.

    Args:
        content: The file's content. **The file exists.** Whether it does is the
            caller's distinction to make and not one this function can see: an
            absent file and an existing empty one both arrive here as an empty
            string, and they are opposite facts. `DaemonClient.read_managed_region`
            answers `None` for the first, and only the second reaches this.

    Returns:
        The settings by name. A region that is genuinely empty — every setting
        withdrawn — still carries the header and the markers, so it parses to
        nothing and is not the same file as one with nothing in it.

    Raises:
        MalformedRegionError: If the file is not, byte for byte, one this format
            writes — see the module documentation. The specific cases are named
            where they can be, because an operator needs to know *which* line is
            wrong; the round trip at the end is what makes the check complete.
    """
    if not content.strip():
        # An existing file with nothing in it. Not "nothing has been applied":
        # this format never writes an empty file — emptying the region still
        # writes the header and both markers — so a file that is blank is one
        # something else made blank, and the next apply would otherwise
        # overwrite it without anybody being told. Provisioning REQ-063 makes
        # this region fully owned, and the safety of owning it wholesale rests
        # entirely on being able to tell "we wrote this" from "somebody else's
        # file is sitting here".
        message = (
            "the managed region is not readable: the file is there and is "
            "empty. This format never writes an empty file — withdrawing every "
            "setting still writes the header and both markers — so something "
            "other than the Reachy tooling has emptied it"
        )
        raise MalformedRegionError(message)
    # `split`, not `splitlines`: the latter also breaks on a carriage return, a
    # form feed and three Unicode separators, none of which this format emits —
    # so a file written with any of them would be read as one of ours. The round
    # trip at the end would catch that anyway; splitting strictly is what makes
    # the error say which line is wrong rather than only that the file is.
    lines = content.split("\n")
    begins = [index for index, line in enumerate(lines) if line == BEGIN_MARKER]
    ends = [index for index, line in enumerate(lines) if line == END_MARKER]
    if len(begins) != 1 or len(ends) != 1 or begins[0] > ends[0]:
        message = (
            f"the managed region is not readable: expected exactly one "
            f"{BEGIN_MARKER!r} followed by exactly one {END_MARKER!r}, and "
            f"found {len(begins)} and {len(ends)}. Something other than the "
            f"Reachy tooling has written this file"
        )
        raise MalformedRegionError(message)
    settings: dict[str, str] = {}
    for offset, line in enumerate(lines[begins[0] + 1 : ends[0]], begins[0] + 2):
        if not line.strip():
            continue
        match = _ENVIRONMENT_LINE.match(line)
        if match is None:
            # The line itself is not quoted. It is inside a region this format
            # owns, so it may hold a value, and a value is exactly where a
            # credential ends up — reachyctl REQ-059. The line number is enough
            # to find it.
            message = (
                f"the managed region is not readable: line {offset} is not an "
                f"Environment assignment this format writes. Something other "
                f"than the Reachy tooling has written this file"
            )
            raise MalformedRegionError(message)
        name = match.group(1)
        if name in settings:
            # One line per setting, and a duplicate is not a tidiness question:
            # taking the last one silently discards a value, and then the next
            # apply writes the survivor back as though it were what the file
            # always said. A file with two is one this format did not write.
            message = (
                f"the managed region is not readable: {name} is assigned twice, "
                f"at line {offset} and earlier. This format writes one line per "
                f"setting, so something other than the Reachy tooling has "
                f"written this file"
            )
            raise MalformedRegionError(message)
        settings[name] = _unescape(match.group(2))
    if render_region(settings) != content:
        # Everything above says why a particular line is not ours. This says
        # that the file as a whole is not, which is the property that actually
        # matters and the only one that cannot be got wrong by omission. It
        # quotes nothing: the difference could be in a value.
        message = (
            "the managed region is not readable: it is not byte for byte what "
            "this format writes, so re-rendering what was read does not "
            "reproduce it. Something other than the Reachy tooling has written "
            "this file"
        )
        raise MalformedRegionError(message)
    return settings


def _escape(value: str) -> str:
    """Make a value safe inside a double-quoted systemd assignment.

    Two different mechanisms, and the second is the one that bites silently.

    systemd's string lexer reads a backslash inside a quoted string as an escape
    and a double quote as the end of the string, so both have to be escaped —
    the backslash first, or the escape this function adds would itself be
    escaped by the pass that follows.

    Separately, systemd performs **specifier expansion** on an `Environment=`
    value: `%H` becomes the host name, `%%` is a literal percent, and an unknown
    specifier makes systemd drop the whole assignment **without failing the
    unit**. A percent-encoded URL is therefore not a setting that arrives wrong,
    it is a setting that does not arrive at all, on a robot that started
    cleanly. So the percent is escaped here rather than left to systemd.

    Args:
        value: The setting's value.

    Returns:
        The value as it appears between the quotes.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    # After the other two, and independent of them: neither introduces a
    # percent, so this cannot double-escape one they wrote.
    return escaped.replace("%", "%%")


def _unescape(value: str) -> str:
    """Read a value back out of a double-quoted systemd assignment.

    Total rather than defensive: `_ENVIRONMENT_LINE` has already refused any
    line whose backslashes or percents are not the escapes `_escape` writes, so
    there is no dangling escape to decide what to do with. A line this cannot
    read is one `parse_region` never reached.

    One pass over the three alternatives rather than three passes, so that a
    value carrying two adjacent escapes cannot have the second pass rewrite what
    the first produced.

    Args:
        value: What appeared between the quotes.

    Returns:
        The setting's value.
    """
    # Exactly one group matches per occurrence; an unmatched group substitutes
    # as empty.
    return _ESCAPED.sub(r"\1\2", value)
