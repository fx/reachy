# 0020: Home Assistant configuration and camera feed

## Summary

Implement the proposed
[Home Assistant Configuration and Camera Feed](../specs/home-assistant-configuration-and-camera-feed/)
contract in three reviewable pull requests: safe live motor groups, live
replacement of the groundstation URL, then the bounded MJPEG feed, operator
documentation and final traceability.

**Spec:** [Home Assistant Configuration and Camera Feed](../specs/home-assistant-configuration-and-camera-feed/)
**Status:** draft
**Depends On:** 0017, 0018, 0019

## Approval

The operator requested:

> add more options to the home assistant entity configuration:
>
> 1. individual motor toggle (head, body, antennae)
> 2. ground station URL so it can be easily configured without redeploying
> 3. the mjpeg stream from the ground station as feed so it can be viewed right in home assistant

The approved interpretation is three independently and immediately effective
motor-group switches; one persisted, immediately adopted groundstation URL text
entity; and a feed consumed through Home Assistant's standard MJPEG integration.
Motor disablement quiesces commands before torque-off, motor enablement
reacquires measured state before movement, and a URL change cleanly replaces the
active remote session. Video remains intentionally unauthenticated behind the
same trusted-network boundary as existing operator surfaces. A feed exists only
for exactly one eligible authenticated robot session; zero or multiple sessions
fail deterministically.

This proposal records the approval contract only. Production behavior starts in
the three implementation tasks below.

## Motivation

Home Assistant already exposes microphone settings and the speaker controls from
[0017](./0017-speaker-controls-in-home-assistant.md), but the physical motor
groups and the remote-session address are absent. Changing either currently
requires another surface or a restart, and a requested state has to remain
separate from state the SDK or session lifecycle actually adopted.

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

- The first implementation pull request MUST establish motor command gates,
  measured-state reseeding and the three switch entities before exposing any
  torque write to Home Assistant.
- The second implementation pull request MUST centralize remote-source ownership
  and generation replacement before making the URL writable at run time.
- The third implementation pull request MUST add the feed only as an observer of
  authenticated sessions and MUST complete operator documentation, requirement
  traces, Markdown registration, snapshot regeneration and final status/count
  synchronization.
- Every task MUST preserve the stable entities delivered by 0017, the controlled
  startup and cancellation boundaries delivered by 0018, and the trajectory,
  ownership and safety invariants delivered by 0019.
- Runbooks MUST contain output actually observed. Any step needing a real robot or
  Home Assistant remains marked pending hardware verification until it is run;
  invented transcripts are not acceptance evidence.

## Design

### Approach

#### Safe motor groups

Add an application-owned entity module beside `audio_entities.py`; no vendored
ESPHome file changes. A motor coordinator owns effective group state, serializes
writes and maps head, body and antenna groups to the SDK identifiers fixed by the
spec. `RobotHandle` and the existing fakes gain the selective disable operation
needed to complement selective enablement.

Every head, body and antenna command path consults the same gates. Disabling first
prevents new commands, waits for an in-flight group command to leave the critical
section, then removes torque. Re-enabling energizes the exact group, reads the
current pose or joints and resets the relevant controller or expression state
before opening its command gate. The switch publishes the read-back after the
transition, including the preceding state when an SDK call fails.

The body torque switch remains separate from `body_motion_enabled`. The former is
live availability; the latter continues to decide at construction whether
predictive gaze allocates and commands body yaw.

#### Live groundstation URL

Add the text entity outside the vendored directory and route it through
`validate_session_url`, `OverrideStore` and the existing resolve-write-adopt
configuration path. Refactor the remote branch of the perception source into one
lifecycle owner that can serialize replacement, invalidate the old generation,
close its `RemotePerception` and `SessionClient`, then create and start the
replacement.

The existing credential, capability set, frame cadence, staleness and reconnect
policy carry into the replacement. A local-only selection persists the URL but
has no remote source to swap. Remote-with-local-fallback continues using its
existing fallback rules during the bounded replacement gap. Failed validation,
persistence, close or construction leaves one known effective URL and never two
active remote producers.

#### Bounded MJPEG feed

Inject a groundstation feed registry from `service.py` into the session and HTTP
surfaces without importing capabilities across their enforced boundary. After the
pipeline's existing JPEG validation succeeds, publish the original compressed
payload to that session's one latest-frame slot before capability processing
continues. The decoded image remains the single input shared by capabilities.

Session authentication and finalization register and unregister feed eligibility.
`/stream.mjpg` snapshots the exactly-one selection, reserves one of four viewer
slots, emits multipart JPEG parts from successive latest-slot generations, and
releases the slot from response cancellation and session invalidation alike.
Zero eligible sessions, ambiguous sessions and capacity exhaustion receive
separate fixed responses. No viewer owns a frame queue or producer task.

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
- **Decision:** Treat each physical motor group as one live torque and command
  boundary, with all three enabled after the approved controlled startup.
  - **Why:** A switch that only changes torque can leave a stale controller
    commanding disabled hardware, while a switch that only suppresses commands
    does not turn the motor off. One serialized boundary is needed for both.
  - **Alternatives considered:** Independent Stewart-actuator and left/right
    antenna controls, and folding body torque into `body_motion_enabled`.
