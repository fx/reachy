# 0013: Satellite behaviour, settings and packaging

## Summary

Implement the satellite's behaviour layer, its settings interface, its
configuration handling and its packaging — completing the application and making
it deployable to a robot.

**Spec:** [HA Satellite](../specs/ha-satellite/)
**Status:** draft
**Depends On:** 0002, 0012

## Motivation

This is where the application becomes a thing a person interacts with: the robot
looks at you while you talk to it, reacts to the conversation, and appears in
Home Assistant as a device with entities.

It also carries the one migration hazard in the project. Home Assistant keys an
ESPHome device on the identity it announces; change that identity and Home
Assistant registers a new device, entities acquire suffixed identifiers, history
detaches, and every automation and dashboard card referencing the old
identifiers silently stops matching. Since this application replaces one with a
different package name, that risk is live in this change specifically.

## Requirements

### Testing Requirements

This change MUST satisfy the project's standing testing rules (see
[Testing conventions](../specs/architecture/index.md#testing-conventions)). CI
enforces these as merge gates:

- Tests run with `pytest`, with async strict mode enabled.
- Unit tests MUST perform no input or output.
- Integration tests MUST exercise real transports in-process rather than mocking
  them.
- Coverage MUST be gated on the diff rather than on the whole tree.
- Type checking MUST run in strict mode for new modules.
- A lint or type suppression MUST carry the rule identifier and a justification.

Skipping or weakening any of these rules to land the PR MUST be treated as a bug
in the PR, not in the rule.

The behaviour layer is where coverage matters most, because it is the only part
of the application that can be exercised exhaustively without hardware. Every
state transition and every event-to-motion mapping MUST be covered, using the
fakes from 0012.

### Functional requirements

The [ha-satellite spec](../specs/ha-satellite/) owns the application's
behaviour, particularly
[REQ-040](../specs/ha-satellite/index.md#req-040-the-announced-device-identity-is-configuration)
on device identity,
[REQ-042](../specs/ha-satellite/index.md#req-042-decision-logic-is-free-of-input-and-output)
on I/O-free logic,
[REQ-046](../specs/ha-satellite/index.md#req-046-voice-pipeline-state-is-expressed-through-movement)
on movement, and
[REQ-048](../specs/ha-satellite/index.md#req-048-the-head-returns-to-neutral-when-tracking-data-goes-stale)
on staleness. Those scenarios are this change's acceptance criteria. What
implementing them requires of this change:

- The announced Home Assistant identity is a configuration value with no
  default derived from the package name, the host name, or any other value that
  changes when the software is repackaged. Deployment documentation states that
  it is pinned to whatever the existing installation announces.
- The behaviour layer is pure: events and detections in, motion intents out, no
  imports of adapters, no clock reads, no sleeps. Time is passed in. A lint rule
  enforces the import restriction.
- Configuration implements
  [architecture REQ-009](../specs/architecture/index.md#req-009-configuration-is-validated-and-self-reporting):
  an unrecognised variable under the application's prefix fails startup, and the
  resolved configuration is logged at boot and exposed by the settings
  interface. This is the direct remedy for the predecessor bug where every
  environment override was inert because the function reading them was never
  called.
- The wheel registers the daemon application entry point, so installing it is
  sufficient for discovery.
- Shutdown on the daemon's termination signal stops motion, releases media, and
  exits.
- Publishing is a wheel on GitHub Releases. No Hugging Face Space is created or
  referenced.

## Design

### Approach

`behaviour/` holds a state machine over voice-pipeline events and a tracking
controller over detections, both pure. `main.py` wires ports to adapters and
runs the loop. `web/` serves the settings interface the daemon links to.

Purity is what makes the application testable at all, so it is enforced rather
than intended: the behaviour package imports nothing from `adapters/`, takes
time as a parameter, and never sleeps.

### Decisions

- **Decision**: The announced identity has no derived default.
  - **Why**: A default derived from the package name would be correct on a fresh
    installation and silently destructive on an upgrade from the predecessor,
    which is the case that actually exists. Requiring it to be set makes the
    hazard visible at configuration time.
  - **Alternatives considered**: Deriving from the package name with an
    override, which puts the destructive behaviour on the happy path.
- **Decision**: Time is a parameter to the behaviour layer.
  - **Why**: Timing-dependent behaviour — staleness, movement duration, pipeline
    timeouts — is otherwise testable only by sleeping, which makes the suite slow
    and flaky.
  - **Alternatives considered**: An injected clock object, equivalent but
    heavier for a layer that only needs the current time.
- **Decision**: The head returns to neutral on staleness rather than holding.
  - **Why**: Holding the last pose looks like successful tracking of a person
    who has left. A neutral head is an honest signal that something upstream
    stopped.
  - **Alternatives considered**: Holding, or a slow drift to neutral — the
    latter is worth revisiting for how it looks, not for what it signals.
- **Decision**: Settings are changeable from the web interface, not only from
  the environment.
  - **Why**: The alternative is a remote shell for every adjustment, which is
    how the predecessor's configuration became unknowable.

### Non-Goals

- No direction-of-arrival head steering — open question in the
  [spec](../specs/ha-satellite/index.md#open-questions).
- No multi-room audio playback; out of scope per the spec.
- No Hugging Face Space publishing.
- No local speech-to-text, text-to-speech or intent handling — those stay in
  Home Assistant by design.

## Tasks

- [ ] Implement the behaviour layer
  - [ ] Voice-pipeline state machine, pure, time passed in
  - [ ] Face-tracking controller mapping normalised centres to gaze targets
  - [ ] Idle behaviour
  - [ ] Staleness handling returning the head to neutral
  - [ ] Lint rule forbidding adapter imports from `behaviour/`
  - [ ] Exhaustive transition and mapping coverage using the 0012 fakes
- [ ] Implement configuration
  - [ ] Settings with prefix validation and unknown-variable rejection
  - [ ] Announced identity as a required value with no derived default
  - [ ] Boot-time resolved-configuration logging
- [ ] Implement the application entry point
  - [ ] Wire ports to adapters
  - [ ] Main loop and daemon lifecycle integration
  - [ ] Graceful shutdown: stop motion, release media, exit
- [ ] Implement the settings interface
  - [ ] Serve the settings page from the application
  - [ ] Read and write every operator-facing setting
  - [ ] Show the resolved configuration, including defaults, with secrets
        reported as set or unset rather than by value
- [ ] Package and publish
  - [ ] Register the daemon application entry point
  - [ ] Include wake-word assets and sounds as package data
  - [ ] Publish the wheel to GitHub Releases on a version tag
  - [ ] Deployment documentation stating the identity-pinning requirement

## Open Questions

- [ ] Which movements represent listening, processing and responding. This is a
      design question best answered by watching the robot rather than by
      specifying it, and the spec deliberately requires only that the three be
      distinguishable.
- [ ] Whether the settings interface writes changes that survive a reinstall.
      Settings held only in the daemon environment survive; settings held in the
      application's own state may not. Current lean: write through to the
      environment via the same managed region 0009 and 0010 own — note that
      resolving it that way would add a dependency on 0009, which is why this
      change does not declare one while the question is open.

## References

- Spec: [HA Satellite](../specs/ha-satellite/)
- Related changes: [0012-satellite-ports-and-adapters](./0012-satellite-ports-and-adapters.md),
  [0009-reachyctl-deploy-and-config](./0009-reachyctl-deploy-and-config.md),
  [0015-docs-and-runbooks](./0015-docs-and-runbooks.md)
