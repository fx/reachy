# 0007: reachyctl skeleton and probe

## Summary

Create the `reachyctl` command surface and implement `probe`: a real robot-link
session client that feeds the groundstation live or recorded frames with no
robot involved.

**Spec:** [reachyctl](../specs/reachyctl/)
**Status:** draft
**Depends On:** 0003, 0004

## Motivation

The groundstation lands in phase 1 and the robot application does not arrive
until phase 3. Without something that speaks the session protocol, the service
would sit for two phases with its transport exercised only by its own test
suite.

The alternative considered and rejected was a compatibility shim letting the
existing stack point at the new service. That would mean building the
pull-and-post topology this design deletes, and it would make the old path the
exercised one while the session stayed untested in anger — precisely backwards.

`probe` gets the same benefit in the right shape, and unlike a shim it is worth
keeping: it stays as the diagnostic that answers whether a tracking problem is
the groundstation producing bad results or the robot applying good ones badly.

## Requirements

### Testing Requirements

This change MUST satisfy the project's standing testing rules (see
[Testing conventions](../specs/architecture/index.md#testing-conventions)). CI
enforces these as merge gates:

- Tests run with `pytest`, with async strict mode enabled.
- Unit tests MUST perform no input or output.
- Integration tests MUST exercise the real transport in-process rather than
  mocking it.
- Contract tests MUST run the golden fixtures from `reachy-contracts`.
- Coverage MUST be gated on the diff rather than on the whole tree.
- Type checking MUST run in strict mode for new modules.
- A lint or type suppression MUST carry the rule identifier and a justification.

Skipping or weakening any of these rules to land the PR MUST be treated as a bug
in the PR, not in the rule.

### Functional requirements

The [reachyctl spec](../specs/reachyctl/) owns the tool's observable behaviour,
particularly
[REQ-057](../specs/reachyctl/index.md#req-057-the-probe-exercises-the-real-session-protocol)
on protocol sharing and
[REQ-058](../specs/reachyctl/index.md#req-058-output-is-machine-readable-on-request)
on structured output. Those scenarios are this change's acceptance criteria.
What implementing them requires of this change:

- The session client is implemented **once**, in a place both `probe` and the
  robot adapter in 0012 import. It does not live inside the CLI. A second
  implementation for testing would prove nothing about the protocol, which is
  the entire point of the requirement.
- Commands are non-interactive by default and render richly only when attached
  to a terminal, so output is scriptable without a flag.
- Structured output and the exit status are established here as conventions
  every later command follows, rather than being retrofitted per command.
- `probe` accepts recorded frames from a directory and live frames from a local
  camera, and reports per-frame results with timings.
- Credential handling is established here under
  [REQ-059](../specs/reachyctl/index.md#req-059-secrets-are-never-written-to-output),
  including the verbose and error paths where secrets usually escape.

## Design

### Approach

The session client lives in `packages/reachy-contracts` alongside the types it
speaks, or in a sibling client package if that turns out to couple the contracts
package to a transport. Either way it is a workspace member, not CLI code — the
placement decision is deliberately left to implementation, but the constraint
that there is exactly one implementation is not.

The CLI itself is a thin command layer over it, which is what makes the later
commands in 0008 and 0009 additive.

### Decisions

- **Decision**: `probe` instead of a compatibility shim.
  - **Why**: A shim rebuilds the topology being deleted and makes it the
    exercised path for two phases. `probe` exercises the real one and survives
    as a diagnostic rather than carrying a deletion date.
  - **Alternatives considered**: A legacy pull-and-post path behind a flag in
    the groundstation.
- **Decision**: One session client, shared by `probe` and the robot.
  - **Why**: A test client that behaves similarly to the real one tests the test
    client.
  - **Alternatives considered**: A lightweight probe-specific client, easier to
    write and worthless as evidence.
- **Decision**: Structured output conventions land in the first CLI change.
  - **Why**: Output format retrofitted command by command produces a tool where
    some commands are scriptable and others are not, and nobody can predict
    which.

### Non-Goals

- No `doctor` — 0008.
- No `deploy`, `config` or `app` — 0009.
- No robot interaction of any kind; this change never touches a robot.
- No `bench` implementation beyond reserving the command name.

## Tasks

- [ ] Implement the shared session client
  - [ ] Connection, credential presentation, capability negotiation
  - [ ] Frame submission with sequence numbers and monotonic timestamps
  - [ ] Result handling, including out-of-order and empty results
  - [ ] Automatic reconnection with bounded growing delay
  - [ ] Contract tests against the golden fixtures
- [ ] Create the CLI skeleton
  - [ ] Command registration, help, version
  - [ ] Structured-output convention and exit-status convention
  - [ ] Terminal-aware rendering, non-interactive by default
  - [ ] Credential loading with redaction on every output path
- [ ] Implement `probe`
  - [ ] Recorded-frame source from a directory
  - [ ] Live-frame source from a local camera
  - [ ] Per-frame result and timing reporting
  - [ ] Integration test running `probe` against an in-process groundstation

## Open Questions

- [ ] Whether the session client ships inside `reachy-contracts` or as its own
      member. Keeping it with the types avoids a member; separating it keeps a
      pure data package free of transport dependencies. Current lean: separate
      member, decided at implementation.
- [ ] Whether `probe` can record what it receives for later replay. It would
      make regressions reproducible; it is scope this change does not need.
      Current lean: defer.

## References

- Spec: [reachyctl](../specs/reachyctl/), [Robot Link](../specs/robot-link/)
- Related changes: [0004-groundstation-session](./0004-groundstation-session.md),
  [0008-reachyctl-doctor](./0008-reachyctl-doctor.md),
  [0012-satellite-ports-and-adapters](./0012-satellite-ports-and-adapters.md)
