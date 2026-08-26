"""Confirmed motor-group state, command gates and bounded diagnostics.

The daemon boundary translates its SDK result into the values below.  This module
then owns the application policy: three exact physical groups, one serialized
transition at a time, and no command reaching a group whose torque is not both
confirmed and freshly reseeded.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import threading
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from reachy_mini_ha_satellite.adapters.daemon import RobotHandle

__all__ = [
    "ANTENNA_MOTOR_IDS",
    "BODY_MOTOR_IDS",
    "HEAD_MOTOR_IDS",
    "MOTOR_GROUPS",
    "MOTOR_IDENTIFIERS",
    "MotorConfirmation",
    "MotorConfirmationOutcome",
    "MotorEvidence",
    "MotorEvidenceError",
    "MotorGroup",
    "MotorGroupCoordinator",
    "MotorGroupLifecycle",
    "MotorTransition",
]

HEAD_MOTOR_IDS: Final = tuple(f"stewart_{index}" for index in range(1, 7))
BODY_MOTOR_IDS: Final = ("body_rotation",)
ANTENNA_MOTOR_IDS: Final = ("right_antenna", "left_antenna")
MOTOR_IDENTIFIERS: Final[Mapping[str, int]] = {
    "body_rotation": 10,
    **{f"stewart_{index}": index + 10 for index in range(1, 7)},
    "right_antenna": 17,
    "left_antenna": 18,
}


class MotorGroup(StrEnum):
    """The three independently controlled physical groups."""

    HEAD = "head"
    BODY = "body"
    ANTENNAS = "antennas"


MOTOR_GROUPS: Final[Mapping[MotorGroup, tuple[str, ...]]] = {
    MotorGroup.HEAD: HEAD_MOTOR_IDS,
    MotorGroup.BODY: BODY_MOTOR_IDS,
    MotorGroup.ANTENNAS: ANTENNA_MOTOR_IDS,
}


class MotorConfirmationOutcome(StrEnum):
    """Bounded daemon outcomes after SDK-specific values are removed."""

    CONFIRMED = "confirmed"
    CONTRADICTED = "contradicted"
    PARTIAL = "partial"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class MotorEvidenceError(StrEnum):
    """Bounded non-identifying evidence failures."""

    INVALID_REQUEST = "invalid_request"
    DUPLICATE_MOTOR = "duplicate_motor"
    UNKNOWN_MOTOR = "unknown_motor"
    WRITE_FAILED = "write_failed"
    READ_FAILED = "read_failed"
    INVALID_REGISTER_VALUE = "invalid_register_value"


@dataclass(frozen=True, slots=True)
class MotorEvidence:
    """Physical evidence for one locally requested motor name.

    ``motor_id=None`` exists only for SDK-neutral unit fakes. The production SDK
    translator requires and validates the explicit numeric ID for every state.
    """

    name: str
    motor_id: int | None = None
    enabled: bool | None = None
    error: MotorEvidenceError | None = None

    def __post_init__(self) -> None:
        """Require a valid optional ID and exactly one value or bounded error."""
        if not self.name:
            raise ValueError("motor evidence requires a name")
        if self.motor_id is not None and (
            type(self.motor_id) is not int or self.motor_id < 0
        ):
            raise ValueError("motor evidence ID must be a non-negative integer")
        if (self.enabled is None) == (self.error is None):
            raise ValueError("motor evidence requires exactly one value or error")


@dataclass(frozen=True, slots=True)
class MotorConfirmation:
    """One terminal correlated daemon result with no SDK objects or identifiers."""

    acknowledged: bool
    outcome: MotorConfirmationOutcome
    evidence: tuple[MotorEvidence, ...] = ()

    @classmethod
    def unavailable(cls) -> MotorConfirmation:
        """Return the stable result for an SDK without the confirmed surface."""
        return cls(False, MotorConfirmationOutcome.UNAVAILABLE)

    @classmethod
    def failed(cls) -> MotorConfirmation:
        """Return the stable result for a call that raised or was malformed."""
        return cls(False, MotorConfirmationOutcome.FAILED)

    def physical_value(
        self,
        expected_names: Sequence[str],
        *,
        allow_contradiction: bool = True,
    ) -> bool | None:
        """Return one complete agreeing physical Boolean, otherwise ``None``."""
        expected = tuple(expected_names)
        allowed = {MotorConfirmationOutcome.CONFIRMED}
        if allow_contradiction:
            allowed.add(MotorConfirmationOutcome.CONTRADICTED)
        by_name = {item.name: item for item in self.evidence}
        if (
            not self.acknowledged
            or self.outcome not in allowed
            or len(by_name) != len(self.evidence)
            or frozenset(by_name) != frozenset(expected)
            or len(expected) != len(frozenset(expected))
        ):
            return None
        ids_present = {item.motor_id is not None for item in by_name.values()}
        if len(ids_present) != 1:
            return None
        values: set[bool] = set()
        for name in expected:
            item = by_name[name]
            if item.error is not None or type(item.enabled) is not bool:
                return None
            expected_id = MOTOR_IDENTIFIERS.get(name)
            if expected_id is None or (
                item.motor_id is not None and item.motor_id != expected_id
            ):
                return None
            values.add(item.enabled)
        if len(values) != 1:
            return None
        return values.pop()


class MotorTransition(StrEnum):
    """Per-group transition state exposed in bounded status."""

    IDLE = "idle"
    QUIESCING = "quiescing"
    CONFIRMING = "confirming"
    RESEEDING = "reseeding"
    TERMINAL = "terminal"


@dataclass(slots=True)
class _GroupState:
    last_confirmed: bool | None = None
    confirmed_at: float | None = None
    gate_open: bool = False
    commands_inflight: int = 0
    transition: MotorTransition = MotorTransition.IDLE
    body_policy: bool | None = None
    policy_capture_pending: bool = False
    generation: int = 0
    diagnostics: deque[dict[str, object]] = field(
        default_factory=lambda: deque(maxlen=_DIAGNOSTIC_CAPACITY)
    )


class MotorGroupLifecycle:
    """One loop-owned transition generation split around blocking hardware I/O."""

    def prepare_is_blocking(self) -> bool:
        """Return whether preparation must run on the finite worker."""
        return True

    def prepare_worker(self) -> object:
        """Quiesce daemon producers on the worker and return an immutable snapshot."""
        return None

    def prepare_loop(self, prepared: object) -> None:
        """Adopt the quiesced ownership snapshot on the event loop."""
        del prepared

    def captured_policy(self, prepared: object) -> bool | None:
        """Report which preceding daemon policy this quiesce took ownership of.

        None here, because the base lifecycle quiesces nothing. Only a group
        whose `prepare_worker` actually displaced a daemon producer has a policy
        to hand back, and only that lifecycle can read it out of its own
        snapshot.
        """
        del prepared
        return None

    def sample_worker(self) -> object:
        """Read measured hardware state on the worker after torque is confirmed on."""
        return None

    def sample_loop(self, sample: object) -> None:
        """Commit fresh controller, target and expression state on the event loop."""
        del sample

    def restore_worker(self, policy: bool | None) -> object:
        """Restore a preceding daemon policy on the worker when still current."""
        del policy
        return None

    def restore_loop(self, restored: object) -> None:
        """Commit restored ownership state on the event loop."""
        del restored


@dataclass(frozen=True, slots=True)
class _Hooks:
    lifecycle: Callable[[], MotorGroupLifecycle] | None = None

    def create(self) -> MotorGroupLifecycle:
        """Create one generation-specific hook owner."""
        if self.lifecycle is None:
            return MotorGroupLifecycle()
        return self.lifecycle()


@dataclass(frozen=True, slots=True)
class _ReservedOperation:
    group: MotorGroup
    requested: bool | None
    publish: Callable[[], None]
    generation: int = 0
    lifecycle: MotorGroupLifecycle | None = None


@dataclass(frozen=True, slots=True)
class _DeferredEnable:
    group: MotorGroup
    actual: bool
    confirmation: MotorConfirmation
    changed: bool
    generation: int
    lifecycle: MotorGroupLifecycle
    sample: object


@dataclass(frozen=True, slots=True)
class _DeferredRefresh:
    group: MotorGroup
    actual: bool
    confirmation: MotorConfirmation
    changed: bool
    generation: int
    lifecycle: MotorGroupLifecycle


_DIAGNOSTIC_CAPACITY: Final = 32


class MotorGroupCoordinator:
    """Serialize torque transitions and every producer reaching those motors."""

    def __init__(
        self,
        handle: RobotHandle,
        *,
        clock: Callable[[], float],
    ) -> None:
        """Start all groups closed until initial physical confirmation."""
        self._handle = handle
        self._clock = clock
        self._lock = threading.RLock()
        self._commands_drained = threading.Condition(self._lock)
        self._operation_mutex = threading.Lock()
        self._groups = {group: _GroupState() for group in MotorGroup}
        self._hooks = {group: _Hooks() for group in MotorGroup}
        self._terminal_requested = threading.Event()
        self._terminal = False
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="satellite-motors",
        )
        self._operation: asyncio.Task[None] | None = None
        # The blocking phase running on that worker right now, if any. `Any`
        # because shutdown asks it one question — is it finished — and never
        # what it returned; the awaiting caller owns the result.
        self._inflight: Future[Any] | None = None
        self._producer_waiters: set[
            tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]
        ] = set()
        self._closed = False

    def set_hooks(
        self,
        group: MotorGroup,
        *,
        lifecycle: Callable[[], MotorGroupLifecycle] | None = None,
    ) -> None:
        """Install what builds one generation's split loop/worker phases."""
        with self._lock:
            self._hooks[group] = _Hooks(lifecycle)

    def reserve_transition(
        self,
        group: MotorGroup,
        requested: bool,
        publish: Callable[[], None],
    ) -> bool:
        """Close the gate and reserve the sole finite worker operation."""
        return self._reserve(_ReservedOperation(group, requested, publish))

    def reserve_refresh(
        self,
        group: MotorGroup,
        publish: Callable[[], None],
    ) -> bool:
        """Reserve one independent read, or refuse it without queueing."""
        return self._reserve(_ReservedOperation(group, None, publish))

    def _reserve(self, operation: _ReservedOperation) -> bool:
        """Create at most one loop-owned task and no executor backlog."""
        with self._lock:
            state = self._groups[operation.group]
            if operation.requested is False:
                state.gate_open = False
            if self._terminal_observed() or self._closed:
                return False
            active = self._operation
            if active is not None and not active.done():
                self._record(
                    operation.group,
                    operation.requested,
                    MotorConfirmation.failed(),
                    None,
                    False,
                )
                return False
            if state.transition is not MotorTransition.IDLE:
                self._record(
                    operation.group,
                    operation.requested,
                    MotorConfirmation.failed(),
                    None,
                    False,
                )
                return False
            if not self._operation_mutex.acquire(blocking=False):
                self._record(
                    operation.group,
                    operation.requested,
                    MotorConfirmation.failed(),
                    None,
                    False,
                )
                return False
            if operation.requested is not None:
                state.gate_open = False
            state.generation += 1
            state.transition = MotorTransition.QUIESCING
            reserved = _ReservedOperation(
                operation.group,
                operation.requested,
                operation.publish,
                state.generation,
                self._hooks[operation.group].create(),
            )
            task = asyncio.get_running_loop().create_task(
                self._run_reserved(reserved),
                name="satellite-motor-operation",
            )
            self._operation = task
            return True

    async def _offload[ResultT](
        self,
        function: Callable[..., ResultT],
        *args: object,
    ) -> ResultT:
        """Run one blocking phase on the sole worker, recorded for shutdown.

        The submitted future is kept, not merely awaited, because cancelling the
        task that awaits it cancels the wrapper and never the thread. Without
        the record, `aclose` reaches `ThreadPoolExecutor.shutdown(wait=True)`
        with a five-second daemon call still running and waits for it *on the
        event loop* — the exact stall every phase here exists to avoid. Startup
        has no task for `_operation` to hold, and a cancelled operation task has
        already let go of its own, so both arrive here.
        """
        submitted = self._executor.submit(function, *args)
        self._inflight = submitted
        try:
            return await asyncio.wrap_future(submitted)
        finally:
            # Only once the worker itself is finished. A cancelled await leaves
            # the thread running, and that is precisely the case the record is
            # for.
            if self._inflight is submitted and submitted.done():
                self._inflight = None

    async def _run_reserved(self, operation: _ReservedOperation) -> None:
        """Keep local state on the loop and blocking daemon work on one worker."""
        complete = False
        try:
            if not self._current(operation):
                return
            lifecycle = operation.lifecycle or MotorGroupLifecycle()
            prepared = (
                await self._offload(lifecycle.prepare_worker)
                if lifecycle.prepare_is_blocking()
                else lifecycle.prepare_worker()
            )
            if not self._current(operation):
                return
            lifecycle.prepare_loop(prepared)
            if not self._current(operation):
                return
            with self._lock:
                state = self._groups[operation.group]
                if state.body_policy is None:
                    state.body_policy = lifecycle.captured_policy(prepared)
                state.policy_capture_pending = False
                state.transition = MotorTransition.CONFIRMING
            outcome = await self._offload(self._execute_reserved, operation)
            if isinstance(outcome, _DeferredEnable):
                complete = await self._finalize_enable(outcome)
            elif isinstance(outcome, _DeferredRefresh):
                complete = await self._finalize_refresh(outcome)
            else:
                complete = outcome is not None
        except asyncio.CancelledError:
            self._fail_operation(operation)
            raise
        except Exception:
            self._fail_operation(operation)
        finally:
            self._operation_mutex.release()
            with self._lock:
                if self._operation is asyncio.current_task():
                    self._operation = None
                state = self._groups[operation.group]
                if (
                    not self._terminal_observed()
                    and state.generation == operation.generation
                ):
                    state.policy_capture_pending = False
                    state.transition = MotorTransition.IDLE
                self._wake_producer_waiters_locked()
        with self._lock:
            may_publish = complete and not self._terminal_observed()
        if may_publish:
            with contextlib.suppress(Exception):
                operation.publish()

    def _execute_reserved(
        self,
        operation: _ReservedOperation,
    ) -> bool | _DeferredEnable | _DeferredRefresh | None:
        """Perform only blocking torque and measured-sampling phases on the worker."""
        if self._terminal_requested.is_set():
            return False
        if operation.requested is None:
            return self._refresh_reserved(operation)
        result = self._transition_reserved(operation)
        if result is not None or self._terminal_requested.is_set():
            return (
                result
                if isinstance(result, (_DeferredEnable, _DeferredRefresh))
                else result is not None
            )
        return self._refresh_reserved(operation)

    async def _finalize_enable(self, deferred: _DeferredEnable) -> bool:
        """Generation-check both loop commits around optional daemon policy restore."""
        operation = _ReservedOperation(
            deferred.group,
            True,
            lambda: None,
            deferred.generation,
            deferred.lifecycle,
        )
        if not self._current(operation):
            return False
        try:
            deferred.lifecycle.sample_loop(deferred.sample)
        except (Exception, asyncio.CancelledError):
            self._promote_failed_enable(deferred)
            raise
        if not self._current(operation):
            return False
        with self._lock:
            policy = self._groups[deferred.group].body_policy
        try:
            restored = await self._offload(deferred.lifecycle.restore_worker, policy)
        except (Exception, asyncio.CancelledError):
            self._promote_failed_enable(deferred)
            raise
        if not self._current(operation):
            return False
        try:
            deferred.lifecycle.restore_loop(restored)
        except (Exception, asyncio.CancelledError):
            self._promote_failed_enable(deferred)
            raise
        with self._lock:
            if not self._current_locked(operation):
                return False
            state = self._groups[deferred.group]
            if not self._promote(state, deferred.actual, gate_open=True):
                return False
            state.body_policy = None
            state.transition = MotorTransition.IDLE
            self._record(
                deferred.group,
                True,
                deferred.confirmation,
                deferred.actual,
                True,
                changed=deferred.changed,
            )
            return True

    async def _finalize_refresh(self, deferred: _DeferredRefresh) -> bool:
        """Restore an already-open group only if its quiesce generation is current."""
        operation = _ReservedOperation(
            deferred.group,
            None,
            lambda: None,
            deferred.generation,
            deferred.lifecycle,
        )
        if not self._current(operation):
            return False
        with self._lock:
            policy = self._groups[deferred.group].body_policy
        try:
            restored = await self._offload(deferred.lifecycle.restore_worker, policy)
        except (Exception, asyncio.CancelledError):
            self._fail_operation(operation)
            raise
        if not self._current(operation):
            return False
        deferred.lifecycle.restore_loop(restored)
        with self._lock:
            if not self._current_locked(operation):
                return False
            state = self._groups[deferred.group]
            if not self._promote(state, deferred.actual, gate_open=True):
                return False
            state.body_policy = None
            state.transition = MotorTransition.IDLE
            self._record(
                deferred.group,
                None,
                deferred.confirmation,
                deferred.actual,
                True,
                changed=deferred.changed,
            )
            return True

    def _promote_failed_enable(self, deferred: _DeferredEnable) -> None:
        """Retain fresh physical truth but never open after a local phase failed."""
        with self._lock:
            if self._terminal_observed():
                return
            state = self._groups[deferred.group]
            if state.generation != deferred.generation:
                return
            if self._promote(state, deferred.actual, gate_open=False):
                self._record(
                    deferred.group,
                    True,
                    deferred.confirmation,
                    deferred.actual,
                    True,
                    changed=deferred.changed,
                )

    def _fail_operation(self, operation: _ReservedOperation) -> None:
        """Close only the current generation after cancellation or local failure."""
        with self._lock:
            if self._terminal_observed():
                return
            state = self._groups[operation.group]
            if state.generation != operation.generation:
                return
            state.gate_open = False
            state.policy_capture_pending = False
            state.transition = MotorTransition.IDLE
            self._record(
                operation.group,
                operation.requested,
                MotorConfirmation.failed(),
                None,
                False,
            )

    def _current(self, operation: _ReservedOperation) -> bool:
        with self._lock:
            return self._current_locked(operation)

    def _current_locked(self, operation: _ReservedOperation) -> bool:
        return (
            not self._terminal_observed()
            and self._groups[operation.group].generation == operation.generation
        )

    async def wait_idle(self) -> None:
        """Await the sole finite operation without accepting another one."""
        with self._lock:
            task = self._operation
        if task is not None:
            await asyncio.shield(task)

    async def aclose(self) -> None:
        """Refuse new work, drain the one active operation, and join its worker."""
        self.terminal()
        cancelled: asyncio.CancelledError | None = None
        while True:
            with self._lock:
                task = self._operation
            if task is None or task.done():
                break
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                if cancelled is None:
                    cancelled = error
        if task is not None and not task.cancelled():
            task.result()
        producer_drain = asyncio.create_task(
            self._wait_for_all_producers(),
            name="satellite-motor-producer-drain",
        )
        while not producer_drain.done():
            try:
                await asyncio.shield(producer_drain)
            except asyncio.CancelledError as error:
                if cancelled is None:
                    cancelled = error
        producer_drain.result()
        while True:
            inflight = self._inflight
            if inflight is None or inflight.done():
                break
            try:
                # Suppressed rather than logged or raised: how the phase ended
                # belongs to whoever awaited it, and all this needs is for the
                # worker to have stopped running. A phase that failed is a
                # finished phase, and the loop leaves on the check above.
                with contextlib.suppress(Exception):
                    await asyncio.shield(asyncio.wrap_future(inflight))
            except asyncio.CancelledError as error:
                if cancelled is None:
                    cancelled = error
        if not self._closed:
            self._closed = True
            # Every blocking phase and every producer reservation was drained
            # above, so this joins threads that have already finished. It is a
            # synchronous wait on the event loop, which is only safe *because*
            # of that: a startup or an operation cancelled out from under a
            # five-second daemon call leaves its thread running, and the drain
            # loop above is what waits for it asynchronously instead.
            self._executor.shutdown(wait=True, cancel_futures=True)
        if cancelled is not None:
            raise cancelled

    async def initialize(self) -> tuple[MotorGroup, ...]:
        """Quiesce, read and reseed enabled groups before registering any switch.

        Awaited for the reason `_run_reserved` is: a correlated torque read is a
        blocking daemon call with a five-second timeout, and three groups of them
        on the event loop is three groups of them during which the stop watcher
        cannot set its event and a terminal request cannot close a gate. Each
        blocking phase goes to the one bounded worker this coordinator already
        owns, and every phase that touches local state stays here — so terminal
        and cancellation reach the same generation checks the post-start
        operation is judged by, and no state moves off the loop to meet them.
        """
        registered: list[MotorGroup] = []
        with self._operation_mutex:
            for group in MotorGroup:
                with self._lock:
                    if self._terminal_observed():
                        return ()
                    state = self._groups[group]
                    state.gate_open = False
                    state.generation += 1
                    generation = state.generation
                    state.transition = MotorTransition.QUIESCING
                    state.policy_capture_pending = True
                    lifecycle = self._hooks[group].create()
                try:
                    prepared = (
                        await self._offload(lifecycle.prepare_worker)
                        if lifecycle.prepare_is_blocking()
                        else lifecycle.prepare_worker()
                    )
                    if not self._startup_current(group, generation):
                        return ()
                    lifecycle.prepare_loop(prepared)
                    with self._lock:
                        state = self._groups[group]
                        if state.body_policy is None:
                            state.body_policy = lifecycle.captured_policy(prepared)
                        state.policy_capture_pending = False
                        state.transition = MotorTransition.CONFIRMING
                    confirmation = await self._offload(self._read, group)
                    if not self._startup_current(group, generation):
                        return ()
                    actual = confirmation.physical_value(
                        MOTOR_GROUPS[group],
                        allow_contradiction=False,
                    )
                    if actual is None:
                        with self._lock:
                            state = self._groups[group]
                            state.gate_open = False
                            state.transition = MotorTransition.IDLE
                            self._record(group, None, confirmation, None, False)
                        continue
                    if actual:
                        with self._lock:
                            self._groups[group].transition = MotorTransition.RESEEDING
                        sample = await self._offload(lifecycle.sample_worker)
                        if not self._startup_current(group, generation):
                            return ()
                        lifecycle.sample_loop(sample)
                        if not self._startup_current(group, generation):
                            return ()
                        with self._lock:
                            policy = self._groups[group].body_policy
                        restored = await self._offload(
                            lifecycle.restore_worker,
                            policy,
                        )
                        if not self._startup_current(group, generation):
                            return ()
                        lifecycle.restore_loop(restored)
                        if not self._startup_current(group, generation):
                            return ()
                    with self._lock:
                        state = self._groups[group]
                        changed = state.last_confirmed is not actual
                        if not self._promote(state, actual, gate_open=actual):
                            return ()
                        if actual:
                            state.body_policy = None
                        state.transition = MotorTransition.IDLE
                        self._record(
                            group,
                            None,
                            confirmation,
                            actual,
                            True,
                            changed=changed,
                        )
                    registered.append(group)
                except asyncio.CancelledError:
                    if self._startup_failed(group, generation):
                        raise
                    return ()
                except Exception:
                    if not self._startup_failed(group, generation):
                        return ()
            return tuple(registered)

    def _startup_current(self, group: MotorGroup, generation: int) -> bool:
        with self._lock:
            return (
                not self._terminal_observed()
                and self._groups[group].generation == generation
            )

    def _startup_failed(self, group: MotorGroup, generation: int) -> bool:
        """Close one failed startup generation; return false when terminal won."""
        with self._lock:
            if self._terminal_observed():
                return False
            state = self._groups[group]
            if state.generation != generation:
                return False
            state.gate_open = False
            state.policy_capture_pending = False
            state.transition = MotorTransition.IDLE
            self._record(group, None, MotorConfirmation.failed(), None, False)
            return True

    def last_confirmed(self, group: MotorGroup) -> bool | None:
        """Return the retained switch Boolean independently of evidence freshness."""
        with self._lock:
            self._terminal_observed()
            return self._groups[group].last_confirmed

    def gate_open(self, group: MotorGroup) -> bool:
        """Return whether a producer may command this group now."""
        with self._lock:
            return not self._terminal_observed() and self._groups[group].gate_open

    def command(self, groups: Sequence[MotorGroup], action: Callable[[], None]) -> bool:
        """Reserve a producer briefly, then call hardware without the state lock."""
        required = tuple(dict.fromkeys(groups))
        with self._lock:
            if self._terminal_observed() or any(
                not self._groups[group].gate_open
                or self._groups[group].transition is not MotorTransition.IDLE
                for group in required
            ):
                return False
            for group in required:
                self._groups[group].commands_inflight += 1
        try:
            action()
        finally:
            with self._lock:
                for group in required:
                    state = self._groups[group]
                    state.commands_inflight -= 1
                self._commands_drained.notify_all()
                self._wake_producer_waiters_locked()
        with self._lock:
            return not self._terminal_observed()

    async def _wait_for_all_producers(self) -> None:
        """Await external-thread producer reservations without blocking the loop."""
        loop = asyncio.get_running_loop()
        waiter = loop.create_future()
        token = (loop, waiter)
        with self._lock:
            if not any(state.commands_inflight for state in self._groups.values()):
                return
            self._producer_waiters.add(token)
        try:
            await waiter
        finally:
            with self._lock:
                self._producer_waiters.discard(token)

    def _wake_producer_waiters_locked(self) -> None:
        """Resolve drain waiters after the final producer releases its reservation."""
        if any(state.commands_inflight for state in self._groups.values()):
            return
        waiters = tuple(self._producer_waiters)
        self._producer_waiters.clear()
        for loop, waiter in waiters:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._resolve_waiter, waiter)

    @staticmethod
    def _resolve_waiter(waiter: asyncio.Future[None]) -> None:
        if not waiter.done():
            waiter.set_result(None)

    def _transition_reserved(
        self,
        operation: _ReservedOperation,
    ) -> bool | _DeferredEnable | None:
        """Confirm torque and sample hardware on the worker for one generation."""
        group = operation.group
        requested = operation.requested
        if requested is None or not self._wait_for_commands(group):
            return None
        confirmation = self._set(group, requested)
        with self._lock:
            if not self._current_locked(operation):
                return None
            state = self._groups[group]
            actual = confirmation.physical_value(MOTOR_GROUPS[group])
            if actual is None:
                state.gate_open = False
                self._record(group, requested, confirmation, None, False)
                return None
            changed = state.last_confirmed is not actual
            if actual is not requested or not actual:
                if not self._promote(state, actual, gate_open=False):
                    return None
                self._record(
                    group,
                    requested,
                    confirmation,
                    actual,
                    True,
                    changed=changed,
                )
                return actual
            state.transition = MotorTransition.RESEEDING
        lifecycle = operation.lifecycle or MotorGroupLifecycle()
        sample = lifecycle.sample_worker()
        if not self._current(operation):
            return None
        return _DeferredEnable(
            group,
            actual,
            confirmation,
            changed,
            operation.generation,
            lifecycle,
            sample,
        )

    def _refresh_reserved(
        self,
        operation: _ReservedOperation,
    ) -> bool | _DeferredRefresh | None:
        """Read physical truth on the worker and preserve any safely open gate."""
        group = operation.group
        if not self._wait_for_commands(group):
            return None
        confirmation = self._read(group)
        with self._lock:
            if not self._current_locked(operation):
                return None
            state = self._groups[group]
            gate_was_open = state.gate_open
            actual = confirmation.physical_value(
                MOTOR_GROUPS[group],
                allow_contradiction=False,
            )
            if actual is None:
                state.gate_open = False
                self._record(group, None, confirmation, None, False)
                return None
            changed = state.last_confirmed is not actual
            if actual and gate_was_open:
                return _DeferredRefresh(
                    group,
                    actual,
                    confirmation,
                    changed,
                    operation.generation,
                    operation.lifecycle or MotorGroupLifecycle(),
                )
            if not self._promote(state, actual, gate_open=False):
                return None
            self._record(
                group,
                None,
                confirmation,
                actual,
                True,
                changed=changed,
            )
            return actual

    def _wait_for_commands(self, group: MotorGroup) -> bool:
        """Let already-reserved producers drain on the worker before torque I/O."""
        with self._commands_drained:
            state = self._groups[group]
            while state.commands_inflight and not self._terminal_requested.is_set():
                self._commands_drained.wait()
            return not self._terminal_observed()

    def terminal(self) -> None:
        """Request terminal state immediately, then close every gate under lock."""
        self._terminal_requested.set()
        with self._lock:
            self._terminal_observed()
            self._commands_drained.notify_all()

    @property
    def terminal_requested(self) -> bool:
        """Whether terminal state has been asked for, before any gate observes it.

        Read without the state lock deliberately: this is what a worker thread
        blocked in a daemon call asks the moment it returns, and it has to be
        answerable while the loop still holds that lock.
        """
        return self._terminal_requested.is_set()

    def safe_to_restore_body_policy(self) -> bool:
        """Return whether ordinary release may restore daemon automatic yaw."""
        with self._lock:
            self._terminal_observed()
            body = self._groups[MotorGroup.BODY]
            operation = self._operation
            return (
                body.last_confirmed is True
                and body.transition in {MotorTransition.IDLE, MotorTransition.TERMINAL}
                and body.body_policy is None
                and not body.policy_capture_pending
                and not any(state.commands_inflight for state in self._groups.values())
                and (operation is None or operation.done())
            )

    def status(self) -> dict[str, object]:
        """Return bounded identifier-free group state and confirmation evidence."""
        with self._lock:
            self._terminal_observed()
            now = self._now()
            self._terminal_observed()
            groups: dict[str, object] = {}
            events: list[dict[str, object]] = []
            for group, state in self._groups.items():
                age = self._age(state.confirmed_at, now)
                groups[group.value] = {
                    "last_confirmed": state.last_confirmed,
                    "confirmation_age": age,
                    "gate_open": state.gate_open and not self._terminal,
                    "transition": state.transition.value,
                }
                events.extend(dict(event) for event in state.diagnostics)
            return {"groups": groups, "events": events}

    def _set(self, group: MotorGroup, requested: bool) -> MotorConfirmation:
        try:
            if requested:
                return self._handle.enable_motors_confirmed(list(MOTOR_GROUPS[group]))
            return self._handle.disable_motors_confirmed(list(MOTOR_GROUPS[group]))
        except Exception:
            return MotorConfirmation.failed()

    def _read(self, group: MotorGroup) -> MotorConfirmation:
        try:
            return self._handle.read_motor_torque(list(MOTOR_GROUPS[group]))
        except Exception:
            return MotorConfirmation.failed()

    def _record(
        self,
        group: MotorGroup,
        requested: bool | None,
        confirmation: MotorConfirmation,
        actual: bool | None,
        fresh: bool,
        *,
        changed: bool = False,
    ) -> None:
        if self._terminal_observed():
            return
        now = self._now()
        if self._terminal_observed():
            return
        state = self._groups[group]
        state.diagnostics.append(
            {
                "group": group.value,
                "requested": requested,
                "acknowledgement": "acknowledged"
                if confirmation.acknowledged
                else "absent",
                "readback": (
                    "enabled"
                    if actual is True
                    else "disabled"
                    if actual is False
                    else "incomplete"
                    if confirmation.outcome
                    in {
                        MotorConfirmationOutcome.CONFIRMED,
                        MotorConfirmationOutcome.CONTRADICTED,
                    }
                    else confirmation.outcome.value
                ),
                "fresh": fresh,
                "changed": changed,
                "confirmation_age": self._age(state.confirmed_at, now),
            }
        )

    @staticmethod
    def _age(confirmed_at: float | None, now: float) -> float | None:
        if confirmed_at is None:
            return None
        return max(0.0, now - confirmed_at)

    def _terminal_observed(self) -> bool:
        """Promote a reentrant or concurrent request and preserve terminal state."""
        if not self._terminal and not self._terminal_requested.is_set():
            return False
        if not self._terminal:
            self._terminal = True
            for state in self._groups.values():
                state.gate_open = False
                state.transition = MotorTransition.TERMINAL
        return True

    def _promote(
        self,
        state: _GroupState,
        actual: bool,
        *,
        gate_open: bool | None,
    ) -> bool:
        """Commit fresh state only while terminal has not won concurrently."""
        if self._terminal_observed():
            return False
        confirmed_at = self._now()
        if self._terminal_observed():
            return False
        previous_confirmed = state.last_confirmed
        previous_at = state.confirmed_at
        state.last_confirmed = actual
        state.confirmed_at = confirmed_at
        if gate_open is not None:
            state.gate_open = gate_open
        if self._terminal_observed():
            state.last_confirmed = previous_confirmed
            state.confirmed_at = previous_at
            state.gate_open = False
            return False
        return True

    def _now(self) -> float:
        now = self._clock()
        return now if math.isfinite(now) else 0.0
