# Home Assistant Configuration and Camera Feed

## Overview

This specification owns the operator-facing contract for live motor-group and
groundstation configuration on the Reachy satellite's Home Assistant device and
for viewing the groundstation's current robot camera frame through Home
Assistant's standard MJPEG integration.

It extends the existing
[HA Satellite](../ha-satellite/),
[Predictive Gaze and Coordinated Motion](../gaze-control/),
[Robot Link](../robot-link/) and
[Groundstation](../groundstation/) contracts without changing their wire format,
trajectory calibration or deployment topology. The behavior described here is
proposed and not yet implemented.

## Background

The satellite already announces stable Home Assistant entities for microphone
and speaker configuration, persists application settings above the environment,
and reports the value actually in effect when a write is refused. Motor torque
and the groundstation session address are not available on that surface. Motors
are enabled together during controlled startup, while the groundstation client is
built once from restart-bound settings.

The robot already sends camera frames as JPEG bytes on its one authenticated,
outbound robot-link session. The groundstation decodes each accepted frame once
for capabilities and does not expose those bytes as video. Reusing that stream is
therefore different from opening another connection to the robot: it adds a
bounded observer to frames the authenticated session already delivered.

Home Assistant provides an MJPEG IP Camera integration whose required input is a
stream URL. The camera is configured through that standard integration. It is
not announced as a native camera entity on the satellite device, and no custom
Home Assistant component is part of this contract.

## Requirements

### REQ-093: Home Assistant configuration reports effective state

The satellite MUST expose stable Home Assistant Configuration entities for the
head motors, body motor, antenna motors and groundstation session URL that report
only confirmed effective state, announce each Boolean motor switch only after an
initial agreeing correlated daemon acknowledgement and physical grouped-torque
read-back, publish a new Boolean only from a later successful read-back including
the actual value when it contradicts a request, and otherwise reject the request,
retain the last-confirmed Boolean without publishing the requested value, keep the
group's command gate closed and surface bounded identifier-free confirmation
diagnostics.

#### Scenario: Home Assistant reconnects after initial confirmation

- **GIVEN** each motor group has an agreeing initial acknowledgement and physical
  Boolean read-back
- **WHEN** the satellite announces its entities or Home Assistant reconnects
- **THEN** the same object identifiers describe the same controls and each initial
  switch Boolean matches confirmed physical torque state for its whole group

#### Scenario: Initial motor confirmation fails

- **GIVEN** a motor group without an agreeing initial acknowledgement and physical
  grouped-torque read-back
- **WHEN** the satellite constructs the Home Assistant entity set
- **THEN** no operable switch or fabricated Boolean is announced for that group,
  its command gate remains closed and bounded diagnostics report the confirmation
  failure

#### Scenario: A non-motor change is refused

- **GIVEN** an entity showing a value that is currently in effect
- **WHEN** validation, persistence or runtime adoption of a requested value fails
- **THEN** the entity continues to report the previous effective value and the
  Home Assistant connection remains usable

#### Scenario: A motor request is confirmed

- **GIVEN** a registered motor switch with a last-confirmed Boolean
- **WHEN** a requested transition receives an agreeing acknowledgement and grouped
  physical read-back
- **THEN** the switch publishes that read-back Boolean as its new last-confirmed
  state

#### Scenario: Physical read-back contradicts the request

- **GIVEN** a registered motor switch whose requested Boolean differs from a
  successful grouped physical read-back
- **WHEN** the transition completes
- **THEN** the switch publishes the actual read-back Boolean rather than the
  request, refuses the requested outcome, keeps the group command gate closed and
  records bounded diagnostics

#### Scenario: Confirmation is absent after registration

- **GIVEN** a registered motor switch with a last-confirmed Boolean
- **WHEN** acknowledgement or grouped read-back is missing, late or fails
- **THEN** no requested Boolean is published, the command is refused, the
  last-confirmed Boolean remains the switch state and diagnostics distinguish that
  retained value from a newly confirmed sample

#### Scenario: A later read-back succeeds

- **GIVEN** a failed command left a registered switch at its last-confirmed Boolean
- **WHEN** a subsequent grouped physical read-back succeeds
- **THEN** the switch may publish that actual Boolean as the new last-confirmed
  state regardless of the earlier request

### REQ-094: Motor groups change safely at run time

