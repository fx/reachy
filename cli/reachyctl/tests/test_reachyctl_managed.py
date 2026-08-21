"""The managed drop-in's format, and the document change 0010 is written against.

Two implementations write this file — this one and the Ansible `daemon_env` role
— so the format is a contract rather than an internal detail. The last test here
is what makes it one: it renders this module's own output and compares it with
the block quoted in `docs/ops/managed-daemon-environment.md`, so the two cannot
drift, and the Ansible side can be written against the document without reading
the Python.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

from reachyctl.managed import (
    BEGIN_MARKER,
    DEFAULT_DAEMON_UNIT,
    END_MARKER,
    MalformedRegionError,
    drop_in_directory,
    drop_in_path,
    parse_region,
    render_region,
)

# RFC 5737 TEST-NET-1 — see the root AGENTS.md on what may enter a tracked file.
ENDPOINT: Final = "ws://192.0.2.10:8000/v1/session"

DOCUMENT: Final = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "ops"
    / "managed-daemon-environment.md"
)

EXAMPLE: Final = {
    "REACHY_GROUNDSTATION_URL": ENDPOINT,
    "REACHY_SATELLITE_LOG_LEVEL": "info",
}


def test_the_region_is_written_in_name_order() -> None:
    """Which is what makes two applies of one declaration byte-identical."""
    rendered = render_region({"B_SETTING": "2", "A_SETTING": "1", "C_SETTING": "3"})

    body = rendered.split(BEGIN_MARKER)[1].split(END_MARKER)[0].strip().splitlines()
    assert body == [
        'Environment="A_SETTING=1"',
        'Environment="B_SETTING=2"',
        'Environment="C_SETTING=3"',
    ]


def test_rendering_the_same_declaration_twice_produces_the_same_bytes() -> None:
    """Provisioning REQ-060 is a property of this format, not of a caller."""
    assert render_region(EXAMPLE) == render_region(
        dict(reversed(list(EXAMPLE.items())))
    )


def test_the_region_round_trips_through_the_parser() -> None:
    """The writer and the reader are one format, so this is the whole of it."""
    assert parse_region(render_region(EXAMPLE)) == EXAMPLE


def test_a_backslash_and_a_quote_survive_the_round_trip() -> None:
    """Systemd reads both inside a quoted string, so both are escaped — backslash first."""
    awkward = {"A_SETTING": 'a\\b"c\\"d'}

    rendered = render_region(awkward)

    assert 'Environment="A_SETTING=a\\\\b\\"c\\\\\\"d"' in rendered
    assert parse_region(rendered) == awkward


def test_a_trailing_backslash_survives_the_round_trip() -> None:
    """The one case an unescaper written as a loop gets wrong at the very end."""
    assert parse_region(render_region({"A_SETTING": "ends with\\"})) == {
        "A_SETTING": "ends with\\",
    }


def test_a_value_containing_an_equals_sign_survives() -> None:
    """A base64 credential ends in one, and the split is on the FIRST equals."""
    assert parse_region(render_region({"A_SETTING": "abc=="})) == {"A_SETTING": "abc=="}


def test_an_absent_file_carries_no_settings_and_is_not_an_error() -> None:
    """A robot nothing has been applied to is not a robot in a bad state."""
    assert parse_region("") == {}
    assert parse_region("\n  \n") == {}


def test_a_file_with_no_markers_is_refused_rather_than_treated_as_empty() -> None:
    """Overwriting it regardless is how two tools start reverting each other."""
    with pytest.raises(MalformedRegionError, match="not readable"):
        parse_region('[Service]\nEnvironment="A_SETTING=1"\n')


def test_a_file_with_the_markers_the_wrong_way_round_is_refused() -> None:
    """Both markers are present, so only their order says the file is not ours."""
    with pytest.raises(MalformedRegionError, match="not readable"):
        parse_region(f"{END_MARKER}\n{BEGIN_MARKER}\n")


def test_a_file_with_two_regions_is_refused() -> None:
    """One region, or this format does not know which one it owns."""
    doubled = render_region(EXAMPLE) + render_region(EXAMPLE)

    with pytest.raises(MalformedRegionError, match="found 2 and 2"):
        parse_region(doubled)


def test_a_line_inside_the_region_that_is_not_ours_is_refused_by_position() -> None:
    """And the message gives the line number rather than the line.

    A line inside the region may hold a value, and a value is exactly where a
    credential ends up — reachyctl REQ-059. The position is enough to find it.
    """
    content = (
        f"[Service]\n{BEGIN_MARKER}\n"
        f'Environment="A_SETTING=1"\n'
        f"ExecStartPre=/bin/echo hunter2\n"
        f"{END_MARKER}\n"
    )

    with pytest.raises(MalformedRegionError) as raised:
        parse_region(content)

    assert "line 4" in str(raised.value)
    assert "hunter2" not in str(raised.value)


def test_a_blank_line_inside_the_region_is_ignored() -> None:
    """An editor that added one has not made the file unreadable."""
    content = f'[Service]\n{BEGIN_MARKER}\n\nEnvironment="A=1"\n\n{END_MARKER}\n'

    assert parse_region(content) == {"A": "1"}


def test_the_paths_are_derived_from_the_unit() -> None:
    """A vendor image naming its unit differently costs an option, not a release."""
    assert drop_in_directory() == (f"/etc/systemd/system/{DEFAULT_DAEMON_UNIT}.d")
    assert drop_in_path().endswith("/10-reachy-managed.conf")
    assert drop_in_path("other.service") == (
        "/etc/systemd/system/other.service.d/10-reachy-managed.conf"
    )


@pytest.mark.filesystem  # reads the committed document; the bytes in it are the contract
def test_the_committed_document_quotes_exactly_what_this_module_renders() -> None:
    """Change 0010's Ansible role is written against that document, not this module.

    Two implementations writing one file have to agree byte for byte, and the
    only way a document stays true is if something compares it with the code. So
    this reads the fenced block out of the operations note and requires it to be
    what `render_region` produces for the same two example settings.
    """
    text = DOCUMENT.read_text(encoding="utf-8")
    blocks = [
        block.split("\n", 1)[1]
        for block in text.split("```")[1::2]
        if block.startswith("ini\n")
    ]

    assert len(blocks) == 1, "the note must carry exactly one ini block to compare"
    assert blocks[0] == render_region(EXAMPLE)
    assert str(drop_in_path()) in text


def test_a_value_ending_in_a_lone_backslash_is_read_rather_than_swallowed() -> None:
    """Nothing this format writes ends that way; something else's file might.

    The unescaper walks the value a character at a time, so a backslash with
    nothing after it is the one position where a loop silently drops a
    character. It is kept.
    """
    content = f'[Service]\n{BEGIN_MARKER}\nEnvironment="A_SETTING=ends with\\"\n{END_MARKER}\n'

    assert parse_region(content) == {"A_SETTING": "ends with\\"}
