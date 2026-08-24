# Predictive Gaze and Coordinated Motion

## Overview

This specification defines how the robot turns timestamped face detections into
stable, socially coherent motion of its head and body. It owns observation
handling, visual prediction, trajectory quality, coordinated gaze allocation,
active-gaze arbitration, bounded hold and return, handoff, controller faults and
controller-specific evidence. The [HA Satellite](../ha-satellite/)
specification continues to own application lifecycle, stale and loss signalling,
the neutral outcome, hardware boundaries and voice-pipeline and idle expression.

## Background

A face detector reports a normalized center and confidence, but a social robot
needs more than a point-to-pose mapping. Results arrive after capture, may be
repeated by a faster behavior loop, may be delayed or dropped, and are observed
from a camera that moves with the head. The six-actuator head and body-yaw axis
also need one coordinated world-gaze target rather than independent corrections.

The proof of concept established a controller around source-qualified
observations, capture-time calibration, bounded prediction, continuous
head–body allocation and online jerk-limited trajectories. It also established
why earlier approaches oscillated: they re-applied cached image errors, mixed
capture-time image data with later robot poses, and moved head and body without
preserving one world-space fixation target.

This specification consumes, rather than restates, the existing contracts for
normalized detection geometry and capture timestamps in
[robot-link](../robot-link/), face centers and confidence in
[perception](../perception/), and pure decision logic, stale and loss signalling,
the neutral outcome, and pipeline and idle expression in
[HA Satellite](../ha-satellite/).

## Requirements

### REQ-074: Each observation is consumed once

Repeated reads of one source-qualified face-detection result MUST NOT compound
its correction while the previously established trajectory continues.

#### Scenario: The behavior loop polls one result repeatedly

- **GIVEN** one fresh result whose source, generation and sequence are unchanged
- **WHEN** the behavior loop reads that cached result several times
- **THEN** the cached result does not compound the commanded correction and the
  current trajectory continues

### REQ-075: Observation time uses the capture clock

Changing nominal detector or behavior-loop cadence MUST NOT change observation
age or the trajectory produced from an otherwise identical timed observation
sequence.

#### Scenario: A result arrives late

- **GIVEN** a result captured before a network and inference delay
- **WHEN** the controller receives it
- **THEN** prediction uses its measured age and does not reinterpret receipt as
  capture

### REQ-076: Observation discontinuities reset prediction

An observation after a detection-source, source-generation, selected-target,
timestamp-order or supported-gap discontinuity MUST NOT inherit target velocity
from the preceding observation stream.

#### Scenario: Remote perception reconnects

- **GIVEN** a tracked target from one remote-session generation
- **WHEN** a new generation begins its sequence again
- **THEN** the resulting motion begins without inheriting direction or speed
  from the preceding generation

### REQ-077: Prediction is bounded separately from staleness

The controller MUST bound predicted image position, target velocity and
prediction horizon while treating receipt-time staleness as the separate trigger
for loss handling.

#### Scenario: A fresh result is older than the prediction horizon

- **GIVEN** a newly received result whose capture age exceeds the prediction
  horizon but remains inside the configured staleness window
- **WHEN** the controller accepts it
- **THEN** it tracks a safely horizon-clamped target and does not enter loss
  solely because extrapolation has stopped

### REQ-078: Calibrated gaze accounts for camera motion

Head motion between image capture and calibration MUST neither shift a
stationary target's world-gaze anchor nor be counted more than once.

#### Scenario: The head moves while inference is running

- **GIVEN** a stationary face and a head that turns between image capture and
  result receipt
- **WHEN** the result is calibrated
- **THEN** ego motion is removed once and the resulting world target does not
  double-count the intervening head movement

### REQ-079: Centering has continuous hysteresis

Crossing configured tracking activation and release regions MUST suppress
bounded image noise without causing command chatter or a command discontinuity.

#### Scenario: Detection noise crosses the stop boundary

- **GIVEN** a settled face whose reported center varies around the configured
  stop region
- **WHEN** successive samples cross that region's edge
- **THEN** the commanded motion approaches zero continuously and does not chatter

### REQ-080: Step tracking settles without whiplash

For a stationary target that makes a 35-degree horizontal yaw step inside the
supported operating envelope, predictive gaze MUST enter a normalized image
error of 0.025 within 3 seconds and limit angular overshoot to 2 degrees.

#### Scenario: A person moves once and then remains still

- **GIVEN** a settled target at one position
- **WHEN** the target moves once by 35 degrees of horizontal yaw and then stops
- **THEN** the head approaches the new fixation directly, remains inside the
  error and overshoot bounds, and does not rubber-band across it

### REQ-081: Moving-target lag is bounded

