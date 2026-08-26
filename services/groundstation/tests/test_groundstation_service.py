"""The composition root, and the boundary it is the only module allowed to cross.

Two things are checked. That startup does what architecture REQ-009 says — reads
the environment once, refuses to start on a variable it does not recognise, and
says out loud what it resolved. And that the import direction the capability
boundary depends on actually holds in the tree, by reading the modules rather
than by trusting them.

The lint rule is the primary enforcement and `just lint-capability-boundary`
proves it fires. This is the second pair of eyes: it walks the real source and
catches an import that arrived any other way.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import ast
import importlib
import json
import signal
from pathlib import Path
from typing import TYPE_CHECKING, cast

import httpx
import pytest
import uvicorn
from groundstation_support import (
    CREDENTIAL,
    ECHO,
    EchoCapability,
    jpeg_bytes,
    make_settings,
)

import reachy_groundstation
from reachy_groundstation.api.app import STREAM_PATH
from reachy_groundstation.config import ENV_PREFIX, REDACTED_SET, Settings
from reachy_groundstation.feed import FeedRegistry
from reachy_groundstation.obs import build_observability
from reachy_groundstation.service import (
    FeedClosingServer,
    build_application,
    build_server,
    main,
)

if TYPE_CHECKING:
    from starlette.types import ASGIApp

    from reachy_groundstation.ports import CapabilityPort

_PACKAGE_ROOT = Path(reachy_groundstation.__file__).parent
_GUARDED = ("api", "session", "pipeline")
_FORBIDDEN = "reachy_groundstation.capabilities"


async def _unserved(scope: object, receive: object, send: object) -> None:
    """Stand in for an application nothing will ever call.

    Args:
        scope: The ASGI scope, never supplied.
        receive: The ASGI receive channel, never supplied.
        send: The ASGI send channel, never supplied.

    Raises:
        AssertionError: If anything does call it after all.
    """
    del scope, receive, send
    message = "the unserved application was called"
    raise AssertionError(message)


def _echo(settings: Settings) -> CapabilityPort:
    """Build the echo capability.

    Args:
        settings: The settings in effect, unused.

    Returns:
        The capability.
    """
    del settings
    return EchoCapability()


def _imported_modules(source: str) -> set[str]:
    """List every module a source file imports.

    Args:
        source: The file's text.

    Returns:
        The imported module names, absolute.

    Raises:
        AssertionError: If a relative import appears, which this package does
            not use and which this check could not resolve.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                message = "this package uses absolute imports throughout"
                raise AssertionError(message)
            if node.module:
                names.add(node.module)
                names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


@pytest.mark.filesystem
@pytest.mark.parametrize("package", _GUARDED)
def test_no_guarded_package_imports_a_capability(package: str) -> None:
    """The dependency runs from the capabilities inwards, never the reverse.

    Reading the tree is input, so this is not a unit test and says so. The bytes
    on disk are what the check is about: an assertion against a fake would pin
    whatever the fake was told to contain.

    Args:
        package: The guarded package to walk.
    """
    for module in sorted((_PACKAGE_ROOT / package).rglob("*.py")):
        imported = _imported_modules(module.read_text(encoding="utf-8"))
        offending = {name for name in imported if name.startswith(_FORBIDDEN)}
        assert offending == set(), f"{module} imports {sorted(offending)}"


@pytest.mark.filesystem
def test_the_composition_root_is_the_one_module_that_may() -> None:
    """A boundary nothing crosses is a boundary nobody needs; this one is used."""
    imported = _imported_modules(
        (_PACKAGE_ROOT / "service.py").read_text(encoding="utf-8"),
    )
    assert any(name.startswith(_FORBIDDEN) for name in imported)


def test_the_application_is_built_around_the_registry_it_is_given() -> None:
    """The composition root composes; nothing else knows what is registered."""
    settings = make_settings()
    app, registry = build_application(settings, build_observability(settings), [_echo])
    assert registry.supported() == ()
    paths = {getattr(route, "path", None) for route in app.routes}
    assert {"/livez", "/readyz", "/metrics", "/config", "/v1/session"} <= paths


@pytest.mark.asyncio
async def test_the_built_registry_warms_up_what_was_registered() -> None:
    """What the application serves is what the factories produced."""
    settings = make_settings()
    _app, registry = build_application(settings, build_observability(settings), [_echo])
    await registry.warm_up()
    assert registry.supported() == (ECHO,)


#:= docs/specs/architecture/index.md#req-009-configuration-is-validated-and-self-reporting
#:% Every component that reads configuration from its environment MUST fail to start
#:% when it encounters a variable matching its own prefix that it does not
#:% recognise, and MUST emit its fully resolved configuration at startup with every
#:% value marked secret replaced by a redacted placeholder.
def test_startup_refuses_an_unrecognised_variable_and_names_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The typo stops the process rather than leaving a default in effect.

    Args:
        monkeypatch: Used to set the process environment this reads.
        capsys: Used to read what was written to standard error.
    """
    monkeypatch.setenv(f"{ENV_PREFIX}CREDENTIAL", CREDENTIAL)
    monkeypatch.setenv(f"{ENV_PREFIX}PROT", "9443")
    status = main([])
    assert status == 78
    assert f"{ENV_PREFIX}PROT" in capsys.readouterr().err


def test_startup_refuses_a_missing_credential(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A groundstation that authenticated nothing would be the worse failure.

    Args:
        monkeypatch: Used to clear the process environment this reads.
        capsys: Used to read what was written to standard error.
    """
    monkeypatch.delenv(f"{ENV_PREFIX}CREDENTIAL", raising=False)
    assert main([]) == 78
    assert "CREDENTIAL" in capsys.readouterr().err