The satellite MUST apply independent head, body and antenna motor-group switches
immediately by quiescing every application- and daemon-owned command producer for
the group before torque-off, establishing exclusive body-command ownership when
needed, confirming each physical grouped-torque transition, and reacquiring and
seeding measured state before movement or the preceding ownership policy resumes,
without weakening existing trajectory, workspace, safe-hold or terminal-release
guarantees.

#### Scenario: Head motors are disabled while tracking

- **GIVEN** predictive gaze actively commanding the head while antenna expression
  and body motion are available
- **WHEN** the operator disables the head group
- **THEN** head commands stop before torque is removed from `stewart_1` through
  `stewart_6`, while enabled unrelated groups may continue and no successful
  transition is reported before the whole head group is confirmed off

#### Scenario: A motor group is re-enabled

- **GIVEN** a disabled motor group whose mechanism may have moved while torque was
  off
- **WHEN** the operator re-enables that group
- **THEN** measured state is reacquired after the whole group is confirmed on,
  stale target and controller state are discarded, and the first later movement
  begins without a target jump

#### Scenario: Body torque is disabled without gaze ownership

- **GIVEN** any combination of face tracking and restart-bound body motion in which
  daemon automatic body yaw may still command `body_rotation`
- **WHEN** the operator disables the body group
- **THEN** the satellite first establishes exclusive body-command ownership and
  quiesces automatic yaw, without changing either setting or its default

#### Scenario: Body torque is re-enabled

- **GIVEN** a disabled body group and the ownership or automatic-yaw policy that
  preceded its disablement
- **WHEN** confirmed physical torque and fresh measured body state become available
- **THEN** body trajectory state is seeded before the preceding ownership policy
  resumes, with no hidden target or target jump

#### Scenario: A body transition fails or shutdown begins

- **GIVEN** a body transition in progress under exclusive command ownership
- **WHEN** confirmation fails, cancellation arrives or terminal shutdown begins
- **THEN** no automatic or application body producer resumes against unknown torque
  state and terminal release remains safe and idempotent

#### Scenario: Antenna motors are disabled

- **GIVEN** a voice-pipeline state producing antenna expression
- **WHEN** the operator disables the antenna group
- **THEN** antenna commands stop before torque is removed from `right_antenna` and
  `left_antenna`, without splitting them into independently configurable groups
  or reporting success before both are confirmed off

### REQ-095: Groundstation replacement is persisted and isolated

The satellite MUST apply one shared session-URL contract across every
configuration surface that accepts at most 255 characters without truncation,
refuses a legacy overlong value with actionable remediation, and changes an
accepted URL immediately through a compensating transition in which the durable
value, sole eligible remote source and effective read-back advance together only
after adoption succeeds or remain together on the preceding value after any
failure, while making source restoration bounded and cancellable, preserving
local fallback and bounded reconnection, and excluding overlap and late results.

#### Scenario: Either configuration surface changes groundstations

- **GIVEN** an active authenticated remote session producing detections
- **WHEN** the operator submits another valid session URL through Home Assistant
  or the application settings page
- **THEN** the replacement becomes durable, effective and readable together,
  detections and connection state come only from it, late preceding results are
  ignored, and fallback and bounded reconnection continue to work

#### Scenario: A change fails before durable commit

- **GIVEN** a running remote source and a candidate URL that passes validation
- **WHEN** replacement preparation, preceding-source retirement, candidate startup
  or durable commit fails
- **THEN** compensation leaves the preceding URL as both restart and runtime
  configuration and effective read-back, restores at most its one remote source,
  and reports remote health unavailable if that source cannot yet be rebuilt

#### Scenario: Repeated source restoration fails and later succeeds

- **GIVEN** compensation preserved the preceding effective URL but could not
  construct its remote source
- **WHEN** bounded restoration attempts fail repeatedly and a later attempt
  succeeds
- **THEN** the satellite remains disconnected with local fallback available until
  exactly one restored source resumes the existing reconnect behavior

#### Scenario: Source restoration reaches its bound

- **GIVEN** compensation cannot reconstruct the preceding remote source
- **WHEN** every bounded restoration attempt fails
- **THEN** the effective URL remains the preceding durable value, remote health
  remains unavailable, local fallback remains available and no unbounded recovery
  work or overlapping client continues

#### Scenario: Source restoration is superseded or shut down

- **GIVEN** source restoration is waiting or attempting construction
- **WHEN** a later operator request begins or application shutdown starts
- **THEN** the pending restoration is cancelled before the new transition or
  shutdown completes and cannot publish a late source afterwards

