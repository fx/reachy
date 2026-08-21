"""How the command layer is put together, at the seams the commands do not show.

Three things live here that the command tests cannot reach: the camera branch of
the source builder, which would otherwise need a device; the entry points; and
what `--version` answers on a checkout that was never installed. All three are
wiring rather than logic, and all three would be discovered broken by an
operator rather than by the suite if they were left unexercised.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import importlib
import sys
from importlib import metadata
from typing import TYPE_CHECKING

import pytest
from reachyctl_support import FakeCapture

from reachyctl import cli
from reachyctl.exits import ExitCode
from reachyctl.frames import CameraFrames, RecordedFrames

if TYPE_CHECKING:
    from pathlib import Path


def test_the_source_builder_opens_a_camera_when_asked_for_one() -> None:
    """With the opener injected, because no test here may require a device."""
    capture = FakeCapture(b"one")

    source = cli._source(None, 4, lambda _index: capture)

    assert isinstance(source, CameraFrames)
    assert source.description == "live frames from camera 4"
    source.close()
    assert capture.released


@pytest.mark.filesystem  # builds a directory of frames; not a unit test
def test_the_source_builder_reads_a_directory_when_asked_for_one(
    tmp_path: Path,
) -> None:
    """The other branch, over a directory this test made.

    Args:
        tmp_path: A directory to put a frame in.
    """
    (tmp_path / "frame-001.jpg").write_bytes(b"\xff\xd8 a frame")

    source = cli._source(tmp_path, None)

    assert isinstance(source, RecordedFrames)


def test_the_installed_entry_point_runs_the_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`reachyctl --version` is what an operator runs first, through this function.

    Args:
        monkeypatch: Used to set the arguments the entry point reads, because
            that is where a console script gets them from.
    """
    monkeypatch.setattr(sys, "argv", ["reachyctl", "--version"])

    with pytest.raises(SystemExit) as raised:
        cli.main()

    assert raised.value.code == ExitCode.OK


def test_the_module_entry_point_reaches_the_same_function() -> None:
    """`python -m reachyctl` and the console script are one command surface."""
    module = importlib.import_module("reachyctl.__main__")

    assert module.main is cli.main


def test_a_checkout_that_was_never_installed_still_answers_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reached through PYTHONPATH there is no metadata, and no traceback either.

    Args:
        monkeypatch: Used to remove the distribution metadata the tool reads.
    """

    def missing(name: str) -> str:
        """Report that the distribution is not installed.

        Args:
            name: What was asked for.

        Returns:
            Nothing; this always raises.

        Raises:
            metadata.PackageNotFoundError: Always.
        """
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "version", missing)

    assert "not installed" in cli._version()