def test_the_entry_point_takes_no_arguments(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A deployment is described in one place, and that place is the environment."""
    assert main(["--port", "9443"]) == 2
    assert "REACHY_GROUNDSTATION_" in capsys.readouterr().err


def test_startup_emits_the_resolved_configuration_before_serving(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The log is written whether or not anything ever connects.

    Standard output is read rather than the processor chain intercepted, because
    what is being checked is that the line an operator will actually see carries
    every setting and no secret.

    Args:
        monkeypatch: Used to set the environment and to stop short of serving.
        capsys: Used to read the line the service wrote.
    """
    monkeypatch.setenv(f"{ENV_PREFIX}CREDENTIAL", CREDENTIAL)
    monkeypatch.setenv(f"{ENV_PREFIX}PORT", "9443")
    served: list[dict[str, object]] = []

    def _run(server: uvicorn.Server) -> None:
        """Record that the server would have started, without starting it.

        Args:
            server: The server `main` built, read for how it was configured.
        """
        served.append({"host": server.config.host, "port": server.config.port})

    # The base class rather than the subclass, because what this stops short of
    # is serving and every server this package builds inherits that from here.
    monkeypatch.setattr(uvicorn.Server, "run", _run)
    assert main([]) == 0

    lines = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    resolved = [line for line in lines if line["event"] == "configuration.resolved"]
    assert resolved[0]["port"] == 9443
    assert resolved[0]["credential"] == REDACTED_SET
    assert CREDENTIAL not in str(resolved[0])
    assert set(Settings.model_fields) <= set(resolved[0])
    assert served[0]["port"] == 9443


def test_the_module_entry_point_defers_to_the_service() -> None:
    """`python -m reachy_groundstation` runs the same startup the tests drive."""
    module = importlib.import_module("reachy_groundstation.__main__")
    assert module.main is main


@pytest.mark.asyncio
async def test_the_composition_root_wires_capability_shutdown() -> None:
    """Closing the registry is the application's own business, not the caller's."""
    settings = make_settings()
    closed: list[str] = []

    class _Closing(EchoCapability):
        async def aclose(self) -> None:
            closed.append(self.descriptor.name)

    app, registry = build_application(
        settings,
        build_observability(settings),
        [lambda _: _Closing()],
    )
    async with app.router.lifespan_context(app):
        assert registry.health()
    assert closed == ["echo"]


def test_a_shutdown_signal_closes_the_feed_before_the_server_starts_stopping() -> None:
    """The order is the fix: uvicorn waits for viewers before it says shutdown.

    A `handle_exit` that only delegated would leave the feed to the lifespan,
    which uvicorn sends after it has waited for every open response — and a
    viewer parked on the feed is one of those.
    """
    feed = FeedRegistry()
    # An application that is never served, because what is under test happens
    # before serving starts. `log_config=None` for the same reason production
    # passes it: uvicorn would otherwise reconfigure this process's logging.
    config = uvicorn.Config(_unserved, log_config=None)
    server = FeedClosingServer(config, feed=feed)

    server.handle_exit(signal.SIGTERM, None)

    assert server.should_exit is True
    with feed.authenticated_session():
        assert feed.publish(jpeg_bytes()) is False
    # A second signal reaches a feed that is already closed and asks uvicorn to
    # stop waiting; neither step minds the other having happened.
    server.handle_exit(signal.SIGTERM, None)
    assert server.should_exit is True


@pytest.mark.asyncio
async def test_the_server_and_the_application_are_composed_around_one_feed() -> None:
    """Two feeds would close the wrong viewers and look exactly like one.

    Both halves are checked against the same session and the same frame, which
    is what makes the answer discriminating: `/stream.mjpg` says 200 only if the
    application is reading this feed, and it says 503 afterwards — with the
    session still open and the frame still published — only if the signal
    reached this feed too.
    """
    settings = make_settings()
    feed = FeedRegistry()
    server = build_server(settings, build_observability(settings), [_echo], feed=feed)

    # `uvicorn.Config` types its application widely enough to include a string;
    # this one is the Starlette instance `build_application` just returned.
    app = cast("ASGIApp", server.config.app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://groundstation.invalid",
    ) as client:
        with feed.authenticated_session():
            feed.publish(jpeg_bytes())
            assert (await client.head(STREAM_PATH)).status_code == 200

            server.handle_exit(signal.SIGTERM, None)

            assert (await client.head(STREAM_PATH)).status_code == 503
