"""The disk-reading half of the asset licence gate, over a fake filesystem.

`test_satellite_asset_registry.py` covers the half that judges an asset's terms,
which needs nothing but the registry. This covers the half that has to look at
the tree — an unregistered file present, a registered file missing, a digest that
no longer matches — and it does so against an in-memory filesystem, so it still
performs no input or output. The three cases are exactly the three ways an asset
could otherwise reach a wheel with nothing recording its licence.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pyfakefs.fake_filesystem import FakeFilesystem

from reachy_mini_ha_satellite.assets.registry import ASSETS, UNREGISTERED, assets_dir
from reachy_mini_ha_satellite.assets.verify import check, main

_ROOT = Path("/assets")


def _build(fs: FakeFilesystem, *, omit: str = "") -> Path:
    """Lay out every path the registry names, with placeholder contents.

    The contents are not the real assets — the fake filesystem holds no
    megabyte of tflite — so nothing here hashes to its registered digest. That
    is why the clean case patches the expected digests rather than the files:
    a test that needed the real bytes would have to read them off a disk.
    """
    fs.create_dir(_ROOT)
    for asset in ASSETS:
        if asset.path != omit:
            fs.create_file(_ROOT / asset.path, contents=asset.path)
    for name in UNREGISTERED:
        path = _ROOT / name
        if not path.exists():
            fs.create_file(path, contents=name)
    return _ROOT


def _digests_of(root: Path) -> dict[str, str]:
    return {
        asset.path: hashlib.sha256((root / asset.path).read_bytes()).hexdigest()
        for asset in ASSETS
        if (root / asset.path).is_file()
    }


class TestCheck:
    """Each finding `check` can report, provoked deliberately."""

    def test_reports_a_digest_that_does_not_match(self, fs: FakeFilesystem) -> None:
        """Every file is present, and none of them hashes to its registered digest."""
        root = _build(fs)
        problems = check(root)
        assert len(problems) == len(ASSETS)
        assert all("does not match the registered" in problem for problem in problems)

    def test_reports_a_registered_file_that_is_missing(
        self, fs: FakeFilesystem
    ) -> None:
        """A file the registry names and the tree does not have."""
        missing = ASSETS[0].path
        root = _build(fs, omit=missing)
        problems = check(root)
        assert f"{missing}: registered but missing from the tree" in problems

    def test_reports_a_file_nothing_registered(self, fs: FakeFilesystem) -> None:
        """A file that would ship with nothing recording its licence."""
        root = _build(fs)
        fs.create_file(root / "sounds" / "smuggled.flac", contents="x")
        problems = check(root)
        assert any(
            problem.startswith("sounds/smuggled.flac: present but not in the registry")
            for problem in problems
        )

    def test_says_nothing_when_the_tree_matches(self, fs: FakeFilesystem) -> None:
        """The clean path: a tree whose digests are the registered ones."""
        root = _build(fs)
        digests = _digests_of(root)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(
                "reachy_mini_ha_satellite.assets.verify.ASSETS",
                tuple(
                    type(asset)(
                        path=asset.path,
                        kind=asset.kind,
                        licence=asset.licence,
                        licence_url=asset.licence_url,
                        attribution=asset.attribution,
                        source=asset.source,
                        sha256=digests[asset.path],
                    )
                    for asset in ASSETS
                ),
            )
            assert check(root) == []

    def test_ignores_bytecode_caches(self, fs: FakeFilesystem) -> None:
        """A `__pycache__` beside the registry is not an unregistered asset."""
        root = _build(fs)
        fs.create_file(root / "__pycache__" / "registry.cpython-312.pyc", contents="x")
        assert not any("__pycache__" in problem for problem in check(root))


class TestDefensiveBranches:
    """Two problems `check` reports that the registry as committed cannot cause.

    They are the failure the checks exist for, so they are exercised rather than
    left as lines nothing has ever run.
    """

    def test_reports_a_duplicated_path(self, fs: FakeFilesystem) -> None:
        """Two entries for one path could disagree about its terms."""
        root = _build(fs)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(
                "reachy_mini_ha_satellite.assets.verify.ASSETS",
                (*ASSETS, ASSETS[0]),
            )
            assert "registry lists the same path more than once" in check(root)

    def test_reports_a_disallowed_licence(self, fs: FakeFilesystem) -> None:
        """The gate, reached from the disk side as well as the registry side."""
        root = _build(fs)
        original = ASSETS[0]
        forbidden = type(original)(
            path=original.path,
            kind=original.kind,
            licence="AGPL-3.0-or-later",
            licence_url=original.licence_url,
            attribution=original.attribution,
            source=original.source,
            sha256=original.sha256,
        )
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(
                "reachy_mini_ha_satellite.assets.verify.ASSETS",
                (forbidden, *ASSETS[1:]),
            )
            problems = check(root)
        assert any("is not in the allowlist" in problem for problem in problems)


class TestEntryPoint:
    """What `just check-assets` actually calls."""

    def test_returns_a_failing_status_when_something_is_wrong(
        self, fs: FakeFilesystem
    ) -> None:
        """`just check-assets` fails the build through this return value."""
        fs.create_dir(assets_dir())
        assert main() == 1

    def test_returns_a_passing_status_when_nothing_is_wrong(self) -> None:
        """A sound tree reports itself and exits zero."""
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr("reachy_mini_ha_satellite.assets.verify.check", lambda: [])
            assert main() == 0


class TestAssetsDir:
    """Where `check` looks when nobody tells it."""

    def test_points_at_the_package_directory(self) -> None:
        """The default root is where the shipped assets actually live."""
        assert assets_dir().name == "assets"
        assert assets_dir().parent.name == "reachy_mini_ha_satellite"
