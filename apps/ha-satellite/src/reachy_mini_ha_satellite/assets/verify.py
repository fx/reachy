"""Check that the shipped assets are exactly what the registry records.

The licence gate has two halves. The half that decides whether an asset's terms
are acceptable is a unit test over `registry.ASSETS`, which performs no input or
output. This is the other half: it reads the directory, so it is a task rather
than a test, run by `just check-assets` and therefore by `just check`.

It fails when a file is present but unregistered, registered but missing, or
registered with a digest that no longer matches — the three ways an asset could
otherwise reach a wheel without anyone having agreed to its terms.

Run against an installed wheel, it verifies that wheel's assets.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from .registry import ALLOWED_LICENCES, ASSETS, UNREGISTERED, assets_dir


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative_paths(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }


def check(root: Path | None = None) -> list[str]:
    """Return every problem found, or an empty list when the assets are sound."""
    root = assets_dir() if root is None else root
    problems: list[str] = []

    registered = {asset.path: asset for asset in ASSETS}
    if len(registered) != len(ASSETS):
        problems.append("registry lists the same path more than once")

    on_disk = _relative_paths(root)

    for extra in sorted(on_disk - registered.keys() - UNREGISTERED):
        problems.append(
            f"{extra}: present but not in the registry, so nothing records its "
            f"licence — add an entry or delete the file"
        )

    for asset in ASSETS:
        path = root / asset.path
        if not path.is_file():
            problems.append(f"{asset.path}: registered but missing from the tree")
            continue
        actual = _digest(path)
        if actual != asset.sha256:
            problems.append(
                f"{asset.path}: digest {actual} does not match the registered "
                f"{asset.sha256} — the file changed without the registry changing"
            )
        if asset.licence not in ALLOWED_LICENCES:
            problems.append(
                f"{asset.path}: licence {asset.licence!r} is not in the allowlist"
            )

    return problems


def main() -> int:
    """Print every problem and return a process exit status."""
    problems = check()
    for problem in problems:
        print(f"asset registry: {problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"asset registry: {len(ASSETS)} assets verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
