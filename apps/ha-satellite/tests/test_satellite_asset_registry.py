"""The licence gate over the assets that ship inside the wheel.

Wake-word models and sounds are redistributed from this public repository and
inside every wheel it publishes, so their terms are a merge concern rather than
a packaging detail. This is the half of the gate that decides whether an asset's
terms are acceptable; it reads nothing from disk, because the registry is a
Python literal for exactly that reason. `just check-assets` is the other half,
and checks that what is on disk is what the registry says it is.
"""

from __future__ import annotations

import re

import pytest

from reachy_mini_ha_satellite.assets.registry import (
    ALLOWED_LICENCES,
    ASSETS,
    UNREGISTERED,
    Asset,
    AssetKind,
)

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")


@pytest.mark.parametrize("asset", ASSETS, ids=lambda asset: asset.path)
class TestEveryAsset:
    """Each entry has to hold up on its own."""

    def test_licence_is_allowed(self, asset: Asset) -> None:
        """The gate itself: unacceptable terms fail here, not in a release."""
        assert asset.licence in ALLOWED_LICENCES, (
            f"{asset.path} ships under {asset.licence!r}, which is not in the "
            f"allowlist. Widening the allowlist is a licensing decision — make "
            f"it deliberately, in review, not to get a build green."
        )

    def test_licence_terms_are_citable(self, asset: Asset) -> None:
        """A licence nobody can look up is not an answer to the question."""
        assert asset.licence_url.startswith("https://")

    def test_attribution_is_recorded(self, asset: Asset) -> None:
        """CC BY, and courtesy under Apache-2.0, require the credit travel."""
        assert asset.attribution.strip()

    def test_source_is_a_fetchable_url(self, asset: Asset) -> None:
        """Provenance has to be specific enough to re-derive the file."""
        assert asset.source.startswith("https://")

    def test_digest_is_a_sha256(self, asset: Asset) -> None:
        """A digest is what makes a silent substitution visible."""
        assert _SHA256.fullmatch(asset.sha256)

    def test_path_is_relative_and_normalised(self, asset: Asset) -> None:
        """A registry entry names a file in this package, not one elsewhere."""
        assert not asset.path.startswith("/")
        assert ".." not in asset.path.split("/")
        assert "\\" not in asset.path


class TestRegistryShape:
    """Properties of the registry as a whole, rather than of one entry."""

    def test_the_registry_is_not_empty(self) -> None:
        """An empty registry would pass every check above vacuously."""
        assert ASSETS

    def test_paths_are_unique(self) -> None:
        """Two entries for one file could disagree about its terms."""
        paths = [asset.path for asset in ASSETS]
        assert len(set(paths)) == len(paths)

    def test_nothing_is_both_registered_and_exempt(self) -> None:
        """A file cannot be a shipped asset and documentation at once."""
        assert not {asset.path for asset in ASSETS} & UNREGISTERED

    def test_the_exemption_list_is_exactly_this(self) -> None:
        """Pins the exemptions, so one cannot be added without saying so here.

        `UNREGISTERED` is what `just check-assets` subtracts before demanding a
        registry entry, so a path added to it is a file that ships in the wheel
        with nothing recording its licence. Pinning it to a second copy makes
        that a two-file edit a reviewer sees, rather than one word in a set.

        Changing this list is a licensing decision. Make it deliberately: satisfy
        yourself the file really is not an asset, and if it is one, register it.
        """
        pinned = {
            "NOTICE.md",
            "__init__.py",
            "registry.py",
            "sounds/LICENSE.md",
            "verify.py",
            "wakewords/LICENSE",
        }
        assert set(UNREGISTERED) == pinned

    def test_every_kind_is_represented(self) -> None:
        """Both a wake word and a sound ship; neither list silently emptied."""
        assert {asset.kind for asset in ASSETS} == set(AssetKind)

    def test_entries_are_immutable(self) -> None:
        """The registry is a record, not a place to stash mutable state."""
        with pytest.raises(AttributeError):
            ASSETS[0].licence = "WTFPL"  # type: ignore[misc]  # frozen dataclass


class TestAllowlist:
    """The allowlist is the policy, so it is checked as well as applied."""

    def test_holds_only_spdx_shaped_identifiers(self) -> None:
        """Free text here would let a near-miss like "Apache 2" slip through."""
        assert all(re.fullmatch(r"[A-Za-z0-9.\-]+", name) for name in ALLOWED_LICENCES)

    def test_excludes_terms_that_cannot_ship_in_this_wheel(self) -> None:
        """A guard on the allowlist itself, not on any particular asset.

        Copyleft and non-commercial terms are the two families that would make
        the wheel undistributable or the repository unpublishable. Naming them
        makes adding one a visible, deliberate edit of this test rather than a
        one-word addition to a set.
        """
        forbidden = {
            "AGPL-3.0",
            "AGPL-3.0-only",
            "AGPL-3.0-or-later",
            "CC-BY-NC-4.0",
            "CC-BY-NC-SA-4.0",
            "GPL-2.0",
            "GPL-3.0",
            "GPL-3.0-only",
            "GPL-3.0-or-later",
            "LicenseRef-Proprietary",
        }
        assert not ALLOWED_LICENCES & forbidden
