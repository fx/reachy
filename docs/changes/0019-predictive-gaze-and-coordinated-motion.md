# 0019: Predictive gaze and coordinated motion

## Summary

Implement the [Predictive Gaze and Coordinated Motion](../specs/gaze-control/)
contract in three reviewable stages: observation and simulation foundations,
coordinated trajectory integration, then safety evidence and rollout. The
validated proof of concept is evidence and a migration reference, not the
implementation contract.

**Spec:** [Predictive Gaze and Coordinated Motion](../specs/gaze-control/)
**Status:** draft
**Depends On:** 0018

## Approval

The operator requested this specification after live experimentation produced a
stable predictive head controller and a promising continuous head–body canary.
They explicitly approved using that proof of concept as a blueprint while
rebuilding the feature in reviewed, traceable increments.

## Motivation

The existing HA satellite implements direct face tracking and a stale-head
return, but its standing specification does not define delayed observation
handling, predictive fixation, trajectory quality, coordinated body motion or
controller safety. Successive live experiments also demonstrated why those
parts cannot be added as independent gains: cached image errors, moving camera
frames and body yaw form one closed control loop.

The proof of concept establishes viable equations, boundaries, tests and
starting values. This change turns that evidence into a maintainable
implementation without treating experimental module layout, helper names or
calibration values as permanent product contracts.

## Requirements

### Testing Requirements