For a target moving at 5 degrees per second inside the supported operating
envelope, predictive gaze MUST after at most 1.5 seconds of acquisition limit
mean lag over the next 4 seconds to 1.5 degrees, maximum lag to 2 degrees and lag
0.5 seconds after motion stops to 1.5 degrees on both horizontal and vertical
axes.

#### Scenario: A person walks steadily around the robot

- **GIVEN** a continuously visible face moving at the supported reference speed
- **WHEN** the estimator acquires its motion
- **THEN** after no more than 1.5 seconds of acquisition, each axis stays within
  the mean and maximum lag envelope over the next 4 seconds and within the final
  lag envelope 0.5 seconds after motion stops

### REQ-082: Static noise does not move the robot

For bounded static detection noise inside the configured non-activation
envelope, predictive gaze MUST issue no tracking motion after settling.

#### Scenario: A stationary face jitters by detector noise

- **GIVEN** deterministic center noise inside the supported noise envelope
- **WHEN** the controller remains settled for the acceptance interval
- **THEN** neither head nor body leaves the deadband because of that noise

### REQ-083: Motion derivatives remain bounded

Every commanded head and body trajectory MUST remain within its configured
position, velocity, acceleration and jerk envelopes across acquisition,
tracking, reversal, workspace limiting, loss, return and recovery.

#### Scenario: A target reverses direction

- **GIVEN** a moving target and a controller with nonzero velocity and
  acceleration
- **WHEN** the target reverses
- **THEN** commanded velocity changes direction through bounded acceleration and
  jerk without a positional jump

### REQ-084: Tracking uses canonical head poses

Tracking commands MUST use zero head roll and canonical head translation so
expressive pose offsets cannot distort fixation or consume the parallel head's
tracking workspace.

#### Scenario: A conversation expression was active before tracking

- **GIVEN** an expressive head pose with roll or translation
- **WHEN** predictive gaze acquires a face
- **THEN** the tracking target is canonical and the expression does not leak into
  gaze geometry

### REQ-085: Head and body preserve one world gaze

When body motion is enabled, every coordinated command MUST preserve the
identity that world-gaze yaw equals body yaw plus head-on-body yaw.

#### Scenario: The body catches up to a lateral target

- **GIVEN** a fixed world-space face target and a body trajectory in progress
- **WHEN** body yaw changes on each controller tick
- **THEN** the head counter-rotates by the corresponding amount and fixation stays
  on the same world target

### REQ-086: Body allocation is continuous

When body motion is enabled, the controller MUST allocate a continuous,
odd-symmetric and monotonic body share that ignores the configured noise floor,
uses a modest body contribution for small lateral gaze shifts and leaves the
head inside its configured comfort region for large shifts.

#### Scenario: A person moves slightly to one side

- **GIVEN** a lateral gaze target just outside the body noise floor
- **WHEN** tracking begins
- **THEN** head and body begin on the same controller tick, the head remains the
  dominant contributor, and the allocation has no threshold jump

#### Scenario: A person moves far around the robot

- **GIVEN** a large lateral gaze target inside the workspace
- **WHEN** tracking continues
- **THEN** the body carries most of the rotation while the head remains near its
  comfort angle and preserves fixation

### REQ-087: Measured body feedback cannot corrupt trajectory limits

When body motion is enabled, ordinary measured-body lag MUST NOT reset the
commanded trajectory or cause its derivative envelopes to be exceeded, while
missing or persistently divergent feedback enters bounded safe hold.

#### Scenario: The body actuator lags its target

- **GIVEN** ordinary actuator lag inside the tracking-error allowance
- **WHEN** measured yaw trails commanded yaw
- **THEN** the commanded trajectory remains inside its derivative envelopes and
  world fixation is preserved despite the lag

#### Scenario: Body feedback remains far from the command

- **GIVEN** command-to-measurement divergence beyond the configured threshold
- **WHEN** it persists for the configured fault interval
- **THEN** no increasingly divergent target is emitted, diagnostics report the
  body-feedback fault, the last safe command is retained, and later recovery
  remains inside the derivative envelopes

### REQ-088: Loss returns all controlled axes smoothly

After the configured loss hold, the controller MUST return every controlled head
and body axis toward neutral through the same derivative-bounded trajectory and
release gaze ownership only after position, velocity and acceleration settle.

#### Scenario: The selected face disappears

- **GIVEN** a robot tracking with nonzero head and body yaw
- **WHEN** fresh empty or stale input persists beyond the hold interval
- **THEN** the controller returns without a positional or derivative snap and
  hands the head back to the current pipeline or idle expression exactly once

### REQ-089: Unsafe targets never reach hardware

A non-finite, malformed, non-canonical or out-of-workspace atomic target MUST
emit no unsafe hardware command, retain derivative-bounded motion from the last
safe command, report safe hold and require consecutive valid samples before
recovery.