#### Scenario: A legacy overlong URL is loaded

- **GIVEN** a previously accepted environment or persisted URL containing 256 to
  512 characters
- **WHEN** the upgraded satellite validates configuration at startup
- **THEN** startup refuses it without truncation and names the 255-character
  limit, the responsible configuration source and how to replace or remove that
  value before restarting

#### Scenario: An overlong runtime request is submitted

- **GIVEN** a running application and a URL longer than 255 characters
- **WHEN** either configuration surface submits it
- **THEN** validation refuses it before source or durable state changes and the
  preceding effective value remains the read-back

#### Scenario: The application restarts

- **GIVEN** a URL successfully adopted through either configuration surface
- **WHEN** the satellite application starts again
- **THEN** that exact URL remains effective without a redeployment

### REQ-096: MJPEG is a bounded latest-frame view

The groundstation MUST retain at most one original payload globally for a
standards-compatible MJPEG stream only after both explicit JPEG-format signature
validation and successful image decode, replace rather than queue that payload
for slow viewers, and add no robot connection, stream-only decode or re-encode,
or capability-processing blockage.

#### Scenario: A viewer is slower than the robot

- **GIVEN** the sole eligible session delivering valid JPEG frames faster than one
  viewer consumes them
- **WHEN** newer frames arrive before that viewer is ready
- **THEN** intermediate frames are replaced and the next part contains the newest
  available original JPEG rather than a backlog

#### Scenario: Session cardinality becomes ambiguous

- **GIVEN** one retained feed frame and one active authenticated robot session
- **WHEN** a second authenticated robot session becomes active
- **THEN** no JPEG remains retained for the feed rather than one being kept for
  each session

#### Scenario: A decodable non-JPEG image arrives

- **GIVEN** one eligible session and a payload such as PNG that the general image
  decoder accepts
- **WHEN** the groundstation validates the payload for the feed
- **THEN** it is rejected before publication and is never labeled `image/jpeg`

#### Scenario: A malformed JPEG arrives

- **GIVEN** one eligible session with a valid latest frame
- **WHEN** a later payload has a JPEG signature but fails image decode
- **THEN** the malformed payload is not published and capability error handling
  continues independently

### REQ-097: Feed eligibility is deterministic

The groundstation MUST serve `/stream.mjpg` only after exactly one active
authenticated robot session has supplied a fresh validated JPEG while it is the
sole session, clear all feed frame state and end viewers whenever authenticated
session cardinality is zero or greater than one, and require another fresh
validated JPEG after cardinality returns to one.

#### Scenario: No eligible robot is connected

- **GIVEN** no active authenticated robot session
- **WHEN** a viewer requests the feed
- **THEN** the request receives the stable unavailable outcome and no stream is
  held open waiting for an unspecified future robot

#### Scenario: Two robots are connected

- **GIVEN** one eligible session with an active feed
- **WHEN** a second robot session authenticates
- **THEN** the retained frame is cleared, the existing feed ends, and new requests
  receive the stable ambiguity outcome rather than a stream selected by timing or
  identifier

#### Scenario: Ambiguity returns to one session

- **GIVEN** ambiguity cleared the feed while two sessions were active
- **WHEN** either session ends and one remains
- **THEN** the prior frame is not resurrected and the feed stays unavailable until
  the remaining session supplies a fresh validated JPEG

#### Scenario: The selected session ends

- **GIVEN** one eligible session with connected viewers
- **WHEN** that session closes or is cancelled
- **THEN** its frame is discarded, its viewers finish, and a later session is
  considered only after supplying its own fresh validated JPEG

### REQ-098: The unauthenticated feed has a bounded privacy surface

The groundstation MUST keep `/stream.mjpg` intentionally unauthenticated within
the deployment's trusted-network boundary while retaining at most one live JPEG
globally in application state, marking responses non-cacheable, never recording
or writing frames or emitting frame content through observability, enforcing a
finite viewer bound, and promptly cancelling viewer work on disconnect or loss
of eligibility.

#### Scenario: A viewer disconnects

- **GIVEN** an active MJPEG response waiting for or sending a frame
- **WHEN** the client disconnects
- **THEN** the viewer releases its slot and no producer, queue or background task
  remains for it

#### Scenario: The viewer limit is reached

