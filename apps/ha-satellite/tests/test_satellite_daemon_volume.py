"""Turning the daemon's own coarse volume up, and failing quietly if it will not.

R7. Every test here is a unit test: `urlopen` is replaced, so nothing opens a
socket — which the harness would refuse anyway. What is under test is the
decisions around the request, because the request itself is four lines and the
decisions are where a robot ends up mute or a start-up ends up refused.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Final

import pytest

from reachy_mini_ha_satellite.adapters.daemon_volume import (
    MAX_DAEMON_VOLUME,
    set_daemon_volume,
)

if TYPE_CHECKING:
    from types import TracebackType

# The loopback address the daemon serves on, which is also this setting's
# default. Not anybody's environment: it is the loopback literal.
_DAEMON: Final = "http://127.0.0.1:8000"


class _Response:
    """Enough of an HTTP response for the one thing the client reads."""

    def __init__(self, status: int) -> None:
        """Say what the daemon answered.

        Args:
            status: The status code.
        """
        self.status = status

    def __enter__(self) -> _Response:
        """Enter the context the client opens it in.

        Returns:
            This response.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Leave it.

        Args:
            exc_type: The exception's type, if one is propagating.
            exc: The exception, if one is propagating.
            traceback: Its traceback, if one is propagating.
        """


class _Recorder:
    """Stands in for `urlopen`, recording what it was asked to send."""

    def __init__(self, status: int = 200) -> None:
        """Say what to answer with.

        Args:
            status: The status every request gets.
        """
        self.status = status
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request, **kwargs: object) -> _Response:
        """Record a request instead of making it.

        Args:
            request: What would have gone to the daemon.
            kwargs: The timeout, which is not asserted on here.

        Returns:
            The scripted response.
        """
        del kwargs
        self.requests.append(request)
        return _Response(self.status)


def _body(request: urllib.request.Request) -> object:
    """Read back the JSON a request was built with.

    Args:
        request: What the client assembled.

    Returns:
        The decoded body.
    """
    data = request.data
    assert isinstance(data, bytes)
    return json.loads(data)


@pytest.fixture(name="urlopen")
def _urlopen(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """Replace the one call that would open a socket.

    Args:
        monkeypatch: Used to swap `urlopen` for the duration of a test.

    Returns:
        The recorder that replaced it.
    """
    recorder = _Recorder()
    monkeypatch.setattr(urllib.request, "urlopen", recorder)
    return recorder


class TestAskingTheDaemonForItsLoudest:
    """The request R7 is, and what it carries."""

    def test_it_posts_the_level_to_the_daemons_own_endpoint(
        self,
        urlopen: _Recorder,
    ) -> None:
        """The volume is not on the media interface, so this is how it is reached.

        Args:
            urlopen: The recorder standing in for the network.
        """
        assert set_daemon_volume(_DAEMON)

        sent = urlopen.requests[0]
        assert sent.full_url == f"{_DAEMON}/api/volume/set"
        assert sent.get_method() == "POST"
        assert _body(sent) == {"volume": MAX_DAEMON_VOLUME}

    def test_the_maximum_is_a_hundred(self) -> None:
        """Which is what the daemon's own API takes."""
        assert MAX_DAEMON_VOLUME == 100

    def test_a_trailing_slash_does_not_double_up(
        self,
        urlopen: _Recorder,
    ) -> None:
        """An operator who pasted the address with one is not making a mistake.

        Args:
            urlopen: The recorder standing in for the network.
        """
        set_daemon_volume(f"{_DAEMON}/")

        assert urlopen.requests[0].full_url == f"{_DAEMON}/api/volume/set"

    def test_a_level_can_be_asked_for_explicitly(
        self,
        urlopen: _Recorder,
    ) -> None:
        """The default is the maximum; the argument is what makes that a choice.

        Args:
            urlopen: The recorder standing in for the network.
        """
        set_daemon_volume(_DAEMON, 40)

        assert _body(urlopen.requests[0]) == {"volume": 40}


class TestWhenItDoesNotWork:
    """None of these may stop the satellite starting."""

    def test_no_configured_address_asks_nothing(self, urlopen: _Recorder) -> None:
        """Which is also how an operator turns the daemon's test sound off.

        Args:
            urlopen: The recorder, which should record nothing.
        """
        assert not set_daemon_volume("")
        assert urlopen.requests == []

    def test_an_address_that_is_not_http_is_refused(
        self,
        urlopen: _Recorder,
    ) -> None:
        """The same allowlist `sounds.py` applies, and for the same reason.

        Args:
            urlopen: The recorder, which should record nothing.
        """
        assert not set_daemon_volume("file:///etc/passwd")
        assert urlopen.requests == []

    def test_a_daemon_that_is_not_there_is_reported_rather_than_raised(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A satellite that could not raise the volume still answers wake words.

        Args:
            monkeypatch: Used to make the request fail.
        """

        def _refuse(*args: object, **kwargs: object) -> _Response:
            """Fail the way a closed port does.

            Args:
                args: Ignored.
                kwargs: Ignored.

            Returns:
                Nothing; this always raises.

            Raises:
                OSError: Always.
            """
            del args, kwargs
            message = "connection refused"
            raise OSError(message)

        monkeypatch.setattr(urllib.request, "urlopen", _refuse)

        assert not set_daemon_volume(_DAEMON)

    def test_a_daemon_that_refuses_the_level_is_reported_too(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An HTTP error is not an exception the caller should have to catch.

        Args:
            monkeypatch: Used to make the request fail with a status.
        """

        def _error(*args: object, **kwargs: object) -> _Response:
            """Fail the way a rejected request does.

            Args:
                args: Ignored.
                kwargs: Ignored.

            Returns:
                Nothing; this always raises.

            Raises:
                HTTPError: Always.
            """
            del args, kwargs
            raise urllib.error.HTTPError(
                f"{_DAEMON}/api/volume/set",
                500,
                "Internal Server Error",
                {},  # type: ignore[arg-type]  # the headers are not read, and the real type is an email.message.Message this test has no use for
                None,
            )

        monkeypatch.setattr(urllib.request, "urlopen", _error)

        assert not set_daemon_volume(_DAEMON)

    def test_a_malformed_answer_does_not_stop_the_application(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`BadStatusLine` and its siblings are not `OSError`s.

        `urllib.error.HTTPError` is one, so catching `OSError` covers it — but
        `http.client.BadStatusLine`, `IncompleteRead` and `LineTooLong` derive
        from `Exception` instead. One of those escaping here would escape
        `VolumeService.start` and take the application down through the
        service-startup loop, over a volume this function is documented as
        setting on a best-effort basis.

        Args:
            monkeypatch: Used to make the daemon answer badly.
        """

        def _garble(*args: object, **kwargs: object) -> _Response:
            """Answer the way a daemon speaking nonsense does.

            Args:
                args: Ignored.
                kwargs: Ignored.

            Returns:
                Nothing; this always raises.

            Raises:
                BadStatusLine: Always.
            """
            del args, kwargs
            raise http.client.BadStatusLine("not a status line")

        monkeypatch.setattr(urllib.request, "urlopen", _garble)

        assert not set_daemon_volume(_DAEMON)

    def test_a_status_outside_the_two_hundreds_is_not_success(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A daemon that answered without doing it has not done it.

        Args:
            monkeypatch: Used to answer with a redirect.
        """
        monkeypatch.setattr(urllib.request, "urlopen", _Recorder(status=302))

        assert not set_daemon_volume(_DAEMON)
