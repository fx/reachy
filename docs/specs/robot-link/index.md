# Robot Link

## Overview

Robot Link is the wire contract between the robot and the groundstation: how a
session is opened, how the two sides agree on what they can do, how frames and
results travel, and what happens when the link degrades or breaks.

It is owned here rather than inside either component, because it has three
consumers — the [robot app](../ha-satellite/), the
[groundstation](../groundstation/), and [`reachyctl probe`](../reachyctl/) — and
a contract owned by one consumer drifts from the others.

The shared types and the golden fixtures that pin them live in the
`reachy-contracts` workspace member. Nothing is implemented yet.

## Background

The predecessor arrangement ran in the opposite direction. The vision service
pulled the robot's MJPEG stream and posted results back to an endpoint on the
robot, so the robot was simultaneously a server and a client, and each result
travelled over its own HTTP request.

That was measured. A cold TCP handshake to the robot took 378 ms at p50, which
put per-post latency between 22 ms and 1241 ms. Enabling connection reuse on
both halves brought posts to 18–85 ms — a twentyfold improvement obtained purely
by keeping one connection open.

The lesson generalises past the fix. Connection reuse was a property of how the
two programs happened to be written, so any later caller could reintroduce the
cost without anything failing. A single long-lived session makes the cheap path
the only path, and removes the robot's inbound listener along with it.

A second problem surfaced during the same work. When both detection switches
were turned off mid-frame the service posted an empty payload and the robot
answered 400. With results keyed to a sequence number on a session, "no
detections for frame N" is an ordinary message rather than a malformed request.

## Requirements

### REQ-010: The robot is a client only

The robot MUST open the session outbound to the groundstation, and the
groundstation MUST NOT require any inbound listener on the robot.

#### Scenario: The robot sits behind a restrictive network

- **GIVEN** a robot on a network that permits outbound connections but no
  inbound ones
- **WHEN** the app starts and connects to the groundstation
- **THEN** the session establishes and results flow, with no port opened on the
  robot

### REQ-011: One session carries every exchange

All frames, results, and control messages for a running app MUST travel over a
single session, established once and reused for the lifetime of that session.

#### Scenario: Steady-state operation

- **GIVEN** an established session carrying frames at ten per second
- **WHEN** traffic continues for an hour
- **THEN** no additional transport connection is opened for the duration

### REQ-012: Capabilities are negotiated at session start

Both sides MUST exchange the set of capabilities they support, each with a
version, before any capability-specific message is sent.

#### Scenario: The groundstation gains a capability

- **GIVEN** a groundstation offering face detection and gesture recognition, and
  a robot app that understands only face detection
- **WHEN** the session is established
- **THEN** the agreed set is face detection alone, and the app is not sent
  gesture results it cannot interpret

#### Scenario: A capability version is incompatible

- **GIVEN** an app requesting a capability at a version the groundstation does
  not offer
- **WHEN** negotiation completes
- **THEN** that capability is absent from the agreed set and the session
  continues with whatever else was agreed

### REQ-013: An empty result is a valid result

A result message carrying no detections MUST be treated as a successful result
for that frame.

#### Scenario: Every detector is disabled mid-session

- **GIVEN** a session whose agreed capabilities are all disabled by
  configuration while frames continue to arrive
- **WHEN** a frame is processed and yields nothing
- **THEN** an empty result is delivered and no error counter advances

### REQ-014: Results are keyed to the frame that produced them

Every frame MUST carry a monotonically increasing sequence number, and every
result MUST identify the sequence number of the frame it derives from.

#### Scenario: Results arrive out of order

- **GIVEN** results for frames 7 and 8 that arrive in the order 8 then 7
- **WHEN** the app applies them
- **THEN** the result for frame 7 is discarded as superseded, because a newer
  frame has already been applied

### REQ-015: Overload drops frames rather than queueing them

When frames arrive faster than they can be processed, the oldest unprocessed
frame MUST be discarded in preference to growing the queue or blocking the
producer.

#### Scenario: The groundstation slows down

- **GIVEN** a session where processing has become slower than the frame rate
- **WHEN** the backlog reaches its bound
- **THEN** the oldest queued frame is dropped, a drop is counted, and the most
  recent frame is still processed

### REQ-016: Results carry the age of the frame they describe

Every result MUST carry the time at which its frame was captured, expressed on a
clock the receiver can compare against without depending on the two machines
agreeing on wall-clock time.

#### Scenario: The consumer judges freshness

- **GIVEN** a result whose frame was captured well in the past because of a
  network stall
- **WHEN** the app receives it
- **THEN** the app can determine the result's age without consulting the
  groundstation's clock

### REQ-017: Stale results stop being acted on

A consumer MUST stop acting on results once none has arrived within a configured
staleness window.

#### Scenario: The groundstation disappears

- **GIVEN** an app tracking a face from groundstation results
- **WHEN** results stop arriving and the staleness window elapses
- **THEN** the app ceases acting on the last known result

### REQ-018: Reconnection is automatic and rate-limited

A client MUST re-establish a dropped session automatically, and MUST increase
the delay between successive failed attempts up to a bound.

#### Scenario: The groundstation restarts

- **GIVEN** an app with an established session
- **WHEN** the groundstation restarts and is unavailable for a period
- **THEN** the app retries with growing delays and resumes once the
  groundstation returns, without operator action

