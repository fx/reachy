# 0020: Home Assistant configuration and camera feed

## Summary

Implement the proposed
[Home Assistant Configuration and Camera Feed](../specs/home-assistant-configuration-and-camera-feed/)
contract in three reviewable pull requests: safe live motor groups, live
replacement of the groundstation URL, then the bounded MJPEG feed, operator
documentation and final traceability.

**Spec:** [Home Assistant Configuration and Camera Feed](../specs/home-assistant-configuration-and-camera-feed/)
**Status:** draft
**Depends On:** 0015, 0017, 0018, 0019

## Approval

The operator requested:

> add more options to the home assistant entity configuration:
>
> 1. individual motor toggle (head, body, antennae)
> 2. ground station URL so it can be easily configured without redeploying
> 3. the mjpeg stream from the ground station as feed so it can be viewed right in home assistant

The operator approved the product, safety, lifecycle and privacy decisions now
owned by
[REQ-093–098](../specs/home-assistant-configuration-and-camera-feed/index.md#requirements),
including the standard Home Assistant MJPEG integration and the proposal's stated
exclusions. This document records their implementation sequencing and rationale;
it does not repeat their observable contract. Production behavior starts in the
three implementation tasks below.

## Motivation

Home Assistant already exposes microphone settings and the speaker controls from
[0017](./0017-speaker-controls-in-home-assistant.md), but the physical motor
groups and the remote-session address are absent. Changing either currently
requires another surface or a restart, and a requested state has to remain
separate from state the SDK or session lifecycle actually adopted.

Three current boundaries cannot support a truthful entity without prerequisite
work. Selective SDK torque calls are fire-and-forget and have no grouped physical
read-back, the settings model accepts 512-character groundstation URLs while a
Home Assistant text state carries at most 255, and the general image decoder also
accepts non-JPEG formats. The staged implementation closes those gaps before it
exposes the corresponding control or `image/jpeg` response.

The robot's authenticated outbound session already carries original JPEG camera
frames. The groundstation decodes them for capabilities and then discards the
compressed payload. A latest-only observer can make the same live input visible
to Home Assistant without changing the robot link, opening an inbound robot
listener or spending robot compute on another capture or encode. It also creates
a new privacy and lifecycle surface, so eligibility, retention, viewer bounds and
cancellation are part of the proposal rather than implementation afterthoughts.

## Requirements

### Testing Requirements

This change MUST satisfy the project's standing testing rules (see
[Testing conventions](../specs/architecture/index.md#testing-conventions)). CI
enforces these as merge gates:

- Tests MUST run with `pytest`, with async strict mode enabled.
- Unit tests MUST perform no input or output: no sockets, filesystem access or
  wall-clock sleeping. Time, hardware, Home Assistant and frame arrival MUST be
  supplied through deterministic fakes.
- Integration tests that exercise HTTP, WebSocket or MJPEG behavior MUST use real
  in-process transports and carry the repository's socket marker.
- No test may require a robot, camera, microphone or Home Assistant instance.
- Coverage MUST be gated on the diff, and new modules MUST pass strict type
  checking.
- Every lint or type suppression MUST carry its rule identifier and a reason.

Skipping or weakening any of these rules to land a pull request MUST be treated
as a bug in that pull request, not in the rule.

Each implementation task runs its focused suites plus `just check`,
`just contracts-check`, `just leak-scan` and `just secret-scan`. The final task
also runs `just duvet` after registration and snapshot regeneration.

### Functional requirements

The
[Home Assistant Configuration and Camera Feed requirements](../specs/home-assistant-configuration-and-camera-feed/index.md#requirements)
own stable effective-state entities, exact motor grouping and transition safety,
session replacement, latest-only MJPEG behavior, feed eligibility, privacy and
viewer lifecycle. Their scenarios are this change's acceptance criteria and are
not restated here. What implementing them requires of this change:

- The first implementation pull request MUST establish or consume the daemon/SDK
  grouped-torque acknowledgement and physical read-back prerequisite, every
  producer's command gate and measured-state reseeding before registering the
  three switch entities.
- The second implementation pull request MUST align every URL surface to one
  255-character validation contract, add the legacy-overlong refusal, and route
  the settings page and entity through one stable replacement owner for source
  construction, serialization, compensation and bounded reconstruction before
  making the URL writable at run time.
- The third implementation pull request MUST add an explicit JPEG-format gate
  before the feed observer and MUST complete operator documentation, requirement
  traces, Markdown registration, snapshot regeneration and final status/count
  synchronization.
- Every task MUST preserve the stable entities delivered by 0017, the controlled
  startup and cancellation boundaries delivered by 0018, and the trajectory,
  ownership and safety invariants delivered by 0019.
- The final task MUST extend the runbook and deployment/image verification
  surfaces delivered by 0015 and its dependency closure rather than creating
  parallel instructions or artifact checks.
- Runbooks MUST contain output actually observed. Any step needing a real robot or
  Home Assistant remains marked pending hardware verification until it is run;
  invented transcripts are not acceptance evidence.

## Design

### Approach

#### Safe motor groups

Add an application-owned entity module beside `audio_entities.py`; no vendored
ESPHome file changes. Before that module registers switches, extend or consume an
upstream daemon/SDK capability that correlates each selective torque request with
its completion and reads physical torque state for every requested motor. The
current `enable_motors` and `disable_motors` methods return `None` after
`send_command`; the daemon's unobserved `status: ok` response and aggregate motor
mode do not satisfy this prerequisite. `RobotHandle` and fakes expose the richer
result without importing the SDK beyond `daemon_app.py`.

A motor coordinator serializes writes and maps the three groups to the SDK
identifiers fixed by REQ-094. Its per-group record contains an optional
`last_confirmed` Boolean plus confirmation evidence, not a tri-state switch value.
Initial read-back gates whether the corresponding stable object ID is inserted in
`ServerState.entities`; later entity messages use the existing Boolean
`SwitchStateResponse` unchanged.

The coordinator is injected into entity setters and every motion-adapter entry
point rather than copied into behavior. Per-group critical sections cover gaze,
pipeline expressions, antenna expressions and all application commands. A setter
closes the gate, performs the acknowledged transition and returns the physical
Boolean read-back. Contradiction updates the entity from that read-back. Missing
confirmation leaves `last_confirmed` untouched, repeats it only as a retained
command read-back if a response is needed, and appends a bounded identifier-free
diagnostic event. A separate successful read-back may update it later.

The body path additionally captures and disables daemon automatic yaw before the
body gate closes, including when `face_tracking_enabled` is false or
`body_motion_enabled` left the application without gaze ownership. After confirmed
re-enable it reads current body state, resets hidden trajectory state and only
then restores the captured automatic-yaw/ownership policy. Failure, cancellation
and shutdown use the same terminal gate and never infer effective torque from a
sent request.

#### Live groundstation URL

Define one 255-character groundstation URL bound in `config.py`; use it in the
settings field, text-entity metadata and both submission paths. The existing
512-character model value narrows without truncation or automatic rewrite. Startup
validation distinguishes environment from persisted override when it reports a
legacy overlong value and gives the corresponding replace/remove remediation.

Refactor the remote branch into one serialized replacement owner around
`RemotePerception`, `SessionClient`, `OverrideStore` and effective read-back. The
settings page and text entity both submit their fully resolved candidate through
this owner instead of calling the current persist-before-`apply_live` sequence.
The owner preserves the preceding resolution and source, prepares the candidate,
retires the old generation, starts the candidate, and only then uses the store's
atomic file replacement as the commit point. It publishes the candidate after
that commit.

The replacement owner is stable even when no `RemotePerception` exists. It retains
the source factory, preceding resolution, optional current source, generation and
optional reconstruction task. Every earlier failure leaves the old durable file
untouched and restores or keeps at most one old-generation source. A commit failure
closes the candidate and attempts to rebuild the old source from the retained
factory and resolution.

Construction failure belongs to that owner because the connectivity supervisor
cannot supervise an object that does not exist. The owner starts one bounded,
capped and cancellable reconstruction retry state; it closes any partial source
before the next attempt and keeps every failed generation ineligible. Once
construction succeeds, it installs exactly one source and hands that object to the
existing connectivity supervisor for ordinary connect/reconnect behavior. Retry
exhaustion records the terminal reconstruction outcome whose effective URL,
health and fallback behavior are owned by REQ-095.

A later operator write acquires the same serialization boundary, cancels and
awaits reconstruction, advances generation and only then prepares its candidate.
Shutdown cancels and awaits reconstruction before source cleanup. Generation
checks reject a factory result racing with cancellation, so neither supersession
nor shutdown can install a late client. This is a compensating state machine, not
an atomic transaction across filesystem and network. The local-only composition
uses the same owner and durable path without manufacturing a remote instance.

#### Bounded MJPEG feed

Inject a groundstation feed registry from `service.py` into the session, pipeline
and HTTP surfaces without importing capabilities across their enforced boundary.
Its state contains session-cardinality metadata, one global optional JPEG payload,
a monotonic frame revision and the fixed viewer semaphore — never a mapping from
session keys to JPEG values.

Authentication and finalization callbacks update cardinality and invalidate the
global payload. A dedicated signature/format validator rejects non-JPEG payloads
before the existing general decoder result can authorize publication; successful
decode remains independently required. Only their conjunction may replace the
global original bytes for the sole active session. The `/stream.mjpg` response
reads successive revisions under a response-scoped viewer reservation. These are
implementation seams for REQ-096–098; their scenarios own the observable
outcomes.

Update `docs/setup/groundstation.md`, `docs/setup/home-assistant.md` and the
relevant operations guidance with the trusted-network warning, standard MJPEG
integration setup, effective entity semantics and live replacement behavior.
Only commands and output actually observed are transcribed.

### Decisions

- **Decision:** Keep custom configuration entities outside the vendored ESPHome
  directory and give their object identifiers explicit compatibility ownership.
  - **Why:** The existing speaker controls established this boundary, and editing
    derived upstream files would create provenance churn for application behavior
    upstream does not own.
  - **Alternatives considered:** Adding entities to the vendored entity module or
    building a custom Home Assistant component.
- **Decision:** Gate switch registration on correlated daemon acknowledgement and
  per-motor physical torque read-back, then retain one last-confirmed Boolean per
  registered group.
  - **Why:** The current SDK returns `None` after a fire-and-forget send, while an
    unobserved daemon response and aggregate motor mode prove no physical group
    state. The existing switch wire carries only a Boolean, so confirmation
    freshness belongs in diagnostics rather than a fabricated third state.
  - **Alternatives considered:** Optimistic switch state, treating no exception as
    success, polling aggregate mode, extending the switch protocol, or adding a
    custom Home Assistant component.
- **Decision:** Treat each physical motor group and all its command producers as
  one live boundary, with the three groups confirmed after controlled startup.
  - **Why:** A switch that only changes torque can leave application or daemon
    commands targeting disabled hardware, while a switch that only suppresses
    commands does not turn the motor off. Body automatic yaw is a producer even
    when gaze never acquired ownership.
  - **Alternatives considered:** Independent Stewart-actuator and left/right
    antenna controls, folding body torque into `body_motion_enabled`, or gating
    only application-authored targets.
- **Decision:** Cap every groundstation URL surface at 255 characters and reject
  legacy overlong values without truncation.
  - **Why:** Home Assistant cannot represent a longer text state, so retaining the
    512-character settings limit would make truthful effective read-back
    impossible.
  - **Alternatives considered:** Truncation, a shorter entity-only limit, hashing,
    or omitting the entity when the current value is too long.
- **Decision:** Give one stable replacement owner the source factory, serialized
  transition, atomic durable commit, compensation and bounded reconstruction
  retry state.
  - **Why:** Persisting first can make restart select a candidate that runtime
    adoption rejected, while a connectivity supervisor cannot reconstruct an
    object that failed before it existed. One owner prevents construction,
    cancellation and generation eligibility from racing.
  - **Alternatives considered:** Persist-before-adopt, delegating construction to
    the existing connectivity supervisor, unbounded retry, an asserted atomic
    filesystem/network transaction, in-place URL mutation, or overlapping clients.
- **Decision:** Reuse one global latest-only original payload after explicit JPEG
  signature validation and successful decode.
  - **Why:** The robot already spent the capture and compression cost, and the
    general decoder accepting a payload does not prove that it is JPEG. Another
    connection, decode, encode or queue adds load without improving the feed.
  - **Alternatives considered:** Trusting decoder success, pulling the daemon
    camera separately, streaming decoded arrays, transcoding, or buffering per
    viewer.
- **Decision:** Fail ambiguity instead of selecting among robot sessions.
  - **Why:** Connection order, frame recency and opaque session identifiers are
    not operator intent. A deterministic refusal is safer than showing an
    arbitrary room.
  - **Alternatives considered:** First connected, newest frame, or a query
    parameter naming a session.
- **Decision:** Keep video HTTP-unauthenticated and rely on trusted-network
  isolation, with a fixed four-viewer bound and no frame history.
  - **Why:** This matches the accepted deployment model and avoids inventing a
    credential distribution and recovery scheme in an otherwise focused change.
    The explicit bound and no-store lifecycle constrain accidental load and data
    exposure within that boundary.
  - **Alternatives considered:** Basic authentication, reuse of the robot-link
    credential, unbounded viewers, and a recording or still-image service.
- **Decision:** Configure Home Assistant through its standard MJPEG IP Camera
  integration.
  - **Why:** Home Assistant already owns this protocol and user interface. The
    satellite's ESPHome connection is not the owner of the groundstation's HTTP
    video surface.
  - **Alternatives considered:** A custom integration, a native camera entity on
    the satellite device, or automatic mutation of Home Assistant's integration
    registry.

### Non-Goals

- No gaze-control calibration, body allocation, derivative, trajectory, workspace
  or safety-limit change.
- No change to the restart-bound false default for `body_motion_enabled`.
- No individual Stewart-actuator or left/right antenna control.
- No ESPHome switch-wire extension, third motor-switch state or custom Home
  Assistant motor component; switches keep the existing Boolean state shape.
- No robot-link wire-format change, second robot client or inbound robot listener.
- No video authentication in this change.
- No frame recording, retention beyond the sole global live JPEG, disk write,
  content log, stream cache or transcode.
- No custom Home Assistant component, native satellite camera entity,
  integration-registry mutation or still-image endpoint.
- No arbitrary stream selection when multiple eligible robot sessions exist.

## Prerequisites and Risks

- **Blocking daemon/SDK prerequisite:** the current public SDK sends selective
  torque commands fire-and-forget, returns `None`, and exposes no correlated
  per-group physical torque read-back. The first implementation task cannot expose
  a switch until a compatible daemon/SDK release provides that capability. If it
  is unavailable when implementation starts, contribute it upstream or hold the
  task; do not substitute optimistic state.
- **Physical read-back breadth:** the prerequisite covers all nine named motors,
  including body and antennas, rather than inferring a group's state from the
  aggregate head motor mode. Hardware-free fakes establish semantics, while the
  staged live gate confirms the deployed daemon reports each physical group.
- **Deliberate compatibility break:** an existing URL longer than 255 characters
  now stops startup with remediation instead of becoming a value Home Assistant
  cannot represent. Migration tests cover environment and persisted sources before
  the runtime entity is added.

## Tasks

- [x] Add safe live motor-group Configuration switches (PR #31)
  - [ ] **Not satisfied as written, and deliberately not ticked.** Before entity
        registration, verify a daemon/SDK release provides correlated
        selective-torque acknowledgement and physical read-back for every motor;
        contribute that capability upstream or hold this task if it does not. **No
        released `reachy-mini` provides it.** The capability was implemented and
        reviewed, and it lives on the branch
        `feat/correlated-motor-torque-readback` of the fork at
        https://github.com/fx/reachy_mini, which the robot runs. That branch is
        neither released nor merged and **no upstream pull request is open yet**,
        so the "contribute that capability upstream" half is outstanding. The
        operator's decision was to ship against that build and record the
        dependency rather than hold the task. Nothing was substituted for
        optimistic state — a robot without that build gets no switch at all
  - [x] Extend `RobotHandle` and deterministic fakes with acknowledged grouped
        enable, disable and read-back results without importing the SDK outside its
        existing entry point (PR #31)
  - [x] Add one serialized coordinator for the exact head, body and antenna
        mappings with an optional initial value, one last-confirmed Boolean and
        bounded identifier-free confirmation diagnostics per group (PR #31)
  - [x] Gate gaze, pipeline-head and antenna expression commands and every other
        application producer before torque-off (PR #31)
  - [x] Inventory daemon-owned move, tracking, expression and automatic-yaw
        producers; prove each inactive or acquire and quiesce it before torque-off
        (PR #31)
  - [x] Acquire exclusive body ownership and quiesce daemon automatic yaw for all
        `face_tracking_enabled` and `body_motion_enabled` combinations, then seed
        measured state before restoring the prior policy after confirmed enable
        (PR #31)
  - [x] Insert each stable switch object ID only after an agreeing initial physical
        read-back, using the existing Boolean switch messages without a protocol
        extension or custom Home Assistant component (PR #31)
  - [x] Publish only successful physical read-back Booleans and actual state on
        contradiction; on failure reject the requested value, keep confirmation
        evidence unchanged and repeat only the retained Boolean when the protocol
        requires a response, then allow a later successful read-back to advance it
        (PR #31)
  - [x] Cover absent initial confirmation, missing, late and contradictory later
        acknowledgement, partial group read-back, retained-state diagnostics,
        subsequent successful read-back, in-flight application and daemon
        producers, automatic-yaw ownership, reseeding, hidden targets, failure,
        cancellation, safe hold, release and shutdown (PR #31)
  - [x] Run the focused satellite suites and repository checks required above
        (PR #31)

- [ ] Add persisted live groundstation URL replacement
  - [ ] Define one 255-character URL bound shared by the settings model, settings
        page, text entity metadata and both submission paths
  - [ ] Refuse legacy 256–512-character environment and persisted values at startup
        without truncation, naming their source and actionable replace/remove path
  - [ ] Add the stable Configuration text entity using the shared session-URL
        validator and settings store
  - [ ] Route both the settings page and entity through one stable serialized
        replacement owner retaining the source factory, preceding resolution,
        optional source, generation and optional reconstruction task
  - [ ] Prepare and start the candidate before atomic durable commit, publish only
        after commit, and compensate every failure to one preceding effective URL
        and at most one eligible source
  - [ ] Own bounded, capped and cancellable reconstruction retries above the
        connectivity supervisor; close partial sources and hand off exactly one
        successfully constructed source for ordinary reconnect behavior
  - [ ] Serialize a later operator write with cancellation and awaiting of prior
        reconstruction, and cancel/await the same state during shutdown so no late
        factory result installs a client
  - [ ] Preserve single-client reconnect, staleness and local-fallback behavior
        across successful, refused, compensated, exhausted and cancelled recovery
  - [ ] Cover boundary lengths 255/256/512, both legacy sources, no truncation,
        preparation/close/start/commit failures, repeated reconstruction failures
        followed by success, retry exhaustion, partial-source cleanup, operator
        supersession, shutdown cancellation, late factory results, rapid writes,
        restart/runtime agreement and both UI read-backs without sockets or a Home
        Assistant instance
  - [ ] Update configuration and operator documentation for the new limit,
        migration refusal, transaction ordering and remediation
  - [ ] Run the focused satellite suites and repository checks required above

- [ ] Add the bounded MJPEG feed, documentation and final traceability
  - [ ] Add one global optional latest-original-JPEG value beside session
        cardinality metadata, with no per-session JPEG mapping
  - [ ] Wire authentication and finalization to clear the global value on zero or
        multiple sessions and require a post-ambiguity validated frame
  - [ ] Add explicit JPEG signature/format validation beside successful general
        image decode, publishing original bytes only when both checks pass
  - [ ] Serve `/stream.mjpg` with standard multipart JPEG framing, no-store
        responses and four bounded viewer slots, without another robot connection,
        stream-only decode/re-encode, capability blockage or per-viewer frame queue
  - [ ] Cover zero, one and multiple sessions, post-ambiguity freshness, malformed
        JPEG, decodable PNG and other non-JPEG payloads, slow viewers, replacement,
        disconnect, cancellation, capacity and application shutdown using unit
        fakes and marked in-process transport tests
  - [ ] Prove logs, metrics, traces, errors and deployment storage contain no frame
        body, credential or installation identifier
  - [ ] Extend the existing container image verification with one authenticated
        fixture session and actual-JPEG multipart read while outbound network stays
        unavailable
  - [ ] Update setup and operations runbooks for the four entities, standard Home
        Assistant MJPEG integration and trusted-network video boundary
  - [ ] Run staged live verification only after hardware-free acceptance, record
        scrubbed outcomes, and leave any unrun step marked pending rather than
        inventing output
  - [ ] Add exact annotations for REQ-093 through REQ-098, register the spec with
        `format = "markdown"`, regenerate the duvet snapshot from the repository
        root and run every repository gate
  - [ ] Update exhaustive spec and requirement counts, mark 0020 complete and
        synchronize `docs/index.yml` and `docs/index.md`

## Verification Stages

1. **Deterministic motor acceptance:** drive every REQ-093 and REQ-094 scenario
   through absent and successful initial confirmation, Boolean registration,
   agreeing, contradictory, missing and later-successful read-back, last-confirmed
   diagnostics, all four face/body-setting combinations, automatic-yaw ownership,
   transition failures, controller faults and terminal lifecycle boundaries.
2. **Deterministic source acceptance:** drive every REQ-095 scenario at the
   255/256/512 boundaries and through both legacy sources, candidate preparation,
   source close/start, atomic file commit, repeated reconstruction failure then
   success, exhaustion, partial cleanup, operator supersession, shutdown
   cancellation and late factory completion, asserting runtime/restart/read-back
   agreement, local fallback and zero client overlap after each outcome.
3. **Deterministic feed acceptance:** drive every REQ-096–098 scenario with actual
   JPEG, malformed JPEG and decodable non-JPEG fixtures through fake session
   cardinality and frame revisions, then verify protocol framing and cancellation
   over a marked in-process HTTP transport and the existing image gate.
4. **Staged live verification:** first prove the deployed daemon's grouped physical
   acknowledgement and verify the four entities without motion, proceed one motor
   group at a time with an abort path, replace the groundstation, then configure
   the standard MJPEG integration. Restore the released artifact and configuration
   on any predeclared threshold breach.
5. **Evidence:** apply REQ-098 and the repository's runbook scrubbing convention
   to every recorded outcome; leave unrun hardware steps marked pending.

## Open Questions

None for implementation approval. Any request to authenticate video, choose among
multiple robot sessions, retain frames, split physical motor groups further or
change gaze/body calibration starts a separate proposal.

## References

- Spec: [Home Assistant Configuration and Camera Feed](../specs/home-assistant-configuration-and-camera-feed/)
- Dependencies:
  [0015-docs-and-runbooks](./0015-docs-and-runbooks.md),
  [0017-speaker-controls-in-home-assistant](./0017-speaker-controls-in-home-assistant.md),
  [0018-satellite-runtime-stability](./0018-satellite-runtime-stability.md),
  [0019-predictive-gaze-and-coordinated-motion](./0019-predictive-gaze-and-coordinated-motion.md)
- Parent contracts:
  [HA Satellite](../specs/ha-satellite/),
  [Predictive Gaze and Coordinated Motion](../specs/gaze-control/),
  [Robot Link](../specs/robot-link/),
  [Groundstation](../specs/groundstation/),
  [Architecture](../specs/architecture/)
- [Home Assistant MJPEG IP Camera integration](https://www.home-assistant.io/integrations/mjpeg/)
