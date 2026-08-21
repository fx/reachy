"""Run the satellite outside the daemon: `python -m reachy_mini_ha_satellite`.

Everything the entry point does lives in `daemon_app.main`, so that startup is a
function with tests on it rather than a module body that only runs when nothing
is watching. The daemon's own way in is the `reachy_mini_apps` entry point,
which points at the class in the same module.
"""

from __future__ import annotations

import sys

from reachy_mini_ha_satellite.daemon_app import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
