# 0013: Satellite behaviour, settings and packaging

## Summary

Implement the satellite's behaviour layer, its settings interface, its
configuration handling and its packaging — completing the application and making
it deployable to a robot.

**Spec:** [HA Satellite](../specs/ha-satellite/)
**Status:** complete
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

- [x] Implement the behaviour layer
  - [x] Voice-pipeline state machine, pure, time passed in
  - [x] Face-tracking controller mapping normalised centres to gaze targets
  - [x] Idle behaviour
  - [x] Staleness handling returning the head to neutral
  - [x] Lint rule forbidding adapter imports from `behaviour/`
  - [x] Exhaustive transition and mapping coverage using the 0012 fakes
- [x] Implement configuration
  - [x] Settings with prefix validation and unknown-variable rejection
  - [x] Announced identity as a required value with no derived default
  - [x] Boot-time resolved-configuration logging
- [x] Implement the application entry point
  - [x] Wire ports to adapters
  - [x] Main loop and daemon lifecycle integration
  - [x] Graceful shutdown: stop motion, release media, exit
- [x] Implement the settings interface
  - [x] Serve the settings page from the application
  - [x] Read and write every operator-facing setting
  - [x] Show the resolved configuration, including defaults, with secrets
        reported as set or unset rather than by value
- [x] Package and publish
  - [x] Register the daemon application entry point
  - [x] Include wake-word assets and sounds as package data
  - [x] Publish the wheel to GitHub Releases on a version tag
  - [x] Deployment documentation stating the identity-pinning requirement

## Open Questions

- [x] **Which movements represent listening, processing and responding.**
      Resolved: the **antennas** carry the signature and the three differ in the
      *kind* of motion rather than in its size, because a person two metres away
      reads motion long before position.

      | State | The antennas |
      |---|---|
      | Listening | both raised, and **still** |
      | Processing | **counter-rotating** — one rises as the other falls |
      | Responding | both **bobbing together**, at twice the rate |

      Still, opposed, together. The head carries the same three more quietly —
      slightly raised while listening, lowered and drifting while processing,
      nodding while responding — but only when there is no face to follow, since
      face tracking wins the head. That is why the antennas rather than the head
      are what the requirement is satisfied on.

      Two more states got movements for the same reason: muted folds the
      antennas down and holds, and disconnected droops both and lowers the head.
      Idle is stillness while somebody has recently been about, and a slow
      symmetric sway once the room has been empty for `idle_seconds`.

      The choice is testable rather than a matter of taste: the suite asserts
      that listening's trajectory is constant, that processing's is
      anti-symmetric, that responding's is symmetric and reverses more often,
      and that no two of the three coincide. A test asserting on the constants
      would have passed whatever they were.

