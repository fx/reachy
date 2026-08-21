"""Reporting a failure without republishing what it said.

Three surfaces publish a failure to somebody who is not an operator, and none of
them may carry the text of an exception raised by code this service does not
control. What they carry is a classification; the log keeps the text.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`. Nothing here touches a socket, a clock or a file.
"""

from __future__ import annotations

import pytest

from reachy_contracts import SessionOffer
from reachy_groundstation.faults import describe_fault, validation_summary


def test_a_failure_is_named_by_its_kind() -> None:
    """The type is what a client can act on; the message is the operator's."""
    assert describe_fault(RuntimeError("could not open /srv/models/face.onnx")) == (
        "RuntimeError"
    )


def test_a_failure_never_repeats_what_it_said() -> None:
    """An exception's text is written by code this service does not control."""
    described = describe_fault(RuntimeError("could not open /srv/models/face.onnx"))
    assert "/srv/models" not in described


def test_a_validation_summary_names_the_field_and_the_kind() -> None:
    """A client can fix a message it is told the shape of."""
    with pytest.raises(ValueError, match="credential") as raised:
        SessionOffer.model_validate({"capabilities": []})
    summary = validation_summary(raised.value)
    assert "credential: missing" in summary


def test_a_validation_summary_discards_the_offending_value() -> None:
    """An offer is the one message with a credential in it."""
    with pytest.raises(ValueError, match="credential") as raised:
        SessionOffer.model_validate({"credential": "x" * 500, "capabilities": []})
    summary = validation_summary(raised.value)
    assert "credential: too_long" in summary
    assert "x" * 100 not in summary


def test_a_validation_summary_falls_back_to_the_exception_type() -> None:
    """Not every `ValueError` is a validation error, and none is unwrapped."""
    assert validation_summary(ValueError("a plain failure")) == "ValueError"
    assert "a plain failure" not in validation_summary(ValueError("a plain failure"))
