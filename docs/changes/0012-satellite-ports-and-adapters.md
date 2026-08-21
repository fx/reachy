# 0012: Satellite ports and adapters

## Summary

Define the satellite's ports and implement the adapters behind them: audio and
motion over the robot daemon's media and control interfaces, remote perception
over the robot-link session, and local perception over the SDK's own detector.

**Spec:** [HA Satellite](../specs/ha-satellite/)
**Status:** complete
**Depends On:** 0007, 0011

## Motivation

The robot is one device on a desk. Anything that can only be tested on it is
effectively untested, so the boundary between decision-making and hardware
access is the single most important structural choice in this application.

It is also what makes 0011's vendored protocol layer tractable: the seams cut
there are filled here at one named interface each, rather than being threaded
back through the protocol code.

## Requirements

### Testing Requirements

This change MUST satisfy the project's standing testing rules (see
[Testing conventions](../specs/architecture/index.md#testing-conventions)). CI
enforces these as merge gates:

- Tests run with `pytest`, with async strict mode enabled.
- Unit tests MUST perform no input or output.
- Integration tests MUST exercise real transports in-process rather than mocking
  them.
- Contract tests MUST run the golden fixtures from `reachy-contracts`.
- Coverage MUST be gated on the diff rather than on the whole tree.
- Type checking MUST run in strict mode for new modules.
- A lint or type suppression MUST carry the rule identifier and a justification.

Skipping or weakening any of these rules to land the PR MUST be treated as a bug
in the PR, not in the rule.

Every port MUST ship with a fake implementation in the test support module, and
those fakes are the mechanism by which 0013's behaviour suite runs without
hardware. A port whose only implementation touches a device makes
[REQ-042](../specs/ha-satellite/index.md#req-042-decision-logic-is-free-of-input-and-output)
unachievable in the change that depends on it.

### Functional requirements

The [ha-satellite spec](../specs/ha-satellite/) owns the application's
behaviour, particularly
[REQ-043](../specs/ha-satellite/index.md#req-043-hardware-access-goes-through-the-daemons-media-layer)
on media access and
[REQ-047](../specs/ha-satellite/index.md#req-047-detection-source-is-selectable)
on detection source. Those scenarios are this change's acceptance criteria. What
implementing them requires of this change:

- Three ports: audio, motion, perception. Each is a narrow interface expressed
  in terms the behaviour layer uses, not in terms the SDK provides — a port that
  mirrors the SDK's shape leaks the SDK into the behaviour layer.
- The audio adapter satisfies both seams cut in 0011, going through the daemon's
  media interface rather than opening devices, so it does not contend with the
  daemon for the microphone array or the speaker.
- The groundstation adapter reuses the session client from 0007. There is one
  implementation of the protocol and this is a second consumer of it, not a
  second implementation.
- The local perception adapter wraps the SDK's own detector, which runs the same
  YuNet weights the groundstation runs — so switching source changes latency and
  CPU cost but not accuracy.
- Detection source selection covers remote, local, and remote-with-local-
  fallback, with remote as the default.
- Frames are taken as hardware-encoded JPEG from the media interface and passed
  through without re-encoding.

## Design

### Approach

`ports.py` declares the three interfaces. `adapters/` implements them. Nothing
in `adapters/` is imported by the behaviour layer directly — wiring happens in
`main.py`, which is the only module that knows which adapter is in use.

The perception port hides the source selection entirely: the behaviour layer
asks for detections and does not know whether they came over a session or out of
a local model. Fallback is therefore a property of the adapter, not a branch in
the behaviour.

### Decisions

- **Decision**: Ports are expressed in behaviour-layer terms, not SDK terms.
  - **Why**: A port shaped like the SDK's interface means every SDK change
    reaches the behaviour layer, and the fakes become SDK emulators rather than
    simple test doubles.
  - **Alternatives considered**: Thin pass-through ports, which are quicker to
    write and put the coupling back.
- **Decision**: Source selection lives inside the perception adapter.
  - **Why**: Fallback is a sourcing concern. Putting it in the behaviour layer
    would mean the state machine has opinions about transport failure.
  - **Alternatives considered**: Explicit source handling in behaviour, which
    spreads one decision across two layers.
- **Decision**: Remote is the default source.
  - **Why**: The measured robot CPU with detection offloaded was 1.52 of four
    cores; local detection saturated it. The robot is also running motion
    control and audio.
  - **Alternatives considered**: Local by default, which is simpler to deploy
    and costs the CPU the rest of the application needs.
- **Decision**: Frames pass through as encoded JPEG.
  - **Why**: The media interface produces hardware-encoded JPEG, and decoding on
    the robot to re-encode for transport would spend robot CPU to achieve
    nothing.

### Non-Goals

- No behaviour logic or state machine — 0013.
- No settings interface or packaging — 0013.
- No changes to the vendored protocol beyond satisfying its seams.
- No direction-of-arrival handling; recorded as an open question in the
  [spec](../specs/ha-satellite/index.md#open-questions).

## Tasks

- [x] Define the ports
  - [x] Audio port covering capture and playback
  - [x] Motion port covering head, antennas and gaze
  - [x] Perception port covering detections and their freshness
  - [x] Fake implementations of all three in test support
- [x] Implement the audio adapter
  - [x] Capture through the daemon media interface at the pipeline's sample rate
  - [x] Playback through the daemon media interface
  - [x] Satisfy both seams cut in 0011
  - [x] Verify no contention with the daemon for the devices
- [x] Implement the motion adapter
  - [x] Head pose and gaze targeting from normalised coordinates
  - [x] Antenna control
  - [x] Release on shutdown
- [x] Implement the perception adapters
  - [x] Groundstation adapter reusing the 0007 session client
  - [x] Local adapter wrapping the SDK detector
  - [x] Source selection: remote, local, remote-with-fallback
  - [x] Frame capture as encoded JPEG, passed through unmodified
  - [x] Contract tests against the golden fixtures

## Open Questions

- [x] **What the fallback trigger should be. Resolved: session loss, not
      staleness.** The two are different failures and keeping them
      distinguishable is worth more than making one signal do both jobs. A
      session that has *dropped* means nothing is coming until something
      changes, so a detector on the robot is better than no detector. Results
      that have gone *stale* while the session is up mean the groundstation is
      there and has stopped answering, which is
      [REQ-048](../specs/ha-satellite/index.md#req-048-the-head-returns-to-neutral-when-tracking-data-goes-stale)'s
      case and whose answer is a neutral head — an honest signal that something
      upstream stopped. Starting a second detector on the strength of a stall
      that may last one frame would spend the cores the rest of the application
      needs in order to hide it.
- [x] **Whether frame capture rate is fixed or adapts to observed round-trip
      time. Resolved: fixed, at ten frames per second.** An adaptive rate is a
      control loop with its own failure modes, and it would be tuned against a
      network nobody has instrumented yet. A constant is honest about being a
      guess where a control loop would not be, and 0014 is where the number
      gets measured.

## References

- Spec: [HA Satellite](../specs/ha-satellite/), [Robot Link](../specs/robot-link/)
- Related changes: [0011-satellite-esphome-vendoring](./0011-satellite-esphome-vendoring.md),
  [0013-satellite-behaviour-and-ui](./0013-satellite-behaviour-and-ui.md),
  [0007-reachyctl-probe](./0007-reachyctl-probe.md)
