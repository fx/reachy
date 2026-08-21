"""Running the tool as a module, for a checkout with no console script installed.

`python -m reachyctl` and the installed `reachyctl` entry point run the same
function, so a contributor working from a source tree meets the same command
surface an operator does.
"""

from __future__ import annotations

from reachyctl.cli import main

if __name__ == "__main__":
    main()
