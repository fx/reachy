"""Run the suite as a module: `python -m reachy_bench`.

A module entry point rather than a console script, because this member is never
published and installing a command called `reachy-bench` on somebody's PATH
would imply otherwise. The `Justfile` recipes are what anyone actually types.
"""

from __future__ import annotations

import sys

from reachy_bench.cli import main

if __name__ == "__main__":
    sys.exit(main())
