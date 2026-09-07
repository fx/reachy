"""Whether the robot daemon is answering, kept apart from whether a command was good.

The application does not own the robot. It is handed a `RobotHandle` whose
commands travel over the daemon's SDK websocket, and that socket can stop
carrying them while every other part of the process is healthy — the daemon
itself still running, the settings interface still served, the camera still
producing frames. The released SDK reports it by raising `ConnectionError`
("Lost connection with the server.") from every command it is asked to send.

**That is a different condition from a command that was bad, and this module
exists so the two cannot be collapsed.** A rejected pose is the application's
fault and says something about the pose; a refused link says nothing about the
pose and everything about the robot. Widening a motion adapter's `except` tuple
to include `ConnectionError` would make a robot that cannot be commanded at all
look exactly like a robot rejecting one sample, which is the reading an operator
cannot act on — the same reason `reachy_checks` distinguishes a skipped check
from a failed one and the groundstation's capability health distinguishes
`disabled` from `failed`.

**It also must not end the process.** `reachy-mini-ha-app.service` is
`Type=oneshot`: nothing restarts a satellite that exits, and the daemon marks it
`error` and leaves it stopped. One transient link failure therefore means a
permanently silent robot until a person intervenes, which is exactly the failure
the runtime-stability work exists to prevent. So a link fault is recorded here
and reported, and the caller carries on.

## What the SDK does about it, and what it does not

`reachy_mini.io.ws_client.WSClient` connects **once**, in its constructor, and
has no reconnection path at all: its receive loop swallows the close and
returns. What it does have is a one-second liveness poll — a background thread
sets `_is_alive` from whether any message arrived in the last second, and
`send_command` raises `ConnectionError` while that is false. So a daemon that
stops publishing for longer than a second and then resumes takes the link down
and brings it back up **by itself**, with no reconnect and nothing for this
application to do but keep trying. A socket that is actually closed never
recovers, and the robot needs its daemon restarted.

Those two look identical from here, which is why nothing in this module tries to
tell them apart. The first command the daemon carries marks the link up again,
and the surface says which of the two states is in force so that an operator can
decide whether to restart the daemon.

**How soon that happens depends on what the robot is doing, and the surfaces say
so rather than promising otherwise.** With gaze acquired — face tracking on —
the motion adapter re-asserts its daemon ownership on every behaviour tick while
the link is down, so recovery is noticed within a tick. With gaze off there is
nothing this application may send that would not change the robot's behaviour to
ask a question, so nothing probes: the state is what the last command observed,
and it returns to `up` at the next thing that moves the robot — a voice-pipeline
antenna or head move. Neither case needs a person, and neither case is the
application giving up.

## What is reported

`DaemonLink.status()` is three bounded values — a state, a count of outages and
a count of refused calls. No address, no credential, no identifier. The two
counts saturate at `COUNTER_LIMIT` rather than rising for the life of the
process, because a robot commanding at twenty hertz against a dead daemon would
otherwise grow the `/status` payload for as long as the outage lasts, and this
repository's diagnostics carry nothing that grows with uptime. A count sitting at
the limit reads "at least this many", which is the only thing an operator does
with it.
"""

from __future__ import annotations

import logging
import threading
from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "COUNTER_LIMIT",
    "DAEMON_LINK_ERRORS",
    "DaemonLink",
    "DaemonLinkState",
    "attempt_daemon_call",
    "report_daemon_call",
]

_LOGGER: Final = logging.getLogger(__name__)

#: Where the two reported counts stop. Four digits is more than an operator
#: reads and small enough that the payload cannot grow with uptime; a count at
#: the limit means "at least this many", which is all either is used for.
COUNTER_LIMIT: Final = 9999

# What "the daemon did not take this command" is spelled as at the SDK boundary.
#
# `ConnectionError` is what `WSClient.send_command` and `send_task_request`
# raise once their liveness poll has gone false, which is every ordinary command
# — `set_target`, `enable_motors`, `set_automatic_body_yaw` — and the controlled
# wake. `TimeoutError` is the other half of the same answer:
# `wait_for_task_completion` raises it when a task the daemon acknowledged never
# finished, which is the wake sequence's failure mode when the link dies
# mid-task. Both mean the daemon did not answer; neither says anything about
# what was asked.
#
# Deliberately not `OSError`, which would also swallow a filesystem failure, and
# deliberately not `Exception`, which would swallow the bad-argument
# `ValueError`s the adapters rely on catching separately.
DAEMON_LINK_ERRORS: Final = (ConnectionError, TimeoutError)


class DaemonLinkState(StrEnum):
    """Whether the last daemon command this application attempted was carried."""

    UP = "up"
    DOWN = "down"


