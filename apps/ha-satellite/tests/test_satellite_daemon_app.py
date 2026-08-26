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
from itertools import product
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

# pylint: disable=no-name-in-module
from aioesphomeapi.api_pb2 import (  # type: ignore[attr-defined]  # generated protobuf module, which mypy cannot see the message classes inside
    SwitchCommandRequest,
    SwitchStateResponse,
)
from satellite_support import FakeRobot, ManualClock

from reachy_mini_ha_satellite import main as satellite_main
from reachy_mini_ha_satellite.config import (
    ConfigurationError,
    OverrideStore,
    Settings,
    overrides_path,
    variable_for,
)
from reachy_mini_ha_satellite.motor_control import (
    BODY_MOTOR_IDS,
    HEAD_MOTOR_IDS,
    MOTOR_IDENTIFIERS,
    MotorGroup,
    MotorGroupCoordinator,
)
from reachy_mini_ha_satellite.motor_entities import MotorSwitchEntity

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from pyfakefs.fake_filesystem import FakeFilesystem

_SDK_MODULES = ("reachy_mini", "reachy_mini.apps", "reachy_mini.apps.app")
_UNDER_TEST = "reachy_mini_ha_satellite.daemon_app"


def _torque_result(
    ids: list[str],
    *,
    operation: str,
    requested_names: list[str] | None = None,
    requested_enabled: bool | None,
    acknowledged: bool = True,
    terminal: bool = True,
    outcome: str = "confirmed",
    result_error: object | None = None,
    missing_names: list[str] | None = None,
    evidence_enabled: bool = True,
) -> SimpleNamespace:
    """Build one complete SDK-shaped result envelope for boundary tests."""
    names = list(ids) if requested_names is None else requested_names
    return SimpleNamespace(
        request_id=uuid4(),
        operation=SimpleNamespace(value=operation),
        requested_names=names,
        requested_enabled=requested_enabled,
        acknowledged=acknowledged,
        terminal=terminal,
        outcome=SimpleNamespace(value=outcome),
        missing_names=[] if missing_names is None else missing_names,
        error=result_error,
        states=[
            SimpleNamespace(
                name=name,
                motor_id=MOTOR_IDENTIFIERS[name],
                enabled=evidence_enabled,
                error=None,
            )
            for name in ids
        ],
    )


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

        assert len(seen) == 1
        assert vars(seen[0])["_raw"] is application.handle

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


