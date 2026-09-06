"""The runbook's interpreter failure, held to the failure the code produces.

`docs/ops/troubleshooting.md` quotes the message an operator sees when no
interpreter can be resolved, and the whole value of that section is that the
sentence on the page is the sentence in the terminal. A paraphrase would train a
reader to search for words the tool never prints, which is the failure mode the
repository's runbook convention exists to prevent: every step is a command
paired with output that was actually produced.

So this reads the document and requires the fenced block to be what the real
resolution says about the stock image's layout, rendered here rather than
transcribed. It is the same mechanism `test_reachyctl_managed.py` uses for the
managed drop-in and `test_checks_runbook.py` uses for the remediation strings.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Final

import pytest
from reachyctl_robot import STOCK_LAUNCHER, FakeRobot, daemon_for

from reachyctl.daemon import InterpreterResolutionError

RUNBOOK: Final = (
    Path(__file__).resolve().parents[3] / "docs" / "ops" / "troubleshooting.md"
)

# The heading the quoted failure sits under. Named rather than searched for
# anywhere in the document, so a block that moved out of this section is a red
# test rather than a quietly unchecked one.
_SECTION: Final = "\n### When the interpreter cannot be resolved\n"


def _rendered() -> str:
    """Produce the failure the tool really raises for the stock image.

    A robot whose unit starts the launcher and which has no interpreter where
    the launcher's environment says one should be. Both halves matter: the
    message names what it derived and what it deliberately did not run.

    Returns:
        The message, as an operator reads it.
    """
    daemon, _access = daemon_for(FakeRobot(exec_start=STOCK_LAUNCHER, interpreters={}))
    with pytest.raises(InterpreterResolutionError) as raised:
        asyncio.run(daemon.interpreter())
    return str(raised.value)


def _quoted() -> str:
    """Pull the fenced block out of the runbook's interpreter section.

    Returns:
        Its content, stripped of the surrounding fences and blank lines.

    """
    _, separator, after = RUNBOOK.read_text(encoding="utf-8").partition(_SECTION)
    assert separator, f"{RUNBOOK.name} no longer carries the interpreter section"
    blocks = [block for index, block in enumerate(after.split("```")) if index % 2 == 1]
    assert blocks, "the interpreter section carries no fenced block"
    return blocks[0].strip()


@pytest.mark.filesystem  # the committed runbook is the subject, so a fake would compare the document with itself
def test_the_runbook_quotes_the_failure_the_tool_actually_raises() -> None:
    """Word for word. The point of the section is that it is searchable."""
    assert _quoted() == _rendered()