- [x] **Whether the settings interface writes changes that survive a reinstall.**
      Resolved **without** taking a dependency on 0009 — and re-examined once
      0009 merged, which confirmed the answer rather than changing it.

      Changes are written to `settings.json` in the application's state
      directory — outside the wheel, so reinstalling the application keeps them.
      They do not survive re-imaging the robot, and the deployment
      documentation says so: anything that has to survive that belongs in the
      daemon's environment, which is 0009's and 0010's managed region.

      **The re-examination, and why writing through was rejected.** 0009 landed
      [`docs/ops/managed-daemon-environment.md`](../ops/managed-daemon-environment.md),
      which makes the drop-in's ownership explicit: the file is owned **in full**,
      and `reachyctl config apply` rewrites it whole. So a settings page that
      wrote through to it would have its work discarded by the next apply —
      silently, and with no way for the operator to tell that had happened. It
      would also need root and a `systemctl restart` of the daemon, which would
      kill the very application serving the request. Writing through is
      therefore not a better answer that this change declined to implement; it
      is an answer the drop-in's contract rules out. The two writers stay
      separate, which is what lets both keep working.

      One consequence is now documented rather than left implicit: because the
      overrides layer sits above the environment, a setting changed on the page
      wins over a later `reachyctl config apply` of that same setting. The page
      reports the layer for each value, so an override is visible as the reason
      a `reachyctl` change did not take, and saving the value back to what the
      environment says removes the override.

      **A second thing 0009 landed did change the code**: `ROBOT_SETTINGS`, the
      one declaration of the robot's setting vocabulary. Three of its names fall
      under this application's prefix without being settings it reads
      (`FRAME_INTERVAL_MS`, `JPEG_QUALITY`, `RESULT_STALENESS_SECONDS`). Under
      REQ-009's unrecognised-variable rule as first implemented, an operator
      running `reachyctl config apply` with the documented vocabulary got a
      satellite that refused to start. `config.declared_elsewhere` reads
      `ROBOT_SETTINGS` and treats those as recognised-but-unread: accepted,
      and reported in the startup log, on the settings page and at `/config`, so
      "why does this variable do nothing?" has a written answer. A name in
      neither set is still fatal.

      The layer sits **above** the environment rather than below it, and that is
      what makes REQ-049 true rather than approximately true: a layer the
      environment overrode would silently ignore a change to any setting anybody
      had ever exported. The consequence is that an override can only be undone
      by writing another one, which is why four settings are readable on the page
      and not writable there — `state_dir`, which names the directory the file
      lives in, and the three that decide whether the page is served and where.
      A page able to switch itself off is a page nobody can switch back on. The resolved configuration records which layer supplied
      each value, so the precedence is visible on the page rather than
      surprising, and saving a value back to what the environment says removes
      the override rather than pinning a duplicate of it.

      The write-through to the environment the original lean described is
      **deferred**, deliberately. It would have made this change depend on 0009,
      and the property it buys — surviving a re-image — is one `reachyctl` is
      the right owner of. Nothing here forecloses it: the overrides file is one
      layer of three, and a fourth fed from the managed region drops in beside
      it.

## Completion notes

- **`complete` here means implemented, covered and gated in continuous
  integration.** It does not mean validated on a robot: the end-to-end session
  is the last item below and it has not happened yet. No runner has a Reachy
  Mini, and the change document that sequences that session is
  [0015](./0015-docs-and-runbooks.md).
- **The spec is registered in `.duvet/config.toml`.** All eleven of its
  requirements are annotated and traced, which is what makes the traceability
  gate mean something for this component.
- **Purity is a build failure, not a convention.** `just lint-behaviour-boundary`
  bans `behaviour/` from importing an adapter, the vendored protocol layer, the
  entry point, the settings interface or the SDK; greps for the dynamic imports
  and the clock reads ruff cannot see; and proves every half of that still fires
  by running it against a committed fixture that breaks all of them.
- **The wheel's size is recorded** by `just wheel-size <wheel>`, which change
  0009 introduced in the same JSON shape `just image-size` emits for the
  container image — so change 0014 reads one format rather than three. This
  change adds the satellite wheel to `just wheels`, so one command builds
  everything a release carries and one recipe measures all of it. At the version
  this change landed at the satellite wheel is 634,604 bytes / 619.7 KiB, of
  which the wake-word models are the largest part.
- **⚠️ The settings interface is unauthenticated, and that is an escalation
  rather than a decision this change took.** The spec asks for a settings
  interface and says nothing about authenticating it, and the surfaces beside it
  are open too — the ESPHome API announces `uses_password=false`, and the
  daemon's own dashboard is reachable by anything that can reach the robot. So
  the trust boundary is the network, and the deployment runbook says so plainly
  instead of leaving it to be inferred.

  What this change does close is the exposure that needs no peer on that network:
  a state-changing request a browser reports as coming from another site is
  refused, so a page an operator visits cannot stop the robot or replace its
  credential. Every response is `no-store`, and the interface can be switched off
  entirely.

  Whether the robot should authenticate its own write surface is a design
  question for the spec, and inventing a scheme here — with a credential to
  distribute and a recovery path when it is lost — would be answering it in the
  wrong place.
