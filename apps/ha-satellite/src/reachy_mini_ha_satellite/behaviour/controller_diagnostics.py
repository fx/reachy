"""Pure bounded scalar evidence for the predictive gaze controller."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from reachy_mini_ha_satellite.behaviour.gaze_controller import (
    ControllerFault,
    ControllerMode,
    ControllerStep,
)

__all__ = ["ControllerDiagnostics", "ControllerEvent", "DiagnosticScalar"]

_DEFAULT_CAPACITY: Final = 128

type DiagnosticScalar = float | bool | str | None


def _bounded_capacity(value: object) -> int:
    """Return one positive integer capacity, rejecting bool explicitly."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        message = "diagnostics capacity must be a positive integer"
        raise ValueError(message)
    return value


@dataclass(frozen=True, slots=True)
class ControllerEvent:
    """One fixed-schema event containing scalar, enum, boolean or null values."""

    at: float
    mode: ControllerMode
    fault: ControllerFault
    safe_hold: bool
    observation_age: float | None
    observation_consumed: bool
    estimator_reset: StrEnum
    prediction_horizon: float
    deadband_active: bool
    world_yaw: float
    elevation: float
    body_yaw: float
    emitted: bool
    command_accepted: bool | None = None

    def __post_init__(self) -> None:
        """Reject non-finite diagnostics rather than serializing invalid JSON."""
        values: tuple[float, ...] = (
            self.at,
            self.prediction_horizon,
            self.world_yaw,
            self.elevation,
            self.body_yaw,
        )
        if self.observation_age is not None:
            values = (*values, self.observation_age)
        if not all(math.isfinite(value) for value in values):
            message = "controller diagnostics must contain only finite scalars"
            raise ValueError(message)

    def snapshot(self) -> dict[str, DiagnosticScalar]:
        """Return the fixed JSON-safe public shape without installation identity."""
        return {
            "at": self.at,
            "mode": self.mode.value,
            "fault": self.fault.value,
            "safe_hold": self.safe_hold,
            "observation_age": self.observation_age,
            "observation_consumed": self.observation_consumed,
            "estimator_reset": self.estimator_reset.value,
            "prediction_horizon": self.prediction_horizon,
            "deadband_active": self.deadband_active,
            "world_yaw": self.world_yaw,
            "elevation": self.elevation,
            "body_yaw": self.body_yaw,
            "emitted": self.emitted,
            "command_accepted": self.command_accepted,
        }


class ControllerDiagnostics:
    """Fixed-capacity deterministic ring with oldest-first snapshots."""

    def __init__(self, capacity: int = _DEFAULT_CAPACITY) -> None:
        """Create an empty ring with deterministic left-edge eviction."""
        self._events: deque[ControllerEvent] = deque(maxlen=_bounded_capacity(capacity))

    @property
    def capacity(self) -> int:
        """Return the immutable maximum number of retained events."""
        maximum = self._events.maxlen
        if maximum is None:
            raise AssertionError("controller diagnostics are always bounded")
        return maximum

    def append(self, event: ControllerEvent) -> None:
        """Append one event, evicting exactly the oldest event when full."""
        self._events.append(event)

    def record(
        self,
        step: ControllerStep,
        *,
        at: float,
        observation_age: float | None,
        emitted: bool,
        command_accepted: bool | None = None,
    ) -> None:
        """Record fixed-schema evidence from one pure controller decision."""
        sample = step.sample
        self.append(
            ControllerEvent(
                at=at,
                mode=step.mode,
                fault=step.state.fault,
                safe_hold=step.safe_hold,
                observation_age=observation_age,
                observation_consumed=step.observation_consumed,
                estimator_reset=step.estimator_reset,
                prediction_horizon=step.prediction_horizon,
                deadband_active=step.deadband_active,
                world_yaw=sample.world_yaw,
                elevation=sample.elevation,
                body_yaw=sample.body_yaw,
                emitted=emitted,
                command_accepted=command_accepted,
            )
        )

    def snapshot(self) -> tuple[dict[str, DiagnosticScalar], ...]:
        """Return an immutable oldest-first copy of every retained event."""
        return tuple(event.snapshot() for event in self._events)

    def reset(self) -> None:
        """Clear diagnostics and nothing else."""
        self._events.clear()
