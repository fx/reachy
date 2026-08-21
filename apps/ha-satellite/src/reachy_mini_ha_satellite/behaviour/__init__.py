"""The robot's decisions, taken without touching the robot.

Events and detections in, motion intents out. Nothing here imports an adapter,
reads a clock or sleeps: time arrives as a parameter to every method that needs
it, which is what makes staleness, animation and pipeline timing testable
without a suite that waits for any of them.

The import restriction is a build failure rather than a convention. `just
lint-behaviour-boundary` bans `reachy_mini_ha_satellite.adapters` inside this
package and proves the ban still fires by running it against a fixture that
breaks it — the same shape as the vendored-boundary and capability-boundary
rules this repository already carries.

Four modules, and the split follows what each of them can be asked:

| Module | What it decides |
|---|---|
| `pipeline` | where the voice pipeline is, given what it did |
| `movement` | what a pipeline state looks like from across the room |
| `tracking` | where the head should point, given what is in front of it |
| `satellite` | which of the two owns the head, and what to command |

`intents` is the vocabulary all four speak: a movement described rather than
performed, mirroring `ports.MotionPort` method for method so that a decision is
always something that can be carried out.
"""

from reachy_mini_ha_satellite.behaviour.intents import (
    LookAhead,
    LookAt,
    MotionIntent,
    MoveAntennas,
    MoveHead,
)
from reachy_mini_ha_satellite.behaviour.movement import Expression, expression
from reachy_mini_ha_satellite.behaviour.pipeline import (
    ERROR_SECONDS,
    PipelineEvent,
    PipelineMachine,
    PipelineState,
    Transition,
)
from reachy_mini_ha_satellite.behaviour.satellite import (
    BehaviourStatus,
    SatelliteBehaviour,
)
from reachy_mini_ha_satellite.behaviour.tracking import (
    FaceTracker,
    GazeOutcome,
    TrackingDecision,
    choose_face,
)

__all__ = [
    "ERROR_SECONDS",
    "BehaviourStatus",
    "Expression",
    "FaceTracker",
    "GazeOutcome",
    "LookAhead",
    "LookAt",
    "MotionIntent",
    "MoveAntennas",
    "MoveHead",
    "PipelineEvent",
    "PipelineMachine",
    "PipelineState",
    "SatelliteBehaviour",
    "TrackingDecision",
    "Transition",
    "choose_face",
    "expression",
]
