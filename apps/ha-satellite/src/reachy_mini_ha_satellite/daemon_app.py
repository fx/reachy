"""The daemon's application class, and the only module that imports the SDK.

ha-satellite REQ-041 asks that installing the wheel be sufficient for the daemon
to find the application, which means declaring a `reachy_mini_apps` entry point
and pointing it at a subclass of the daemon's own application base class. That
base class lives in the Reachy Mini SDK, and **importing any part of the SDK
executes `import gi`** three modules away — so a package that imported it at all
would need PyGObject and the whole GStreamer stack to be importable, which
architecture REQ-005 forbids of the test suite.

The resolution is the one this package already uses for the SDK's face detector:
confine it. **This module is the only file in the package that names the SDK**,
and the only things that reach it are the ways of starting the application:
`__main__.py`, so that `python -m reachy_mini_ha_satellite` and the daemon reach
the same startup rather than two, and the daemon itself — which does not import
this module at all but *executes* it, as the guard at the foot of the file
explains. Nothing else reaches it, and nothing imports it as a side effect of
importing something else: `main.py` is handed a `RobotHandle` — a protocol in
`adapters/daemon.py` — and never learns where it came from, which is what lets
the whole composition root be exercised against a fake on a machine with no
GStreamer.

It is also the bridge between two ways of being asked to stop. The daemon hands
over a `threading.Event`; the application runs on an event loop and waits on an
`asyncio.Event`. One thread waits on the first and sets the second, which is the
only correct way across that boundary — an `asyncio.Event` set from another
thread without going through the loop is a silent no-op.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import threading
from typing import TYPE_CHECKING, Any, Final, cast
from uuid import UUID

from reachy_mini.apps.app import ReachyMiniApp

from reachy_mini_ha_satellite.config import (
    ConfigurationError,
    Settings,
    variable_for,
)
from reachy_mini_ha_satellite.main import run
from reachy_mini_ha_satellite.motor_control import (
    MotorConfirmation,
    MotorConfirmationOutcome,
    MotorEvidence,
    MotorEvidenceError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from reachy_mini_ha_satellite.adapters.daemon import MediaInterface, PoseMatrix

__all__ = ["DEFAULT_SETTINGS_URL", "ReachyMiniHaSatellite", "main"]

_LOGGER: Final = logging.getLogger(__name__)

# Where the daemon's dashboard links to for this application's settings. The
# wildcard host is the SDK's own convention for an application-served page — the
# dashboard substitutes the address it reached the robot on — and the port is
# the `web_port` setting's default. `test_satellite_daemon_app.py` pins the two
# together, so changing the default without changing this is a red run rather
# than a link to a closed port.
DEFAULT_SETTINGS_URL: Final = "http://0.0.0.0:8088"

# What an unusable configuration exits with. EX_CONFIG, which is what an init
# system should see.
_EX_CONFIG: Final = 78

# How long shutdown waits for the thread watching the daemon's stop event. It is
# already released when this is reached — the event is set on the line before —
# so this bounds a thread that is finishing rather than a wait anybody expects to
# spend.
_WATCHER_JOIN_SECONDS: Final = 2.0


def _settings_port() -> str:
    """Work out which port the settings interface will actually bind.

    **The environment and the default, and deliberately not the overrides
    file.** `web_port` is one of `config.BOOTSTRAP_SETTINGS`: an override for it
    is ignored by `load_settings`, because a page able to move itself somewhere
    unreachable is a page nobody can move back. Reading one here would point the
    dashboard at a port nothing bound — which is the same failure from the other
    end, and the reason the two layers have to be read the same way.

    Resolved without validating anything, deliberately. This runs before
    startup, and a value the settings model will refuse is `load_settings`'s
    refusal to make, with its message; here it simply leaves the link at the
    default rather than crashing the daemon's application list.

    Returns:
        The port, as it will appear in the URL.
    """
    default = str(Settings.model_fields["web_port"].default)
    configured = os.environ.get(variable_for("web_port"), "").strip()
    return configured if configured.isdigit() else default


_TORQUE_OUTCOMES: Final = {
    outcome.value: outcome
    for outcome in MotorConfirmationOutcome
    if outcome is not MotorConfirmationOutcome.UNAVAILABLE
}
_TORQUE_ERRORS: Final = {error.value: error for error in MotorEvidenceError}
_MISSING: Final = object()


def _enum_value(value: object) -> str:
    """Read one SDK enum's bounded wire value without importing its type."""
    raw = getattr(value, "value", value)
    return raw if isinstance(raw, str) else ""


