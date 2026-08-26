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
head motors, body motor, antenna motors and groundstation session URL, with each
entity reporting the value known to be in effect rather than an unconfirmed
request.

#### Scenario: Home Assistant reconnects

- **GIVEN** an existing installation with automations and dashboards that refer
  to the configuration entities
- **WHEN** the satellite is upgraded or Home Assistant reconnects
- **THEN** the same object identifiers describe the same controls and their
  published values match the running application

#### Scenario: A requested change is refused

- **GIVEN** an entity showing a value that is currently in effect
- **WHEN** validation, persistence or hardware adoption of a requested value
  fails
- **THEN** the entity continues to report the previous effective value and the
  Home Assistant connection remains usable

### REQ-094: Motor groups change safely at run time

The satellite MUST apply independent head, body and antenna motor-group switches
immediately by quiescing that group's commands before disabling its exact motors
and by reacquiring measured state before re-enabling movement, without weakening
existing trajectory, workspace, ownership, safe-hold or terminal-release
guarantees.

#### Scenario: Head motors are disabled while tracking

- **GIVEN** predictive gaze actively commanding the head while antenna expression
  and body motion are available
- **WHEN** the operator disables the head group
- **THEN** head commands stop before torque is removed from `stewart_1` through
  `stewart_6`, while commands for enabled unrelated groups may continue

#### Scenario: A motor group is re-enabled

- **GIVEN** a disabled motor group whose mechanism may have moved while torque was
  off
- **WHEN** the operator re-enables that group
- **THEN** its motors are enabled, measured state is reacquired, stale controller
  state is discarded, and the first later movement begins without a target jump

#### Scenario: Body torque is changed

- **GIVEN** the restart-bound `body_motion_enabled` setting in either state
- **WHEN** the operator changes the live body-motor switch
- **THEN** only `body_rotation` torque availability changes; the setting's
  false-by-default shipping decision and the calibrated controller limits remain
  unchanged

#### Scenario: Antenna motors are disabled

- **GIVEN** a voice-pipeline state producing antenna expression
- **WHEN** the operator disables the antenna group
- **THEN** antenna commands stop before torque is removed from `right_antenna` and
  `left_antenna`, without splitting them into independently configurable groups

### REQ-095: Groundstation replacement is persisted and isolated

The satellite MUST validate and persist an accepted groundstation session URL
through its application-settings override layer and immediately replace any
active remote source by retiring its client and source generation before a
replacement can publish connection state or results.

#### Scenario: The running satellite changes groundstations

- **GIVEN** an active authenticated remote session producing detections
- **WHEN** the operator submits another valid session URL through Home Assistant
- **THEN** the preceding client closes, its results become ineligible, one new
  source generation starts with the replacement URL, and local fallback and
  bounded reconnection retain their existing behavior

#### Scenario: URL validation fails

- **GIVEN** a running remote session and a submitted value that is not an accepted
  credential-free WebSocket session URL
- **WHEN** the satellite validates the request
- **THEN** no override is written, the current client and source generation remain
  active, and the entity reads back the current URL

#### Scenario: The application restarts

- **GIVEN** a URL successfully adopted from Home Assistant
- **WHEN** the satellite application starts again
- **THEN** the same override supplies the session URL without a redeployment

### REQ-096: MJPEG is a bounded latest-frame view

The groundstation MUST expose the latest validated original JPEG from an eligible
robot session as a standards-compatible MJPEG stream without opening another
robot connection, decoding or re-encoding solely for streaming, blocking
capability processing, or accumulating intermediate frames for a slow viewer.

#### Scenario: A viewer is slower than the robot

- **GIVEN** an eligible session delivering valid JPEG frames faster than one
  viewer consumes them
- **WHEN** newer frames arrive before that viewer is ready
- **THEN** intermediate frames are replaced and the next part contains the newest
  available original JPEG rather than a backlog

#### Scenario: A malformed frame arrives

- **GIVEN** an eligible session with a valid latest frame
- **WHEN** a later payload fails the groundstation's existing JPEG validation
- **THEN** the malformed payload is not published and capability error handling
  continues independently

### REQ-097: Feed eligibility is deterministic

