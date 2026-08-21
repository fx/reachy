"""The image verifier's own logic, exercised without a container.

What is worth testing here is not the WebSocket — that is `websockets`, and the
protocol it carries is `reachy_contracts`, both of which have tests of their own.
It is the small amount of judgement this script adds: deriving the session
endpoint from a base URL, refusing a base URL it cannot derive one from, building
an offer the service will accept, and giving up on readiness rather than waiting
for ever.

The readiness tests drive `wait_until_ready` through an injected clock and an
injected fetch, so no socket is opened and no wall time is spent.
"""

from __future__ import annotations

import asyncio
import urllib.error
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import verify_groundstation_image
from verify_groundstation_image import (
    VerificationError,
    capture_stamp,
    frame_for,
    offer_for,
    session_url,
    wait_until_ready,
)

from reachy_contracts import FACE_CAPABILITY, SessionOffer
from reachy_groundstation.session.framing import (
    MessageKind,
    decode_control,
    decode_frame,
)

if TYPE_CHECKING:
    from pyfakefs.fake_filesystem import FakeFilesystem

_BASE = "http://groundstation:8080"


def _no_delay(seconds: float) -> None:
    """Stand in for `time.sleep`, so polling costs no wall time.

    Args:
        seconds: How long the caller wanted to wait, which is not waited.
    """
    del seconds


def test_the_session_endpoint_is_derived_from_the_base_url() -> None:
    """The scheme decides the WebSocket scheme; the path is the service's."""
    assert session_url(_BASE) == "ws://groundstation:8080/v1/session"
    assert session_url("https://example.invalid/") == (
        "wss://example.invalid/v1/session"
    )


@pytest.mark.parametrize("base", ["groundstation:8080", "ftp://example.invalid", ""])
def test_a_base_url_with_no_usable_scheme_is_refused(base: str) -> None:
    """Guessing a scheme would report a broken service instead of a bad flag."""
    with pytest.raises(VerificationError, match="http or https"):
        session_url(base)


def test_the_offer_presents_the_credential_and_asks_for_the_face_capability() -> None:
    """What is sent is what the service parses, built from the same types."""
    kind, payload = decode_control(offer_for("example-credential"))
    assert kind is MessageKind.OFFER
    offer = SessionOffer.from_wire(payload)
    assert offer.credential.get_secret_value() == "example-credential"
    assert [capability.name for capability in offer.capabilities] == [FACE_CAPABILITY]


@pytest.mark.filesystem  # the committed fixture's bytes are what gets sent
def test_the_frame_carries_the_image_bytes_unaltered() -> None:
    """A frame is a header and the compressed bytes; nothing re-encodes them."""
    fixture = verify_groundstation_image._DEFAULT_FRAME
    header, payload = decode_frame(frame_for(fixture, sequence=7))
    assert header.sequence == 7
    assert header.captured_at == capture_stamp(7)
    assert payload == fixture.read_bytes()


def test_a_missing_frame_is_reported_as_a_missing_file(fs: FakeFilesystem) -> None:
    """Better than a stack trace out of `read_bytes` two frames deeper.

    An in-memory filesystem rather than a real temporary one: no bytes on disk
    are the thing under test here, so this is an ordinary unit test and
    performs no input or output.

    Args:
        fs: The in-memory filesystem, which is empty.
    """
    del fs
    with pytest.raises(VerificationError, match="is not a file"):
        frame_for(Path("/frames/no-such-frame.jpg"))


def test_readiness_is_polled_until_the_service_says_yes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A service still warming up is the normal case, not a failure."""
    answers = iter([(False, {"ready": False}), (False, {"ready": False}), (True, {})])

    def _readiness(base_url: str, timeout: float) -> tuple[bool, object]:
        del base_url, timeout
        return next(answers)

    monkeypatch.setattr(verify_groundstation_image, "readiness", _readiness)
    monkeypatch.setattr("verify_groundstation_image.time.sleep", _no_delay)
    assert wait_until_ready(_BASE, deadline_seconds=30.0) == {}


def test_a_service_that_never_warms_up_fails_rather_than_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The message carries the last answer, which names what did not warm up."""

    def _never_ready(base_url: str, timeout: float) -> tuple[bool, object]:
        del base_url, timeout
        return False, {"capabilities": ["face is warming"]}

    monkeypatch.setattr(verify_groundstation_image, "readiness", _never_ready)
    monkeypatch.setattr("verify_groundstation_image.time.sleep", _no_delay)
    clock = iter([0.0, 0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0])

    def _monotonic() -> float:
        return next(clock)

    monkeypatch.setattr("verify_groundstation_image.time.monotonic", _monotonic)
    with pytest.raises(VerificationError, match="did not report ready"):
        wait_until_ready(_BASE, deadline_seconds=2.0)


def test_readiness_reports_a_refused_connection_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A container that is not listening yet must poll, not crash."""

    def _refuse(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    monkeypatch.setattr("verify_groundstation_image.urllib.request.urlopen", _refuse)
    ready, detail = verify_groundstation_image.readiness(_BASE, timeout=0.01)
    assert ready is False
    assert isinstance(detail, str)
    assert "Connection refused" in detail


def test_a_binary_message_where_text_was_due_is_refused() -> None:
    """Results are text; a binary answer means the framing has changed."""

    class _Binary:
        async def recv(self) -> Any:  # noqa: ANN401  # mirrors `websockets`, whose `recv` returns text or bytes
            return b"\x00\x01"

    with pytest.raises(VerificationError, match="binary bytes"):
        asyncio.run(verify_groundstation_image._receive(_Binary()))  # type: ignore[arg-type]  # a stand-in for `ClientConnection`, which is a concrete class rather than a protocol