#### Scenario: The groundstation is unreachable for a long time

- **GIVEN** an app whose groundstation address does not resolve
- **WHEN** reconnection attempts continue over several minutes
- **THEN** the delay between attempts stops growing at its bound rather than
  increasing without limit

### REQ-019: Sessions are authenticated

The groundstation MUST reject a session whose client does not present a valid
credential.

#### Scenario: An unauthenticated client connects

- **GIVEN** a groundstation configured with a credential
- **WHEN** a client connects without presenting it
- **THEN** the session is refused and no capability negotiation takes place

### REQ-020: The wire format is pinned by shared fixtures

Every message type MUST have a golden fixture in the shared contracts package,
and both the producing and the consuming implementation MUST be verified against
that same fixture.

#### Scenario: One side changes a field name

- **GIVEN** a pull request that renames a field in a message type on one side
  only
- **WHEN** the contract tests run
- **THEN** they fail, because the fixture no longer round-trips through both
  sides

### REQ-021: Detection geometry is resolution-independent

Positions in results MUST be expressed in normalised image coordinates rather
than pixels.

#### Scenario: The capture resolution changes

- **GIVEN** an app configured to send frames at one resolution
- **WHEN** the resolution is halved and the same scene is captured
- **THEN** the reported position of a detection is unchanged

## Design

### Topology

```
   ┌──────────────────┐                      ┌──────────────────┐
   │   Reachy Mini    │  frames ─────────▶   │  groundstation   │
   │   app = client   │  ◀───────  results   │   /v1/session    │
   └──────────────────┘                      └──────────────────┘
```

One connection, opened by the robot, carrying both directions for its lifetime.

### Session lifecycle

1. The client connects and presents its credential.
2. The client sends its capability offer; the groundstation replies with the
   agreed set.
3. Capability-specific traffic flows in both directions.
4. On disconnection the client re-establishes and negotiates again. Negotiation
   is not resumed or cached — a groundstation that restarted with a different
   capability set is a normal case.

### Message shape

Frames travel as binary payloads with a small header carrying the sequence
number and capture timestamp. Results travel as structured messages naming the
capability that produced them, the sequence number they answer, and the payload
for that capability.

Multiplexing by capability is what keeps future work additive. A new capability
introduces new message types under its own name; it does not change the session,
the framing, or anything an existing capability sends.

### Coordinates

Positions are normalised to the range [-1, 1] on both axes, with the origin at
the image centre. This matches what the robot's motion layer consumes and makes
REQ-021 hold across the resolution changes that capture-side tuning produces.

### Clocks

Frame timestamps are taken from a monotonic clock on the capture side and
carried through unchanged. The consumer compares the returned timestamp against
its own monotonic clock, so freshness never depends on the two machines agreeing
on wall-clock time — which, on a robot whose clock is set over the network at
boot, they sometimes do not.

### Decision Records

#### One session instead of per-request connections

A cold handshake to the robot measured 378 ms at p50, so per-request connections
put results between 22 ms and 1241 ms; reuse brought them to 18–85 ms. The
earlier fix achieved reuse by writing both halves carefully, which left the
expensive path available to any future caller. Making the session the only
transport removes that possibility, and removes the robot's inbound listener as
a side effect. Rejected alternative: keeping request-per-result with mandatory
connection reuse, which preserves the hazard the measurement exposed.

#### Frames travel up rather than being pulled down

The robot's media layer can produce hardware-encoded JPEG directly, so there is
nothing to gain from a second component scraping a stream off the robot. Pushing
frames on the session the robot already holds removes the pull path, the second
connection, and the inbound listener together. Rejected alternative: the
groundstation pulling the stream, which is the predecessor topology and needs
the robot to serve.

#### Capability negotiation from the first commit

The groundstation is named for its relationship to the robot rather than for
vision specifically, because it is expected to host other heavy computation.
Negotiating capabilities at session start is what makes that additive later. It
is close to impossible to retrofit onto a deployed wire format on hardware that
has to be reached physically, and it is roughly a day's work now. Rejected
alternative: a detection-shaped session with other message types added beside it
later.

## Constraints

- The link runs over WLAN measured at 100–170 ms idle round-trip with occasional
  700 ms spikes. Every timeout and staleness window is chosen against that, not
  against a wired network.
- The robot has four cores and is simultaneously running motion control and
  audio. Encoding and transport on the robot side stay well inside one core.
- Frames are already JPEG-compressed by capture hardware, so the protocol
  carries them as opaque bytes rather than re-encoding.

## Open Questions

- **Whether results are ever pushed without a corresponding frame.** A future
  capability that runs on the groundstation's own schedule — a periodic model
  refresh, say — would need unsolicited messages. The current shape allows them
  structurally, but nothing defines their semantics. Current default: every
  result answers a frame.
- **Whether credentials rotate without restarting the session.** Rotation
  currently means reconnecting, which is cheap given automatic reconnection.
  Current default: reconnect.

## References

- [architecture](../architecture/) — repository-level conventions
- [groundstation](../groundstation/) — the server side of this contract
- [ha-satellite](../ha-satellite/) — the robot-side client
- [reachyctl](../reachyctl/) — `probe` is a second client of this contract
- [benchmarks](../benchmarks/) — the latency baseline this protocol is measured against

## Changelog

| Date | Change | Document |
|------|--------|----------|
| 2026-08-20 | Initial spec created | — |
