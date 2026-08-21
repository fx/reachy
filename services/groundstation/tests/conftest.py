"""Test configuration for the groundstation.

Two things happen here.

The first is that `tests/support/` goes on `sys.path`, so the tests can import
`groundstation_support` by name. A member's `tests/` is deliberately not a
package — see the root `AGENTS.md` — so there is no other way for one test module
to reach a shared helper. It is a subdirectory rather than this one because the
root `pyproject.toml` names the same path to mypy, and a mypy path root makes
whatever sits directly under it a top-level module — which would make this file
and the satellite's `conftest.py` both the module `conftest`.

The second is that structlog is pointed at a logger that returns instead of
writing. The service logs on paths every test exercises, and writing those lines
to standard output would be input and output a unit test is not allowed to
perform. A test that wants to read what was logged uses `captured_logs` from
`groundstation_support`, which installs its own processor chain for the duration
of the test and restores whatever was configured before it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import structlog

_SUPPORT_DIR = Path(__file__).parent / "support"


def _make_helpers_importable() -> None:
    """Let the tests import `groundstation_support` by name."""
    path = str(_SUPPORT_DIR)
    if path not in sys.path:
        sys.path.insert(0, path)


_make_helpers_importable()


@pytest.fixture(autouse=True)
def _silent_logging() -> None:
    """Configure structlog to build lines and then discard them."""
    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars],
        wrapper_class=structlog.make_filtering_bound_logger(0),
        logger_factory=structlog.ReturnLoggerFactory(),
        cache_logger_on_first_use=False,
    )
