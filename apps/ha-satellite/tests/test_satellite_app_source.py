"""The published application source, checked against the wheel it installs.

`apps/ha-satellite/app-source/` is what the Reachy Mini daemon downloads and
installs when an operator installs this satellite from the robot's own surfaces.
It carries no code: it names one released wheel, and the daemon discovers the
application through the entry point that wheel declares. Three things about it
can be wrong in a way nobody notices until a robot is in front of them, and each
has a test here.

**It can name a version this repository has not released.** The version appears
in the source's own project metadata, in the release tag inside the wheel's URL,
in that wheel's file name, and in the Space's own README. All four move with
every other version in the repository, and release automation is what moves
them.

**That automation can move some of them.** release-please's generic updater is
line-based and replaces at most one version per line, and its pattern treats
what follows a `-` as a pre-release tag — so `…-0.2.0-py3-none-any.whl` on one
line becomes `…-0.3.0-none-any.whl`. `TestARelease` replays that updater against
the committed files and fails if the layout stops surviving it, which is the
only way to find out short of merging a release pull request.

**It can carry something belonging to somebody's installation.** It is published
to a public Space, so `TestWhatItCarries` holds it to the same rule the rest of
this repository is held to.

They are contract tests rather than unit tests and each says so with
`@pytest.mark.filesystem`, for the reason the root `AGENTS.md` gives: the bytes
on disk are the thing under test.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Final

import pytest

from reachy_contracts import __version__

_REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[3]
_APPLICATION_ROOT: Final = Path(__file__).resolve().parents[1]
_SOURCE: Final = _APPLICATION_ROOT / "app-source"

_APPLICATION_MANIFEST: Final = _APPLICATION_ROOT / "pyproject.toml"
_SOURCE_MANIFEST: Final = _SOURCE / "pyproject.toml"
_SOURCE_README: Final = _SOURCE / "README.md"
_SOURCE_PAGE: Final = _SOURCE / "index.html"
_RELEASE_PLEASE: Final = _REPOSITORY_ROOT / "release-please-config.json"

# What `just wheels` builds and the release workflow attaches, spelled the way a
# wheel's file name spells a distribution: underscores, and the pure-Python tag.
_WHEEL_NAME: Final = "reachy_mini_ha_satellite-{version}-py3-none-any.whl"

# A GitHub release asset. The owner and repository are deliberately not pinned —
# what matters is that the tag and the wheel both name the version, which is
# what makes the source installable from a release rather than from a branch,
# an index or somebody's machine.
_RELEASE_ASSET: Final = re.compile(
    r"\Ahttps://github\.com/[^/\s]+/[^/\s]+/releases/download/"
    r"v(?P<tag>[^/\s]+)/(?P<wheel>[^/\s]+)\Z",
)

# release-please's generic updater, reproduced from `src/updaters/generic.ts`.
# The patterns are upstream's, and the two that matter are the pre-release group
# in `_GENERIC_VERSION` — which is what eats `-py3` — and the single, non-global
# replacement each line gets.
_GENERIC_VERSION: Final = re.compile(
    r"(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(-(?P<pre_release>[\w.]+))?(\+(?P<build>[-\w.]+))?",
)
_GENERIC_INLINE: Final = re.compile(
    r"x-release-please-(?:major|minor|patch|version-date|version|date)",
)
_GENERIC_BLOCK_START: Final = re.compile(
    r"x-release-please-start-(?:major|minor|patch|version-date|version|date)",
)
_GENERIC_BLOCK_END: Final = re.compile(r"x-release-please-end")

# A version no release will ever derive, so a leftover is unambiguous.
_NEXT_VERSION: Final = "97.98.99"

# Every host the published source is allowed to point a reader at. It is
# published to a public Space and read before an install, so a link in it is
# part of what an operator is being asked to trust.
_PERMITTED_HOSTS: Final = frozenset({"github.com"})

_IPV4: Final = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_URL: Final = re.compile(r"https?://(?P<host>[^/\s)\"'>]+)")
_USERINFO: Final = re.compile(r"://[^/\s]*@")


def _files() -> list[Path]:
    """Everything published, which is everything in the directory."""
    return sorted(path for path in _SOURCE.rglob("*") if path.is_file())


def _source_requirement() -> str:
    """The source's one requirement."""
    declared = tomllib.loads(_SOURCE_MANIFEST.read_text(encoding="utf-8"))
    requirements = declared["project"]["dependencies"]
    assert len(requirements) == 1, requirements
    return str(requirements[0])