#### Scenario: Pose validation fails transiently

- **GIVEN** one invalid calibrated or measured pose during tracking
- **WHEN** the controller validates the next command
- **THEN** no unsafe command is emitted, diagnostics report safe hold, and
  recovery occurs only after the configured valid-sample gate

### REQ-090: Tracking ownership is explicit

Predictive gaze MUST exclusively own the head during acquisition, tracking,
loss hold, neutral return and safety clamp while leaving antenna expression
independent and yielding the head once on return to idle.

#### Scenario: Tracking ends during processing

- **GIVEN** the voice pipeline is processing while gaze owns the head
- **WHEN** gaze completes its neutral return
- **THEN** the processing head expression is emitted once after ownership passes,
  while its antenna expression was never suppressed

### REQ-091: Controller diagnostics are bounded and private

The operator diagnostics surface MUST report bounded scalar controller state,
observation ages, estimator transitions, limits, commands and fault categories
without retaining images, face crops, credentials or installation identifiers.

#### Scenario: An operator captures a failed trajectory

- **GIVEN** a tracking or safety event
- **WHEN** the operator reads and resets the controller trace
- **THEN** the bounded event history explains the state transition and reset
  clears only diagnostics without moving the robot or changing controller state

### REQ-092: Deterministic simulation gates the controller

Controller acceptance MUST be demonstrated without hardware by deterministic
simulation covering step targets, sustained horizontal and vertical motion,
reversal, bounded noise, observation delay and loss, cadence stalls, workspace
rejection, coordinated body catch-up, feedback divergence and neutral return.

#### Scenario: The motion implementation changes

- **GIVEN** the deterministic plant, latency and fault fixtures
- **WHEN** the controller suite runs
- **THEN** every accuracy, overshoot, derivative, fixation, safety and lifecycle
  acceptance envelope remains enforced before optional hardware validation

## Design

### Contract boundaries

The controller consumes the face center and confidence defined by
[perception](../perception/), the frame sequence and capture timestamp supplied
by [robot-link](../robot-link/), and adapter-local source generation and receipt
time. It does not change the face wire payload. Target persistence between
different people is therefore derived from center and confidence history rather
than a persistent person identifier.

The [HA Satellite](../ha-satellite/) owns application lifecycle, perception
source selection, stale and loss signalling, the required neutral outcome, web
configuration, voice-pipeline and idle expression, and graceful shutdown. This
specification owns active-gaze arbitration, the bounded hold, return and handoff,
controller fault behavior, and coordinated motion output.

### Observation and estimator state

An observation carries source generation, sequence, capture time, receipt time,
normalized target and confidence. Source, generation, selected-target,
timestamp-order and supported-gap discontinuities start a new estimate. Repeated
reads of one identity advance only the trajectory.

The validated design uses a two-axis position and velocity estimate with bounded
capture-time prediction. Raw measured image error alone controls deadband
activation; prediction cannot activate motion from centered noise. Independently
gated horizontal and vertical target-velocity estimates preserve response to
pure movement on either axis.

### Calibration and pose history

The camera calibration boundary converts an actionable image target to an
absolute world-gaze anchor once per new observation. A bounded measured-pose
history reconstructs the head rotation at capture and query times so robot ego
motion is removed exactly once. Measured rotation is projected to the nearest
proper rotation only inside a conservative residual envelope; command poses use
stricter validation.

### Trajectory state

World yaw, world elevation and optional body yaw each carry commanded position,
velocity and acceleration. Online steps apply explicit velocity, acceleration,
jerk and workspace limits. Commanded body state is initialized from hardware
feedback and then remains distinct from the measured-body observer, preventing
actuator lag from erasing trajectory history or violating derivatives.

The loss and safety paths brake and return through the same trajectory state.
Controller idle is a settled near-neutral state rather than an instantaneous
replacement of hidden derivatives with zero.

### Continuous body allocation

Body output is independently configurable and restart-bound. The validated
allocation evidence uses these calibration knots:

| Absolute world yaw | Body share | Body goal |
|---:|---:|---:|
| 2° or less | 0% | 0° |
| 10° | 16.8% | 1.68° |
| 15° | 25% | 3.75° |
| 30° | 42.5% | 12.75° |
| 45° | 60% | 27° |
| 70° | 70.7% | 49.5° |

The shares are starting calibration values rather than permanent product
constants. The observable contract is continuity, symmetry, monotonicity,
small-angle head dominance, large-angle body participation and preservation of
one world-gaze target. Body output remains an explicit opt-in until live
calibration confirms its envelope and social character.

### Safety and feedback

