"""One document, read by `doctor --intent` and by `config --declaration`.

What is tested here is the part that is new in this change: `config` reads the
same document `doctor` asserts against, and the one place the two keys overlap is
refused rather than resolved by precedence. The rest of the parsing has its tests
beside `doctor`, which is where it was written.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from reachyctl.declaration import IDENTITY_SETTING, load_declaration, load_intent
from reachyctl.errors import ConfigurationError

if TYPE_CHECKING:
    from collections.abc import Callable

DOCUMENT: Final = Path("/declared/intent.json")


def _reader(content: str) -> Callable[[Path], str]:
    """Build a reader that hands back one document.

    Args:
        content: What the file holds.

    Returns:
        A callable shaped like the reader the loaders take.
    """

    def read(path: Path) -> str:
        """Hand back the prepared document.

        Args:
            path: Ignored.

        Returns:
            The content.
        """
        del path
        return content

    return read


def test_the_declaration_is_the_configuration_half_of_the_intent_document() -> None:
    """Two documents describing one robot are two documents that will disagree."""
    content = json.dumps(
        {
            "configuration": {"REACHY_SATELLITE_LOG_LEVEL": "info"},
            "announced_identity": "reachy-example",
        },
    )

    assert load_declaration(DOCUMENT, _reader(content)) == {
        "REACHY_SATELLITE_LOG_LEVEL": "info",
    }


def test_a_document_declaring_no_configuration_declares_nothing_to_apply() -> None:
    """Which `apply` acts on: it empties the region."""
    assert load_declaration(DOCUMENT, _reader("{}")) == {}


def test_a_document_saying_the_identity_twice_the_same_way_is_accepted() -> None:
    """Saying it twice is redundant; saying it twice differently is the problem."""
    content = json.dumps(
        {
            "configuration": {IDENTITY_SETTING: "reachy-example"},
            "announced_identity": "reachy-example",
        },
    )

    intent = load_intent(DOCUMENT, _reader(content))

    assert intent.announced_identity == "reachy-example"
    assert intent.configuration[IDENTITY_SETTING] == "reachy-example"


def test_a_document_saying_the_identity_twice_differently_is_refused() -> None:
    """It describes a robot that cannot exist.

    The failure it would otherwise produce is an apply that succeeds followed by
    a `doctor` that fails, which is an afternoon.
    """
    content = json.dumps(
        {
            "configuration": {IDENTITY_SETTING: "reachy-one"},
            "announced_identity": "reachy-two",
        },
    )

    with pytest.raises(ConfigurationError) as raised:
        load_intent(DOCUMENT, _reader(content))

    message = str(raised.value)
    assert "reachy-one" in message
    assert "reachy-two" in message
    assert "cannot exist" in message


def test_a_declaration_that_cannot_be_read_reports_the_same_way_an_intent_does() -> (
    None
):
    """One loader, so a mistake in the document reads the same for both commands."""

    def refuse(path: Path) -> str:
        """Fail to read.

        Args:
            path: Ignored.

        Returns:
            Never.

        Raises:
            OSError: Always.
        """
        del path
        raise OSError(2, "No such file or directory")

    with pytest.raises(ConfigurationError, match="could not be read"):
        load_declaration(DOCUMENT, refuse)