def _apply_release_please(content: str, version: str) -> str:
    """Rewrite versions the way release-please's generic updater does.

    Line-based, one replacement per line, inline markers taking precedence over
    an open block — all three are upstream's behaviour and all three are why the
    committed files are laid out the way they are.
    """
    updated: list[str] = []
    in_block = False
    for line in content.split("\n"):
        if _GENERIC_INLINE.search(line):
            updated.append(_GENERIC_VERSION.sub(version, line, count=1))
        elif in_block:
            updated.append(_GENERIC_VERSION.sub(version, line, count=1))
            if _GENERIC_BLOCK_END.search(line):
                in_block = False
        else:
            if _GENERIC_BLOCK_START.search(line):
                in_block = True
            updated.append(line)
    return "\n".join(updated)


class TestWhatItInstalls:
    """One released wheel, at this repository's version, and nothing else."""

    @pytest.mark.filesystem
    def test_the_source_carries_this_repository_s_version(self) -> None:
        """One version for the whole repository, and this is one of its places."""
        declared = tomllib.loads(_SOURCE_MANIFEST.read_text(encoding="utf-8"))

        assert declared["project"]["version"] == __version__

    @pytest.mark.filesystem
    def test_it_requires_the_release_asset_for_that_version(self) -> None:
        """The tag and the wheel both name it, so neither can drift alone."""
        name, _, url = _source_requirement().partition(" @ ")
        asset = _RELEASE_ASSET.fullmatch(url)

        assert name == "reachy-mini-ha-satellite"
        assert asset is not None, url
        assert asset["tag"] == __version__
        assert asset["wheel"] == _WHEEL_NAME.format(version=__version__)

    @pytest.mark.filesystem
    def test_the_wheel_it_names_is_the_one_this_member_builds(self) -> None:
        """A release asset nothing produces is a source that installs nothing."""
        application = tomllib.loads(_APPLICATION_MANIFEST.read_text(encoding="utf-8"))
        distribution = application["project"]["name"].replace("-", "_")

        assert _WHEEL_NAME.format(version=__version__).startswith(f"{distribution}-")

    @pytest.mark.filesystem
    def test_it_ships_no_package_of_its_own(self) -> None:
        """It is metadata. A package here would be a second copy of the code."""
        declared = tomllib.loads(_SOURCE_MANIFEST.read_text(encoding="utf-8"))

        assert declared["tool"]["setuptools"]["packages"] == []
        assert "optional-dependencies" not in declared["project"]

    #:= docs/specs/stock-robot-installation/index.md#req-104-the-application-installs-through-the-daemon-s-own-path
    #:% The satellite MUST be installable onto an unmodified robot through the daemon's
    #:% own application-installation path, without copying files onto the robot, opening
    #:% a shell on it, or editing any file its image ships.
    @pytest.mark.filesystem
    def test_the_daemon_can_install_the_directory_as_it_stands(self) -> None:
        """What the daemon's installer needs of a downloaded Space directory.

        It runs `uv pip install <the directory it downloaded>`, falling back to
        `pip`, and warns and gives up when there is neither a `pyproject.toml`
        nor a `setup.py` in the root. Then it enumerates the
        `reachy_mini_apps` entry points of the environment it installed into.
        So: a project file in the root, a build backend that needs nothing from
        this repository, and a requirement whose wheel carries the entry point.
        """
        declared = tomllib.loads(_SOURCE_MANIFEST.read_text(encoding="utf-8"))
        application = tomllib.loads(_APPLICATION_MANIFEST.read_text(encoding="utf-8"))

        assert _SOURCE_MANIFEST.parent == _SOURCE
        assert declared["build-system"]["build-backend"] == "setuptools.build_meta"
        assert list(application["project"]["entry-points"]["reachy_mini_apps"]) == [
            "reachy-mini-ha-satellite",
        ]


