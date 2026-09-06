"""The publish helper's refusals, without a network, an account or a token.

Publishing itself cannot be tested here — there is no Hugging Face account in
this repository's development environment, which is the recorded reason the
Space is published by a person rather than by continuous integration. What can
be tested is every refusal, and that is the part an operator meets: a missing
token, a Space named something the daemon will not find its own metadata under,
a source that has drifted from the wheel it names, a source that is not what is
committed, and a release that does not carry that wheel yet.

All but one are decided locally, and two of those from git's own output, handed
in as a string so no test runs git. The remaining one asks the release, which in
the real script is a `HEAD` request and here is a supplied function — that is
what the `Opener` protocol exists for, and it is the only reason the script has
one. The one thing a supplied function cannot see is what happens to the
redirect GitHub answers a release asset with, so `TestFollowingTheRedirect` asks
the redirect handler directly.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import io
import re
import urllib.error
import urllib.request
from http.client import HTTPMessage
from pathlib import Path
from typing import Any, Final

import pytest
from publish_app_source import (
    APPLICATION_MANIFEST,
    SOURCE_DIRECTORY,
    SPACE_VARIABLE,
    AppSource,
    KeepHeadOnRedirect,
    PublishRefusalError,
    check_agrees_with_repository,
    check_release_asset,
    committed_names,
    entry_point_name,
    read_source,
    repository_from_remote,
    resolve_space_id,
    resolve_token,
    uncommitted_changes,
)
from pyfakefs.fake_filesystem import FakeFilesystem

_VERSION: Final = "1.2.3"
_WHEEL: Final = (
    "https://github.com/owner/repository/releases/download/v1.2.3/"
    "reachy_mini_ha_satellite-1.2.3-py3-none-any.whl"
)
_NAME: Final = "reachy-mini-ha-satellite"

# The repository `_WHEEL` above is a release asset of. Every check that judges
# the wheel is told which repository this checkout publishes from, because a
# version is not an identity.
_REPOSITORY: Final = "owner/repository"

# Where the scaffolded sources below live. It is an in-memory filesystem, so any
# absolute path works and none of this reaches a disk — which is why these are
# ordinary unit tests and carry no `filesystem` marker. The bytes here are
# scaffolding rather than a contract; the two tests that read the COMMITTED
# source are the marked ones.
_FAKE_ROOT: Final = Path("/publish-app-source-tests")

# The committed source's own path, used where a test judges git output about it
# rather than reading it. `SOURCE_DIRECTORY` is the same path; this spelling is
# what makes the expected relative names in those tests readable.
_SOURCE: Final = SOURCE_DIRECTORY


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
        fs: FakeFilesystem,
    ) -> None:
        """Two of them, or none, and there is no name for the Space to take."""
        fs.create_dir(_FAKE_ROOT)
        manifest = _FAKE_ROOT / "pyproject.toml"
        manifest.write_text("[project]\nname = 'x'\n", encoding="utf-8")
        with pytest.raises(
            PublishRefusalError, match="0 reachy_mini_apps entry points"
        ):
            entry_point_name(manifest)


class TestTheCommittedSource:
    """What the source declares, and what it has to agree with."""

    def test_one_requirement_naming_a_release_asset_is_read(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """The name half is dropped; the URL is what is checked and published."""
        fs.create_dir(_FAKE_ROOT)

        source = read_source(_manifest(_FAKE_ROOT / "source", f"{_NAME} @ {_WHEEL}"))

        assert source == AppSource(version=_VERSION, wheel_url=_WHEEL)

    def test_a_missing_source_is_refused_by_path(self, fs: FakeFilesystem) -> None:
        """A refusal naming the path beats a traceback naming the same path."""
        fs.create_dir(_FAKE_ROOT)

        with pytest.raises(PublishRefusalError, match="no application source at"):
            read_source(_FAKE_ROOT / "absent")

    @pytest.mark.parametrize(
        ("requirement", "expected"),
        [
            (f"{_NAME}", "not the `<name> @ <url>` form"),
            (f"{_NAME} @ file:///tmp/wheel.whl", "not a release asset"),
        ],
    )
    def test_a_requirement_of_another_shape_is_refused(
        self,
        fs: FakeFilesystem,
        requirement: str,
        expected: str,
    ) -> None:
        """A local path or an index lookup installs something unreleased."""
        fs.create_dir(_FAKE_ROOT)
        source_directory = _manifest(_FAKE_ROOT / "source", requirement)
        with pytest.raises(PublishRefusalError, match=expected):
            check_agrees_with_repository(
                read_source(source_directory), _VERSION, _REPOSITORY
            )

    def test_a_source_at_another_version_is_refused(self) -> None:
        """Release automation moves both; a drifted source installs the wrong one."""
        source = AppSource(version="1.2.2", wheel_url=_WHEEL)
        with pytest.raises(
            PublishRefusalError, match=re.escape("declares version '1.2.2'")
        ):
            check_agrees_with_repository(source, _VERSION, _REPOSITORY)

    def test_a_url_naming_another_version_is_refused(self) -> None:
        """The half-updated case: the project moved and the URL did not."""
        stale = _WHEEL.replace("v1.2.3", "v1.2.2")
        with pytest.raises(PublishRefusalError, match=re.escape("release tag v1.2.2")):
            check_agrees_with_repository(
                AppSource(version=_VERSION, wheel_url=stale),
                _VERSION,
                _REPOSITORY,
            )

    def test_agreement_returns_the_one_version(self) -> None:
        """Three declarations, one answer, so a caller reports one value."""
        source = AppSource(version=_VERSION, wheel_url=_WHEEL)

        assert check_agrees_with_repository(source, _VERSION, _REPOSITORY) == _VERSION

    @pytest.mark.filesystem
    def test_the_committed_source_agrees_with_this_checkout(self) -> None:
        """The one that fails when somebody edits the source and not the version.

        The repository half is taken from the source's own URL on purpose. What
        it has to equal is the checkout's `origin` remote, which is a property
        of the machine rather than of the tree, so comparing them belongs in
        `TestTheReleaseOrigin` and at publish time. This test is about the
        version.
        """
        from reachy_contracts import __version__

        source = read_source(SOURCE_DIRECTORY)
        published_from = source.wheel_url.removeprefix("https://github.com/").split(
            "/releases/",
        )[0]

        assert check_agrees_with_repository(source, __version__, published_from)


class TestTheReleaseOrigin:
    """A version is not an identity, so the wheel's repository is checked too."""

    # The SSH spelling of a git remote puts a user and a host either side of an
    # `@`, so the leak scanner reads it as an e-mail address. Every value below
    # is the documentation placeholder `owner/repository`, and the user half is
    # git's own fixed account name rather than anybody's, so each such line
    # carries the inline marker where a reviewer reads it.
    @pytest.mark.parametrize(
        "remote",
        [
            "git@github.com:owner/repository.git",  # leak-scan:allow
            "git@github.com:owner/repository",  # leak-scan:allow
            "https://github.com/owner/repository.git",
            "https://github.com/owner/repository/",
            "ssh://git@github.com/owner/repository.git",  # leak-scan:allow
            "  https://github.com/owner/repository\n",
        ],
    )
    def test_every_spelling_of_the_remote_yields_the_repository(
        self,
        remote: str,
    ) -> None:
        """Derived from the checkout rather than written down in two places."""
        assert repository_from_remote(remote) == _REPOSITORY

    @pytest.mark.parametrize(
        "remote",
        [
            "",
            "git@gitlab.com:owner/repository.git",  # leak-scan:allow  # as above
            "/srv/git/repository.git",
        ],
    )
    def test_a_remote_it_cannot_read_is_refused(self, remote: str) -> None:
        """Better than guessing at a repository the wheel then has to match."""
        with pytest.raises(PublishRefusalError, match="not a GitHub repository"):
            repository_from_remote(remote)

    def test_a_wheel_from_another_repository_is_refused(self) -> None:
        """The one this exists for: somebody else's release under the same tag."""
        source = AppSource(version=_VERSION, wheel_url=_WHEEL)

        with pytest.raises(PublishRefusalError, match="somebody else's code"):
            check_agrees_with_repository(source, _VERSION, "someone/elsewhere")


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


