"""What `deploy` will accept as a thing to deploy, and what it will not.

The wheel is where a deploy's idea of "the intended version" comes from, so
everything that could make that idea wrong is refused here rather than
discovered after the robot has been restarted. The fixture wheel is built in
memory by `reachyctl_fixture_wheel`, so these are unit tests that perform no
input.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import io
import zipfile
from typing import TYPE_CHECKING

import pytest
from reachyctl_fixture_wheel import (
    FIXTURE_DISTRIBUTION,
    FIXTURE_VERSION,
    fixture_wheel,
)

from reachyctl.exits import ExitCode
from reachyctl.wheels import (
    WheelError,
    build_wheel,
    describe_wheel,
    normalise,
    read_wheel,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


def test_a_fixture_wheel_reads_back_as_what_it_says_it_is() -> None:
    """No application present at all, which is the point of deploying a wheel."""
    name, content = fixture_wheel()

    wheel = describe_wheel(name, content)

    assert wheel.distribution == FIXTURE_DISTRIBUTION
    assert wheel.version == FIXTURE_VERSION
    assert wheel.size_bytes == len(content)
    assert wheel.file_name == name
    assert FIXTURE_VERSION in wheel.describe()


def test_a_file_that_is_not_named_like_a_wheel_is_refused() -> None:
    """Before the archive is opened, because the name is the cheapest check."""
    _, content = fixture_wheel()

    with pytest.raises(WheelError, match="not a wheel file name"):
        describe_wheel("reachy-deploy-fixture.tar.gz", content)


def test_a_file_that_is_not_a_zip_is_refused() -> None:
    """A truncated download is the ordinary way this happens."""
    with pytest.raises(WheelError, match="not a readable zip"):
        describe_wheel("thing-1.0-py3-none-any.whl", b"not a zip at all")


def test_a_wheel_with_no_metadata_is_refused() -> None:
    """There is nothing to verify against, so there is nothing to deploy."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("thing/__init__.py", b"")

    with pytest.raises(WheelError, match=r"0 '\.dist-info/METADATA'"):
        describe_wheel("thing-1.0-py3-none-any.whl", buffer.getvalue())


def test_a_wheel_whose_metadata_names_no_version_is_refused() -> None:
    """A deploy ends by asserting a version, so it cannot start without one."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("thing-1.0.dist-info/METADATA", b"Name: thing\n\n")

    with pytest.raises(WheelError, match="no Version field"):
        describe_wheel("thing-1.0-py3-none-any.whl", buffer.getvalue())


def test_a_name_and_a_metadata_that_disagree_about_the_version_are_refused() -> None:
    """A file renamed by hand would otherwise be verified against the wrong version."""
    name, content = fixture_wheel(metadata_version="9.9.9")

    with pytest.raises(WheelError, match="cannot start from two of them"):
        describe_wheel(name, content)


def test_a_name_and_a_metadata_that_disagree_about_the_distribution_are_refused() -> (
    None
):
    """The metadata is what installs; the name is what a person reads."""
    name, content = fixture_wheel(metadata_name="something-else")

    with pytest.raises(WheelError, match="must agree"):
        describe_wheel(name, content)


def test_a_distribution_spelled_with_underscores_still_matches() -> None:
    """A wheel's file name always spells a hyphen as an underscore."""
    name, content = fixture_wheel(metadata_name="Reachy_Deploy.Fixture")

    assert describe_wheel(name, content).distribution == FIXTURE_DISTRIBUTION
    assert normalise("Reachy_Deploy.Fixture") == FIXTURE_DISTRIBUTION


def test_a_wheel_that_is_refused_costs_a_configuration_status() -> None:
    """Nothing was asked of the robot, so a script must not read it as a diagnosis."""
    assert WheelError("").exit_code is ExitCode.CONFIGURATION


@pytest.mark.filesystem  # builds into a real directory; not a unit test
def test_building_a_member_hands_back_the_one_wheel_it_produced(
    tmp_path: Path,
) -> None:
    """With the builder injected, because a unit of this suite runs no build.

    Args:
        tmp_path: Where the pretended build writes.
    """
    name, content = fixture_wheel()
    calls: list[Sequence[str]] = []

    def run(command: Sequence[str]) -> int:
        """Pretend to build, and write what a build would have written.

        Args:
            command: What would have been run.

        Returns:
            Success.
        """
        calls.append(command)
        (tmp_path / name).write_bytes(content)
        return 0

    built = build_wheel("reachyctl", tmp_path, run)

    assert built == tmp_path / name
    assert calls == [
        [
            "uv",
            "build",
            "--package",
            "reachyctl",
            "--wheel",
            "--out-dir",
            str(tmp_path),
        ],
    ]


@pytest.mark.filesystem  # builds into a real directory; not a unit test
def test_a_build_that_failed_is_refused_rather_than_producing_nothing(
    tmp_path: Path,
) -> None:
    """The status is named, because the reason is in the build's own output.

    Args:
        tmp_path: Where the pretended build would have written.
    """
    with pytest.raises(WheelError, match="failed with status 2"):
        build_wheel("reachyctl", tmp_path, lambda _command: 2)


@pytest.mark.filesystem  # builds into a real directory; not a unit test
def test_a_build_that_left_two_wheels_is_refused(tmp_path: Path) -> None:
    """There would be two versions to verify, and no way to choose.

    Args:
        tmp_path: Where the pretended build writes.
    """

    def run(_command: Sequence[str]) -> int:
        """Write two wheels.

        Args:
            _command: Ignored.

        Returns:
            Success.
        """
        for version in ("1.2.3", "1.2.4"):
            name, content = fixture_wheel(version=version)
            (tmp_path / name).write_bytes(content)
        return 0

    with pytest.raises(WheelError, match="left 2 wheel"):
        build_wheel("reachyctl", tmp_path, run)


@pytest.mark.filesystem  # reads a real file, which is what `read_wheel` is for
def test_reading_a_wheel_off_this_machine_reads_what_is_in_it(tmp_path: Path) -> None:
    """The one function here that performs input, exercised over a real file.

    Args:
        tmp_path: Where the wheel is written.
    """
    name, content = fixture_wheel()
    (tmp_path / name).write_bytes(content)

    wheel = read_wheel(tmp_path / name)

    assert wheel.version == FIXTURE_VERSION
    assert wheel.content == content


@pytest.mark.filesystem  # names a path that is not there; not a unit test
def test_a_wheel_that_cannot_be_read_says_why(tmp_path: Path) -> None:
    """The operating system's reason is safe to quote; a file's contents are not.

    Args:
        tmp_path: A directory with nothing in it.
    """
    with pytest.raises(WheelError, match="could not be read"):
        read_wheel(tmp_path / "absent-1.0-py3-none-any.whl")
