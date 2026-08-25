"""Bounded private scalar diagnostics for predictive gaze."""

from __future__ import annotations

from reachy_mini_ha_satellite.behaviour.controller_diagnostics import (
    ControllerDiagnostics,
)
from reachy_mini_ha_satellite.behaviour.gaze_controller import (
    ControllerConfig,
    initial_controller_state,
    step_controller,
)


def test_diagnostics_ring_evicts_oldest_and_snapshots_deterministically() -> None:
    """Fixed capacity retains the newest events in stable oldest-first order."""
    diagnostics = ControllerDiagnostics(capacity=2)
    config = ControllerConfig()
    state = initial_controller_state(config)

    for index in range(3):
        at = index * 0.05
        step = step_controller(
            state,
            None,
            now=at,
            dt=0.0 if index == 0 else 0.05,
            config=config,
        )
        state = step.state
        diagnostics.record(
            step,
            at=at,
            observation_age=None,
            emitted=False,
        )

    first = diagnostics.snapshot()
    second = diagnostics.snapshot()
    assert first == second
    assert [event["at"] for event in first] == [0.05, 0.1]


def test_diagnostics_schema_has_only_allowed_scalar_values_and_no_forbidden_keys() -> (
    None
):
    """No payload key can retain an image, installation identity or free-form error."""
    diagnostics = ControllerDiagnostics(capacity=1)
    config = ControllerConfig()
    step = step_controller(
        initial_controller_state(config),
        None,
        now=0.0,
        dt=0.0,
        config=config,
    )
    diagnostics.record(step, at=0.0, observation_age=None, emitted=False)

    event = diagnostics.snapshot()[0]
    forbidden = {
        "source",
        "generation",
        "sequence",
        "identity",
        "face",
        "image",
        "config",
        "credential",
        "network",
        "path",
        "exception",
    }
    assert forbidden.isdisjoint(event)
    assert all(
        value is None or isinstance(value, str | float | bool)
        for value in event.values()
    )


def test_reset_clears_only_ring_contents() -> None:
    """The diagnostics owner has no reference through which it could move a robot."""
    diagnostics = ControllerDiagnostics(capacity=1)
    config = ControllerConfig()
    state = initial_controller_state(config)
    step = step_controller(state, None, now=0.0, dt=0.0, config=config)
    diagnostics.record(step, at=0.0, observation_age=None, emitted=False)
    controller_before = step.state

    diagnostics.reset()

    assert diagnostics.snapshot() == ()
    assert step.state == controller_before