The groundstation MUST serve `/stream.mjpg` only while exactly one active
authenticated robot session has supplied a valid current JPEG, reject zero-session
and multiple-session ambiguity deterministically, and remove a session's feed
eligibility and frame when that session ends.

#### Scenario: No eligible robot is connected

- **GIVEN** no active authenticated session with a valid frame
- **WHEN** a viewer requests the feed
- **THEN** the request receives the stable unavailable outcome and no stream is
  held open waiting for an unspecified future robot

#### Scenario: Two eligible robots are connected

- **GIVEN** two active authenticated sessions that have each supplied a valid
  frame
- **WHEN** a viewer requests the feed or an existing feed becomes ambiguous
- **THEN** the groundstation returns the stable ambiguity outcome or ends the
  existing stream rather than selecting a robot by timing or identifier

#### Scenario: The selected session ends

- **GIVEN** one eligible session with connected viewers
- **WHEN** that session closes, is refused or is cancelled
- **THEN** its latest frame is discarded, its viewers finish, and a later session
  is considered only by a new request

### REQ-098: The unauthenticated feed has a bounded privacy surface

The groundstation MUST keep `/stream.mjpg` intentionally unauthenticated within
the deployment's trusted-network boundary while retaining only the bounded live
latest-frame slot, never recording or writing frames, never caching a response or
frame outside that slot, emitting no frame content through observability,
enforcing a finite viewer bound, and promptly cancelling viewer work on
disconnect or loss of eligibility.

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
Configuration category and publish a read-back after each attempted change.

The motor switches start enabled, preserving controlled-wake behavior. They are
runtime torque gates, not a replacement for behavior settings. In particular,
the body switch does not change `body_motion_enabled`: it only determines whether
the existing controller is allowed to command and energize `body_rotation`.
Head maps to all six Stewart motors as one group, body maps to the rotation motor,
and both antenna joints remain one group.

The groundstation entity uses the session client's existing URL validation, so
WebSocket scheme and credential-exclusion rules retain one owner. It writes the
same persisted overrides file as the application settings page. The remote source
owns replacement as one serialized lifecycle transition: invalidate old results,
close the old client, create a new source generation and only then start the new
client. A failed transition leaves or restores the prior effective source rather
than permitting two producers.

### MJPEG feed

The groundstation keeps one original compressed latest-frame slot per eligible
session, replacing the prior value atomically after the existing JPEG validation
succeeds. The capability pipeline continues to receive its one decoded frame;
streaming reads the validated compressed bytes and does not add another decode.
Each viewer tracks only the slot generation it last sent, so a slow consumer
skips directly to the newest generation.

`GET /stream.mjpg` responds as `multipart/x-mixed-replace`, with each part marked
`image/jpeg` and carrying the original payload length. Responses prohibit cache
storage. Zero eligible sessions, multiple eligible sessions and exhausted viewer
capacity have separate stable non-success outcomes. The initial viewer bound is
four; reaching it does not allocate per-viewer frame storage.

Session eligibility begins only after robot-link authentication and receipt of a
valid JPEG. Session completion unregisters the slot in a `finally` boundary and
wakes viewers so they can finish. The endpoint never selects among multiple
eligible sessions, even if one connected first or produced a newer frame.

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

Only compressed bytes already accepted from the authenticated robot-link session
enter the live slot. They are neither written to disk nor copied into logs,
metrics, traces, caches or error messages. Viewer tasks are response-scoped and
lose eligibility immediately when the selected session ends or ambiguity appears.

## Constraints

- Gaze calibration, allocation, trajectory limits and the restart-bound,
  false-by-default body-motion shipping decision remain owned by the gaze-control
  specification.
- The robot-link wire types, outbound-only topology, authenticated session and
  single-client transport remain unchanged.
- Motor controls stop at the three physical groups. Individual Stewart actuators
  and left/right antenna controls are outside this contract.
- The stream observes the existing robot upload. It does not add a robot inbound
  listener, a second camera pull, frame recording, retention or transcoding.
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
| 2026-08-26 | Proposed live Home Assistant configuration and a bounded groundstation camera feed | [0020-home-assistant-configuration-and-camera-feed](../../changes/0020-home-assistant-configuration-and-camera-feed.md) |