class _ConfirmedRobotHandle:
    """Translate the optional canary SDK torque surface at the sole SDK boundary."""

    def __init__(self, raw: object) -> None:
        self._raw: Any = raw

    @property
    def media(self) -> MediaInterface:
        return cast("MediaInterface", self._raw.media)

    def enable_motors(self, ids: list[str] | None = None) -> None:
        self._raw.enable_motors(ids)

    def enable_motors_confirmed(self, ids: list[str]) -> MotorConfirmation:
        return self._confirmed("enable_motors_confirmed", ids, "set", True)

    def disable_motors_confirmed(self, ids: list[str]) -> MotorConfirmation:
        return self._confirmed("disable_motors_confirmed", ids, "set", False)

    def read_motor_torque(self, ids: list[str]) -> MotorConfirmation:
        return self._confirmed("read_motor_torque", ids, "read", None)

    def _confirmed(
        self,
        method_name: str,
        ids: list[str],
        operation: str,
        requested_enabled: bool | None,
    ) -> MotorConfirmation:
        method = getattr(self._raw, method_name, None)
        if not callable(method):
            return MotorConfirmation.unavailable()
        return self._translate(
            method(list(ids)),
            ids,
            operation,
            requested_enabled,
        )

    @staticmethod
    def _translate(
        result: object,
        expected: list[str],
        expected_operation: str,
        expected_enabled: bool | None,
    ) -> MotorConfirmation:
        """Validate the complete SDK envelope before constructing local evidence."""
        request_id = getattr(result, "request_id", _MISSING)
        operation_value = getattr(result, "operation", _MISSING)
        requested_names = getattr(result, "requested_names", _MISSING)
        requested_enabled = getattr(result, "requested_enabled", _MISSING)
        acknowledged = getattr(result, "acknowledged", _MISSING)
        terminal = getattr(result, "terminal", _MISSING)
        outcome_value = getattr(result, "outcome", _MISSING)
        states = getattr(result, "states", _MISSING)
        missing_names = getattr(result, "missing_names", _MISSING)
        result_error = getattr(result, "error", _MISSING)
        outcome = _TORQUE_OUTCOMES.get(_enum_value(outcome_value))
        if (
            not isinstance(request_id, UUID)
            or _enum_value(operation_value) != expected_operation
            or not isinstance(requested_names, list)
            or requested_names != expected
            or len(requested_names) != len(frozenset(requested_names))
            or requested_enabled is not expected_enabled
            or acknowledged is not True
            or terminal is not True
            or outcome is None
            or not isinstance(states, list)
            or not isinstance(missing_names, list)
            or missing_names
            or result_error is not None
        ):
            return MotorConfirmation.failed()
        if expected_operation == "read":
            if (
                expected_enabled is not None
                or outcome is not MotorConfirmationOutcome.CONFIRMED
            ):
                return MotorConfirmation.failed()
        elif expected_operation == "set":
            if type(expected_enabled) is not bool or outcome not in {
                MotorConfirmationOutcome.CONFIRMED,
                MotorConfirmationOutcome.CONTRADICTED,
            }:
                return MotorConfirmation.failed()
        else:
            return MotorConfirmation.failed()

        translated: list[MotorEvidence] = []
        expected_names = frozenset(expected)
        for state in states:
            name = getattr(state, "name", None)
            motor_id = getattr(state, "motor_id", None)
            enabled = getattr(state, "enabled", None)
            error_value = getattr(state, "error", None)
            if (
                not isinstance(name, str)
                or name not in expected_names
                or (motor_id is not None and type(motor_id) is not int)
            ):
                return MotorConfirmation.failed()
            if type(enabled) is bool and error_value is None:
                translated.append(
                    MotorEvidence(name=name, motor_id=motor_id, enabled=enabled)
                )
                continue
            error = _TORQUE_ERRORS.get(_enum_value(error_value))
            if enabled is not None or error is None:
                return MotorConfirmation.failed()
            translated.append(MotorEvidence(name=name, motor_id=motor_id, error=error))

        confirmation = MotorConfirmation(True, outcome, tuple(translated))
        actual = confirmation.physical_value(
            expected,
            allow_contradiction=expected_operation == "set",
        )
        if actual is None:
            return MotorConfirmation.failed()
        if expected_operation == "set" and (
            (outcome is MotorConfirmationOutcome.CONFIRMED)
            != (actual is expected_enabled)
        ):
            return MotorConfirmation.failed()
        return confirmation

    def wake_up(self) -> None:
        self._raw.wake_up()

    def set_target(
        self,
        head: PoseMatrix | None = None,
        antennas: list[float] | None = None,
        body_yaw: float | None = None,
    ) -> None:
        self._raw.set_target(head=head, antennas=antennas, body_yaw=body_yaw)

    def get_current_head_pose(self) -> PoseMatrix:
        return cast("PoseMatrix", self._raw.get_current_head_pose())

    def get_current_joint_positions(self) -> tuple[list[float], list[float]]:
        return cast(
            "tuple[list[float], list[float]]",
            self._raw.get_current_joint_positions(),
        )

    def look_at_image(
        self,
        u: int,
        v: int,
        duration: float = 1.0,
        perform_movement: bool = True,
    ) -> PoseMatrix:
        return cast(
            "PoseMatrix",
            self._raw.look_at_image(
                u,
                v,
                duration=duration,
                perform_movement=perform_movement,
            ),
        )

    def set_automatic_body_yaw(self, enabled: bool) -> None:
        self._raw.set_automatic_body_yaw(enabled)


