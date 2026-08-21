"""The contract-artifact registry, rendered in memory."""

from __future__ import annotations

import json

import pytest

from reachy_contracts.contracts_export import (
    CONTRACTS,
    INDEX_PATH,
    Contract,
    render_all,
)
from reachy_contracts.fixtures import FIXTURES


def test_every_message_type_has_a_published_schema() -> None:
    """The registry covers the corpus: one schema per message type.

    Two fixtures share the face result's envelope — one with detections and one
    without — so the corpus is one entry longer than the schema set.
    """
    schema_paths = {contract.path for contract in CONTRACTS}
    fixture_slugs = {entry.name.removesuffix(".json") for entry in FIXTURES}

    assert schema_paths == {
        f"robot-link/{slug}.schema.json"
        for slug in fixture_slugs - {"empty-face-result"}
    }


def test_a_generated_schema_describes_its_message_type() -> None:
    """The schema comes from the declaration that validates the message."""
    rendered = render_all()
    schema = json.loads(rendered["robot-link/frame-header.schema.json"])

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["title"] == "FrameHeader"
    assert set(schema["required"]) == {"sequence", "captured_at"}
    assert schema["additionalProperties"] is False


def test_a_generated_schema_ends_in_a_newline() -> None:
    """A generated file is still a text file the drift gate diffs."""
    rendered = render_all()

    assert all(
        content.endswith("\n")
        for path, content in rendered.items()
        if path != INDEX_PATH
    )


def test_the_index_lists_every_registered_artifact() -> None:
    """The index is what tells a reader the directory is generated."""
    index = render_all()[INDEX_PATH]

    assert all(contract.path in index for contract in CONTRACTS)


def test_an_empty_registry_still_renders_an_index() -> None:
    """The gate compares against a real file even with nothing registered."""
    rendered = render_all(())

    assert set(rendered) == {INDEX_PATH}
    assert "No contract artifacts are registered." in rendered[INDEX_PATH]


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
