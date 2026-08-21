"""The golden fixture corpus and the loader every consumer reads it through.

These tests read committed files, which is input, so they carry the
`filesystem` marker rather than posing as unit tests. Reading the bytes is not
incidental here: the bytes *are* the contract, and a fake filesystem would pin
whatever the fake was told to return.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import pytest

from reachy_contracts.fixtures import (
    FIXTURES,
    Fixture,
    fixture_bytes,
    fixture_for,
    golden_file_names,
    load_fixture,
    round_trip,
)
from reachy_contracts.session import ResultEnvelope, SessionOffer
from reachy_contracts.values import FaceDetections

pytestmark = pytest.mark.filesystem


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda entry: entry.name)
def test_every_fixture_re_serialises_to_the_bytes_it_came_from(
    fixture: Fixture,
) -> None:
    """Byte-identical, not merely equal.

    Two messages can be equal while one writes a field the other omits, or
    writes the same fields in another order. A consumer of the other
    implementation would then fail to parse a message its own tests called
    correct, which is the drift this corpus exists to catch.
    """
    _message, reserialised = round_trip(fixture)

    assert reserialised == fixture_bytes(fixture.name)


def test_the_corpus_covers_every_committed_file() -> None:
    """A file the manifest does not name is a fixture nothing exercises."""
    assert golden_file_names() == tuple(sorted(entry.name for entry in FIXTURES))


def test_a_fixture_can_be_loaded_as_the_type_the_caller_expects() -> None:
    """The typed entry point a consumer uses when it knows what it wants."""
    offer = load_fixture("session-offer.json", SessionOffer)

    assert offer.credential.get_secret_value() == "example-credential"
    assert [capability.name for capability in offer.capabilities] == [
        "face",
        "gesture",
    ]


def test_the_empty_result_fixture_pins_a_frame_with_no_detections() -> None:
    """A frame with no detections has a committed wire form of its own."""
    result = load_fixture("empty-face-result.json", ResultEnvelope[FaceDetections])

    assert result.payload.faces == ()
    assert result.capability == "face"


def test_a_fixture_the_corpus_does_not_declare_cannot_be_read() -> None:
    """Only manifest names reach the filesystem, so no path is constructible."""
    with pytest.raises(KeyError, match="no such fixture"):
        fixture_bytes("../../../etc/passwd")


def test_looking_up_an_unregistered_fixture_is_an_error() -> None:
    """A typo in a fixture name fails rather than reading nothing."""
    with pytest.raises(KeyError, match="no such fixture"):
        fixture_for("session-offer.jsonn")


def test_every_fixture_summary_says_what_it_pins() -> None:
    """The manifest is documentation for the corpus as well as an index."""
    assert all(entry.summary for entry in FIXTURES)
