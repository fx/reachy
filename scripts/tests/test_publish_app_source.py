"""The publish helper's refusals, without a network, an account or a token.

Publishing itself cannot be tested here — there is no Hugging Face account in
this repository's development environment, which is the recorded reason the
Space is published by a person rather than by continuous integration. What can
be tested is everything that happens before anything is contacted, and that is
the part an operator meets: a missing token, a Space named something the daemon
will not find its own metadata under, a source that has drifted from the wheel
it names, and a release that does not carry that wheel yet.

The last of those reaches a URL in the real script and a supplied function here,
which is what the `Opener` protocol exists for.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import re
import urllib.error
from pathlib import Path
from typing import Any, Final

import pytest
from publish_app_source import (
    APPLICATION_MANIFEST,
    SOURCE_DIRECTORY,
    SPACE_VARIABLE,
    AppSource,
    PublishRefusalError,
    check_agrees_with_repository,
    check_release_asset,
    entry_point_name,
    read_source,
    resolve_space_id,
    resolve_token,
)

_VERSION: Final = "1.2.3"
_WHEEL: Final = (
    "https://github.com/owner/repository/releases/download/v1.2.3/"
    "reachy_mini_ha_satellite-1.2.3-py3-none-any.whl"
)
_NAME: Final = "reachy-mini-ha-satellite"


def _manifest(directory: Path, requirement: str, version: str = _VERSION) -> Path:
    """Write a source directory shaped like the committed one."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "pyproject.toml").write_text(
        "[project]\n"
        'name = "reachy-mini-ha-satellite-app-source"\n'
        f'version = "{version}"\n'
        f'dependencies = ["{requirement}"]\n',
        encoding="utf-8",
    )
    return directory


class TestTheToken:
    """Nothing is contacted without one, and the refusal says how to make one."""

    def test_either_variable_supplies_it(self) -> None:
        """`huggingface_hub` reads both names, so this reads both names."""
        assert resolve_token({"HF_TOKEN": "  secret  "}) == "secret"
        assert resolve_token({"HUGGING_FACE_HUB_TOKEN": "secret"}) == "secret"

    @pytest.mark.parametrize("environ", [{}, {"HF_TOKEN": "   "}])
    def test_an_absent_or_blank_token_is_refused_by_name(
        self,
        environ: dict[str, str],
    ) -> None:
        """A variable set to whitespace is not a token, and saying so is cheaper."""
        with pytest.raises(
            PublishRefusalError, match="HF_TOKEN or HUGGING_FACE_HUB_TOKEN"
        ):
            resolve_token(environ)


class TestTheTargetSpace:
    """The Space's name is not cosmetic: the daemon keys metadata on it."""

    def test_a_well_formed_target_resolves(self) -> None:
        """`owner/name`, with the name being the entry point's."""
        assert resolve_space_id({SPACE_VARIABLE: f" someone/{_NAME} "}, _NAME) == (
            f"someone/{_NAME}"
        )

    def test_an_absent_target_is_refused_with_the_shape_to_supply(self) -> None:
        """It is not committed, so the refusal has to say what to set."""
        with pytest.raises(PublishRefusalError, match=f"<owner>/{_NAME}"):
            resolve_space_id({}, _NAME)

    @pytest.mark.parametrize(
        "space_id",
        ["someone", f"someone/{_NAME}/extra", f"some one/{_NAME}", f"/{_NAME}"],
    )
    def test_something_that_is_not_owner_slash_name_is_refused(
        self,
        space_id: str,
    ) -> None:
        """Refused here rather than in a request made with it."""
        with pytest.raises(PublishRefusalError, match="not <owner>/<name>"):
            resolve_space_id({SPACE_VARIABLE: space_id}, _NAME)

    def test_a_space_named_anything_else_is_refused_with_the_reason(self) -> None:
        """The daemon reads back under the entry-point name, so they must match."""
        with pytest.raises(PublishRefusalError, match="entry-point name"):
            resolve_space_id({SPACE_VARIABLE: "someone/ha-satellite"}, _NAME)


class TestTheEntryPointName:
    """Read from the application's manifest, never repeated in the rule."""

    @pytest.mark.filesystem
    def test_the_committed_application_declares_exactly_one(self) -> None:
        """One entry point, and it is the name a Space has to carry."""
        assert entry_point_name(APPLICATION_MANIFEST) == _NAME

    def test_a_manifest_declaring_no_single_entry_point_is_refused(
        self,
        tmp_path: Path,
    ) -> None:
        """Two of them, or none, and there is no name for the Space to take."""
        manifest = tmp_path / "pyproject.toml"
        manifest.write_text("[project]\nname = 'x'\n", encoding="utf-8")
        with pytest.raises(
            PublishRefusalError, match="0 reachy_mini_apps entry points"
        ):
            entry_point_name(manifest)


