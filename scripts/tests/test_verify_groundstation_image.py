"""The image verifier's own logic, exercised without a container.

What is worth testing here is deliberately small. The session protocol belongs to
`reachy_session_client`, which has its own suite covering negotiation, framing,
results and reconnection — re-testing it through this script would test the
client twice and this script not at all. What is left is the judgement this
module adds: deriving the session endpoint from a base URL, refusing one it
cannot derive an endpoint from, reading the frame without re-encoding it, giving
up on readiness rather than waiting for ever, and reading a feed answer that is
not the one it was hoping for.

Deriving the *feed* connection is here for the same reason as deriving the
session endpoint: the base URL's scheme decides it, and it is the one of the
three code paths that opens its socket by hand. A replaced `open_connection`
records the host, the port and the TLS context it was asked for, which is what
makes "`https://` is supported by all of this script" checkable without a
certificate.

`drive_session` itself is exercised for real rather than here: `just
image-verify` runs it against the built image on a network with no route off the
host, which is the only place it can prove anything — and the feed part it reads
there is the same one the groundstation's own marked transport tests read over a
socket.

The readiness tests drive `wait_until_ready` through an injected clock and an
injected fetch, and the feed tests drive its parsers over bytes, so no socket is
opened and no wall time is spent.
"""

from __future__ import annotations

import asyncio
import ssl
import urllib.error
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import verify_groundstation_image
from verify_groundstation_image import (
    VerificationError,
    frame_bytes,
    read_feed_part,
    session_url,
    wait_until_ready,
)

from reachy_groundstation.api.app import STREAM_PATH
from reachy_session_client import validate_session_url

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


def test_the_derived_endpoint_is_one_the_shared_client_accepts() -> None:
    """The client validates the URL it is given, so this has to satisfy it.

    Deriving an endpoint the client refuses would fail at `SessionClient`'s
    constructor with a message about the URL rather than about the service, and
    the two are the sort of thing a reader conflates at three in the morning.
    """
    assert validate_session_url(session_url(_BASE)) == session_url(_BASE)


@pytest.mark.filesystem  # the committed fixture's bytes are what gets sent
def test_the_frame_is_read_without_being_re_encoded() -> None:
    """Compressed bytes go to the client as they are; nothing re-encodes them."""
    fixture = verify_groundstation_image._DEFAULT_FRAME
    assert frame_bytes(fixture) == fixture.read_bytes()
    assert frame_bytes(fixture).startswith(b"\xff\xd8")


def test_a_missing_frame_is_reported_as_a_missing_file(fs: FakeFilesystem) -> None:
    """Better than a stack trace out of `read_bytes` two frames deeper.

    An in-memory filesystem rather than a real temporary one: no bytes on disk
    are the thing under test here, so this is an ordinary unit test and performs
    no input or output.

    Args:
        fs: The in-memory filesystem, which is empty.
    """
    del fs
    with pytest.raises(VerificationError, match="is not a file"):
        frame_bytes(Path("/frames/no-such-frame.jpg"))


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


def test_a_body_that_is_not_json_is_reported_as_the_text_that_arrived() -> None:
    """A proxy error page must name what answered, not raise a decode error."""
    assert (
        verify_groundstation_image._decoded(b"<html>502</html>") == "<html>502</html>"
    )
    assert verify_groundstation_image._decoded(b'{"ready": true}') == {"ready": True}


class _FakeWriter:
    """Records the request and answers the calls a stream writer gets.

    Attributes:
        written: Everything the caller wrote.
    """

    def __init__(self) -> None:
        """Start with nothing written."""
        self.written = bytearray()

    def write(self, data: bytes) -> None:
        """Record outgoing bytes.

        Args:
            data: What the caller wrote.
        """
        self.written += data

    async def drain(self) -> None:
        """Accept a flush that has nowhere to go."""

    def close(self) -> None:
        """Accept a close that has nothing to close."""

    async def wait_closed(self) -> None:
        """Accept a wait for a close that already happened."""


class _FakeConnection:
    """Records how the connection was opened, and answers with canned bytes.

    How it was opened is half of what the feed reader decides: the host and the
    port come from the base URL, and whether there is a TLS context at all is
    the difference between reaching an `https://` deployment and failing to.

    Attributes:
        writer: What the request was written to.
        opened: The positional and keyword arguments the connection was asked
            for, or `None` until it has been.
    """

    def __init__(self, response: bytes) -> None:
        """Start with nothing opened.

        Args:
            response: The bytes the service is pretending to send.
        """
        self.writer = _FakeWriter()
        self.opened: tuple[tuple[object, ...], dict[str, object]] | None = None
        self._response = response

    async def connect(
        self,
        *args: object,
        **kwargs: object,
    ) -> tuple[asyncio.StreamReader, _FakeWriter]:
        """Stand in for `asyncio.open_connection`.

        Args:
            args: The host and port it was asked for.
            kwargs: Everything else, `ssl` included.

        Returns:
            A reader holding the canned response, and the recording writer.
        """
        self.opened = (args, kwargs)
        reader = asyncio.StreamReader()
        reader.feed_data(self._response)
        reader.feed_eof()
        return reader, self.writer


def _answering(
    monkeypatch: pytest.MonkeyPatch,
    response: bytes,
) -> _FakeConnection:
    """Make the next connection deliver a canned response.

    A `StreamReader` fed in memory rather than a socket: what is under test is
    the parsing and the connection's own parameters, and a real listener would
    add input and output for nothing — a TLS one would add a certificate too.

    Args:
        monkeypatch: How the connection is replaced.
        response: The bytes the service is pretending to send.

    Returns:
        The connection, which records how it was opened and what was written.
    """
    connection = _FakeConnection(response)
    monkeypatch.setattr(
        "verify_groundstation_image.asyncio.open_connection",
        connection.connect,
    )
    return connection