The application commands task-space head and body targets through the daemon's
motion boundary; it does not compute or clip individual parallel-head actuator
positions. Atomic validation checks scalar finiteness, canonical roll and
translation, head/body/world-gaze identity and configured workspace. Body
feedback is observed independently and compared with the command trajectory.

Safety clamp retains the last accepted atomic target, brakes hidden derivatives
and exposes a stable fault category. Transient pose and feedback failures recover
through a consecutive-valid-sample gate rather than through an immediate retry.

### Diagnostics

Status distinguishes perception outcome from controller mode. The detailed trace
uses a bounded ring of scalar events: observation identity and age, estimator
reset, prediction and deadband state, commanded and measured trajectory values,
allocation, active limits, emission decision and adapter fault category. Reset is
a diagnostics operation and leaves motion untouched.

### Acceptance evidence

The deterministic plant models independent nonlinear camera projection,
calibration, observation cadence and latency, command delay, head and body lag,
noise, frame loss, workspace rejection and feedback faults. It evaluates
controller behavior at supported motion-loop cadences rather than reproducing
one nominal timing.

The proof-of-concept evidence that informed this specification includes:

| Scenario | Evidence envelope |
|---|---|
| 35° step | Normalized error below 0.025 at 3 s; overshoot below 2° |
| 5°/s horizontal motion | Mean lag below 1.5°; maximum below 2° |
| 5°/s vertical motion | Mean lag below 1.5°; maximum below 2° |
| Reversal | Normalized error below 0.025; overshoot below 3° |
| Static bounded noise | No deadband activation or commanded tracking motion |
| Body catch-up | Exact world-gaze identity and bounded body derivatives |
| Loss return | No neutral crossing beyond 1° and no derivative discontinuity |

These envelopes are the normative acceptance thresholds where requirements above
state them. Other plant constants and intermediate values remain implementation
and calibration evidence.

## Constraints

- The predictive behavior and simulation remain free of hardware, socket,
  filesystem and wall-clock dependencies.
- The robot daemon remains the owner of head inverse kinematics and individual
  actuator commands.
- The application shares the robot's compute and motion resources with audio,
  wake-word and daemon work.
- Observation delay and cadence vary under network and inference load; nominal
  frame rate is not a control-time source.
- Body limits and allocation values require live calibration before body motion
  becomes a default behavior.
- Expressive roll, translation and idle motifs form a separate motion layer and
  do not alter the canonical tracking pose or world-gaze target.

## Open Questions

- **Persistent person identity.** The current face payload has no track identity.
  Current default: retain the selected face through deterministic center and
  confidence association, resetting prediction on an association break.
- **Body default.** Simulation and the current canary support coordinated body
  motion, but the intended shipping default still needs explicit product and
  hardware approval. Current default: restart-bound opt-in.
- **Live body calibration.** The exact allocation shares, comfort angle,
  derivative limits and feedback-fault threshold need a scrubbed calibration
  session. Current default: use the proof-of-concept values only as starting
  hypotheses.
- **Idle motion.** The existing pipeline idle expression may resume after gaze
  reaches controller idle. Current default: idle never blends into an active
  gaze target and does not command body yaw.
- **Controller configuration surface.** The proof of concept exposes core tuning
  fields and leaves safety constants internal. Current default: only values that
  can be changed without resetting controller state are live.

## References

- [HA Satellite](../ha-satellite/) — application lifecycle, stale and loss
  signalling, neutral outcome, and pipeline and idle expression
- [Robot Link](../robot-link/) — observation identity, capture timestamps,
  freshness and normalized geometry
- [Perception](../perception/) — face center and confidence semantics
- [Architecture](../architecture/) — hardware-free testing and quality gates
- [Benchmarks](../benchmarks/) — structured performance evidence and hardware
  opt-in
- Chaumette and Hutchinson, *Visual Servo Control I*: 
  https://doi.org/10.1109/MRA.2006.250573
- ViSP image-based visual-servo tutorial:
  https://visp-doc.inria.fr/doxygen/visp-daily/tutorial-ibvs.html
- Sampled-data visual servoing:
  https://doi.org/10.1109/TCST.2023.3292311
- Eye, head and torso coordination:
  https://doi.org/10.1145/3361218
- Minimum-jerk movement:
  https://doi.org/10.1523/JNEUROSCI.05-07-01688.1985
- Jerk-limited online trajectory generation:
  https://www.roboticsproceedings.org/rss17/p015.html
- Closed-chain singularity analysis:
  https://doi.org/10.1109/70.56660

## Changelog

| Date | Change | Document |
|------|--------|----------|
| 2026-08-24 | Initial predictive gaze and coordinated motion specification | [0019-predictive-gaze-and-coordinated-motion](../../changes/0019-predictive-gaze-and-coordinated-motion.md) |