class TestTheCommittedSource:
    """What the source declares, and what it has to agree with."""

    def test_one_requirement_naming_a_release_asset_is_read(
        self,
        tmp_path: Path,
    ) -> None:
        """The name half is dropped; the URL is what is checked and published."""
        source = read_source(_manifest(tmp_path / "source", f"{_NAME} @ {_WHEEL}"))

        assert source == AppSource(version=_VERSION, wheel_url=_WHEEL)

    def test_a_missing_source_is_refused_by_path(self, tmp_path: Path) -> None:
        """A refusal naming the path beats a traceback naming the same path."""
        with pytest.raises(PublishRefusalError, match="no application source at"):
            read_source(tmp_path / "absent")

    @pytest.mark.parametrize(
        ("requirement", "expected"),
        [
            (f"{_NAME}", "not the `<name> @ <url>` form"),
            (f"{_NAME} @ file:///tmp/wheel.whl", "not a release asset"),
        ],
    )
    def test_a_requirement_of_another_shape_is_refused(
        self,
        tmp_path: Path,
        requirement: str,
        expected: str,
    ) -> None:
        """A local path or an index lookup installs something unreleased."""
        source_directory = _manifest(tmp_path / "source", requirement)
        with pytest.raises(PublishRefusalError, match=expected):
            check_agrees_with_repository(read_source(source_directory), _VERSION)

    def test_a_source_at_another_version_is_refused(self) -> None:
        """Release automation moves both; a drifted source installs the wrong one."""
        source = AppSource(version="1.2.2", wheel_url=_WHEEL)
        with pytest.raises(
            PublishRefusalError, match=re.escape("declares version '1.2.2'")
        ):
            check_agrees_with_repository(source, _VERSION)

    def test_a_url_naming_another_version_is_refused(self) -> None:
        """The half-updated case: the project moved and the URL did not."""
        stale = _WHEEL.replace("v1.2.3", "v1.2.2")
        with pytest.raises(PublishRefusalError, match=re.escape("release tag v1.2.2")):
            check_agrees_with_repository(
                AppSource(version=_VERSION, wheel_url=stale),
                _VERSION,
            )

    def test_agreement_returns_the_one_version(self) -> None:
        """Three declarations, one answer, so a caller reports one value."""
        source = AppSource(version=_VERSION, wheel_url=_WHEEL)

        assert check_agrees_with_repository(source, _VERSION) == _VERSION

    @pytest.mark.filesystem
    def test_the_committed_source_agrees_with_this_checkout(self) -> None:
        """The one that fails when somebody edits the source and not the version."""
        from reachy_contracts import __version__

        assert check_agrees_with_repository(read_source(SOURCE_DIRECTORY), __version__)


class TestTheReleaseAsset:
    """Publishing before releasing is the mistake this catches off the robot."""

    def test_a_published_wheel_passes(self) -> None:
        """One request, and nothing is read of it."""
        asked: list[str] = []

        def opener(url: str) -> int:
            asked.append(url)
            return 200

        check_release_asset(_WHEEL, opener)

        assert asked == [_WHEEL]

    def test_a_release_without_the_wheel_is_refused_in_order(self) -> None:
        """The refusal says which way round the two commands go.

        `HTTPError`'s `hdrs` parameter is annotated as an
        `email.message.Message` and accepts a mapping at run time, which is
        what the suppression below is for.
        """

        def opener(_url: str) -> int:
            headers: Any = {}
            raise urllib.error.HTTPError(_url, 404, "Not Found", headers, None)

        with pytest.raises(
            PublishRefusalError, match="Publish the Space after the release"
        ):
            check_release_asset(_WHEEL, opener)

    def test_an_unreachable_release_is_refused_rather_than_raising(self) -> None:
        """A publish with no network says so instead of a traceback."""

        def opener(_url: str) -> int:
            raise OSError("no route to host")

        with pytest.raises(PublishRefusalError, match="could not be reached"):
            check_release_asset(_WHEEL, opener)

    def test_any_other_status_is_refused(self) -> None:
        """Not 200 is not the asset, whatever else it is."""

        def opener(_url: str) -> int:
            return 302

        with pytest.raises(PublishRefusalError, match="answered 302"):
            check_release_asset(_WHEEL, opener)
