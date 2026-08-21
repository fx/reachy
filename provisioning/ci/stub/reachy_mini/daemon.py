"""The process the container target's `reachy-mini-daemon.service` runs.

It does one interesting thing and then waits: at start, it records every
distribution its own environment holds apart from its own and the packaging
tools, and calls those the applications it is running. That is a deliberately
thin model of the real relationship — the application runs as a child of the
daemon and the daemon picks it up when it starts — and it is the part the roles
actually depend on, because it is what makes "installing a wheel requires a
daemon restart before the application is running" true in the container as well
as on the robot.

Everything else the vendor's daemon does is absent. See
`provisioning/ci/README.md`.
"""

from __future__ import annotations

import json
import signal
import sys
from importlib.metadata import distributions
from pathlib import Path

from reachy_mini import STATE_PATH

# This distribution and the packaging tools that arrive with any environment.
# Everything else in here is something somebody installed on purpose, which in
# this container means something a provisioning run put there.
_NOT_AN_APPLICATION = frozenset({"reachy-mini", "pip", "setuptools", "wheel"})


def _normalise(name: str) -> str:
    """Fold a distribution name the way `importlib.metadata` compares them.

    Args:
        name: The name as the metadata spells it.

    Returns:
        The name lowercased with underscores and dots folded to hyphens.
    """
    return name.lower().replace("_", "-").replace(".", "-")


def snapshot() -> dict[str, str]:
    """Record what this environment holds that looks like an application.

    Returns:
        The version by normalised distribution name.
    """
    found: dict[str, str] = {}
    for distribution in distributions():
        name = _normalise(distribution.metadata["Name"] or "")
        if not name or name in _NOT_AN_APPLICATION:
            continue
        found[name] = distribution.version
    return found


def main() -> int:
    """Record what is running and then wait to be stopped.

    Returns:
        The process exit status, which is only ever reached by a signal that
        systemd sent.
    """
    state = Path(STATE_PATH)
    state.parent.mkdir(parents=True, exist_ok=True)
    # Written beside and renamed into place. `write_text` truncates first, and
    # the verification role reads this same file moments after the daemon
    # restarts — a read landing in that window would get an empty or partial
    # document, and the gate would go red intermittently with no defect behind
    # it. A rename within one directory is atomic, so a reader sees the old
    # document or the new one.
    pending = state.with_name(f"{state.name}.pending")
    pending.write_text(json.dumps({"applications": snapshot()}), encoding="utf-8")
    pending.replace(state)
    signal.pause()
    return 0


if __name__ == "__main__":
    sys.exit(main())