class TestPublishingWhatIsCommitted:
    """The claim the whole route rests on: the Space is reviewable here.

    Both halves are judged from git's own output, handed in as a string, so
    neither test runs git or touches a repository.
    """

    def test_a_clean_source_passes(self) -> None:
        """`git status --porcelain` says nothing when nothing has changed."""
        uncommitted_changes("", _SOURCE)

    @pytest.mark.parametrize(
        "status",
        [" M apps/ha-satellite/app-source/pyproject.toml\n", "A  x\nD  y\n"],
    )
    def test_anything_uncommitted_is_refused_with_what_it_was(
        self,
        status: str,
    ) -> None:
        """A modification, an addition and a deletion are all the same answer."""
        with pytest.raises(PublishRefusalError, match="uncommitted changes"):
            uncommitted_changes(status, _SOURCE)

    def test_the_upload_is_restricted_to_what_git_tracks(self) -> None:
        """`upload_folder` reads no `.gitignore`, so this is what stops a leak."""
        listed = (
            "apps/ha-satellite/app-source/pyproject.toml\0"
            "apps/ha-satellite/app-source/README.md\0"
            "apps/ha-satellite/app-source/index.html\0"
        )

        assert committed_names(listed, _SOURCE) == [
            "README.md",
            "index.html",
            "pyproject.toml",
        ]

    def test_a_source_git_does_not_track_is_refused(self) -> None:
        """An empty allow-list would publish nothing and report success."""
        with pytest.raises(PublishRefusalError, match="tracks no file"):
            committed_names("", _SOURCE)

    @pytest.mark.filesystem
    def test_the_committed_source_is_the_three_files_the_documents_name(
        self,
    ) -> None:
        """A file added to the directory is a file published to a public Space."""
        listed = "\0".join(
            f"apps/ha-satellite/app-source/{name}"
            for name in sorted(path.name for path in SOURCE_DIRECTORY.iterdir())
        )

        assert committed_names(listed, SOURCE_DIRECTORY) == [
            "README.md",
            "index.html",
            "pyproject.toml",
        ]


class TestFollowingTheRedirect:
    """A release asset is always a redirect, and a `GET` would download it.

    Neither test opens anything: `redirect_request` is asked directly what
    follow-up request it would build, which is the whole of the behaviour that
    matters and the only part a supplied opener cannot see.
    """

    @staticmethod
    def _follow(handler: urllib.request.HTTPRedirectHandler) -> str | None:
        """Ask a handler what method it would follow a redirected `HEAD` with."""
        original = urllib.request.Request(
            "https://example.invalid/asset.whl",
            method="HEAD",
        )

        redirected = handler.redirect_request(
            original,
            io.BytesIO(b""),
            302,
            "Found",
            HTTPMessage(),
            "https://example.invalid/elsewhere/asset.whl",
        )

        return None if redirected is None else redirected.get_method()

    def test_the_handler_keeps_the_method(self) -> None:
        """Otherwise `--dry-run` downloads the wheel it is asking about."""
        assert self._follow(KeepHeadOnRedirect()) == "HEAD"

    def test_the_standard_library_does_not(self) -> None:
        """Which is why the subclass exists — and when it can be deleted."""
        assert self._follow(urllib.request.HTTPRedirectHandler()) == "GET"