class TestARelease:
    """The four versions move together, or the release ships a broken source."""

    @pytest.mark.filesystem
    @pytest.mark.parametrize("name", ["pyproject.toml", "README.md"])
    def test_every_version_the_file_names_is_rewritten(self, name: str) -> None:
        """A leftover is a source naming a wheel the release did not build."""
        content = (_SOURCE / name).read_text(encoding="utf-8")

        updated = _apply_release_please(content, _NEXT_VERSION)

        assert __version__ not in updated, updated
        assert _NEXT_VERSION in updated

    @pytest.mark.filesystem
    def test_the_rewritten_manifest_still_names_one_release_asset(self) -> None:
        """The failure this rules out rewrote the wheel's platform tag away."""
        content = _SOURCE_MANIFEST.read_text(encoding="utf-8")

        declared = tomllib.loads(_apply_release_please(content, _NEXT_VERSION))

        requirements = declared["project"]["dependencies"]
        assert len(requirements) == 1
        _, _, url = str(requirements[0]).partition(" @ ")
        asset = _RELEASE_ASSET.fullmatch(url)
        assert asset is not None, url
        assert asset["tag"] == _NEXT_VERSION
        assert asset["wheel"] == _WHEEL_NAME.format(version=_NEXT_VERSION)
        assert declared["project"]["version"] == _NEXT_VERSION

    @pytest.mark.filesystem
    def test_a_file_naming_no_version_is_left_alone(self) -> None:
        """The page carries no version precisely so nothing has to update it."""
        content = _SOURCE_PAGE.read_text(encoding="utf-8")

        assert _apply_release_please(content, _NEXT_VERSION) == content

    @pytest.mark.filesystem
    def test_release_automation_is_told_about_both_files(self) -> None:
        """A version release-please does not know about ships whatever it was."""
        configured = json.loads(_RELEASE_PLEASE.read_text(encoding="utf-8"))
        extra_files = configured["packages"]["."]["extra-files"]
        generic = {
            entry["path"]
            for entry in extra_files
            if isinstance(entry, dict) and entry.get("type") == "generic"
        }

        for path in (_SOURCE_MANIFEST, _SOURCE_README):
            assert str(path.relative_to(_REPOSITORY_ROOT)) in generic


class TestWhatItCarries:
    """Published to a public Space, and read before anybody installs it."""

    #:= docs/specs/stock-robot-installation/index.md#req-104-the-application-installs-through-the-daemon-s-own-path
    #:% The satellite MUST be installable onto an unmodified robot through the daemon's
    #:% own application-installation path, without copying files onto the robot, opening
    #:% a shell on it, or editing any file its image ships.
    @pytest.mark.filesystem
    def test_it_carries_no_credential_address_or_identity(self) -> None:
        """REQ-104's third scenario, and the repository's own standing rule."""
        for path in _files():
            content = path.read_text(encoding="utf-8")

            assert not _IPV4.search(content), path
            assert not _USERINFO.search(content), path
            assert "ws://" not in content, path
            assert "wss://" not in content, path
            assert "REACHY_SATELLITE_" not in content, path
            hosts = {match["host"] for match in _URL.finditer(content)}
            assert hosts <= _PERMITTED_HOSTS, (path, hosts)

    @pytest.mark.filesystem
    def test_the_space_card_is_what_hugging_face_reads(self) -> None:
        """`sdk` and the two tags are what make it a Reachy Mini application."""
        content = _SOURCE_README.read_text(encoding="utf-8")
        _, _, rest = content.partition("---\n")
        front_matter, _, _ = rest.partition("\n---\n")
        declared = front_matter.split("\n")

        assert content.startswith("---\n")
        assert "sdk: static" in declared
        assert "  - reachy_mini" in declared
        assert "  - reachy_mini_python_app" in declared

    @pytest.mark.filesystem
    def test_the_operator_is_told_that_nothing_is_announced_yet(self) -> None:
        """The one hazard a first-time installation has to meet before it bites."""
        content = _SOURCE_README.read_text(encoding="utf-8")

        assert "Nothing is announced to Home Assistant until" in content
