"""Run the satellite outside the daemon: `python -m reachy_mini_ha_satellite`.

Everything startup does lives in `daemon_app.main`, so that it is a function with
tests on it rather than a module body that only runs when nothing is watching.
This file exists so that the *package* is runnable, which is what the runbooks
document for a deployment session somebody wants to watch.

**It is not what the daemon runs, and it does not make `daemon_app` runnable.**
The daemon takes the module half of the `reachy_mini_apps` entry point and
launches `python -u -m reachy_mini_ha_satellite.daemon_app` — a different module
name, which this file has no bearing on. `daemon_app` therefore carries its own
`__main__` guard over the same `main`, and the comment above it says why.
"""

from __future__ import annotations

import sys

from reachy_mini_ha_satellite.daemon_app import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
