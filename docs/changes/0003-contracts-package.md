# 0003: Contracts package

## Summary

Implement `packages/reachy-contracts`: the shared wire types for the robot link
session, the golden fixtures that pin them, and the schema generation that keeps
the published contract honest.

**Spec:** [Robot Link](../specs/robot-link/)
**Status:** draft
**Depends On:** 0001, 0002

## Motivation

Three components speak this protocol — the groundstation, the robot
application, and `reachyctl probe`. A wire format owned by one of them drifts
from the other two, and the failure shows up as a robot behaving oddly rather
than as a test going red.

Landing the contracts first also unblocks the groundstation and the CLI to be
built against a fixed target rather than against each other.

## Requirements

### Testing Requirements

This change MUST satisfy the project's standing testing rules (see
[Testing conventions](../specs/architecture/index.md#testing-conventions)). CI
enforces these as merge gates:

- Tests run with `pytest`, with async strict mode enabled.
- Unit tests MUST perform no input or output.
- Coverage MUST be gated on the diff rather than on the whole tree.
- Type checking MUST run in strict mode for new modules.
- A lint or type suppression MUST carry the rule identifier and a justification.

Skipping or weakening any of these rules to land the PR MUST be treated as a bug
in the PR, not in the rule.

This package is the one place where a coverage shortfall is least acceptable: it
has no I/O, no hardware dependency, and no excuse. It MUST reach full diff
coverage.

### Functional requirements

The [robot link spec](../specs/robot-link/) owns the protocol's observable
behaviour — capability negotiation, sequencing, staleness, coordinates, and the
fixture guarantee in
[REQ-020](../specs/robot-link/index.md#req-020-the-wire-format-is-pinned-by-shared-fixtures).
Those scenarios are this change's acceptance criteria. What implementing them
requires of this change:

- Types are declared once here and imported by every consumer. No consumer
  defines its own copy of a wire type, and a lint rule enforces the import
  direction.
- Golden fixtures are data files, not constructed in test code, so the same
  bytes can be fed to a producer and a consumer independently.
- Every message type round-trips: fixture to object to fixture, byte-identical.
- Schema generation writes into `docs/contracts/`, where the drift job from 0002
  compares it against the committed copy.
- Capability names and versions are values in the contract, so adding a
  capability does not change the negotiation types themselves.
- Normalised coordinates are a distinct type with validation, rather than a bare
  pair of floats, so an un-normalised value fails at the boundary instead of
  becoming a strange head movement.

## Design

### Approach

Message types are declared with a validating model library, which gives both
run-time validation and schema generation from one declaration. The dependency
on 0002 is the contract-drift job: this change is what gives that job something
to compare, so it edits a job 0002 created rather than adding its own.

The package splits into: the session envelope types (negotiation, framing,
errors), the per-capability payload types, and the fixture loader shared by the
consumers' test suites. The fixture loader lives here rather than in each
consumer, so all three exercise the fixtures identically.

### Decisions

- **Decision**: Fixtures are files, not factory functions.
  - **Why**: A factory shared between producer and consumer tests can be wrong
    in the same way for both, which is exactly the drift the fixtures exist to
    catch. A file is a third party to both.
  - **Alternatives considered**: Property-based generation, which is valuable
    for validation logic and cannot pin a byte-level wire format.
- **Decision**: Normalised coordinates are a validated type.
  - **Why**: The one geometry bug class this protocol is exposed to is a
    coordinate that was never normalised, and it is silent — it produces a
    plausible-looking head movement. Validating at the type boundary makes it
    loud.
  - **Alternatives considered**: Documenting the convention and checking it in
    the consumer, which is where the predecessor's coordinate handling already
    lived.
- **Decision**: Capability identity is data, not type structure.
  - **Why**: [Groundstation REQ-022](../specs/groundstation/index.md#req-022-capabilities-register-without-transport-changes)
    requires adding a capability without touching the transport. If the
    negotiation types enumerated capabilities, every new one would change them.

### Non-Goals

- No transport implementation — that is 0004.
- No detection logic; the perception payload types are declared here, and what
  fills them is 0005.
- No authentication mechanism beyond the credential's place in the envelope.

## Tasks

- [ ] Implement the session envelope types
  - [ ] Negotiation offer and agreement, with capability names and versions
  - [ ] Frame header: sequence number and monotonic capture timestamp
  - [ ] Result envelope keyed to a sequence number
  - [ ] Error and close types
- [ ] Implement the shared value types
  - [ ] Normalised coordinate type with validation and its tests
  - [ ] Perception payload types: face detection, gesture detection
- [ ] Build the fixture corpus
  - [ ] One golden fixture per message type, as data files
  - [ ] Fixture loader exported for consumer test suites
  - [ ] Round-trip test asserting byte-identical re-serialisation
- [ ] Wire schema generation
  - [ ] Generate JSON Schema into `docs/contracts/`
  - [ ] Make the 0002 drift job compare against it
  - [ ] Add the lint rule forbidding consumers from declaring their own wire types

## Open Questions

- [ ] Whether frames are described here at all, given they are opaque bytes with
      a header. Declaring the header here keeps one owner; declaring it in the
      transport keeps the contracts package free of framing concerns. Current
      lean: the header belongs here, the framing belongs to 0004.
- [ ] How capability versions compare — exact match, or a compatibility range.
      Exact is simpler and forces coordinated upgrades. Current lean: exact,
      revisited when a second capability version exists.

## References

- Spec: [Robot Link](../specs/robot-link/)
- Related changes: [0004-groundstation-session](./0004-groundstation-session.md),
  [0002-ci-and-hygiene-gates](./0002-ci-and-hygiene-gates.md)