#:= docs/specs/ha-satellite/index.md#req-041-the-application-is-discoverable-by-the-robot-daemon
#:% The application MUST advertise itself through the daemon's application entry
#:% point mechanism so that installing the wheel is sufficient for the daemon to
#:% find it.
class ReachyMiniHaSatellite(ReachyMiniApp):
    """The application the daemon starts, and the whole of what it needs.

    Two class attributes matter. `custom_app_url` is what the dashboard links
    to, and `dont_start_webserver` switches off the SDK's own settings server —
    this application serves its own, because the settings it exposes are its
    configuration and the redaction rules that go with it.
    """

    custom_app_url: str | None = DEFAULT_SETTINGS_URL
    dont_start_webserver: bool = True

    def __init__(self, running_on_wireless: bool = False) -> None:
        """Prepare the application, pointing the dashboard at the right port.

        Args:
            running_on_wireless: The daemon's own flag, passed through
                untouched.
        """
        super().__init__(running_on_wireless)
        self.custom_app_url = f"http://0.0.0.0:{_settings_port()}"

    def run(self, reachy_mini: object, stop_event: threading.Event) -> None:
        """Run the satellite until the daemon asks it to stop.

        Args:
            reachy_mini: The daemon's handle onto the robot. Typed loosely
                here and precisely one layer down: `main.build_application`
                takes an `adapters.daemon.RobotHandle`, which the SDK's object
                satisfies structurally, and stating the SDK's own type here
                would make this signature the place the SDK's shape is
                asserted rather than `adapters/daemon.py`.
            stop_event: Set by the daemon when the application should stop.
        """
        asyncio.run(_run_until(reachy_mini, stop_event))


