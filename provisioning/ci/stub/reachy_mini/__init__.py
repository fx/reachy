"""A stand-in for the vendor daemon, for the provisioning idempotency gate.

This package is **not** the Reachy Mini SDK and is never installed anywhere
except inside the container target `just provision-idempotency` builds. It exists
so that the layout the roles reach for — a `reachy-mini` distribution in the
environment the unit runs, a process for systemd to supervise, and an application
control reached as `python -m reachy_mini.apps` — is the layout that is there.

`provisioning/ci/README.md` records what the container models and what it does
not. The short version is that nothing here touches hardware, because a container
has none, and the roles do not either: everything they do is a file, a unit and
an install.
"""

from __future__ import annotations

__all__ = ["STATE_PATH"]

# Where the daemon records what it picked up at its last start. Under `/run`
# because it describes this boot: a robot that has been rebooted and not restarted
# its daemon has not started its applications either.
STATE_PATH = "/run/reachy-mini/state.json"
