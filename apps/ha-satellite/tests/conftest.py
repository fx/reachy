"""Test configuration for the satellite, and the in-memory filesystem it uses.

Two things happen here, both of them for the sake of the tests carried over from
the vendored ESPHome upstream.

The first is that this directory goes on `sys.path`. A member's `tests/` is
deliberately not a package (see the root `AGENTS.md`), so the carried tests
cannot import their shared helpers the way they did upstream, as
`tests.unit.conftest`. They import `esphome_test_support` instead, and this is
what makes that name resolvable without turning the directory into a package or
moving test helpers into the shipped wheel.

The second is `tmp_path`. This repository requires that a unit test perform no
input or output at all, and upstream's tests write preference files and
wake-word configurations into a real temporary directory. Overriding pytest's
`tmp_path` with a directory inside a fake filesystem satisfies both at once: the
carried tests keep their bodies exactly as upstream wrote them, and nothing
touches a disk. Only tests that ask for `tmp_path` get the fake filesystem, so a
test that patches its own reads — `test_satellite_esphome_util.py` does — is
left alone.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pyfakefs.fake_filesystem import FakeFilesystem

_TESTS_DIR = Path(__file__).parent

#: Where the fake `tmp_path` lives. Any absolute path works; pyfakefs builds a
#: fresh filesystem for each test, so the name never needs to be unique.
_FAKE_TMP = Path("/reachy-satellite-tests")


def _make_helpers_importable() -> None:
    """Let the carried tests import `esphome_test_support` by name."""
    path = str(_TESTS_DIR)
    if path not in sys.path:
        sys.path.insert(0, path)


def _preload_lazily_imported_modules() -> None:
    """Import everything the carried tests reach for from inside a test body.

    A fake filesystem cannot service an import: while it is active, the real tree
    the module would be read from is not visible. Upstream's tests import the
    module under test inside each test function, so every module they can reach
    has to be in `sys.modules` before the first fake filesystem exists. Doing it
    here, at collection time, is the whole fix.
    """
    import numpy  # noqa: F401  # imported to populate sys.modules, not to use
    import pymicro_wakeword  # noqa: F401  # imported to populate sys.modules
    import pyopen_wakeword  # noqa: F401  # imported to populate sys.modules

    from reachy_mini_ha_satellite.esphome import (  # noqa: F401  # same reason
        api_server,
        entity,
        models,
        peripheral_api,
        satellite,
        seams,
        util,
        wake_word,
        webrtc,
        zeroconf,
    )


_make_helpers_importable()
_preload_lazily_imported_modules()


@pytest.fixture
def tmp_path(fs: FakeFilesystem) -> Path:
    """An empty directory in a fake filesystem, replacing pytest's real one."""
    fs.create_dir(_FAKE_TMP)
    return _FAKE_TMP
