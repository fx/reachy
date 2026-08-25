"""The robot's decisions, taken without touching the robot.

Events, source-qualified detections and calibrated world targets in; motion
intents out. Nothing here imports an adapter, reads a clock or sleeps: time and
calibration arrive as values, which makes staleness, animation and trajectory
timing testable without a suite that waits for any of them.

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
| `tracking` | source-qualified face selection and estimator discontinuities |
| `gaze_controller` | prediction, deadband, coordinated trajectories, loss and holds |
| `satellite` | two-phase calibration join, head ownership and pipeline handoff |

`intents` is the movement vocabulary: coordinated gaze, pipeline head and
independent antennas, each backed by a `ports.MotionPort` method.
"""

from reachy_mini_ha_satellite.behaviour.intents import (
    CommandGaze,
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
    PreparedGazeTick,
    SatelliteBehaviour,
)
from reachy_mini_ha_satellite.behaviour.tracking import GazeSelector, choose_face
from reachy_mini_ha_satellite.ports import GazeDirective, GazeOutcome

__all__ = [
    "ERROR_SECONDS",
    "BehaviourStatus",
    "CommandGaze",
    "Expression",
    "GazeDirective",
    "GazeOutcome",
    "GazeSelector",
    "MotionIntent",
    "MoveAntennas",
    "MoveHead",
    "PipelineEvent",
    "PipelineMachine",
    "PipelineState",
    "PreparedGazeTick",
    "SatelliteBehaviour",
    "Transition",
    "choose_face",
    "expression",
]