- **GIVEN** the configured maximum number of active feed viewers
- **WHEN** another viewer requests the endpoint
- **THEN** the request receives the stable capacity outcome without allocating a
  stream or affecting the robot session and capability pipeline

#### Scenario: An operator inspects runtime evidence

- **GIVEN** active camera streaming
- **WHEN** logs, metrics, traces, caches and deployment storage are inspected
- **THEN** they contain bounded counters and lifecycle state only, with no JPEG
  body, recorded stream, camera frame, credential or installation identifier

## Design

### Home Assistant entities

The four new controls are authored beside the existing speaker controls rather
than inside the vendored ESPHome protocol directory. Their object identifiers are
`head_motors`, `body_motor`, `antenna_motors` and `groundstation_url`; these names
are compatibility identifiers, not display labels. The three motor entities are
switches and the address is a text entity. All four appear in Home Assistant's
Configuration category.

The existing ESPHome switch state is Boolean and has no availability field, so the
proposal adds neither a third state nor a protocol extension. The current SDK's
selective torque methods return `None` after a fire-and-forget command and expose
no per-group physical torque query. That is not confirmation. Implementation
therefore has a hard prerequisite on a daemon/SDK contract that correlates each
grouped request with an acknowledgement and returns physical torque state for
every named motor in the group.

Before the entity set becomes operable, each motor group is read successfully.
Only a group with an agreeing initial acknowledgement and read-back receives its
stable switch object ID; an unconfirmed group gets no fabricated entity or state
for that process. An installation that knew the object ID therefore observes the
ordinary ESPHome entity-list/connection lifecycle, not a synthetic switch Boolean.
After registration the entity stores one last-confirmed Boolean and the
confirmation evidence separately. A successful read-back replaces and
publishes that Boolean even when it contradicts the requested value. Missing or
failed confirmation leaves the command gate closed and the Boolean unchanged; a
command response may repeat that retained Boolean so Home Assistant returns to the
known state, but it does not mark it as a new sample. A later successful read-back
can advance it.

A bounded identifier-free motor diagnostic record carries the static group,
requested Boolean, acknowledgement outcome, read-back outcome, whether the
published value changed and the age of the last confirmation. It is the surface
that distinguishes a retained last-confirmed Boolean from fresh evidence without
claiming a switch-wire capability that does not exist.

The motor switches start enabled after controlled wake confirms physical state.
They are runtime torque gates, not a replacement for behavior settings. Head maps
to all six Stewart motors as one group, body maps to `body_rotation`, and both
antenna joints remain one group. Each gate covers every producer that can reach
that hardware. For body motion this includes daemon automatic yaw even when face
tracking never acquired gaze ownership. A body transition records the preceding
automatic-yaw policy, disables it before torque-off, seeds fresh measured state
after confirmed re-enable and restores that policy only after the body gate is
safe. Failure and shutdown keep the producer quiesced rather than restoring it
against unknown torque.

The text entity and the shared settings model both cap `groundstation_url` at 255
characters, so the entity can represent every value any configuration layer
accepts. The session client's scheme and credential-exclusion validation remains
the other shared half of the URL contract. Existing 256–512-character environment
or persisted values are rejected at startup with source-specific remediation;
none is truncated or silently hidden from Home Assistant.

The settings page and Home Assistant entity call one long-lived replacement owner.
It retains the source factory, preceding effective configuration, optional current
source, transition generation and at most one reconstruction task. A serialized
operation validates and prepares a candidate, retires the preceding source, starts the
candidate, and only then atomically replaces the durable settings file and
publishes the new effective value. Failure before durable replacement leaves that
file untouched.

Failure during durable replacement closes the candidate and asks the same owner
to reconstruct the preceding source from its retained factory and configuration.
If construction fails before a `RemotePerception` exists, the owner — not the
connectivity supervisor — enters a bounded, cancellable reconstruction state.
Each failed partial object is closed before another attempt; no generation becomes
eligible until one complete source is installed. Successful construction hands
that source to its ordinary connectivity supervisor, which then owns connection
and reconnection rather than construction.

A later operator command serializes through the owner, cancels and awaits the old
reconstruction state, then starts its own transition. Shutdown does the same
cancellation without starting another source. Exhaustion leaves the preceding URL
as durable and effective read-back, remote health unavailable and local fallback
available; a later command or restart may begin a new bounded attempt. This is
explicit ordering and compensation, not a claim of an atomic filesystem-and-
network transaction.

### MJPEG feed