class DaemonLink:
    """The process-wide record of whether the daemon is taking commands.

    One instance per process, created by `main.run` before the controlled wake —
    which is the first command the application sends and therefore the first
    observation — and handed to everything that commands the daemon. A second
    instance would be a second answer to a question with one true answer, and
    the surface would report whichever half of the application happened to own
    it.

    Every method is safe to call from any thread: motion commands run on the
    event loop, the wake sequence and the motor lifecycle's daemon writes run on
    worker threads, and all of them report here.
    """

    def __init__(self) -> None:
        """Start with a link nothing has yet had refused.

        `UP` rather than a third "not yet asked" state, because the first
        observation happens before any surface exists to read one: `main.run`
        enables the motors and wakes the robot before it assembles the
        application that serves `/status`. A tri-state would be a state nobody
        can observe.
        """
        self._lock = threading.Lock()
        self._state = DaemonLinkState.UP
        self._outages = 0
        self._refused_calls = 0

    @property
    def state(self) -> DaemonLinkState:
        """What the last attempted daemon command reported."""
        with self._lock:
            return self._state

    @property
    def down(self) -> bool:
        """Whether the daemon refused the last command this application sent."""
        with self._lock:
            return self._state is DaemonLinkState.DOWN

    def record_success(self) -> bool:
        """Record one daemon call the link carried.

        Returns:
            True when this was the call that brought the link back, so that a
            caller wanting to act on the recovery can, and so the log line is
            written once rather than at every tick of a healthy robot.
        """
        with self._lock:
            recovered = self._state is DaemonLinkState.DOWN
            self._state = DaemonLinkState.UP
        if recovered:
            _LOGGER.warning("satellite.daemon_link restored")
        return recovered

    def record_failure(self) -> bool:
        """Record one daemon call the link refused.

        Returns:
            True when this was the call that took the link down. The log line
            hangs off that rather than off every refusal: a behaviour loop
            commanding at twenty hertz against a dead daemon would otherwise
            write twenty lines a second for as long as the outage lasts, into
            the daemon's own journal.
        """
        with self._lock:
            self._refused_calls = min(self._refused_calls + 1, COUNTER_LIMIT)
            lost = self._state is DaemonLinkState.UP
            self._state = DaemonLinkState.DOWN
            if lost:
                self._outages = min(self._outages + 1, COUNTER_LIMIT)
        if lost:
            _LOGGER.error(
                "satellite.daemon_link lost; the application stays up and "
                "commands the daemon again on its own. Restart the robot's "
                "daemon service if it does not return",
            )
        return lost

    def status(self) -> dict[str, object]:
        """Return the bounded report of the link and how often it has gone.

        Returns:
            The state, how many separate outages there have been, and how many
            individual calls were refused across all of them — the two counts
            saturating at `COUNTER_LIMIT`, so this payload has a fixed maximum
            size however long the robot runs. Three bounded values naming
            nothing installed: no address, no credential, no identity.
        """
        with self._lock:
            return {
                "state": self._state.value,
                "outages": self._outages,
                "refused_calls": self._refused_calls,
            }


def attempt_daemon_call(link: DaemonLink, call: Callable[[], None]) -> bool:
    """Make one daemon call, reporting the link rather than raising when it is down.

    For a caller whose contract is that it does not die: the behaviour loop, the
    controlled wake, and the shutdown that hands the daemon its policy back.
    Anything that is not a link fault propagates untouched — a bad pose is still
    a bad pose and the caller still has to deal with it.

    Args:
        link: What to record the outcome on.
        call: The daemon call to attempt.

    Returns:
        True when the daemon took it, False when the link refused it.
    """
    try:
        call()
    except DAEMON_LINK_ERRORS:
        link.record_failure()
        return False
    link.record_success()
    return True


def report_daemon_call[ResultT](
    link: DaemonLink,
    call: Callable[[], ResultT],
) -> ResultT:
    """Make one daemon call, recording the link and letting every failure through.

    The other half of the pair, for a caller that **already has a containing
    failure path and needs it to run**: a motor-group lifecycle phase whose
    refusal closes that group's gate, and the coordinator's own confirmed-torque
    calls, which turn any exception into `MotorConfirmation.failed()`. Swallowing
    a link fault there would open a gate over torque nobody confirmed, so this
    records and re-raises rather than deciding for the caller.

    Both helpers exist because the two contracts are genuinely different, and
    having one would mean the difference living at each call site. What they
    share is `DAEMON_LINK_ERRORS`: "which exceptions mean the link" is answered
    in exactly one place.

    Args:
        link: What to record the outcome on.
        call: The daemon call to make.

    Returns:
        Whatever the call returned.

    Raises:
        BaseException: Whatever the call raised, link faults included.
    """
    try:
        result = call()
    except DAEMON_LINK_ERRORS:
        link.record_failure()
        raise
    link.record_success()
    return result
