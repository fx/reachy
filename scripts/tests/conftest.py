"""Put `scripts/` on the path so its helpers import by name.

`scripts/` is not a workspace member and deliberately not a package — the
`Justfile` calls these files, nothing imports them at run time. Their tests still
have to, so this adds the directory to `sys.path` rather than making the whole
thing a package for the sake of one import.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]

if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