- **⚠️ The wheel is published only when the release tag was created with
  `RELEASE_PLEASE_TOKEN`.** GitHub raises no workflow run for a tag the default
  `GITHUB_TOKEN` created, and the release job deliberately falls back to it so
  that version derivation works before that secret exists. This is the condition
  `images.yml`'s publish job has been under since change 0006, recorded here
  rather than worked around so the two publishing jobs keep one shape. The
  workflow takes a manual dispatch, which publishes for a tag by hand until the
  secret exists.
- **The settings interface stops the application; it does not restart it.** The
  daemon's application manager marks a cleanly-exited application `done` and
  leaves it stopped, so a page promising a restart would promise something
  nothing performs. The button says *Stop*, the page says where to start it
  again, and REQ-049's "without a shell" still holds because the robot dashboard
  is a web interface too.
- **`face_tracking_enabled` is a restart-required setting**, not a live one.
  Switching it on means building a detector — opening a robot-link session, or
  loading a model onto the robot's own cores — which happens once in
  `build_application`. The behaviour layer can adopt the flag in isolation, which
  is exactly why the page must not claim it applied.
- **The groundstation address is refused if it carries a credential.** It is not
  a secret setting, so it reaches the boot log, the settings page and `/config`
  by value, and no redactor can remove a value it was never given. The rule is
  `reachy_session_client.validate_session_url`, called rather than restated, and
  it is applied before the first line of resolved configuration is emitted.
- **Every released wheel's digest is published beside it.** The release job
  writes a `SHA256SUMS` file into the release and the deployment runbook has the
  operator check it before installing — a wheel goes into the environment the
  daemon runs applications from, so it runs with the daemon's access.
- **A mute outlives a disconnection.** Both states are sticky, and the overlap
  is where sticky stopped being enough: a muted robot that lost Home Assistant
  and got it back showed *idle*, because the reconnection emits `IDLE` and the
  vendored layer never re-announces the mute. The machine remembers the mute
  underneath the disconnection and restores it, so the only thing that forgets
  it is being unmuted. It is the failure the sticky-muted rule exists to
  prevent, reached by another route.
- **The settings page's structure is a gate, not a habit.** The rendered page is
  parsed in the test suite and any block-level element inside a `<p>`, any form
  inside a form and any unclosed tag fails the run. A browser meeting `<p><form>`
  closes the paragraph implicitly and builds a DOM the template does not
  describe, and a page nobody can predict from its source is one nobody can
  reason about.
- **The purity boundary bans `asyncio` from `behaviour/`, not just
  `asyncio.sleep`.** A rule about the qualified spelling is a rule about one
  spelling: `from asyncio import sleep` reads no such attribute and would have
  slipped past, and so would a clock read through `loop.time()`. Banning the
  module closes the class, and it costs nothing — the layer is synchronous by
  construction.
- **`just wheel-verify` is the packaging gate**, and this change extends change
  0009's recipe rather than adding a second one: it now also asks the satellite
  wheel the question that is specific to it. A wheel that builds is not a
  wheel that works: a missing `reachy_mini_apps` entry point installs perfectly
  and never appears in the daemon's list, and an asset shipping without its
  registry entry ships somebody else's file under terms nobody agreed to.
  Neither is visible to a successful build, and nothing reaches a release until
  both are checked.
- **Deployment is documented** in
  [`docs/ops/satellite-deployment.md`](../ops/satellite-deployment.md), which
  leads with the identity-pinning hazard because that is the one thing a
  deployment can get irreversibly wrong.
- **Pending the end-to-end session.** Everything here is exercised against
  change 0012's fakes and, for the assets and the listening socket, against the
  real files and a loopback port. What no runner can check is the robot: that
  the three antenna movements read as distinct across a room, that the head
  tracks smoothly at the tuned deadzone and smoothing, that the daemon's
  dashboard links to the settings page, and that Home Assistant discovers the
  device under the announced identity and keeps its history across the upgrade.

## References

- Spec: [HA Satellite](../specs/ha-satellite/)
- Related changes: [0012-satellite-ports-and-adapters](./0012-satellite-ports-and-adapters.md),
  [0009-reachyctl-deploy-and-config](./0009-reachyctl-deploy-and-config.md),
  [0015-docs-and-runbooks](./0015-docs-and-runbooks.md)