class TestConfirmedTorqueBoundary:
    """SDK results become bounded local evidence only in the daemon entry point."""

    def test_missing_canary_method_reports_unavailable(
        self, daemon_app: ModuleType
    ) -> None:
        """Released SDK 1.9 stays runnable but exposes no optimistic switch."""
        bridge = daemon_app._ConfirmedRobotHandle(SimpleNamespace())

        result = bridge.read_motor_torque(["one"])

        assert result.outcome.value == "unavailable"
        assert not result.acknowledged
        assert result.evidence == ()

    def test_canary_result_retains_only_ids_needed_for_internal_validation(
        self,
        daemon_app: ModuleType,
    ) -> None:
        """Correlation IDs disappear while motor IDs remain available for matching."""
        raw = SimpleNamespace(
            read_motor_torque=lambda ids: SimpleNamespace(
                request_id=uuid4(),
                operation=SimpleNamespace(value="read"),
                requested_names=list(ids),
                requested_enabled=None,
                acknowledged=True,
                terminal=True,
                outcome=SimpleNamespace(value="confirmed"),
                missing_names=[],
                error=None,
                states=[
                    SimpleNamespace(
                        name=name,
                        motor_id=MOTOR_IDENTIFIERS[name],
                        enabled=True,
                        error=None,
                    )
                    for name in ids
                ],
            )
        )
        bridge = daemon_app._ConfirmedRobotHandle(raw)

        result = bridge.read_motor_torque(list(HEAD_MOTOR_IDS))

        assert result.physical_value(HEAD_MOTOR_IDS) is True
        assert not hasattr(result, "request_id")
        assert [item.motor_id for item in result.evidence] == [
            MOTOR_IDENTIFIERS[name] for name in HEAD_MOTOR_IDS
        ]

    @pytest.mark.parametrize(
        (
            "operation",
            "names_kind",
            "requested_enabled",
            "acknowledged",
            "terminal",
            "outcome",
            "has_error",
            "has_missing",
            "evidence_enabled",
        ),
        list(
            product(
                ("read", "set"),
                ("exact", "reversed", "duplicate", "missing", "extra"),
                (None, False, True),
                (False, True),
                (False, True),
                ("confirmed", "contradicted", "partial", "failed"),
                (False, True),
                (False, True),
                (False, True),
            )
        ),
    )
    def test_read_result_envelope_cartesian_matrix(
        self,
        daemon_app: ModuleType,
        operation: str,
        names_kind: str,
        requested_enabled: bool | None,
        acknowledged: bool,
        terminal: bool,
        outcome: str,
        has_error: bool,
        has_missing: bool,
        evidence_enabled: bool,
    ) -> None:
        """Only one complete READ envelope class may cross the SDK boundary."""
        expected = list(HEAD_MOTOR_IDS)
        requested_names = {
            "exact": expected,
            "reversed": list(reversed(expected)),
            "duplicate": [*expected[:-1], expected[0]],
            "missing": expected[:-1],
            "extra": [*expected, BODY_MOTOR_IDS[0]],
        }[names_kind]
        result = _torque_result(
            expected,
            operation=operation,
            requested_names=requested_names,
            requested_enabled=requested_enabled,
            acknowledged=acknowledged,
            terminal=terminal,
            outcome=outcome,
            result_error=(SimpleNamespace(value="read_failed") if has_error else None),
            missing_names=[expected[-1]] if has_missing else [],
            evidence_enabled=evidence_enabled,
        )
        bridge = daemon_app._ConfirmedRobotHandle(
            SimpleNamespace(read_motor_torque=lambda _ids: result)
        )

        translated = bridge.read_motor_torque(expected)

        valid = (
            operation == "read"
            and names_kind == "exact"
            and requested_enabled is None
            and acknowledged
            and terminal
            and outcome == "confirmed"
            and not has_error
            and not has_missing
        )
        assert (translated.outcome.value == "confirmed") is valid
        assert translated.physical_value(HEAD_MOTOR_IDS) is (
            evidence_enabled if valid else None
        )

    @pytest.mark.parametrize(
        ("requested", "evidence", "outcome", "valid"),
        [
            (True, True, "confirmed", True),
            (False, False, "confirmed", True),
            (True, False, "contradicted", True),
            (False, True, "contradicted", True),
            (True, False, "confirmed", False),
            (False, True, "confirmed", False),
            (True, True, "contradicted", False),
            (False, False, "contradicted", False),
            (True, True, "partial", False),
            (False, False, "failed", False),
        ],
    )
    def test_set_result_envelope_requires_outcome_to_match_physical_agreement(
        self,
        daemon_app: ModuleType,
        requested: bool,
        evidence: bool,
        outcome: str,
        valid: bool,
    ) -> None:
        """Confirmed means agreement and contradicted means actual disagreement."""
        result = _torque_result(
            list(BODY_MOTOR_IDS),
            operation="set",
            requested_enabled=requested,
            outcome=outcome,
            evidence_enabled=evidence,
        )
        method_name = (
            "enable_motors_confirmed" if requested else "disable_motors_confirmed"
        )
        bridge = daemon_app._ConfirmedRobotHandle(
            SimpleNamespace(**{method_name: lambda _ids: result})
        )

        translated = (
            bridge.enable_motors_confirmed(list(BODY_MOTOR_IDS))
            if requested
            else bridge.disable_motors_confirmed(list(BODY_MOTOR_IDS))
        )

        assert translated.outcome.value == (outcome if valid else "failed")
        assert translated.physical_value(BODY_MOTOR_IDS) is (
            evidence if valid else None
        )

    def test_invalid_set_envelope_cannot_advance_any_coordinator_effect(
        self,
        daemon_app: ModuleType,
    ) -> None:
        """Envelope failure retains state and blocks reseed, restore and publication."""
        invalid = _torque_result(
            list(BODY_MOTOR_IDS),
            operation="set",
            requested_enabled=False,
            outcome="confirmed",
            evidence_enabled=False,
        )
        delattr(invalid.states[0], "motor_id")
        bridge = daemon_app._ConfirmedRobotHandle(
            SimpleNamespace(disable_motors_confirmed=lambda _ids: invalid)
        )
        translated = bridge.disable_motors_confirmed(list(BODY_MOTOR_IDS))
        assert translated.outcome.value == "failed"

        robot = FakeRobot()
        groups = MotorGroupCoordinator(robot, clock=ManualClock())
        assert MotorGroup.BODY in groups.initialize()
        events: list[str] = []

        def _prepare() -> bool:
            events.append("prepare")
            return True

        groups.set_hooks(
            MotorGroup.BODY,
            prepare=_prepare,
            reseed=lambda: events.append("reseed"),
            restore=lambda _policy: events.append("restore"),
        )
        robot.motor_disables_confirmed.append(translated)
        robot.motor_reads.append(translated)
        control = MotorSwitchEntity(
            coordinator=groups,
            group=MotorGroup.BODY,
            key=7,
        )

        responses = list(
            control.handle_message(SwitchCommandRequest(key=7, state=False))
        )

        assert responses == [SwitchStateResponse(key=7, state=True)]
        assert events == ["prepare"]
        assert groups.last_confirmed(MotorGroup.BODY) is True
        assert not groups.gate_open(MotorGroup.BODY)

    @pytest.mark.parametrize(
        "field",
        [
            "request_id",
            "operation",
            "requested_names",
            "requested_enabled",
            "acknowledged",
            "terminal",
            "outcome",
            "states",
            "missing_names",
            "error",
        ],
    )
    def test_missing_required_result_field_fails_closed(
        self,
        daemon_app: ModuleType,
        field: str,
    ) -> None:
        """Missing fields cannot accidentally equal a nullable valid value."""
        result = _torque_result(
            list(HEAD_MOTOR_IDS),
            operation="read",
            requested_enabled=None,
        )
        delattr(result, field)
        bridge = daemon_app._ConfirmedRobotHandle(
            SimpleNamespace(read_motor_torque=lambda _ids: result)
        )

        translated = bridge.read_motor_torque(list(HEAD_MOTOR_IDS))

        assert translated.outcome.value == "failed"
        assert translated.physical_value(HEAD_MOTOR_IDS) is None

    @pytest.mark.parametrize("field", ["name", "motor_id", "enabled", "error"])
    def test_missing_required_per_state_field_fails_closed(
        self,
        daemon_app: ModuleType,
        field: str,
    ) -> None:
        """Absent state fields never inherit the semantics of explicit null."""
        result = _torque_result(
            list(HEAD_MOTOR_IDS),
            operation="read",
            requested_enabled=None,
        )
        delattr(result.states[0], field)
        bridge = daemon_app._ConfirmedRobotHandle(
            SimpleNamespace(read_motor_torque=lambda _ids: result)
        )

        translated = bridge.read_motor_torque(list(HEAD_MOTOR_IDS))

        assert translated.outcome.value == "failed"
        assert translated.evidence == ()

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("name", None),
            ("name", 11),
            ("name", BODY_MOTOR_IDS[0]),
            ("motor_id", None),
            ("motor_id", True),
            ("motor_id", "11"),
            ("motor_id", 11.0),
            ("motor_id", MOTOR_IDENTIFIERS[HEAD_MOTOR_IDS[1]]),
            ("enabled", None),
            ("enabled", 1),
            ("enabled", "true"),
            ("error", "read_failed"),
            ("error", SimpleNamespace(value="read_failed")),
        ],
        ids=[
            "null-name",
            "integer-name",
            "wrong-group-name",
            "explicit-null-id",
            "bool-id",
            "string-id",
            "float-id",
            "mismatched-id",
            "null-enabled",
            "integer-enabled",
            "string-enabled",
            "string-error",
            "enum-like-error",
        ],
    )
    def test_wrong_per_state_field_type_or_value_fails_closed(
        self,
        daemon_app: ModuleType,
        field: str,
        value: object,
    ) -> None:
        """Every SDK state is explicit, typed and matched to its fixed motor."""
        result = _torque_result(
            list(HEAD_MOTOR_IDS),
            operation="read",
            requested_enabled=None,
        )
        setattr(result.states[0], field, value)
        bridge = daemon_app._ConfirmedRobotHandle(
            SimpleNamespace(read_motor_torque=lambda _ids: result)
        )

        translated = bridge.read_motor_torque(list(HEAD_MOTOR_IDS))

        assert translated.outcome.value == "failed"
        assert translated.evidence == ()

    @pytest.mark.parametrize(
        "replacement",
        [
            object(),
            {
                "name": HEAD_MOTOR_IDS[0],
                "motor_id": MOTOR_IDENTIFIERS[HEAD_MOTOR_IDS[0]],
                "enabled": True,
                "error": None,
            },
        ],
        ids=["opaque-object", "mapping"],
    )
    def test_non_attribute_state_variants_fail_closed(
        self,
        daemon_app: ModuleType,
        replacement: object,
    ) -> None:
        """The translator supports SDK attribute objects only, never loose mappings."""
        result = _torque_result(
            list(HEAD_MOTOR_IDS),
            operation="read",
            requested_enabled=None,
        )
        result.states[0] = replacement
        bridge = daemon_app._ConfirmedRobotHandle(
            SimpleNamespace(read_motor_torque=lambda _ids: result)
        )

        translated = bridge.read_motor_torque(list(HEAD_MOTOR_IDS))

        assert translated.outcome.value == "failed"
        assert translated.evidence == ()

    @pytest.mark.parametrize(
        ("outcome", "acknowledged", "terminal"),
        [
            (outcome, acknowledged, terminal)
            for outcome in ("confirmed", "contradicted", "partial", "failed")
            for acknowledged in (False, True)
            for terminal in (False, True)
        ],
    )
    def test_every_outcome_acknowledgement_and_terminal_combination_fails_closed(
        self,
        daemon_app: ModuleType,
        outcome: str,
        acknowledged: bool,
        terminal: bool,
    ) -> None:
        """Only terminal acknowledged confirmed reads can register or open a gate."""
        raw = SimpleNamespace(
            read_motor_torque=lambda ids: SimpleNamespace(
                request_id=uuid4(),
                operation=SimpleNamespace(value="read"),
                requested_names=list(ids),
                requested_enabled=None,
                acknowledged=acknowledged,
                terminal=terminal,
                outcome=SimpleNamespace(value=outcome),
                missing_names=[],
                error=None,
                states=[
                    SimpleNamespace(
                        name=name,
                        motor_id=MOTOR_IDENTIFIERS[name],
                        enabled=True,
                        error=None,
                    )
                    for name in ids
                ],
            )
        )
        bridge = daemon_app._ConfirmedRobotHandle(raw)
        translated = bridge.read_motor_torque(list(HEAD_MOTOR_IDS))
        robot = FakeRobot(motor_reads=[translated])
        groups = MotorGroupCoordinator(robot, clock=ManualClock())

        registered = groups.initialize()

        accepted = outcome == "confirmed" and acknowledged and terminal
        assert (MotorGroup.HEAD in registered) is accepted
        assert groups.gate_open(MotorGroup.HEAD) is accepted
        assert groups.last_confirmed(MotorGroup.HEAD) is (True if accepted else None)

    def test_absent_per_motor_boolean_fails_closed(
        self,
        daemon_app: ModuleType,
    ) -> None:
        """A named motor with neither a physical value nor error is incomplete."""
        raw = SimpleNamespace(
            read_motor_torque=lambda ids: SimpleNamespace(
                request_id=uuid4(),
                operation=SimpleNamespace(value="read"),
                requested_names=list(ids),
                requested_enabled=None,
                acknowledged=True,
                terminal=True,
                outcome=SimpleNamespace(value="confirmed"),
                missing_names=[],
                error=None,
                states=[
                    SimpleNamespace(
                        name=name,
                        motor_id=MOTOR_IDENTIFIERS[name],
                        enabled=None if index == 0 else True,
                        error=None,
                    )
                    for index, name in enumerate(ids)
                ],
            )
        )
        bridge = daemon_app._ConfirmedRobotHandle(raw)

        result = bridge.read_motor_torque(list(HEAD_MOTOR_IDS))

        assert result.outcome.value == "failed"
        assert result.physical_value(HEAD_MOTOR_IDS) is None

    def test_malformed_or_unexpected_sdk_evidence_fails_closed(
        self,
        daemon_app: ModuleType,
    ) -> None:
        """Unknown enum values and motors cannot be smuggled into local policy."""
        raw = SimpleNamespace(
            read_motor_torque=lambda _ids: SimpleNamespace(
                acknowledged=True,
                outcome=SimpleNamespace(value="future-outcome"),
                states=[SimpleNamespace(name="unexpected", enabled=True, error=None)],
            )
        )
        bridge = daemon_app._ConfirmedRobotHandle(raw)

        result = bridge.read_motor_torque(["one"])

        assert result.outcome.value == "failed"
        assert result.physical_value(("one",)) is None
