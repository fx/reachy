"""Write every generated contract artifact under `docs/contracts/`.

    just contracts

Two registries feed one directory. `reachy_contracts.contracts_export` holds the
JSON Schema per robot-link message type; `reachy_checks.checks_export` holds the
`doctor` check reference. They cannot be one registry, because
`reachy-contracts` declares exactly one dependency and `reachy-checks` depends on
it — a registry in the contracts package that imported the checks package would
make that a cycle.

They also cannot be two separate runs. `export` renders the index that lists
every artifact from the contracts it is handed, so a second run would rewrite
the index over its own half and the drift gate would flip between the two on
alternate invocations. This driver is what holds both together for one call, and
it is the only reason it exists.

A third registry is one import and one entry in `REGISTRIES` below.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Final

from reachy_checks.checks_export import CONTRACTS as CHECK_CONTRACTS
from reachy_contracts.contracts_export import CONTRACTS as WIRE_CONTRACTS
from reachy_contracts.contracts_export import Contract, export

__all__ = ["DEFAULT_OUT_DIR", "REGISTRIES", "all_contracts", "main"]

DEFAULT_OUT_DIR: Final = "docs/contracts"

# Every registry that contributes an artifact, in the order their entries appear
# in the generated index's table — which is sorted by path, so the order here
# only decides what a duplicate path would collide with.
REGISTRIES: Final[tuple[tuple[Contract, ...], ...]] = (WIRE_CONTRACTS, CHECK_CONTRACTS)


def all_contracts(
    registries: tuple[tuple[Contract, ...], ...] = REGISTRIES,
) -> tuple[Contract, ...]:
    """Flatten every registry into the set the exporter is handed.

    Args:
        registries: The registries to combine.

    Returns:
        Every contract, in registry order.
    """
    return tuple(contract for registry in registries for contract in registry)


def main(argv: list[str]) -> int:
    """Write every artifact and report what was written.

    Args:
        argv: The arguments after the program name. An optional output
            directory, defaulting to `docs/contracts`.

    Returns:
        A process exit status.
    """
    out_dir = Path(argv[0] if argv else DEFAULT_OUT_DIR)
    for written in export(out_dir, all_contracts()):
        print(written)
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main(sys.argv[1:]))
