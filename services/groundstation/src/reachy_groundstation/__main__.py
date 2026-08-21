"""Run the groundstation: `python -m reachy_groundstation`.

Everything the entry point does lives in `reachy_groundstation.service.main`, so
that startup is a function with tests on it rather than a module body that only
runs when nothing is watching.
"""

from __future__ import annotations

import sys

from reachy_groundstation.service import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
