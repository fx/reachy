# 0015: Documentation and runbooks

## Summary

Write the setup and operations runbooks, complete the agent documentation, and
publish the generated contract artifacts — so that someone, or something,
arriving at this repository cold can get a working installation without asking a
person.

**Spec:** [Architecture](../specs/architecture/)
**Status:** draft
**Depends On:** 0013, 0014

## Motivation

Every component exists by this point, and the knowledge of how to assemble them
is spread across fifteen change documents and eight specs — which is the right
place for reasoning and the wrong place for instructions.

The documentation is targeted primarily at language models, which mostly means
being more explicit than a human reader needs: every step a command, every
command with the output to expect, so a reader can tell whether a step worked
without asking someone.

This lands last deliberately. Runbooks written before the thing they describe
document intentions rather than behaviour, and the difference is invisible until
someone follows them.

## Requirements

### Testing Requirements

This change MUST satisfy the project's standing testing rules (see
[Testing conventions](../specs/architecture/index.md#testing-conventions)). CI
enforces these as merge gates:

- Tests run with `pytest`, with async strict mode enabled.
- Coverage MUST be gated on the diff rather than on the whole tree, for any code
  shipped with this change.
- Type checking MUST run in strict mode for new modules.
- A lint or type suppression MUST carry the rule identifier and a justification.

Skipping or weakening any of these rules to land the PR MUST be treated as a bug
in the PR, not in the rule.

Documentation has a testing obligation of its own here: every runbook MUST be
executed end to end against a real installation before this change lands, and
the recorded expected output MUST be what the commands actually printed. A
runbook whose expected output was written from memory is worse than none,
because it teaches a reader to distrust the parts that are correct.

### Functional requirements

The [architecture spec](../specs/architecture/) owns the documentation
conventions and
[REQ-008](../specs/architecture/index.md#req-008-generated-contract-artifacts-cannot-drift)
owns contract-artifact freshness. What implementing them requires of this
change:

- Runbooks are imperative and verifiable: each step is a command paired with the
  output to expect.
- The setup path is covered end to end — groundstation deployment, robot
  provisioning, satellite deployment, Home Assistant integration, verification.
- The Home Assistant runbook states the device-identity constraint prominently,
  because following the setup without knowing it is how entity history gets
  detached.
- Generated contract artifacts are published into `docs/contracts/` and the
  drift job from 0002 covers all of them.
- Root `AGENTS.md` is completed with the invariants that emerged during
  implementation, rather than the ones predicted in 0001.
- A troubleshooting runbook maps each `doctor` check failure to its cause and
  remedy, sharing the remediation text from the 0008 registry rather than
  restating it.
- The repository `README.md` is replaced with something that orients a reader in
  under a minute and links onward.

#### Scenario: A runbook step's output changes

- **GIVEN** a runbook recording the expected output of a command
- **WHEN** the command's output changes in a later release
- **THEN** the runbook is updated in the same change, because the runbooks are
  executed as part of release verification

## Design

### Approach

Runbooks are organised by task rather than by component, because a reader has a
goal rather than a subsystem:

| Runbook | Covers |
|---|---|
| `docs/setup/groundstation.md` | Deploying the service with compose |
| `docs/setup/robot.md` | Provisioning a stock robot and deploying the satellite |
| `docs/setup/home-assistant.md` | Adding the satellite to Home Assistant, including the identity constraint |
| `docs/ops/deploy.md` | Updating a running installation |
| `docs/ops/troubleshooting.md` | Diagnosing failures, keyed to `doctor` check identifiers |

Troubleshooting keys on check identifiers rather than on symptoms, so the tool's
output leads directly to the relevant section.

### Decisions

- **Decision**: Runbooks are executed before this change lands.
  - **Why**: Expected output written from memory is wrong in small ways that
    train readers to ignore it, and an agent following it cannot tell which
    parts to trust.
  - **Alternatives considered**: Reviewing them for plausibility, which is what
    produces documentation nobody trusts.
- **Decision**: Troubleshooting shares remediation text with the check registry.
  - **Why**: Two copies of "how to fix this" drift, and the copy in the tool is
    the one people actually see.
  - **Alternatives considered**: Independent prose, which reads better and goes
    stale.
- **Decision**: `AGENTS.md` is completed last.
  - **Why**: The invariants worth writing down are the ones that turned out to
    matter, not the ones predicted before any code existed.

### Non-Goals

- No documentation site or published build; the repository renders on its own.
- No API reference prose — the generated contract artifacts are the reference.
- No tutorial or conceptual introduction; the specs carry the reasoning.

## Tasks

- [ ] Write the setup runbooks
  - [ ] Groundstation deployment, executed and verified
  - [ ] Robot provisioning and satellite deployment, executed and verified
  - [ ] Home Assistant integration, with the identity constraint stated
        prominently
- [ ] Write the operations runbooks
  - [ ] Updating a running installation
  - [ ] Troubleshooting keyed to `doctor` check identifiers, sharing remediation
        text with the registry
- [ ] Publish the generated contracts
  - [ ] Generate every schema and interface description into `docs/contracts/`
  - [ ] Extend the 0002 drift job to cover all of them
- [ ] Complete the agent documentation
  - [ ] Root `AGENTS.md` with the invariants that emerged in implementation
  - [ ] Review each per-member `AGENTS.md` against what was built
  - [ ] Replace `README.md`
- [ ] Verify
  - [ ] Execute every runbook end to end against a real installation
  - [ ] Record actual output as expected output

## Open Questions

- [ ] Whether runbook execution becomes part of release verification
      permanently, or is a one-off for this change. Permanent is better and
      needs a robot in the loop at release time. Current lean: permanent for the
      hardware-free runbooks, manual for the rest.
- [ ] Whether a Hugging Face Space is published as an additional distribution
      channel. Explicitly out of scope for now; the entry-point mechanism means
      a wheel is sufficient, and nothing in the layout forecloses adding one.

## References

- Spec: [Architecture](../specs/architecture/)
- Related changes: [0013-satellite-behaviour-and-ui](./0013-satellite-behaviour-and-ui.md),
  [0014-benchmarks-and-gates](./0014-benchmarks-and-gates.md),
  [0008-reachyctl-doctor](./0008-reachyctl-doctor.md)
