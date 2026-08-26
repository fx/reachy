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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Final

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
    diagnostics: deque[dict[str, object]] = field(
        default_factory=lambda: deque(maxlen=_DIAGNOSTIC_CAPACITY)
    )


@dataclass(frozen=True, slots=True)
class _Hooks:
    prepare: Callable[[], bool] | None = None
    reseed: Callable[[], Callable[[], None] | None] | None = None
    restore: Callable[[bool], None] | None = None


@dataclass(frozen=True, slots=True)
class _ReservedOperation:
    group: MotorGroup
    requested: bool | None
    publish: Callable[[], None]


@dataclass(frozen=True, slots=True)
class _DeferredEnable:
    group: MotorGroup
    actual: bool
    prior_policy: bool | None
    confirmation: MotorConfirmation
    changed: bool
    finalize: Callable[[], None]


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
        self._closed = False

    def set_hooks(
        self,
        group: MotorGroup,
        *,
        prepare: Callable[[], bool] | None = None,
        reseed: Callable[[], Callable[[], None] | None] | None = None,
        restore: Callable[[bool], None] | None = None,
    ) -> None:
        """Install application-owned quiesce, reseed and policy callbacks."""
        with self._lock:
            self._hooks[group] = _Hooks(prepare, reseed, restore)

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
            if operation.requested is not None:
                state.gate_open = False
            state.transition = MotorTransition.QUIESCING
            task = asyncio.get_running_loop().create_task(
                self._run_reserved(operation),
                name="satellite-motor-operation",
            )
            self._operation = task
            return True

    async def _run_reserved(self, operation: _ReservedOperation) -> None:
        """Run blocking phases on one worker and finalize behavior on the loop."""
        loop = asyncio.get_running_loop()
        complete = False
        try:
            outcome = await loop.run_in_executor(
                self._executor,
                self._execute_reserved,
                operation,
            )
            if isinstance(outcome, _DeferredEnable):
                with self._lock:
                    terminal = self._terminal_observed()
                if not terminal:
                    try:
                        outcome.finalize()
                    except (Exception, asyncio.CancelledError):
                        complete = self._fail_deferred(outcome)
                    else:
                        complete = await loop.run_in_executor(
                            self._executor,
                            self._complete_deferred,
                            outcome,
                        )
            else:
                complete = outcome
        finally:
            with self._lock:
                if self._operation is asyncio.current_task():
                    self._operation = None
                state = self._groups[operation.group]
                if not self._terminal_observed():
                    state.transition = MotorTransition.IDLE
        with self._lock:
            may_publish = complete and not self._terminal_observed()
        if may_publish:
            # A disappearing Home Assistant connection cannot invalidate the
            # already-confirmed physical state or leave this task exceptional.
            with contextlib.suppress(Exception):
                operation.publish()

    def _execute_reserved(
        self,
        operation: _ReservedOperation,
    ) -> bool | _DeferredEnable:
        """Perform blocking phases on the sole worker and report fresh evidence."""
        if self._terminal_requested.is_set():
            return False
        with self._operation_mutex:
            if operation.requested is None:
                return self._refresh_operation(operation.group) is not None
            result = self._transition_operation(
                operation.group,
                operation.requested,
                defer_finalize=True,
            )
            if result is not None or self._terminal_requested.is_set():
                return (
                    result
                    if isinstance(result, _DeferredEnable)
                    else result is not None
                )
            return self._refresh_operation(operation.group) is not None

    def _fail_deferred(self, deferred: _DeferredEnable) -> bool:
        """Retain truthful physical state when loop-side finalization fails."""
        with self._lock:
            if self._terminal_observed():
                return False
            state = self._groups[deferred.group]
            if not self._promote(state, deferred.actual, gate_open=False):
                return False
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

    def _complete_deferred(self, deferred: _DeferredEnable) -> bool:
        """Restore daemon policy on the worker, then safely open the gate."""
        with self._lock:
            if self._terminal_observed():
                return False
            hooks = self._hooks[deferred.group]
        try:
            if hooks.restore is not None and deferred.prior_policy is not None:
                hooks.restore(deferred.prior_policy)
        except (Exception, asyncio.CancelledError):
            return self._fail_deferred(deferred)
        with self._lock:
            if self._terminal_observed():
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
        if not self._closed:
            self._closed = True
            # The active future was awaited above, so this joins an idle worker.
            self._executor.shutdown(wait=True, cancel_futures=True)
        if cancelled is not None:
            raise cancelled

    def initialize(self) -> tuple[MotorGroup, ...]:
        """Read every exact group and open only completely confirmed gates."""
        registered: list[MotorGroup] = []
        with self._lock:
            if self._terminal_observed():
                return ()
            for group in MotorGroup:
                if self._terminal_observed():
                    return ()
                state = self._groups[group]
                hooks = self._hooks[group]
                captured_policy: bool | None = None
                if hooks.prepare is not None:
                    state.policy_capture_pending = True
                    try:
                        captured_policy = hooks.prepare()
                    except asyncio.CancelledError:
                        if self._terminal_observed():
                            return ()
                        state.policy_capture_pending = False
                        state.gate_open = False
                        self._record(
                            group,
                            None,
                            MotorConfirmation.failed(),
                            None,
                            False,
                        )
                        raise
                    except Exception:
                        if self._terminal_observed():
                            return ()
                        state.policy_capture_pending = False
                        state.gate_open = False
                        self._record(
                            group,
                            None,
                            MotorConfirmation.failed(),
                            None,
                            False,
                        )
                        continue
                    if self._terminal_observed():
                        return ()
                    state.body_policy = captured_policy
                    state.policy_capture_pending = False
                if self._terminal_observed():
                    return ()
                try:
                    confirmation = self._read(group)
                except asyncio.CancelledError:
                    if self._terminal_observed():
                        return ()
                    state.gate_open = False
                    self._record(
                        group,
                        None,
                        MotorConfirmation.failed(),
                        None,
                        False,
                    )
                    raise
                if self._terminal_observed():
                    return ()
                actual = confirmation.physical_value(
                    MOTOR_GROUPS[group],
                    allow_contradiction=False,
                )
                if actual is None:
                    state.gate_open = False
                    self._record(group, None, confirmation, None, False)
                    continue
                if actual and hooks.restore is not None and captured_policy is not None:
                    try:
                        hooks.restore(captured_policy)
                    except asyncio.CancelledError:
                        if self._terminal_observed():
                            return ()
                        state.gate_open = False
                        self._record(
                            group,
                            None,
                            MotorConfirmation.failed(),
                            None,
                            False,
                        )
                        raise
                    except Exception:
                        if self._terminal_observed():
                            return ()
                        state.gate_open = False
                        self._record(
                            group,
                            None,
                            MotorConfirmation.failed(),
                            None,
                            False,
                        )
                        continue
                    if self._terminal_observed():
                        return ()
                    state.body_policy = None
                changed = state.last_confirmed is not actual
                if not self._promote(state, actual, gate_open=actual):
                    return ()
                self._record(
                    group,
                    None,
                    confirmation,
                    actual,
                    True,
                    changed=changed,
                )
                registered.append(group)
            return tuple(registered)

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
        with self._lock:
            return not self._terminal_observed()

    def transition(self, group: MotorGroup, requested: bool) -> bool | None:
        """Quiesce, confirm and reseed without holding the state lock over I/O."""
        with self._operation_mutex:
            result = self._transition_operation(group, requested)
        return result if not isinstance(result, _DeferredEnable) else None

    def _transition_operation(
        self,
        group: MotorGroup,
        requested: bool,
        *,
        defer_finalize: bool = False,
    ) -> bool | _DeferredEnable | None:
        with self._lock:
            state = self._groups[group]
            if self._terminal_observed():
                return None
            state.gate_open = False
            state.transition = MotorTransition.QUIESCING
            hooks = self._hooks[group]
            prior_policy = state.body_policy
            if hooks.prepare is not None:
                state.policy_capture_pending = True

        try:
            prepared_policy = hooks.prepare() if hooks.prepare is not None else None
        except asyncio.CancelledError:
            with self._lock:
                if self._terminal_observed():
                    return None
                state.policy_capture_pending = False
                state.transition = MotorTransition.IDLE
                self._record(group, requested, MotorConfirmation.failed(), None, False)
            raise
        except Exception:
            with self._lock:
                if self._terminal_observed():
                    return None
                state.policy_capture_pending = False
                state.transition = MotorTransition.IDLE
                self._record(group, requested, MotorConfirmation.failed(), None, False)
            return None

        with self._lock:
            if self._terminal_observed():
                return None
            if prior_policy is None:
                prior_policy = prepared_policy
                state.body_policy = prepared_policy
            state.policy_capture_pending = False
            state.transition = MotorTransition.CONFIRMING
        if not self._wait_for_commands(group):
            return None
        try:
            confirmation = self._set(group, requested)
        except asyncio.CancelledError:
            with self._lock:
                if self._terminal_observed():
                    return None
                state.transition = MotorTransition.IDLE
                self._record(group, requested, MotorConfirmation.failed(), None, False)
            raise
        with self._lock:
            if self._terminal_observed():
                return None
            actual = confirmation.physical_value(MOTOR_GROUPS[group])
            if actual is None:
                state.transition = MotorTransition.IDLE
                self._record(group, requested, confirmation, None, False)
                return None
            changed = state.last_confirmed is not actual
            if actual is not requested or not actual:
                if not self._promote(state, actual, gate_open=False):
                    return None
                state.transition = MotorTransition.IDLE
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

        finalize: Callable[[], None] | None = None
        try:
            if hooks.reseed is not None:
                finalize = hooks.reseed()
        except asyncio.CancelledError:
            with self._lock:
                if self._terminal_observed():
                    return None
                if not self._promote(state, actual, gate_open=False):
                    return None
                state.transition = MotorTransition.IDLE
                self._record(
                    group,
                    requested,
                    confirmation,
                    actual,
                    True,
                    changed=changed,
                )
            raise
        except Exception:
            with self._lock:
                if self._terminal_observed():
                    return None
                if not self._promote(state, actual, gate_open=False):
                    return None
                state.transition = MotorTransition.IDLE
                self._record(
                    group,
                    requested,
                    confirmation,
                    actual,
                    True,
                    changed=changed,
                )
            return actual

        with self._lock:
            if self._terminal_observed():
                return None
        if finalize is not None and defer_finalize:
            return _DeferredEnable(
                group,
                actual,
                prior_policy,
                confirmation,
                changed,
                finalize,
            )
        try:
            if finalize is not None:
                finalize()
        except Exception:
            with self._lock:
                if self._terminal_observed():
                    return None
                if not self._promote(state, actual, gate_open=False):
                    return None
                state.transition = MotorTransition.IDLE
                self._record(
                    group,
                    requested,
                    confirmation,
                    actual,
                    True,
                    changed=changed,
                )
            return actual
        with self._lock:
            if self._terminal_observed():
                return None
        try:
            if hooks.restore is not None and prior_policy is not None:
                hooks.restore(prior_policy)
        except asyncio.CancelledError:
            with self._lock:
                if self._terminal_observed():
                    return None
                if not self._promote(state, actual, gate_open=False):
                    return None
                state.transition = MotorTransition.IDLE
                self._record(
                    group,
                    requested,
                    confirmation,
                    actual,
                    True,
                    changed=changed,
                )
            raise
        except Exception:
            with self._lock:
                if self._terminal_observed():
                    return None
                if not self._promote(state, actual, gate_open=False):
                    return None
                state.transition = MotorTransition.IDLE
                self._record(
                    group,
                    requested,
                    confirmation,
                    actual,
                    True,
                    changed=changed,
                )
            return actual

        with self._lock:
            if self._terminal_observed():
                return None
            if not self._promote(state, actual, gate_open=True):
                return None
            state.body_policy = None
            state.transition = MotorTransition.IDLE
            self._record(
                group,
                requested,
                confirmation,
                actual,
                True,
                changed=changed,
            )
            return actual

    def refresh(self, group: MotorGroup) -> bool | None:
        """Read independently without holding the state lock over I/O."""
        with self._operation_mutex:
            return self._refresh_operation(group)

    def _refresh_operation(self, group: MotorGroup) -> bool | None:
        with self._lock:
            state = self._groups[group]
            if self._terminal_observed():
                return None
            hooks = self._hooks[group]
            gate_was_open = state.gate_open
            prepare = hooks.prepare if state.body_policy is None else None
            if prepare is not None:
                state.policy_capture_pending = True
            if state.transition is MotorTransition.IDLE:
                state.transition = MotorTransition.QUIESCING

        try:
            captured_policy = prepare() if prepare is not None else None
        except asyncio.CancelledError:
            with self._lock:
                if self._terminal_observed():
                    return None
                state.policy_capture_pending = False
                state.gate_open = False
                state.transition = MotorTransition.IDLE
                self._record(group, None, MotorConfirmation.failed(), None, False)
            raise
        except Exception:
            with self._lock:
                if self._terminal_observed():
                    return None
                state.policy_capture_pending = False
                state.gate_open = False
                state.transition = MotorTransition.IDLE
                self._record(group, None, MotorConfirmation.failed(), None, False)
            return None

        with self._lock:
            if self._terminal_observed():
                return None
            if prepare is not None:
                state.body_policy = captured_policy
                state.policy_capture_pending = False
            state.transition = MotorTransition.CONFIRMING
        if not self._wait_for_commands(group):
            return None
        try:
            confirmation = self._read(group)
        except asyncio.CancelledError:
            with self._lock:
                if self._terminal_observed():
                    return None
                state.gate_open = False
                state.transition = MotorTransition.IDLE
                self._record(group, None, MotorConfirmation.failed(), None, False)
            raise
        with self._lock:
            if self._terminal_observed():
                return None
            actual = confirmation.physical_value(
                MOTOR_GROUPS[group],
                allow_contradiction=False,
            )
            if actual is None:
                state.gate_open = False
                state.transition = MotorTransition.IDLE
                self._record(group, None, confirmation, None, False)
                return None
            changed = state.last_confirmed is not actual
            policy = state.body_policy

        if actual and prepare is not None and gate_was_open:
            try:
                if hooks.restore is not None and policy is not None:
                    hooks.restore(policy)
            except asyncio.CancelledError:
                with self._lock:
                    if self._terminal_observed():
                        return None
                    if not self._promote(state, actual, gate_open=False):
                        return None
                    state.transition = MotorTransition.IDLE
                    self._record(
                        group,
                        None,
                        confirmation,
                        actual,
                        True,
                        changed=changed,
                    )
                raise
            except Exception:
                with self._lock:
                    if self._terminal_observed():
                        return None
                    if not self._promote(state, actual, gate_open=False):
                        return None
                    state.transition = MotorTransition.IDLE
                    self._record(
                        group,
                        None,
                        confirmation,
                        actual,
                        True,
                        changed=changed,
                    )
                return actual
            with self._lock:
                if self._terminal_observed():
                    return None
                state.body_policy = None

        with self._lock:
            if not self._promote(
                state,
                actual,
                gate_open=False if not actual else None,
            ):
                return None
            state.transition = MotorTransition.IDLE
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

    def safe_to_restore_body_policy(self) -> bool:
        """Return whether ordinary release may restore daemon automatic yaw."""
        with self._lock:
            self._terminal_observed()
            body = self._groups[MotorGroup.BODY]
            return (
                body.last_confirmed is True
                and body.transition in {MotorTransition.IDLE, MotorTransition.TERMINAL}
                and body.body_policy is None
                and not body.policy_capture_pending
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
