"""The daemon's entry point, exercised without the SDK being importable.

`daemon_app` is the only module in this package that imports the Reachy Mini
SDK, and importing the SDK executes `import gi` three modules away — which is
exactly why the import is confined to one file. The tests below stand a stub in
its place before importing the module, so what is under test is this
repository's code and the runner needs no GStreamer, which is architecture
REQ-005.

The stub is the SDK's own shape: the two class attributes the dashboard reads,
the `threading.Event` the daemon hands over, and `wrapped_run`, which is what
the daemon calls.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import asyncio
import importlib
import runpy
import sys
import threading
from types import ModuleType
from typing import TYPE_CHECKING

import pytest
from satellite_support import FakeRobot

from reachy_mini_ha_satellite import main as satellite_main
from reachy_mini_ha_satellite.config import (
    ConfigurationError,
    OverrideStore,
    Settings,
    overrides_path,
    variable_for,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from pyfakefs.fake_filesystem import FakeFilesystem

_SDK_MODULES = ("reachy_mini", "reachy_mini.apps", "reachy_mini.apps.app")
_UNDER_TEST = "reachy_mini_ha_satellite.daemon_app"


class StubReachyMiniApp:
    """The SDK's application base class, in the shape the daemon uses it."""

    custom_app_url: str | None = None
    dont_start_webserver: bool = False

    def __init__(self, running_on_wireless: bool = False) -> None:
        """Prepare, without checking whether a daemon is listening.

        Args:
            running_on_wireless: The daemon's own flag.
        """
        self.running_on_wireless = running_on_wireless
        self.stop_event = threading.Event()
        self.handle = FakeRobot()

    def wrapped_run(self, *args: object, **kwargs: object) -> None:
        """Run the application with a robot handle, as the daemon does.

        Args:
            args: Unused; the SDK passes them to its own robot constructor.
            kwargs: Unused, for the same reason.
        """
        del args, kwargs
        self.run(self.handle, self.stop_event)

    def run(self, reachy_mini: object, stop_event: threading.Event) -> None:
        """Overridden by the subclass under test.

        Args:
            reachy_mini: The robot handle.
            stop_event: The daemon's termination signal.
        """
        raise NotImplementedError

    def stop(self) -> None:
        """Ask the application to stop, as the daemon does."""
        self.stop_event.set()


