"""The thing `deploy` deploys: a wheel, read for what it actually claims to be.

`deploy` is defined over a wheel rather than over the satellite, and the change
document is explicit about why: hard-coding the application would make this
change depend on the one that writes it, inverting the build order and removing
the reason the tool is sequenced early — that it is the tooling used *during*
the rewrite. A wheel-shaped command is also exercisable against a trivial
fixture wheel with no application present at all, which is how the deploy
sequence is tested here.

**The version comes out of the archive, not out of the file name.** A wheel's
name is a convention and the metadata inside it is the thing the installer
reads, so a file renamed by hand — which is exactly what happens when somebody
copies a build around — would otherwise make this tool verify against a version
nothing was ever going to install. Both are read, and a disagreement is refused
rather than resolved by precedence: neither answer is trustworthy once they
differ.

Reading an archive is input, so it is one function (`read_wheel`) and everything
that decides anything is written against bytes.
"""

from __future__ import annotations

import io
import re
import subprocess
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from typing import TYPE_CHECKING, Final

from reachyctl.errors import ConfigurationError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

__all__ = [
    "Wheel",
    "WheelError",
    "build_wheel",
    "describe_wheel",
    "normalise",
    "read_wheel",
    "spawn_build",
]

# `{distribution}-{version}(-{build})?-{python}-{abi}-{platform}.whl`. Only the
# first two fields are read here; the rest are matched so that a file which is
# not a wheel name at all is refused rather than parsed into nonsense.
_WHEEL_NAME: Final = re.compile(
    r"^(?P<distribution>[^-]+)-(?P<version>[^-]+)"
    r"(-[0-9][^-]*)?-[^-]+-[^-]+-[^-]+\.whl$",
)

_DIST_INFO_METADATA: Final = re.compile(r"^[^/]+\.dist-info/METADATA$")

# PEP 503's normalisation, which is what makes `Reachy_Mini` and `reachy-mini`
# the same distribution — and what the file name and the metadata have to agree
# about, since the file name spells a hyphen as an underscore.
_SEPARATORS: Final = re.compile(r"[-_.]+")


class WheelError(ConfigurationError):
    """The wheel is not one this tool can deploy.

    A configuration failure rather than a diagnosis: the operator named a file,
    and the file is not what it was said to be. Nothing has been asked of the
    robot.
    """


def normalise(name: str) -> str:
    """Normalise a distribution name the way packaging does.

    Args:
        name: The name as it was written.

    Returns:
        The normalised form, so two spellings of one distribution compare
        equal.
    """
    return _SEPARATORS.sub("-", name).lower()


@dataclass(frozen=True, slots=True, kw_only=True)
class Wheel:
    """One wheel, and what it says it is.

    Attributes:
        file_name: The archive's file name, which is also the name it is given
            on the robot.
        distribution: The normalised distribution name from its metadata.
        version: The version from its metadata. This is the version a deploy
            verifies is running afterwards.
        content: The archive's bytes, read once and transferred whole. The
            change document's open question about incremental transfer resolves
            to a whole transfer, so there is one thing to hold and one thing to
            send.
    """

    file_name: str
    distribution: str
    version: str
    content: bytes

    @property
    def size_bytes(self) -> int:
        """How large the archive is.

        Returns:
            Its size in bytes, which is what a progress line reports before a
            transfer over a link measured in hundreds of milliseconds.
        """
        return len(self.content)

    def describe(self) -> str:
        """Say what this wheel is, in one line.

        Returns:
            The distribution, its version and its size.
        """
        return (
            f"{self.distribution} {self.version} ({self.size_bytes} bytes, "
            f"{self.file_name})"
        )


def describe_wheel(file_name: str, content: bytes) -> Wheel:
    """Read what a wheel claims to be, from its name and from its metadata.

    Args:
        file_name: The archive's file name.
        content: The archive's bytes.

    Returns:
        What it is.

    Raises:
        WheelError: If the name is not a wheel name, if the archive is not
            readable, if it carries no `.dist-info/METADATA`, if that metadata
            names no distribution or version, or if the name and the metadata
            disagree.
    """
    named = _WHEEL_NAME.match(file_name)
    if named is None:
        message = (
            f"{file_name} is not a wheel file name; a wheel is named "
            f"'<distribution>-<version>-<python>-<abi>-<platform>.whl'"
        )
        raise WheelError(message)
    metadata = _metadata_of(file_name, content)
    distribution = metadata["distribution"]
    version = metadata["version"]
    if normalise(named.group("distribution")) != normalise(distribution):
        message = (
            f"{file_name} is named for {named.group('distribution')!r} and its "
            f"metadata says {distribution!r}; the two must agree, because the "
            f"metadata is what the robot installs and the name is what a person "
            f"reads"
        )
        raise WheelError(message)
    if named.group("version") != version:
        message = (
            f"{file_name} is named for version {named.group('version')} and its "
            f"metadata says {version}; a deploy verifies the version that was "
            f"installed, so it cannot start from two of them"
        )
        raise WheelError(message)
    return Wheel(
        file_name=file_name,
        distribution=normalise(distribution),
        version=version,
        content=content,
    )


