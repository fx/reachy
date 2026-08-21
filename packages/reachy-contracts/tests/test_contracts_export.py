"""The contract-artifact registry, rendered in memory."""

from __future__ import annotations

import pytest

from reachy_contracts.contracts_export import (
    CONTRACTS,
    INDEX_PATH,
    Contract,
    render_all,
)


def test_the_registry_is_empty_until_the_wire_types_exist() -> None:
    """Nothing is generated yet, and the gate is wired anyway."""
    assert CONTRACTS == ()


def test_an_empty_registry_still_renders_an_index() -> None:
    """The gate compares against a real file even with nothing registered."""
    rendered = render_all(())

    assert set(rendered) == {INDEX_PATH}
    assert "No contract artifacts are generated yet." in rendered[INDEX_PATH]


def test_a_registered_contract_is_rendered_and_listed() -> None:
    """Registering a generator is the whole of the work left."""
    contract = Contract(
        path="robot-link/session.schema.json",
        summary="the session envelope",
        render=lambda: '{"title": "session"}',
    )

    rendered = render_all([contract])

    assert rendered[contract.path] == '{"title": "session"}'
    assert contract.path in rendered[INDEX_PATH]
    assert contract.summary in rendered[INDEX_PATH]


def test_two_contracts_claiming_one_path_is_an_error() -> None:
    """A collision would make the output depend on registration order."""
    duplicate = Contract(path="a.json", summary="a", render=lambda: "{}")

    with pytest.raises(ValueError, match="same path"):
        render_all([duplicate, duplicate])


def test_a_contract_cannot_claim_the_index_path() -> None:
    """The index is generated too, so nothing else may overwrite it."""
    intruder = Contract(path=INDEX_PATH, summary="not this", render=lambda: "")

    with pytest.raises(ValueError, match="same path"):
        render_all([intruder])