This change MUST satisfy the project's standing testing rules (see
[Testing conventions](../specs/architecture/index.md#testing-conventions)). CI
enforces these as merge gates:

- Tests run with `pytest`, with async strict mode enabled.
- Unit tests perform no input or output: no sockets, filesystem access or
  wall-clock sleeping. Time, observations and hardware behavior are supplied
  through deterministic fakes.
- Coverage MUST be gated on the diff rather than on the whole tree.
- Type checking MUST run in strict mode for new modules.
- A lint or type suppression MUST carry the rule identifier and a justification.
- No test may require a robot or camera. Focused tests MUST cover observation
  timing and identity, stale and outlier input, prediction bounds, trajectory
  limits, handoff, cancellation, fallback and shutdown through deterministic
  clocks, simulation and the existing motion fakes.

Skipping or weakening any of these rules to land the PR MUST be treated as a bug
in the PR, not in the rule.

### Functional requirements

The [gaze-control requirements](../specs/gaze-control/#requirements) own the
observable modes, accuracy envelopes, head–body fixation, safety behavior,
handoff and diagnostic scenarios. Those scenarios are this change's acceptance
criteria and are not restated here. What implementation requires of this change:

- The first implementation PR MUST establish source-qualified observation timing
  and the deterministic controller oracle before replacing robot motion.
- The second implementation PR MUST replace the legacy tracking path without
  retaining an independently active gain, trim or linear-return loop around the
  predictive controller.
- Legacy `gaze_deadzone` and `gaze_smoothing` overrides MUST be accepted only for
  compatibility during migration and MUST NOT influence predictive control or be
  written by the new settings surface.
- Body motion MUST remain restart-bound and explicitly configurable while its
  live calibration evidence remains provisional.
- The final implementation PR MUST add requirement traces, register the new spec
  in duvet with Markdown format, regenerate the committed snapshot and leave all
  repository gates green.

## Design

### Approach

#### Observation and pure controller

Perception adapters retain source generation, sequence, capture time and receipt
time for every completed result, including an empty one. A pure controller
consumes each identity once, estimates normalized image position and velocity,
bounds prediction, applies a smooth hysteretic deadband, and advances immutable
world-yaw, elevation and optional body trajectory state.

A deterministic plant supplies independent nonlinear projection, distortion,
observation latency, frame loss, actuator lag and fault injection. Its
acceptance cases cover step targets, sustained horizontal and vertical motion,
reversal, static noise, late observations, cadence stalls, workspace rejection,
loss return, body catch-up and measured-body divergence.

#### Calibrated motion integration

The motion adapter owns measured pose history, camera calibration queries and
robot command construction. Each new actionable observation is calibrated once,
rebased from query pose to capture pose, and converted into an absolute
world-gaze anchor. The controller advances at the behavior cadence without
recalibrating cached observations.

Tracking samples use canonical roll and translation. The adapter sends a world
head pose and optional body target atomically through the daemon. The body
allocator preserves world gaze while the body trajectory changes, and a
separate measured-body observer detects missing or persistent divergent
feedback without replacing commanded trajectory derivatives.

#### Safety, diagnostics and rollout

The controller validates observations, time, configuration, pose history,
workspace, derivative limits and atomic samples. Rejected work retains the last
safe command while hidden trajectory state brakes. Recovery is gated by
consecutive valid evidence.

The diagnostics surface retains a bounded scalar trace of observation age,
estimator resets, prediction, deadband state, trajectory commands, allocation,
limits and stable fault categories. It carries no image content or installation
identity.

Rollout begins with deterministic simulation and a head-only canary. Coordinated
body motion remains restart-bound and follows only after head behavior and
measured feedback are verified. Live transcripts are omitted; any recorded
outcome is scrubbed prose and aggregate measurement.

### Decisions

- **Use one controller state for prediction, head motion, body allocation, loss
  and return.**
  - **Why:** Independent correction loops caused repeated-error integration,
    frame-of-reference mistakes and head/body conflict during the proof of
    concept.
  - **Alternatives considered:** Separate gaze gains, vertical trim, a second
    body-follow loop and blocking SDK interpolation.
- **Keep controller math pure and hardware-free.**
  - **Why:** Latency, dropouts, trajectory derivatives and safety transitions are
    testable exhaustively only through injected time, observations and a
    deterministic plant.
  - **Alternatives considered:** A controller embedded in the daemon adapter or
    hardware-only tuning.
- **Use the daemon's calibrated gaze and task-space IK boundaries.**
  - **Why:** Camera intrinsics, distortion, extrinsics and the six-actuator head
    remain the daemon's responsibility; the application should neither reproduce
    calibration nor command individual actuators.
  - **Alternatives considered:** FOV-only projection and application-owned
    parallel-head kinematics.
- **Use a dependency-free online jerk-limited servo first.**
  - **Why:** The proof of concept meets the acceptance envelope without adding a
    new native dependency to the shared robot environment.
  - **Alternatives considered:** Ruckig and blocking point-to-point trajectories.
- **Treat body allocation values as calibration hypotheses.**
  - **Why:** Continuity and world-fixation invariants are stable behavior, while
    exact shares, comfort angles and derivative limits need live evaluation.
  - **Alternatives considered:** Permanently specifying the proof-of-concept
    allocation table.
- **Keep the existing idle expression separate from active gaze.**
  - **Why:** Tracking uses canonical pose and exclusive head ownership; idle
    character can resume after controller idle without corrupting fixation.
  - **Alternatives considered:** Blending random idle roll or translation into
    the tracking target.

### Non-Goals

- No change to face-model inference, face payloads, robot-link framing or
  normalized coordinate semantics.
- No persistent biometric identity, recognition or face-image retention.
- No new idle-animation catalog, recorded emotion library or conversation-motion
  redesign.
- No audio, wake-word, Home Assistant protocol or speaker-control change.
- No application-owned Stewart-platform inverse kinematics or direct actuator
  command.
- No new online-trajectory dependency in this change.
- No promise that simulation-derived body calibration values become the shipping
  default without live evidence.

## Tasks

- [ ] Establish observation and predictive-servo foundations
  - [ ] Inventory and preserve the proof-of-concept outside the repository, then
        restore the satellite member to the clean dependency baseline
  - [ ] Reconstruct only spec-backed behavior rather than merging the experimental
        working tree wholesale
  - [ ] Preserve source generation, sequence, capture time and receipt time in
        `apps/ha-satellite/src/reachy_mini_ha_satellite/ports.py`,
        `adapters/groundstation.py` and `adapters/perception_local.py`
  - [ ] Add the pure estimator, deadband, trajectory and allocation state in a
        behavior-layer controller module
  - [ ] Add deterministic nonlinear camera, latency, dropout, actuator and fault
        fixtures under `apps/ha-satellite/tests/`
  - [ ] Gate the step, sustained-motion, reversal, noise, late-observation,
        cadence-stall, loss and workspace scenarios from the spec
  - [ ] Keep the released runtime path available until the foundation passes
        focused and HA-satellite member verification

- [ ] Integrate coordinated trajectory generation and head–body allocation
  - [ ] Replace legacy gaze intents with source-qualified observation and loss
        directives while preserving pure behavior and head arbitration
  - [ ] Add measured-pose history, calibrated capture/query rebasing and
        canonical task-space commands in `adapters/motion_reachy.py`
  - [ ] Advance jerk-limited world-yaw and elevation trajectories on every
        behavior tick without recalibrating cached observations
  - [ ] Add continuous, odd-symmetric and monotonic head–body allocation with
        atomic world-gaze-preserving commands
  - [ ] Keep commanded body trajectory separate from its measured observer and
        add bounded feedback fault and recovery behavior
  - [ ] Return every controlled axis through the same trajectory and force one
        correct pipeline-head handoff after controller idle
  - [ ] Replace obsolete settings behavior while preserving safe migration of
        stale local overrides
  - [ ] Add deterministic cancellation and shutdown coverage proving command
        cessation, terminal release and cleanup ordering

- [ ] Complete safety, diagnostics, traceability and staged rollout
  - [ ] Add strict configuration, pose, workspace, derivative and atomic-sample
        validation with last-safe-command hold and gated recovery
  - [ ] Add bounded identifier-free status and trace endpoints, including a
        motion-free diagnostics reset
  - [ ] Run focused controller tests, the full HA-satellite suite, diff coverage,
        repository checks and requirements traceability
  - [ ] Add exact annotations for REQ-074 through REQ-092, register
        `docs/specs/gaze-control/index.md` with `format = "markdown"`, and
        regenerate the duvet snapshot from the repository root
  - [ ] Update duvet registration narrative and counts to nine specs and 92
        requirements, and update the traceability claims in `AGENTS.md` and
        `REVIEW.md`
  - [ ] Define head-only and body-enabled abort thresholds before deployment,
        retain the last released artifact and resolved configuration as the
        rollback target, and prohibit body enablement until head-only evidence
        passes
  - [ ] Run the rollback-ready head-only canary against the deterministic
        envelopes; on threshold breach restore the retained artifact and verify
        application, perception, Home Assistant and groundstation health
  - [ ] If head evidence is green, run the separately gated coordinated-body
        canary with the same abort, rollback and post-rollback checks
  - [ ] Record only scrubbed aggregate outcomes; defer any unclosed calibration or
        shipping-default decision to a later proposal
  - [ ] Mark 0019 complete and synchronize `docs/index.yml` and `docs/index.md`
        when every task and traceability item is complete

## Open Questions

- **Body default:** Should coordinated body motion ship enabled after canary
  calibration, or remain restart-bound opt-in? Deferred to the canary outcome or
  a later proposal if 0019 evidence is insufficient.
- **Body calibration:** Which allocation shares, head comfort angle, derivative
  limits and feedback-divergence threshold become supported defaults? Deferred
  when the calibration cannot finish inside the rollout task.
- **Target association:** Is deterministic center-and-confidence association
  sufficient for multiple faces, or does a later wire-compatible tracking
  identity need its own proposal?
- **Diagnostics surface:** Should the detailed controller trace remain
  operator-facing after calibration, or reduce to counters and current status?
- **Idle ownership:** At what point may the existing idle head expression resume
  after predictive return, and should idle ever use body yaw?

## References

- Spec: [Predictive Gaze and Coordinated Motion](../specs/gaze-control/)
- Parent behavior: [HA Satellite](../specs/ha-satellite/)
- Dependency: [0018-satellite-runtime-stability](./0018-satellite-runtime-stability.md)
- Visual servoing: https://doi.org/10.1109/MRA.2006.250573
- Sampled-data visual servoing: https://doi.org/10.1109/TCST.2023.3292311
- Head and torso coordination: https://doi.org/10.1145/3361218
- Minimum-jerk motion: https://doi.org/10.1523/JNEUROSCI.05-07-01688.1985
- Jerk-limited trajectory generation: https://www.roboticsproceedings.org/rss17/p015.html
