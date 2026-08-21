"""The two implementations of the managed region, rendered side by side.

`docs/ops/managed-daemon-environment.md` opens by saying that two independent
implementations write one file on the robot: `reachyctl config apply` and the
Ansible `daemon_env` role. Independence is the point — neither imports the other,
so neither can make the agreement true by construction — and independence with
nothing comparing the two is how they drift until one tool's apply starts
reverting the other's on a real robot.

This is the thing that compares them. Every test below renders or parses the same
input through both sides and requires the same answer: the same bytes out of the
writers, the same settings back out of the readers, and each side's reader
accepting what the other side's writer produced. The document is checked too,
because the Ansible side was written against the document rather than against the
Python, and a document nothing verifies is a document that stops being true.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

import reachy_managed
from reachyctl import managed as reachyctl_managed

if TYPE_CHECKING:
    from collections.abc import Mapping

# RFC 5737 TEST-NET-1 — see the root AGENTS.md on what may enter a tracked file.
ENDPOINT: Final = "ws://192.0.2.10:8000/v1/session"

REPOSITORY: Final = Path(__file__).resolve().parents[2]

DOCUMENT: Final = REPOSITORY / "docs" / "ops" / "managed-daemon-environment.md"

PLUGINS: Final = REPOSITORY / "provisioning" / "ansible" / "plugins" / "filter"

# An import of the CLI, in either spelling. Matched on import statements rather
# than on the text, because these modules name `reachyctl` in prose constantly —
# what they must not do is depend on it.
_IMPORT: Final = re.compile(r"^\s*(?:import|from)\s+reachyctl\b")

EXAMPLE: Final = {
    "REACHY_GROUNDSTATION_URL": ENDPOINT,
    "REACHY_SATELLITE_LOG_LEVEL": "info",
}

# Every shape the format has an opinion about, and the two escapes it performs.
# A declaration that exercises only ordinary values would agree across both
# implementations however either one escaped, which is the half of the format
# most likely to be written differently twice.
DECLARATIONS: Final[tuple[Mapping[str, str], ...]] = (
    {},
    EXAMPLE,
    {"A_SETTING": ""},
    {"A_SETTING": "a value with spaces"},
    {"A_SETTING": "carries=an=equals"},
    {"A_SETTING": r"one\backslash"},
    {"A_SETTING": "\\"},
    {"A_SETTING": '"'},
    {"A_SETTING": r'both \ and " together'},
    {"A_SETTING": r"trailing\\"},
    {"A_SETTING": "ünïcödé and 日本語"},
    # systemd expands `%` specifiers in an `Environment=` value, so these are
    # the cases where "written" and "in force" come apart if the format does
    # not carry the percent itself.
    {"A_SETTING": "one%percent"},
    {"A_SETTING": "%"},
    {"A_SETTING": "%%"},
    {"A_SETTING": "ws://192.0.2.10:8000/v1/session?token=a%20b"},
    {"A_SETTING": "%H"},
    {"A_SETTING": r'every%thing \ at " once'},
    {"C_SETTING": "3", "A_SETTING": "1", "B_SETTING": "2"},
)


@pytest.mark.parametrize("declaration", DECLARATIONS)
def test_both_implementations_render_the_same_bytes(
    declaration: Mapping[str, str],
) -> None:
    """A role whose output differed by a line ending writes a region reachyctl refuses.

    Args:
        declaration: The settings to render.
    """
    assert reachy_managed.render_region(declaration) == (
        reachyctl_managed.render_region(declaration)
    )


@pytest.mark.parametrize("declaration", DECLARATIONS)
def test_reachyctl_accepts_what_the_role_writes(
    declaration: Mapping[str, str],
) -> None:
    """Which is the direction that matters on a robot both tools operate.

    `reachyctl` refuses any region it could not have written itself, so a role
    that got the format subtly wrong would produce a file the operator's own
    tooling then declines to converge — with no way to tell that from somebody
    having edited it by hand.

    Args:
        declaration: The settings the role would write.
    """
    written = reachy_managed.render_region(declaration)

    assert reachyctl_managed.parse_region(written) == declaration


@pytest.mark.parametrize("declaration", DECLARATIONS)
def test_the_role_accepts_what_reachyctl_writes(
    declaration: Mapping[str, str],
) -> None:
    """And this is the other direction: a region `config apply` wrote is converged.

    Args:
        declaration: The settings reachyctl would write.
    """
    written = reachyctl_managed.render_region(declaration)

    state = reachy_managed.region_state(present=True, content=written)

    assert state["state"] == reachy_managed.MANAGED
    assert state["settings"] == declaration


def test_both_implementations_write_the_same_path_for_the_same_unit() -> None:
    """One file, so one path. A vendor unit named differently moves both alike."""
    assert reachy_managed.drop_in_path() == reachyctl_managed.drop_in_path()
    assert reachy_managed.drop_in_directory() == reachyctl_managed.drop_in_directory()
    assert reachy_managed.drop_in_path("other.service") == (
        reachyctl_managed.drop_in_path("other.service")
    )


def test_both_implementations_agree_on_the_literal_parts() -> None:
    """The header, the markers and the section are quoted in the contract document."""
    assert reachy_managed.HEADER == reachyctl_managed.HEADER
    assert reachy_managed.BEGIN_MARKER == reachyctl_managed.BEGIN_MARKER
    assert reachy_managed.END_MARKER == reachyctl_managed.END_MARKER
    assert reachy_managed.DEFAULT_DAEMON_UNIT == (reachyctl_managed.DEFAULT_DAEMON_UNIT)
    assert reachy_managed.DEFAULT_DROP_IN_NAME == (
        reachyctl_managed.DEFAULT_DROP_IN_NAME
    )


@pytest.mark.parametrize(
    "content",
    [
        # A backslash this format never writes: it escapes only `"` and `\\`.
        'Environment="A_SETTING=ends with\\q"',
        # A quote that is not escaped, so the line has three of them.
        'Environment="A_SETTING=one"two"',
        # A trailing backslash, which would escape the closing quote.
        'Environment="A_SETTING=ends with\\"',
        # A name carrying a quote, which the escaping never produces.
        'Environment="A"SETTING=value"',
        # A blank line. This format does not write them.
        'Environment="A_SETTING=1"\n',
        # A directive that is not an assignment at all.
        "ExecStart=/bin/true",
        # A bare percent, which systemd would expand as a specifier. This
        # format writes `%%`, so a line carrying one is not one it wrote.
        'Environment="A_SETTING=one%percent"',
        # A percent in the NAME, which the escaping never produces either.
        'Environment="A%SETTING=value"',
    ],
)
def test_both_readers_refuse_the_same_files(content: str) -> None:
    """A disagreement here is one tool converging a file the other will not touch.

    Args:
        content: The line or lines to put between the markers.
    """
    file = (
        f"{reachy_managed.HEADER}{reachy_managed.SECTION}\n"
        f"{reachy_managed.BEGIN_MARKER}\n{content}\n"
        f"{reachy_managed.END_MARKER}\n"
    )

    assert reachy_managed.region_state(present=True, content=file)["state"] == (
        reachy_managed.UNREADABLE
    )
    with pytest.raises(reachyctl_managed.MalformedRegionError):
        reachyctl_managed.parse_region(file)


def test_both_readers_refuse_a_file_that_is_there_and_empty() -> None:
    """Absent and empty are opposite facts, and this format never writes an empty file."""
    assert reachy_managed.region_state(present=True, content="")["state"] == (
        reachy_managed.UNREADABLE
    )
    with pytest.raises(reachyctl_managed.MalformedRegionError):
        reachyctl_managed.parse_region("")


def test_an_absent_file_is_neither_readable_nor_a_fault() -> None:
    """It means nothing has been applied to this robot, and the next run proceeds."""
    state = reachy_managed.region_state(present=False)

    assert state["state"] == reachy_managed.ABSENT
    assert state["settings"] == {}
    assert not state["complaint"]


@pytest.mark.filesystem  # reads the committed document; the bytes in it are the contract
def test_the_committed_document_quotes_exactly_what_the_role_renders() -> None:
    """The Ansible side was written against the document, so the document is checked.

    `reachyctl`'s own suite makes the same comparison from its side. Both are
    here rather than one, because the document is the only thing either
    implementation was written against and a note that has quietly stopped
    describing the file is worse than no note.
    """
    text = DOCUMENT.read_text(encoding="utf-8")
    blocks = [
        block.split("\n", 1)[1]
        for block in text.split("```")[1::2]
        if block.startswith("ini\n")
    ]

    assert len(blocks) == 1, "the note must carry exactly one ini block to compare"
    assert blocks[0] == reachy_managed.render_region(EXAMPLE)
    assert reachy_managed.drop_in_path() in text


@pytest.mark.filesystem  # reads the plugin sources; what they import is the property
def test_the_role_reaches_nothing_of_reachyctl() -> None:
    """Independence is what makes the comparisons above evidence rather than tautology.

    Two implementations that share a renderer agree about everything and prove
    nothing, and the playbook would then need the whole CLI — its transport, its
    terminal renderer — installed on the machine it runs from. The shared
    packages are the two that exist to be shared: the check registry and the
    setting vocabulary.
    """
    sources = sorted(PLUGINS.glob("*.py"))

    assert sources, "the filter plugins must be where the tests look for them"
    for source in sources:
        imports = [
            line
            for line in source.read_text(encoding="utf-8").splitlines()
            if _IMPORT.match(line)
        ]
        assert not imports, (
            f"{source.name} imports from reachyctl: {imports}. The role is the "
            f"second implementation of this format, not a caller of the first"
        )
