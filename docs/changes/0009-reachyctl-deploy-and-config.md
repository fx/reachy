# 0009: reachyctl deploy, config and app

## Summary

Implement the commands that operate a robot: building and installing the
satellite wheel with verification, managing configuration with preview, and
controlling the application lifecycle.

**Spec:** [reachyctl](../specs/reachyctl/)
**Status:** complete
**Depends On:** 0002, 0008

## Motivation

Deploying to the robot currently means a remote copy, a package install and a
service restart typed by hand, with no confirmation that the result is what was
intended. That produced a specific failure worth designing against: a package
that installs successfully into an environment the running daemon is not using
looks identical to success at every step.

This change is deliberately sequenced before the satellite rewrite, because it
is the tooling used *during* that rewrite. That ordering only works if `deploy`
is defined over **a wheel** rather than over the finished satellite: it builds a
named workspace member or accepts a path, so it neither waits for 0013 nor
assumes anything about what the wheel contains.

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

Preview mode MUST be tested by asserting the target is unchanged after a preview
run, not by asserting the command printed a diff. The guarantee is that nothing
happened, and only an after-state assertion tests that.

### Functional requirements

The [reachyctl spec](../specs/reachyctl/) owns the observable behaviour —
[REQ-051](../specs/reachyctl/index.md#req-051-deployment-verifies-its-own-result)
on deployment verification,
[REQ-052](../specs/reachyctl/index.md#req-052-configuration-changes-can-be-previewed-without-being-applied)
on preview, and
[REQ-053](../specs/reachyctl/index.md#req-053-configuration-values-are-validated-before-they-are-sent)
on validation. Those scenarios are this change's acceptance criteria. What
implementing them requires of this change:

- This change completes the tool's operator-facing surface, so it is also the
  one that **publishes the `reachyctl` wheel** to GitHub Releases — the artifact
  the [architecture spec](../specs/architecture/index.md#versioning-and-distribution)
  requires but that no earlier change owned. That is why it depends on 0002 for
  the repository-wide version.
- `deploy` operates on a wheel identified either by workspace member name or by
  path. It does not hard-code the satellite, so it is implementable and testable
  before 0013 exists and remains useful for any application the daemon can run.
- Deployment verifies by asking the daemon what version is running after the
  restart, not by trusting the install step's exit status.
- Configuration values are validated locally against the shared contracts before
  the robot is contacted, so an invalid value costs no round trip and leaves no
  partial state.
- The daemon environment is treated as a fully owned managed region, matching
  [provisioning REQ-063](../specs/provisioning/index.md#req-063-the-managed-configuration-is-fully-owned)
  — removing a setting removes it from the robot rather than leaving it behind.
- Applying daemon environment changes requires a daemon restart, and the command
  says so before doing it, because it interrupts whatever the robot is doing.
- Log access streams from the robot's journal, filtered to the application.
- Every command reuses the check registry from 0008 rather than reimplementing
  reachability or version probes.

## Design

### Approach

The robot is reached in-process over its remote-access and daemon interfaces
rather than by invoking command-line clients, so failures arrive structured and
progress reporting reflects real state.

`deploy` is a sequence of verifiable steps — build, transfer, install, restart,
start, verify — each reporting progress, with the final verification being the
one that decides success.

Configuration is modelled as a desired state compared against the robot's
current state, which gives preview and idempotence from the same code path
rather than as two behaviours.

### Decisions

- **Decision**: Deployment verifies the running version rather than the install.
  - **Why**: The predecessor's characteristic failure was an install that
    succeeded into an unused environment. Exit status cannot distinguish it.
  - **Alternatives considered**: Leaving verification to `doctor`, which relies
    on the operator remembering to run it.
- **Decision**: Validation happens locally, before contacting the robot.
  - **Why**: A round trip that ends in rejection is slow on a link measured at
    100–170 ms idle, and a rejection partway through a multi-step apply leaves
    partial state.
  - **Alternatives considered**: Validating on the robot, which is authoritative
    and much later.
- **Decision**: The managed environment region is owned wholesale.
  - **Why**: Appending settings without pruning means removing one from the
    declaration leaves it on the robot, and the robot diverges from the thing
    that describes it.
- **Decision**: `deploy` is defined over a wheel, not over the satellite.
  - **Why**: Hard-coding the satellite would make this change depend on 0013,
    inverting the build order and removing the reason `reachyctl` is sequenced
    early — that it is the tooling used during the rewrite. A wheel-shaped
    command is also testable against a trivial fixture wheel, with no
    application at all.
  - **Alternatives considered**: Depending on 0013 and moving the CLI into phase
    three, which delays every diagnostic until the largest change has landed.
- **Decision**: No rollback.
  - **Why**: Retaining previous wheels costs space on a device that has little,
    and redeploying a known-good version is the recovery path.
  - **Alternatives considered**: Version retention on the robot, recorded as an
    open question in the [spec](../specs/reachyctl/index.md#open-questions).

### Non-Goals

- No provisioning — 0010 owns durable machine state, and `reachyctl provision`
  wraps it rather than duplicating it.
- No rollback or version retention.
- No `bench` implementation — 0014.
- No network configuration management.

## Tasks

- [x] Build a fixture wheel for testing deployment with no application present
- [x] Implement robot access
  - [x] In-process remote shell and file transfer
  - [x] Daemon interface client reusing the 0008 check registry
  - [x] Structured error reporting for both
- [x] Implement `deploy`
  - [x] Build a wheel from a named workspace member, or accept one by path
  - [x] Transfer and install into the robot's application environment
  - [x] Restart the daemon and start the application
  - [x] Verify the running version and fail on mismatch
  - [x] Live progress reporting across the step sequence
- [x] Implement `config`
  - [x] `get` reading effective configuration from the robot
  - [x] `diff` comparing declared against actual
  - [x] `set` and `apply` with local validation against the contracts
  - [x] Preview mode, with a test asserting the robot is unchanged after it
  - [x] Managed-region ownership including removal of withdrawn settings
  - [x] Explicit warning before a daemon restart
- [x] Implement `app`
  - [x] `start`, `stop` via the daemon interface
  - [x] `logs` streaming from the robot journal, filtered to the application
- [x] Publish the tool
  - [x] Publish the `reachyctl` wheel to GitHub Releases on a version tag, using
        the repository-wide version from 0002
  - [x] Record the wheel's size as a release output, for 0014 to gate on
  - [x] Verify the published wheel installs and runs as a standalone tool

## Open Questions

- [x] **Whether `deploy` should refuse to run when the robot is mid-conversation.**
      Resolved as the lean recorded here: it warns and does not refuse.
      Refusing needs application state the daemon does not expose — "is somebody
      talking to it right now" is not a thing that can be asked — so the
      alternative to warning is not safety, it is a `--force` flag that
      everybody types by reflex. The deploy says the application is running
      before it does anything, and says the restart is about to happen before
      the restart, which is the moment in which interrupting is still possible.
- [x] **Whether wheel transfer should be incremental.** Resolved as the lean
      recorded here: the wheel is transferred whole, in one step, over one
      connection. Nothing this repository ships puts models in a wheel — they
      belong to the groundstation's image — so the artifact is measured in tens
      of kilobytes rather than in hundreds of megabytes, and an incremental
      transfer would be machinery with nothing to save. Revisit if a wheel ever
      carries weights.

## Completion notes

- **`deploy` is defined over a wheel**, identified by `--member` to build or
  `--wheel` to send. It neither waits for 0013 nor assumes anything about what
  the wheel contains, and the deploy sequence is exercised end to end against a
  fixture wheel with no application in it.
- **The daemon control surface is the one provisional piece.** The module the
  daemon's start/stop/status verbs are reached through is
  `RobotLayout.daemon_control`, defaulting to `reachy_mini.apps`, so meeting a
  robot that spells it differently is `--daemon-control` rather than a release.
  Confirming it is the first thing the deferred hardware session does.
- **The managed region's exact shape is documented** in
  [`docs/ops/managed-daemon-environment.md`](../ops/managed-daemon-environment.md),
  which change 0010's Ansible `daemon_env` role is written against. A contract
  test renders the tool's own output and compares it with the block in that
  document, so the two cannot drift.
- **The wheel size is recorded** by `just wheel-size <wheel>`, which emits one
  JSON object on standard output in the same shape as `just image-size`. The
  release workflow writes one per wheel, into the log, the job summary and an
  uploaded artifact named `reachyctl-wheel-size`.
- **`doctor` gained a robot.** The daemon-side checks 0008 left permanently
  skipped now run when `--robot` is given, which is what makes REQ-054's first
  scenario reachable at all.

## References

- Spec: [reachyctl](../specs/reachyctl/)
- Related changes: [0008-reachyctl-doctor](./0008-reachyctl-doctor.md),
  [0010-provisioning-ansible](./0010-provisioning-ansible.md),
  [0013-satellite-behaviour-and-ui](./0013-satellite-behaviour-and-ui.md)