@pytest.fixture
def daemon_app(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    """Import the entry-point module against a stubbed SDK.

    Args:
        monkeypatch: Used to install the stub in `sys.modules` and to take it
            back out, so no other test sees a half-real SDK.

    Yields:
        The module under test.
    """
    package = ModuleType("reachy_mini")
    apps = ModuleType("reachy_mini.apps")
    app = ModuleType("reachy_mini.apps.app")
    app.ReachyMiniApp = StubReachyMiniApp  # type: ignore[attr-defined]  # a stub module has whatever attributes it is given
    for name, module in zip(_SDK_MODULES, (package, apps, app), strict=True):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delitem(sys.modules, _UNDER_TEST, raising=False)

    yield importlib.import_module(_UNDER_TEST)

    sys.modules.pop(_UNDER_TEST, None)


class TestWhatTheDaemonReads:
    """The two class attributes the dashboard looks at."""

    def test_it_subclasses_the_daemon_s_application_base_class(
        self,
        daemon_app: ModuleType,
    ) -> None:
        """Which is what the `reachy_mini_apps` entry point has to resolve to.

        Args:
            daemon_app: The module under test.
        """
        assert issubclass(daemon_app.ReachyMiniHaSatellite, StubReachyMiniApp)

    def test_the_sdk_s_own_settings_server_is_switched_off(
        self,
        daemon_app: ModuleType,
    ) -> None:
        """This application serves its own, with its own redaction rules.

        Args:
            daemon_app: The module under test.
        """
        assert daemon_app.ReachyMiniHaSatellite.dont_start_webserver

    def test_the_dashboard_link_matches_the_default_port(
        self,
        daemon_app: ModuleType,
    ) -> None:
        """A link to a closed port is worse than no link.

        Args:
            daemon_app: The module under test.
        """
        default = Settings.model_fields["web_port"].default

        assert daemon_app.DEFAULT_SETTINGS_URL.endswith(f":{default}")

    def test_the_link_follows_a_configured_port(
        self,
        daemon_app: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """So moving the interface does not silently break the dashboard's link.

        Args:
            daemon_app: The module under test.
            monkeypatch: Used to set the port in the environment.
        """
        monkeypatch.setenv(variable_for("state_dir"), "/reachy-satellite-nowhere")
        monkeypatch.setenv(variable_for("web_port"), "9100")

        application = daemon_app.ReachyMiniHaSatellite()

        assert application.custom_app_url.endswith(":9100")

    def test_the_link_ignores_a_port_left_in_the_overrides_file(
        self,
        daemon_app: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        fs: FakeFilesystem,
    ) -> None:
        """The link has to resolve the port the way the interface binds it.

        `web_port` is a bootstrap setting, so `load_settings` ignores an
        override for it and the interface binds what the environment says. A
        link that read the override would point at a port nothing bound — the
        same failure from the other end.

        Args:
            daemon_app: The module under test.
            monkeypatch: Used to point the state directory at the fake
                filesystem and to set the port in the environment.
            fs: An in-memory filesystem to write a stale override into.
        """
        del fs
        monkeypatch.setenv(variable_for("state_dir"), "/reachy-satellite-daemon")
        monkeypatch.setenv(variable_for("web_port"), "9100")
        OverrideStore(overrides_path()).save({"web_port": "9200"})

        application = daemon_app.ReachyMiniHaSatellite()

        assert application.custom_app_url.endswith(":9100")

    def test_a_port_that_is_not_a_number_leaves_the_link_alone(
        self,
        daemon_app: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Refusing the value is `load_settings`'s job, and it does it later.

        Args:
            daemon_app: The module under test.
            monkeypatch: Used to set a nonsense port.
        """
        monkeypatch.setenv(variable_for("state_dir"), "/reachy-satellite-nowhere")
        monkeypatch.setenv(variable_for("web_port"), "not a port")

        application = daemon_app.ReachyMiniHaSatellite()

        assert application.custom_app_url == daemon_app.DEFAULT_SETTINGS_URL


class TestBridgingTheStopSignal:
    """A `threading.Event` in, an `asyncio.Event` out."""

    def test_the_daemon_s_signal_stops_the_application(
        self,
        daemon_app: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An `asyncio.Event` set from another thread would be a silent no-op.

        Args:
            daemon_app: The module under test.
            monkeypatch: Used to replace the composition root with something
                that records the handle it was given and waits to be stopped.
        """
        seen: list[object] = []

        async def _run(handle: object, stop: asyncio.Event) -> None:
            """Record the handle and wait for the stop event.

            Args:
                handle: What the daemon supplied.
                stop: The event the bridge sets.
            """
            seen.append(handle)
            await stop.wait()

        monkeypatch.setattr(daemon_app, "run", _run)
        application = daemon_app.ReachyMiniHaSatellite()
        threading.Timer(0.0, application.stop).start()

        application.wrapped_run()

        assert seen == [application.handle]

    def test_finishing_for_its_own_reasons_leaves_no_thread_on_a_closed_loop(
        self,
        daemon_app: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The settings page asking it to stop is the ordinary way in.

        The watcher wakes on the event the coroutine sets on its way out, and
        reaches for a loop `asyncio.run` is about to close. Joining it before
        returning is what keeps that call inside the loop's lifetime; without
        it, an ordinary shutdown ends with a traceback on a daemon thread.

        Args:
            daemon_app: The module under test.
            monkeypatch: Used to replace the composition root with one that
                returns at once.
        """
        failures: list[str] = []
        monkeypatch.setattr(
            threading,
            "excepthook",
            lambda args: failures.append(str(args.exc_value)),
        )

        async def _run(handle: object, stop: asyncio.Event) -> None:
            """Return immediately, as a stop request from the page makes it.

            Args:
                handle: Unused.
                stop: Unused.
            """
            del handle, stop

        monkeypatch.setattr(daemon_app, "run", _run)
        application = daemon_app.ReachyMiniHaSatellite()

        application.wrapped_run()

        assert failures == []

    def test_finishing_for_its_own_reasons_releases_the_watcher(
        self,
        daemon_app: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The settings interface asking for a restart is one such reason.

        Args:
            daemon_app: The module under test.
            monkeypatch: Used to replace the composition root with one that
                returns at once.
        """

        async def _run(handle: object, stop: asyncio.Event) -> None:
            """Return immediately.

            Args:
                handle: Unused.
                stop: Unused.
            """
            del handle, stop

        monkeypatch.setattr(daemon_app, "run", _run)
        application = daemon_app.ReachyMiniHaSatellite()

        application.wrapped_run()

        assert application.stop_event.is_set()


class TestRunningItDirectly:
    """`python -m reachy_mini_ha_satellite`, for the deployment session."""

    def test_it_takes_no_arguments(self, daemon_app: ModuleType) -> None:
        """Everything is configuration, in one place.

        Args:
            daemon_app: The module under test.
        """
        assert daemon_app.main(["--verbose"]) == 2

    def test_an_unusable_configuration_exits_with_the_configuration_status(
        self,
        daemon_app: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """EX_CONFIG, which is what an init system should see.

        Args:
            daemon_app: The module under test.
            monkeypatch: Used to make startup refuse.
        """

        async def _run(handle: object, stop: asyncio.Event) -> None:
            """Refuse to start.

            Args:
                handle: Unused.
                stop: Unused.

            Raises:
                ConfigurationError: Always.
            """
            del handle, stop
            message = "REACHY_SATELLITE_DEVICE_NAME is not set"
            raise ConfigurationError(message)

        monkeypatch.setattr(daemon_app, "run", _run)

        assert daemon_app.main([]) == 78

    def test_a_clean_run_exits_successfully(
        self,
        daemon_app: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The ordinary way out.

        Args:
            daemon_app: The module under test.
            monkeypatch: Used to replace the composition root.
        """

        async def _run(handle: object, stop: asyncio.Event) -> None:
            """Return immediately.

            Args:
                handle: Unused.
                stop: Unused.
            """
            del handle, stop

        monkeypatch.setattr(daemon_app, "run", _run)

        assert daemon_app.main([]) == 0

    def test_a_termination_signal_asks_it_to_stop(
        self,
        daemon_app: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Run outside the daemon, nothing else is listening for one.

        Args:
            daemon_app: The module under test.
            monkeypatch: Used to capture what was installed.
        """
        import signal

        installed: dict[int, Callable[..., None]] = {}
        monkeypatch.setattr(
            signal,
            "signal",
            lambda number, handler: installed.__setitem__(number, handler),
        )
        application = daemon_app.ReachyMiniHaSatellite()

        daemon_app._stop_on_signals(application)
        installed[signal.SIGTERM]()

        assert application.stop_event.is_set()

    def test_a_platform_that_refuses_the_handler_is_survived(
        self,
        daemon_app: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Not the main thread is the usual reason, and it is not fatal.

        Args:
            daemon_app: The module under test.
            monkeypatch: Used to make installing a handler fail.
        """
        import signal

        def _refuse(number: int, handler: object) -> None:
            """Refuse to install a handler.

            Args:
                number: Which signal.
                handler: What was offered.

            Raises:
                ValueError: Always, as the standard library does off the main
                    thread.
            """
            del number, handler
            message = "signal only works in main thread"
            raise ValueError(message)

        monkeypatch.setattr(signal, "signal", _refuse)

        daemon_app._stop_on_signals(daemon_app.ReachyMiniHaSatellite())

        assert True  # not raising is the assertion


class TestTheModuleEntryPoint:
    """`python -m` and the daemon's entry point are one command surface."""

    def test_the_module_entry_point_reaches_the_same_function(
        self,
        daemon_app: ModuleType,
    ) -> None:
        """So there is one startup path with tests on it, not two.

        Args:
            daemon_app: The module under test.
        """
        sys.modules.pop("reachy_mini_ha_satellite.__main__", None)
        module = importlib.import_module("reachy_mini_ha_satellite.__main__")

        assert module.main is daemon_app.main

    def test_running_the_package_reaches_that_function(
        self,
        daemon_app: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`python -m reachy_mini_ha_satellite`, which the runbooks document.

        The test above asserts the two names refer to one function, which is
        not the same as the command working: it never executes the guard that
        calls it. This does, by the machinery `python -m` uses, so a documented
        way in cannot quietly stop being one.

        Args:
            daemon_app: The module under test, imported against the stubbed
                SDK so that running the package finds the stub.
            monkeypatch: Used to put an argument on the command line.
        """
        del daemon_app
        monkeypatch.setattr(sys, "argv", ["reachy_mini_ha_satellite", "--verbose"])
        monkeypatch.delitem(
            sys.modules,
            "reachy_mini_ha_satellite.__main__",
            raising=False,
        )

        with pytest.raises(SystemExit) as raised:
            runpy.run_module("reachy_mini_ha_satellite", run_name="__main__")

        assert raised.value.code == 2


class TestBeingExecutedTheWayTheDaemonExecutesIt:
    """The launch that actually happens on the robot, and it is not an import.

    The Reachy Mini daemon does not resolve the `reachy_mini_apps` entry point
    to its object and instantiate it. It takes the **module** half — everything
    left of the colon — and starts the application as a subprocess,
    `python -u -m reachy_mini_ha_satellite.daemon_app`. So the module the entry
    point names has to be a program as well as an import target, and
    `__main__.py` does not make it one: that file makes the *package* runnable,
    which is a different name.

    Without the guard at the foot of `daemon_app`, that command imports the
    module, finds nothing to do and exits 0 — which the daemon reports as an
    application that finished successfully, seconds after starting, with no
    output at all. That is what happened on the robot, and these are the tests
    that go red if it happens again.

    `runpy.run_module` with `run_name="__main__"` is that command's own
    machinery — it is what `python -m` runs — so this exercises the execution
    path rather than asserting that some text appears in the file. It runs
    in-process against the stubbed SDK, so no subprocess is started and the
    tests stay ordinary unit tests.
    """

    def test_the_module_runs_rather_than_importing_and_exiting(
        self,
        daemon_app: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The regression, stated as the daemon experiences it.

        Exiting 2 is `main` refusing an argument, so this asserts two things at
        once: the module has an execution path, and that path hands it
        `sys.argv[1:]` rather than inventing its own arguments. A module with
        no execution path exits nothing at all — `run_module` returns a
        namespace and `pytest.raises` fails — which is precisely the silent
        exit 0 the daemon saw.

        Args:
            daemon_app: The module under test, imported against the stubbed
                SDK so that running it again finds the stub in `sys.modules`.
            monkeypatch: Used to put an argument on the command line.
        """
        del daemon_app
        monkeypatch.setattr(sys, "argv", [_UNDER_TEST, "--verbose"])
        monkeypatch.delitem(sys.modules, _UNDER_TEST, raising=False)

        with pytest.raises(SystemExit) as raised:
            runpy.run_module(_UNDER_TEST, run_name="__main__")

        assert raised.value.code == 2

    def test_the_status_it_exits_with_is_the_one_startup_returned(
        self,
        daemon_app: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Which is what makes the daemon's report of the run mean something.

        The composition root is replaced on the module `daemon_app` imports
        `run` from, not on `daemon_app` itself: `run_module` executes a fresh
        copy of the module, and that copy binds whatever
        `reachy_mini_ha_satellite.main.run` is at the moment it imports it.

        Args:
            daemon_app: The module under test, imported against the stubbed
                SDK.
            monkeypatch: Used to replace the composition root, empty the
                command line and drop the imported copy of the module.
        """
        del daemon_app

        async def _run(handle: object, stop: asyncio.Event) -> None:
            """Return immediately, as a stop request from the page makes it.

            Args:
                handle: Unused.
                stop: Unused.
            """
            del handle, stop

        monkeypatch.setattr(satellite_main, "run", _run)
        monkeypatch.setattr(sys, "argv", [_UNDER_TEST])
        monkeypatch.delitem(sys.modules, _UNDER_TEST, raising=False)

        with pytest.raises(SystemExit) as raised:
            runpy.run_module(_UNDER_TEST, run_name="__main__")

        assert raised.value.code == 0
