"""Tests for the repository-wide version value type.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`. These tests touch no socket, no clock and no file.
"""

from __future__ import annotations

import dataclasses

import pytest

from reachy_contracts import VERSION, SemanticVersion, __version__


def test_parse_reads_each_component() -> None:
    """A well-formed string yields its three components as integers."""
    assert SemanticVersion.parse("1.20.300") == SemanticVersion(1, 20, 300)


def test_str_round_trips_through_parse() -> None:
    """Rendering a parsed version reproduces the string it came from."""
    assert str(SemanticVersion.parse("0.4.11")) == "0.4.11"


def test_versions_order_by_component_significance() -> None:
    """Ordering is major, then minor, then patch — not lexicographic."""
    assert SemanticVersion.parse("0.9.0") < SemanticVersion.parse("0.10.0")
    assert SemanticVersion.parse("1.0.0") > SemanticVersion.parse("0.99.99")


@pytest.mark.parametrize(
    "text",
    [
        "1.2",
        "1.2.3.4",
        "1.2.3-rc1",
        "1.2.3+build.5",
        "01.2.3",
        "1.02.3",
        "v1.2.3",
        "1.2.x",
        "",
        " 1.2.3 ",
        "1.2.3\n",
        "1.2.3\n4.5.6",
    ],
)
def test_parse_rejects_malformed_input(text: str) -> None:
    """Anything that is not exactly three unpadded numeric components fails."""
    with pytest.raises(ValueError, match=r"not a MAJOR\.MINOR\.PATCH version"):
        SemanticVersion.parse(text)


def test_construction_rejects_negative_components() -> None:
    """A negative component is refused at construction, not silently kept."""
    with pytest.raises(ValueError, match="minor must not be negative"):
        SemanticVersion(1, -1, 0)


def test_versions_are_immutable() -> None:
    """A version is a value: reassigning a component is an error."""
    version = SemanticVersion.parse("1.2.3")
    with pytest.raises(dataclasses.FrozenInstanceError):
        version.patch = 4  # type: ignore[misc]  # the point of the assertion


def test_package_version_is_a_valid_version() -> None:
    """The version the distribution metadata is built from parses."""
    assert SemanticVersion.parse(__version__) == VERSION
    assert str(VERSION) == __version__


@pytest.mark.asyncio
async def test_async_tests_run_under_strict_mode() -> None:
    """Prove the async harness is wired.

    In strict mode an unmarked coroutine test is not collected as a test at all,
    so this asserts that the marker, the plugin and the configuration agree —
    which is the property the whole workspace's async tests depend on.
    """
    assert SemanticVersion.parse(str(VERSION)) == VERSION
