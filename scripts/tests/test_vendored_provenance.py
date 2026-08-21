"""The provenance guard, exercised against every way it is meant to fail.

This script is what stops a derived file dropping out of drift reporting, and a
guard nobody has watched fail is a guard that does not exist — the same reasoning
as `just lint-boundary`. So each `SystemExit` below is provoked deliberately, and
each assertion checks that the message *names the thing*: the file, the key, the
path or the value the reader has to act on.

Everything runs against an in-memory filesystem, so the suite performs no input
or output.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import vendored_provenance
from pyfakefs.fake_filesystem import FakeFilesystem
from vendored_provenance import collect

_ROOT = Path("/repo")
_VENDORED = "vendored"
_UPSTREAM = "https://example.invalid/upstream"
_COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _header(upstream_path: str, *, url: str = _UPSTREAM, commit: str = _COMMIT) -> str:
    return (
        "# Vendored.\n"
        f"#   upstream-url:     {url}\n"
        f"#   upstream-path:    {upstream_path}\n"
        f"#   upstream-commit:  {commit}\n"
        '"""A module."""\n'
    )


@pytest.fixture
def repo(fs: FakeFilesystem, monkeypatch: pytest.MonkeyPatch) -> FakeFilesystem:
    """A repository with one vendored root holding one well-formed file."""
    fs.create_file(_ROOT / _VENDORED / "carried.py", contents=_header("up/carried.py"))
    monkeypatch.setattr(vendored_provenance, "REPOSITORY_ROOT", _ROOT)
    monkeypatch.setattr(vendored_provenance, "VENDORED_ROOTS", (_VENDORED,))
    monkeypatch.setattr(vendored_provenance, "EXEMPT_FILES", frozenset())
    return fs


class TestTheHappyPath:
    """What a sound tree returns, so the failures below mean something."""

    @pytest.mark.usefixtures("repo")
    def test_returns_the_url_commit_and_upstream_paths(self) -> None:
        """One well-formed file yields exactly what the recipe consumes."""
        assert collect() == (_UPSTREAM, _COMMIT, ["up/carried.py"])

    def test_ignores_bytecode(self, repo: FakeFilesystem) -> None:
        """A `__pycache__` is not an unattributed file."""
        repo.create_file(
            _ROOT / _VENDORED / "__pycache__" / "carried.pyc", contents="x"
        )
        assert collect()[2] == ["up/carried.py"]


class TestSelfContradictoryHeaders:
    """A header that disagrees with itself, which is the whole point of the tool."""

    def test_a_repeated_key_fails(self, repo: FakeFilesystem) -> None:
        """Keeping the last value would let one file record two commits."""
        repo.create_file(
            _ROOT / _VENDORED / "twice.py",
            contents=(
                "# Vendored.\n"
                f"#   upstream-url:     {_UPSTREAM}\n"
                "#   upstream-path:    up/twice.py\n"
                f"#   upstream-commit:  {_COMMIT}\n"
                "#   upstream-commit:  ffffffffffffffffffffffffffffffffffffffff\n"
            ),
        )
        with pytest.raises(SystemExit) as raised:
            collect()
        message = str(raised.value)
        assert "vendored/twice.py:5" in message
        assert "upstream-commit appears more than once" in message
        assert _COMMIT in message
        assert "ffffffffffffffffffffffffffffffffffffffff" in message

    def test_an_empty_value_fails(self, repo: FakeFilesystem) -> None:
        """A key with nothing after the colon is not provenance."""
        repo.create_file(
            _ROOT / _VENDORED / "blank.py",
            contents=(
                f"#   upstream-url:     {_UPSTREAM}\n"
                "#   upstream-path:    \n"
                f"#   upstream-commit:  {_COMMIT}\n"
            ),
        )
        with pytest.raises(
            SystemExit, match=r"vendored/blank\.py:2.*present but empty"
        ):
            collect()

    def test_a_value_with_whitespace_fails(self, repo: FakeFilesystem) -> None:
        """It would split into two paths on its way to the drift comparison."""
        repo.create_file(
            _ROOT / _VENDORED / "spaced.py",
            contents=_header("up/two words.py"),
        )
        with pytest.raises(SystemExit) as raised:
            collect()
        assert "contains whitespace" in str(raised.value)
        assert "up/two words.py" in str(raised.value)


class TestDisagreementBetweenFiles:
    """Two files that record different provenance, and who to blame."""

    def test_two_upstreams_fail_and_name_a_file_for_each(
        self, repo: FakeFilesystem
    ) -> None:
        """Which files disagree is the first thing the reader needs."""
        repo.create_file(
            _ROOT / _VENDORED / "elsewhere.py",
            contents=_header("up/elsewhere.py", url="https://example.invalid/other"),
        )
        with pytest.raises(SystemExit) as raised:
            collect()
        message = str(raised.value)
        assert "more than one upstream" in message
        assert "vendored/carried.py" in message
        assert "vendored/elsewhere.py" in message

    def test_two_commits_fail_and_name_a_file_for_each(
        self, repo: FakeFilesystem
    ) -> None:
        """Half-finished re-vendoring is the way this normally happens."""
        repo.create_file(
            _ROOT / _VENDORED / "older.py",
            contents=_header("up/older.py", commit="f" * 40),
        )
        with pytest.raises(SystemExit) as raised:
            collect()
        message = str(raised.value)
        assert "more than one upstream commit" in message
        assert "vendored/carried.py" in message
        assert "vendored/older.py" in message

    def test_a_duplicated_upstream_path_names_both_local_files(
        self, repo: FakeFilesystem
    ) -> None:
        """The message fires exactly when the reader needs to know which two."""
        repo.create_file(
            _ROOT / _VENDORED / "copy.py", contents=_header("up/carried.py")
        )
        with pytest.raises(SystemExit) as raised:
            collect()
        message = str(raised.value)
        assert "up/carried.py claimed by" in message
        assert "vendored/carried.py" in message
        assert "vendored/copy.py" in message


class TestNothingIsDroppedQuietly:
    """Every way an unattributed file could otherwise slip through."""

    def test_a_file_with_no_header_fails(self, repo: FakeFilesystem) -> None:
        """The plain case: something arrived with no provenance at all."""
        repo.create_file(_ROOT / _VENDORED / "bare.py", contents='"""No header."""\n')
        with pytest.raises(SystemExit) as raised:
            collect()
        assert "vendored/bare.py" in str(raised.value)
        assert "upstream-commit, upstream-path, upstream-url" in str(raised.value)

    def test_a_header_past_the_scanned_lines_fails(self, repo: FakeFilesystem) -> None:
        """A header pushed out of range is missing, and the message says so."""
        padding = "# filler\n" * (vendored_provenance._HEADER_LINES + 1)
        repo.create_file(
            _ROOT / _VENDORED / "buried.py", contents=padding + _header("up/buried.py")
        )
        with pytest.raises(SystemExit) as raised:
            collect()
        assert "vendored/buried.py" in str(raised.value)
        assert f"first {vendored_provenance._HEADER_LINES} lines" in str(raised.value)

    def test_a_binary_that_is_not_exempt_fails(self, repo: FakeFilesystem) -> None:
        """A binary cannot carry a header, so it has to be exempted by hand."""
        repo.create_file(_ROOT / _VENDORED / "model.bin", contents=b"\xff\xfe\x00\x01")
        with pytest.raises(SystemExit) as raised:
            collect()
        assert "vendored/model.bin" in str(raised.value)
        assert "not UTF-8 text" in str(raised.value)

    @pytest.mark.usefixtures("repo")
    def test_a_root_that_is_not_a_directory_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A renamed root would take every file under it out of the report."""
        monkeypatch.setattr(vendored_provenance, "VENDORED_ROOTS", ("gone",))
        with pytest.raises(SystemExit, match=r"gone: listed as a vendored root"):
            collect()

    def test_a_root_holding_no_files_fails(
        self, repo: FakeFilesystem, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty root would report a clean comparison over nothing."""
        repo.create_dir(_ROOT / "empty")
        monkeypatch.setattr(vendored_provenance, "VENDORED_ROOTS", (_VENDORED, "empty"))
        with pytest.raises(
            SystemExit, match=r"empty: a vendored root holding no files"
        ):
            collect()

    def test_a_broken_symlink_fails(self, repo: FakeFilesystem) -> None:
        """Nothing can record where a dangling link came from."""
        repo.create_symlink(_ROOT / _VENDORED / "dangling.py", _ROOT / "nowhere.py")
        with pytest.raises(SystemExit) as raised:
            collect()
        assert "vendored/dangling.py" in str(raised.value)
        assert "not a readable file" in str(raised.value)


class TestExemptionsAreClaims:
    """An exemption asserts something, so both directions are checked."""

    @pytest.mark.usefixtures("repo")
    def test_an_exempt_file_carrying_a_header_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file is either derived or ours, and the two claims contradict."""
        monkeypatch.setattr(
            vendored_provenance, "EXEMPT_FILES", frozenset({"vendored/carried.py"})
        )
        with pytest.raises(SystemExit) as raised:
            collect()
        assert "vendored/carried.py" in str(raised.value)
        assert "exempt from carrying provenance" in str(raised.value)

    @pytest.mark.usefixtures("repo")
    def test_an_exemption_for_a_file_that_is_gone_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stale exemption hides the renamed file it used to cover."""
        monkeypatch.setattr(
            vendored_provenance, "EXEMPT_FILES", frozenset({"vendored/renamed.py"})
        )
        with pytest.raises(SystemExit) as raised:
            collect()
        assert "vendored/renamed.py" in str(raised.value)
        assert "stale exemption" in str(raised.value)

    def test_an_exempt_file_without_a_header_is_accepted(
        self, repo: FakeFilesystem, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ordinary case an exemption exists for."""
        repo.create_file(_ROOT / _VENDORED / "ours.py", contents='"""Ours."""\n')
        monkeypatch.setattr(
            vendored_provenance, "EXEMPT_FILES", frozenset({"vendored/ours.py"})
        )
        assert collect()[2] == ["up/carried.py"]

    def test_an_exempt_binary_is_accepted(
        self, repo: FakeFilesystem, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deliberately exempted, which is the only way a binary may be here."""
        repo.create_file(_ROOT / _VENDORED / "sound.flac", contents=b"\xff\xfe")
        monkeypatch.setattr(
            vendored_provenance, "EXEMPT_FILES", frozenset({"vendored/sound.flac"})
        )
        assert collect()[2] == ["up/carried.py"]


class TestNoRootsAtAll:
    """Searching nowhere must not look like finding nothing wrong."""

    @pytest.mark.usefixtures("repo")
    def test_an_empty_root_list_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Nothing to search is not the same as nothing to attribute."""
        monkeypatch.setattr(vendored_provenance, "VENDORED_ROOTS", ())
        with pytest.raises(SystemExit, match=r"carries a provenance header"):
            collect()