The groundstation keeps one original compressed latest frame globally. A
dedicated format gate verifies the JPEG signature rather than treating successful
OpenCV decode as proof of format; the existing decoder accepts PNG and other image
formats. Only a payload that passes both checks can replace the global value and
be labeled `image/jpeg`. The original bytes remain unchanged. Any transition to
zero or multiple authenticated sessions clears the value. Returning from multiple
sessions to one leaves it empty until the remaining session supplies a fresh
actual JPEG, so ambiguity can never resurrect an earlier image. The capability
pipeline continues to receive its one decoded frame; streaming does not add
another decode.

`GET /stream.mjpg` responds as `multipart/x-mixed-replace`, with each part marked
`image/jpeg` and carrying the original payload length. Responses prohibit cache
storage. Zero eligible sessions, multiple authenticated sessions and exhausted
viewer capacity have separate stable non-success outcomes. The initial viewer
bound is four; reaching it does not allocate per-viewer frame storage, and a slow
viewer advances directly to the newest global frame.

Session cardinality changes wake viewers so they can finish or observe the next
fresh frame as applicable. The endpoint never selects among multiple sessions,
even if one connected first or produced a newer frame.

### Home Assistant integration

An operator adds Home Assistant's built-in MJPEG IP Camera integration and supplies
the feed URL. That integration is a separate Home Assistant device entry; the
satellite neither announces a camera entity nor mutates Home Assistant's
integration registry. No still-image endpoint is part of this proposal.

### Trust and data handling

The endpoint follows the existing operator surfaces in treating network reach as
the trust boundary. Its lack of HTTP authentication is explicit because JPEG
frames are more sensitive than health or configuration metadata. Deployment
guidance places the groundstation on a trusted network and warns against exposing
the endpoint outside it.

Only compressed bytes already accepted from the sole authenticated robot-link
session enter the one global live value. They are neither written to disk nor
copied into logs, metrics, traces, caches or error messages. Viewer tasks are
response-scoped and lose eligibility immediately when that session ends or
ambiguity appears.

## Constraints

- Gaze calibration, allocation, trajectory limits and the restart-bound,
  false-by-default body-motion shipping decision remain owned by the gaze-control
  specification.
- The robot-link wire types, outbound-only topology, authenticated session and
  single-client transport remain unchanged.
- Motor controls stop at the three physical groups. Individual Stewart actuators
  and left/right antenna controls are outside this contract.
- Motor switches depend on a correlated daemon acknowledgement and grouped
  physical torque read-back capability that the current fire-and-forget SDK
  surface does not provide; no switch is exposed until that prerequisite and its
  initial confirmation succeed.
- Motor switches retain the existing Boolean wire shape. There is no third state,
  availability extension or custom Home Assistant component; confirmation age and
  failure remain bounded diagnostic facts beside the last-confirmed Boolean.
- The stream observes the existing robot upload. It does not add a robot inbound
  listener, a second camera pull, frame recording, retention or transcoding, and
  it labels only explicitly validated JPEG-format bytes as `image/jpeg`.
- The standard Home Assistant MJPEG integration is the only camera integration in
  scope. A custom component and a native satellite camera entity are excluded.
- Hardware-free tests use fakes for the robot, camera and Home Assistant. Live
  verification records only scrubbed outcomes after automated acceptance passes.

## Open Questions

No product or safety decision remains open for the proposed scope. Viewer-count
or deployment-authentication changes beyond the selected trusted-network model
need a separate proposal.

## References

- [HA Satellite REQ-040 and REQ-046–050](../ha-satellite/index.md#requirements)
- [Predictive Gaze and Coordinated Motion REQ-083–090](../gaze-control/index.md#req-083-motion-derivatives-remain-bounded)
- [Robot Link REQ-010, REQ-011, REQ-015, REQ-018 and REQ-019](../robot-link/index.md#requirements)
- [Groundstation](../groundstation/)
- [Architecture REQ-005 and REQ-009](../architecture/index.md#req-005-behaviour-is-testable-without-hardware)
- [Home Assistant MJPEG IP Camera integration](https://www.home-assistant.io/integrations/mjpeg/)

## Changelog

| Date | Change | Document |
|------|--------|----------|
| 2026-08-26 | Proposed acknowledged motor controls, transactional live configuration and a bounded actual-JPEG camera feed | [0020-home-assistant-configuration-and-camera-feed](../../changes/0020-home-assistant-configuration-and-camera-feed.md) |
