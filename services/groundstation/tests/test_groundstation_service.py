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
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from groundstation_support import (
    CREDENTIAL,
    ECHO,
    EchoCapability,
    make_settings,
)

import reachy_groundstation
from reachy_groundstation.config import ENV_PREFIX, REDACTED_SET, Settings
from reachy_groundstation.obs import build_observability
from reachy_groundstation.service import build_application, main

if TYPE_CHECKING:
    from reachy_groundstation.ports import CapabilityPort

_PACKAGE_ROOT = Path(reachy_groundstation.__file__).parent
_GUARDED = ("api", "session", "pipeline")
_FORBIDDEN = "reachy_groundstation.capabilities"


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

    def _run(app: object, **kwargs: object) -> None:
        """Record that the server would have started, without starting it.

        Args:
            app: The application, unused.
            kwargs: How it would have been served.
        """
        del app
        served.append(dict(kwargs))

    monkeypatch.setattr("reachy_groundstation.service.uvicorn.run", _run)
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