- **Decision:** Adopt a valid URL by replacing the entire remote source rather
  than mutating a connected client.
  - **Why:** Session client state includes sequence, agreement, reconnect and
    cached-result identity. Reusing it across destinations makes late results and
    reconnect work indistinguishable between generations.
  - **Alternatives considered:** Restarting the satellite, changing an internal
    URL field in place, or overlapping old and new clients during handoff.
- **Decision:** Reuse the original validated JPEG in one latest-only slot.
  - **Why:** The robot already spent the capture and compression cost. Another
    connection, decode, encode or queue adds load and latency without improving
    what Home Assistant receives.
  - **Alternatives considered:** Pulling the daemon camera separately, streaming
    decoded arrays, transcoding, or buffering per viewer.
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
- No robot-link wire-format change, second robot client or inbound robot listener.
- No video authentication in this change.
- No frame recording, retention beyond the live slot, disk write, content log,
  stream cache or transcode.
- No custom Home Assistant component, native satellite camera entity,
  integration-registry mutation or still-image endpoint.
- No arbitrary stream selection when multiple eligible robot sessions exist.

## Tasks

- [ ] Add safe live motor-group Configuration switches
  - [ ] Add selective motor disablement to the daemon boundary and deterministic
        fakes without importing the SDK outside its existing entry point
  - [ ] Add one serialized effective-state coordinator for the exact head, body
        and antenna mappings
  - [ ] Gate every gaze, pipeline-head, body and antenna command before torque-off
        and reacquire measured state before reopening movement
  - [ ] Add stable switch entities outside the vendored directory and publish
        read-backs for successful and refused transitions
  - [ ] Cover independent groups, in-flight command quiescing, SDK failures,
        re-enable reseeding, safe hold, release and shutdown without hardware
  - [ ] Run the focused satellite suites and repository checks required above

- [ ] Add persisted live groundstation URL replacement
  - [ ] Add the stable Configuration text entity using the shared session-URL
        validator and settings override store
  - [ ] Make the groundstation URL a live setting with one serialized remote-source
        lifecycle owner
  - [ ] Retire the old client and generation before starting a replacement, and
        discard racing results from retired generations
  - [ ] Preserve single-client reconnect, staleness and local-fallback behavior
        across successful, refused and cancelled replacements
  - [ ] Cover validation, persistence failure, close failure, replacement failure,
        rapid writes, restart persistence and Home Assistant read-back without a
        real network or Home Assistant instance
  - [ ] Run the focused satellite suites and repository checks required above

- [ ] Add the bounded MJPEG feed, documentation and final traceability
  - [ ] Add a session-scoped latest-original-JPEG registry whose eligibility
        begins after authentication and valid-frame receipt and ends in every
        session finalization path
  - [ ] Publish only payloads that pass existing JPEG validation, without another
        robot connection, stream-only decode or re-encode, capability blockage or
        per-viewer frame queue
  - [ ] Serve `/stream.mjpg` with standard multipart JPEG framing, no-store
        responses, exactly-one-session selection and four bounded viewer slots
  - [ ] Cover zero, one and multiple sessions, malformed frames, slow viewers,
        replacement, disconnect, cancellation, capacity and application shutdown
        using unit fakes and marked in-process transport tests
  - [ ] Prove logs, metrics, traces, errors and deployment storage contain no frame
        body, credential or installation identifier
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

1. **Deterministic motor acceptance:** exercise each group independently and in
   concurrent command boundaries, including failed disable, failed enable,
   measured-state failure, safe hold, terminal release and application shutdown.
2. **Deterministic source acceptance:** exercise valid and invalid URL writes,
   durable override failure, ordered replacement, rapid superseding writes,
   stale old-generation results, reconnect and local fallback.
3. **Deterministic feed acceptance:** exercise all eligibility cardinalities,
   valid and malformed JPEGs, frame replacement under a stalled consumer, viewer
   exhaustion, cancellation, session loss and service shutdown, then drive the
   endpoint over an in-process HTTP transport.
4. **Staged live verification:** first verify entity identity and effective-state
   read-back without moving hardware; then verify one motor group at a time with
   an abort path; then replace the groundstation URL and prove only the new source
   advances; finally configure Home Assistant's standard MJPEG integration and
   observe one feed. Stop and restore the released artifact and configuration on
   any safety, lifecycle, identity or privacy threshold breach.
5. **Evidence:** record only scrubbed outcomes and aggregate counters. Raw frames,
   credentials, addresses, host identity and invented transcripts do not enter
   the repository.

## Open Questions

None for implementation approval. Any request to authenticate video, choose among
multiple robot sessions, retain frames, split physical motor groups further or
change gaze/body calibration starts a separate proposal.

## References

- Spec: [Home Assistant Configuration and Camera Feed](../specs/home-assistant-configuration-and-camera-feed/)
- Dependencies:
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