def _stream_response(body: bytes, status: bytes = b"200 OK") -> bytes:
    """Build what the feed would have written for one part.

    Args:
        body: The part's payload.
        status: The status line's code and reason.

    Returns:
        The whole response, headers and part alike.

    """
    return (
        b"HTTP/1.0 "
        + status
        + b"\r\nContent-Type: multipart/x-mixed-replace; boundary=reachyframe\r\n"
        b"Cache-Control: no-store\r\n\r\n"
        b"--reachyframe\r\nContent-Type: image/jpeg\r\n"
        b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body
    )


@pytest.mark.asyncio
async def test_one_part_is_read_off_the_feed_by_its_declared_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The part is taken by its length, which is what a length is for.

    Args:
        monkeypatch: How the connection is replaced.
    """
    payload = b"\xff\xd8\xff" + bytes(range(64))
    connection = _answering(monkeypatch, _stream_response(payload))

    headers, body = await read_feed_part(_BASE)

    assert body == payload
    assert headers["content-type"] == "image/jpeg"
    assert f"GET {STREAM_PATH} HTTP/1.0".encode("ascii") in connection.writer.written
    assert b"Host: groundstation:8080" in connection.writer.written


@pytest.mark.asyncio
async def test_a_feed_that_refuses_is_reported_with_the_code_it_refused_with(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """503 here means the session this run holds open did not make it eligible.

    Args:
        monkeypatch: How the connection is replaced.
    """
    _answering(
        monkeypatch,
        b'HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json\r\n\r\n{"feed":"no_eligible_session"}',
    )
    with pytest.raises(VerificationError, match="answered 503"):
        await read_feed_part(_BASE)


@pytest.mark.asyncio
async def test_a_feed_that_is_not_a_multipart_stream_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proxy answering 200 with its own page is not the service streaming.

    Args:
        monkeypatch: How the connection is replaced.
    """
    _answering(
        monkeypatch,
        b"HTTP/1.0 200 OK\r\nContent-Type: text/html\r\n\r\n<html>hello</html>",
    )
    with pytest.raises(VerificationError, match="not as a multipart stream"):
        await read_feed_part(_BASE)


@pytest.mark.asyncio
async def test_a_part_shorter_than_it_declared_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading fewer bytes than a length promised is a failure, not a short read.

    Args:
        monkeypatch: How the connection is replaced.
    """
    truncated = _stream_response(b"\xff\xd8\xff" + bytes(64))[:-32]
    _answering(monkeypatch, truncated)
    with pytest.raises(VerificationError, match="the feed closed after"):
        await read_feed_part(_BASE)


@pytest.mark.asyncio
async def test_something_that_is_not_an_http_response_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wrong port answers with something, and it is worth naming.

    Args:
        monkeypatch: How the connection is replaced.
    """
    _answering(monkeypatch, b"not an http server\r\n\r\n")
    with pytest.raises(VerificationError, match="not an HTTP response"):
        await read_feed_part(_BASE)


@pytest.mark.parametrize(
    ("base", "host", "port", "secure"),
    [
        ("http://groundstation:8080", "groundstation", 8080, False),
        ("http://groundstation", "groundstation", 80, False),
        ("https://groundstation", "groundstation", 443, True),
        ("https://groundstation:8443/", "groundstation", 8443, True),
    ],
)
@pytest.mark.asyncio
async def test_the_feed_connection_follows_the_scheme_of_the_base_url(
    monkeypatch: pytest.MonkeyPatch,
    base: str,
    host: str,
    port: int,
    secure: bool,
) -> None:
    """`https://` is a supported base URL, and this is the third path that reads it.

    `session_url` maps it to `wss://` and readiness reaches it through urllib, so
    a feed reader that opened a plaintext socket on port 80 would fail against
    the deployment the rest of the script supports — and fail as though the
    service were broken.

    Args:
        monkeypatch: How the connection is replaced.
        base: The base URL the script is given.
        host: The host it should connect to.
        port: The port it should connect to.
        secure: Whether that connection should be wrapped in TLS.
    """
    connection = _answering(monkeypatch, _stream_response(b"\xff\xd8\xff"))

    await read_feed_part(base)

    assert connection.opened is not None
    args, kwargs = connection.opened
    assert args == (host, port)
    assert isinstance(kwargs["ssl"], ssl.SSLContext) is secure


@pytest.mark.asyncio
async def test_an_https_feed_connection_verifies_the_certificate_it_is_given() -> None:
    """Encrypted is not the point; reaching the intended host is.

    A context that accepted any certificate would let a misdirected connection
    verify an image, which is the failure this whole script exists to catch.
    """
    endpoint = verify_groundstation_image._feed_endpoint("https://groundstation")
    assert endpoint.tls is not None
    assert endpoint.tls.verify_mode is ssl.CERT_REQUIRED
    assert endpoint.tls.check_hostname is True


@pytest.mark.parametrize("base", ["groundstation:8080", "ftp://example.invalid", ""])
@pytest.mark.asyncio
async def test_the_feed_refuses_the_base_urls_the_session_endpoint_refuses(
    base: str,
) -> None:
    """One scheme rule for the whole script, not one per code path.

    Args:
        base: A base URL no part of this script speaks.
    """
    with pytest.raises(VerificationError, match="http or https"):
        await read_feed_part(base)
