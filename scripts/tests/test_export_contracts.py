"""The driver that writes `docs/contracts/`, and the one thing it has to get right.

Two registries feed one directory, and the reason a driver exists at all is that
the index has to be rendered over both of them at once. A run that wrote half
the index would still produce a clean tree on the run after it and a dirty one
on the run after that, which is a drift gate that fails at random.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from export_contracts import DEFAULT_OUT_DIR, REGISTRIES, all_contracts, main

from reachy_checks.checks_export import CONTRACTS as CHECK_CONTRACTS
from reachy_checks.checks_export import DOCTOR_CHECKS_PATH
from reachy_contracts.contracts_export import CONTRACTS as WIRE_CONTRACTS
from reachy_contracts.contracts_export import INDEX_PATH, render_all


def test_every_registry_is_driven() -> None:
    """A registry left out is an artifact nothing regenerates and nothing gates."""
    paths = {contract.path for contract in all_contracts()}

    assert paths >= {contract.path for contract in WIRE_CONTRACTS}
    assert paths >= {contract.path for contract in CHECK_CONTRACTS}
    assert DOCTOR_CHECKS_PATH in paths


def test_no_two_registries_claim_the_same_path() -> None:
    """Two would make the generated tree depend on which one ran last."""
    paths = [contract.path for contract in all_contracts()]

    assert len(paths) == len(set(paths))


def test_the_index_lists_the_artifacts_of_every_registry() -> None:
    """The whole reason the driver exists rather than two separate runs."""
    index = render_all(all_contracts())[INDEX_PATH]

    for contract in all_contracts():
        assert contract.path in index


def test_writing_twice_produces_the_same_tree(tmp_path: Path) -> None:
    """The drift gate compares bytes, so generation has to be deterministic."""
    main([str(tmp_path)])
    first = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in sorted(tmp_path.rglob("*"))
        if path.is_file()
    }

    main([str(tmp_path)])
    second = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in sorted(tmp_path.rglob("*"))
        if path.is_file()
    }

    assert first == second
    assert Path(INDEX_PATH) in first


def test_an_artifact_no_registry_produces_any_more_is_deleted(tmp_path: Path) -> None:
    """Otherwise the drift gate passes over a contract nothing generates.

    A write-only generator cannot see a removed or renamed contract: the old
    file stays committed, regeneration leaves it alone, and the gate compares a
    tree that still publishes it against a run that never wrote it and finds no
    difference. That is REQ-008 failing by the one route it cannot detect, so
    the directory is owned rather than merely written into.
    """
    main([str(tmp_path)])
    stale = tmp_path / "robot-link" / "withdrawn.schema.json"
    stale.write_text("{}\n", encoding="utf-8")
    orphan = tmp_path / "gone"
    orphan.mkdir()
    (orphan / "leftover.md").write_text("nothing generates this\n", encoding="utf-8")

    main([str(tmp_path)])

    assert not stale.exists()
    assert not orphan.exists()
    assert (tmp_path / INDEX_PATH).exists()
    assert (tmp_path / "robot-link").is_dir()


def test_the_default_output_directory_is_the_published_one() -> None:
    """`just contracts` passes it explicitly; a caller that does not gets the same."""
    assert DEFAULT_OUT_DIR == "docs/contracts"
    assert len(REGISTRIES) == 2


def test_it_reports_what_it_wrote(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A generator that printed nothing would leave a caller guessing what ran."""
    status = main([str(tmp_path)])
    written = capsys.readouterr().out.splitlines()

    assert status == 0
    assert len(written) == len(all_contracts()) + 1
    assert str(tmp_path / INDEX_PATH) in written


pytestmark = pytest.mark.filesystem  # writes a generated tree and reads it back