def _metadata_of(file_name: str, content: bytes) -> dict[str, str]:
    """Read the distribution and version out of a wheel's metadata.

    Args:
        file_name: The archive's file name, for the messages.
        content: The archive's bytes.

    Returns:
        The `distribution` and `version` the metadata declares.

    Raises:
        WheelError: If the archive cannot be opened, carries no metadata, or
            carries metadata that names neither.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = [
                name for name in archive.namelist() if _DIST_INFO_METADATA.match(name)
            ]
            if len(names) != 1:
                message = (
                    f"{file_name} carries {len(names)} '.dist-info/METADATA' "
                    f"entries; a wheel carries exactly one"
                )
                raise WheelError(message)
            raw = archive.read(names[0])
    except zipfile.BadZipFile as error:
        message = f"{file_name} is not a readable zip archive: {error}"
        raise WheelError(message) from error
    parsed = BytesParser().parsebytes(raw)
    distribution = parsed.get("Name", "")
    version = parsed.get("Version", "")
    if not distribution or not version:
        message = (
            f"{file_name} carries metadata with no "
            f"{'Name' if not distribution else 'Version'} field"
        )
        raise WheelError(message)
    return {"distribution": distribution, "version": version}


def read_wheel(path: Path) -> Wheel:
    """Read a wheel off this machine.

    Args:
        path: Where it is.

    Returns:
        What it is.

    Raises:
        WheelError: If it cannot be read, or is not a wheel this tool can
            deploy. The reason a file could not be opened is the operating
            system's and is safe to quote; its contents are not.
    """
    try:
        content = path.read_bytes()
    except OSError as error:
        reason = error.strerror or type(error).__name__
        message = f"the wheel {path} could not be read: {reason}"
        raise WheelError(message) from error
    return describe_wheel(path.name, content)


def spawn_build(command: Sequence[str]) -> int:
    """Run a build on this machine and hand back its exit status.

    The build's own output goes to this process's streams rather than being
    captured, because a build that failed is read by a person and the reason is
    somewhere in the middle of it — and because a build of any size otherwise
    looks like the tool having hung.

    Args:
        command: The build command.

    Returns:
        Its exit status.
    """
    # S603: the argv is `build_wheel`'s own literal list with a member name and
    # an output directory appended. No shell is involved and nothing is
    # interpolated into a string, so there is no injection surface here.
    # `uv` is resolved on PATH deliberately: the build has to use the same `uv`
    # the contributor and the release workflow use, and pinning an absolute path
    # would make a deploy build with whatever this machine had when the tool was
    # installed.
    return subprocess.run(command, check=False).returncode  # noqa: S603


def build_wheel(
    member: str,
    output: Path,
    run: Callable[[Sequence[str]], int] = spawn_build,
) -> Path:
    """Build one workspace member's wheel.

    Building is the one thing a deploy does on this machine rather than on the
    robot, and it is reached through a callable so that everything above it is
    exercisable without a build. The builder is `uv`, which is what the rest of
    this repository builds with, so the wheel a deploy sends is the wheel a
    release publishes rather than one produced by a second path.

    Args:
        member: The workspace member's distribution name.
        output: The directory to build into.
        run: How to run the build. Returns the process's exit status. Injected
            so that everything above this function is exercisable without one.

    Returns:
        Where the wheel landed.

    Raises:
        WheelError: If the build failed, or produced no wheel or more than one.
    """
    status = run(
        ["uv", "build", "--package", member, "--wheel", "--out-dir", str(output)],
    )
    if status != 0:
        message = f"building {member} failed with status {status}; its output is above"
        raise WheelError(message)
    built = sorted(output.glob("*.whl"))
    if len(built) != 1:
        message = (
            f"building {member} left {len(built)} wheel(s) in {output}; exactly "
            f"one is expected, so there is one thing to deploy and one version "
            f"to verify"
        )
        raise WheelError(message)
    return built[0]