async def _run_until(handle: object, stop_event: threading.Event) -> None:
    """Run the application, translating the daemon's stop signal as it goes.

    Args:
        handle: The daemon's handle onto the robot.
        stop_event: The daemon's termination signal.
    """
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _watch() -> None:
        """Wait for the daemon's signal and raise it on the event loop."""
        stop_event.wait()
        # The loop is still running when this is reached on the ordinary path,
        # because the coroutine below has not returned yet. On the other path
        # the event was set *by* that coroutine on its way out, and the join
        # below is what keeps this call inside the loop's lifetime. The suppress
        # is for the case neither covers — a cancellation that closed the loop
        # first — where the alternative is a traceback on a daemon thread at the
        # end of a shutdown that already happened.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(stop.set)

    watcher = threading.Thread(target=_watch, name="satellite-stop", daemon=True)
    watcher.start()
    try:
        await run(_ConfirmedRobotHandle(handle), stop)
    finally:
        # The application may have finished for its own reasons — the settings
        # interface asked it to stop, or something raised. Setting the daemon's
        # event releases the watcher rather than leaving a thread parked on it
        # for the life of the process, and joining it is what stops that thread
        # reaching for a loop `asyncio.run` is about to close.
        stop_event.set()
        watcher.join(timeout=_WATCHER_JOIN_SECONDS)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the satellite outside the daemon: `python -m reachy_mini_ha_satellite`.

    The daemon is the ordinary way in. This exists for the deployment session
    where somebody needs to watch it start, and it takes the same configuration
    and prints the same refusals.

    Args:
        argv: Command-line arguments. None are accepted: everything this
            application reads it reads from its environment and its settings
            interface, so a deployment is described in one place.

    Returns:
        The process exit status.
    """
    if argv:
        sys.stderr.write(
            "reachy-mini-ha-satellite takes no arguments; it is configured "
            "through REACHY_SATELLITE_* variables and its settings page.\n",
        )
        return 2

    application = ReachyMiniHaSatellite()
    _stop_on_signals(application)
    try:
        application.wrapped_run()
    except ConfigurationError as error:
        sys.stderr.write(f"{error}\n")
        return _EX_CONFIG
    return 0


def _stop_on_signals(application: ReachyMiniHaSatellite) -> None:
    """Ask the application to stop when the process is asked to.

    Inside the daemon this is the daemon's job. Run directly, nothing else is
    listening, and a satellite that ignored a termination signal would be one
    that leaves the head where it was — which is exactly what REQ-050 is about.

    Args:
        application: What to stop.
    """
    for received in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(received, lambda *_: application.stop())
        except (OSError, ValueError):
            # Not the main thread, or a platform without the signal. Neither is
            # worth refusing to start over.
            _LOGGER.debug("could not install a handler for %s", received)


# ⚠️ Not redundant with `__main__.py`, and deleting it stops the satellite
# starting on the robot at all.
#
# The daemon does not import this module and instantiate the class the
# `reachy_mini_apps` entry point names. It takes the **module** half of the entry
# point — everything left of the colon — and launches the application as a
# subprocess, `python -u -m reachy_mini_ha_satellite.daemon_app`. So the module
# the entry point points at has to be a program as well as an import target;
# `__main__.py` makes the *package* runnable, which is a different name and not
# the one the daemon runs.
#
# Without this block that command imports this module, finds nothing to do and
# exits 0. The daemon reports the application as finished, successfully, within
# seconds of starting it and with no output at all — which is what it looked
# like on the robot, and why it took a hand-patched file to diagnose.
#
# `scripts/verify_satellite_wheel.py` executes the entry point's module exactly
# as the daemon does and refuses a wheel where it exits 0 having done nothing,
# so this is a red release rather than a silent robot if it goes missing again.
#
# REQ-041 is cited twice, here and on the class, because "sufficient for the
# daemon to find it" takes both: the class is what the declaration resolves to,
# and this is what makes the thing the daemon actually launches run.
#:= docs/specs/ha-satellite/index.md#req-041-the-application-is-discoverable-by-the-robot-daemon
#:% The application MUST advertise itself through the daemon's application entry
#:% point mechanism so that installing the wheel is sufficient for the daemon to
#:% find it.
if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
