# 0015: Documentation and runbooks

## Summary

Write the setup and operations runbooks, complete the agent documentation, and
publish the generated contract artifacts — so that someone, or something,
arriving at this repository cold can get a working installation without asking a
person.

**Spec:** [Architecture](../specs/architecture/)
**Status:** complete
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

- [x] Write the setup runbooks
  - [x] Groundstation deployment, executed and verified
  - [x] Robot provisioning and satellite deployment, executed and verified —
        every hardware-free step executed, the robot-facing steps marked
        pending; see the deferral below
  - [x] Home Assistant integration, with the identity constraint stated
        prominently
- [x] Write the operations runbooks
  - [x] Updating a running installation
  - [x] Troubleshooting keyed to `doctor` check identifiers, sharing remediation
        text with the registry
- [x] Publish the generated contracts
  - [x] Generate every schema and interface description into `docs/contracts/`
  - [x] Extend the 0002 drift job to cover all of them
- [x] Complete the agent documentation
  - [x] Root `AGENTS.md` with the invariants that emerged in implementation
  - [x] Review each per-member `AGENTS.md` against what was built
  - [x] Replace `README.md`
- [x] Register the last spec in `.duvet/config.toml`
  - [x] `docs/specs/architecture/index.md`, with annotations for REQ-001 and
        REQ-003, the two that had none
  - [x] All eight specs and all 73 requirements traced
- [ ] Verify
  - [x] Execute every runbook step that does not need a robot, and record what
        it actually printed
  - [ ] **Deferred: execute the hardware-dependent steps against a real
        installation.** Not ticked, and deliberately not ticked. The user
        deferred all verification against real robot hardware to a separate
        end-to-end session held after implementation is complete. Every step
        that needs a robot carries a **⏳ PENDING HARDWARE VERIFICATION**
        marker in place of output, and the outstanding list is
        [`docs/tasks.md`](../tasks.md). Writing plausible output for those
        steps would have been the failure this change's own decision record
        names.
  - [x] Record actual output as expected output, for everything executed

## Completion notes

- **`complete` here means written, executed as far as this repository can
  execute it, and gated.** It does not mean validated against a Reachy Mini.
  Nothing in this repository has one attached, and the user deferred hardware
  verification to a separate end-to-end session held after implementation. That
  deferral is recorded as the standing follow-up in
  [`docs/tasks.md`](../tasks.md), with the list of steps it has to execute.
- **What was executed, and is transcribed verbatim.** The groundstation image
  built and brought up under `docker compose`, `/livez`, `/readyz`,
  `/capabilities`, `/config` and `/metrics` answering it, the startup log,
  `reachyctl probe` driving five frames through it over a real session,
  `reachyctl doctor` in both its passing and its failing shapes, `just models`,
  `just image-verify`, `just wheels`, `just wheel-verify`, `just wheel-size`,
  `just bench`, `just provision-idempotency` against the container target, the
  `ROBOT_SETTINGS` vocabulary and the refusal a name outside it produces, and
  the two ways the service refuses to start.
- **What is marked pending, and why it is marked rather than invented.** Every
  step that talks to a robot carries a **⏳ PENDING HARDWARE VERIFICATION**
  block saying in so many words that nothing below it is a transcript. The
  decision record above says expected output written from memory is wrong in
  small ways that train readers to ignore it; a fabricated transcript would
  have been exactly that, and a silently omitted step would have been worse.
- **⚠️ Writing the runbooks found a real gap, which is what writing them last is
  for.** `reachy_contracts.ROBOT_SETTINGS` — the vocabulary `reachyctl config`
  and the Ansible `daemon_env` role validate against and write — declares seven
  names, and the satellite application reads exactly one of them
  (`REACHY_SATELLITE_LOG_LEVEL`). Three more are under its prefix and reported
  as having no effect; the other three are not under its prefix at all. The
  settings it does need — `REACHY_SATELLITE_DEVICE_NAME` above all, which has no
  default and without which it refuses to start — cannot be written through that
  path, because `validate_settings` refuses a name the vocabulary does not
  declare.

  Two consequences, both documented where an operator meets them rather than
  worked around: a robot provisioned from a declaration alone has a satellite
  that will not start, and `doctor`'s `home-assistant.identity` check compares
  against `REACHY_HOME_ASSISTANT_IDENTITY` while the satellite announces
  `REACHY_SATELLITE_DEVICE_NAME` — so it can pass while the two disagree, on the
  one hazard this whole documentation set is built around. Reconciling them is a
  change to the reachyctl and ha-satellite specs and is tracked in
  [`docs/tasks.md`](../tasks.md).
- **The troubleshooting runbook shares the remediation text rather than
  restating it, and two mechanisms hold it there.**
  `docs/contracts/doctor-checks.md` is generated from `reachy_checks.CHECKS` and
  covered by the contract-drift gate; `test_checks_runbook.py` reads the runbook
  and requires its sections to be exactly the registered identifiers, in the
  registry's order, each quoting that check's remediation word for word.
  Whitespace is normalised before comparing and only whitespace, so the document
  may wrap and may not paraphrase.
- **The generated contracts now come from two registries.** `reachy-contracts`
  cannot import `reachy-checks` — the dependency runs the other way — so the
  check reference is registered in `reachy_checks.checks_export` and
  `scripts/export_contracts.py` hands both registries to one `export`. Two
  separate runs would each rewrite the index over their own half, and the drift
  gate would flip between them on alternate invocations.
- **The last spec is registered.** `docs/specs/architecture/index.md` joins the
  other seven, which makes all eight specs and all 73 requirements traced. Seven
  of its nine already carried annotations; the two that did not are REQ-001,
  cited from the `Justfile`'s `--locked` installs, and REQ-003, cited from
  `.gitignore`, whose ignore rules and their tracked `.example` siblings are the
  mechanism the requirement describes. Both files needed a `[[source]]` block of
  their own.
- **Stale forward references were swept out of shipped code.** Five docstrings
  and comments still said a thing "arrives in change 0009" or held "until it
  lands" after the change had landed. A docstring asserting something the code
  does not do is a defect in a repository where several rules are enforced in
  review, because a reviewer reads those sentences as evidence. The root
  `AGENTS.md` now says so.

## Open Questions

- [x] **Whether runbook execution becomes part of release verification
      permanently, or is a one-off for this change. Resolved: permanent for the
      hardware-free steps, manual and deliberate for the rest — which is the
      lean, with the mechanism made specific.**

      The hardware-free half is already permanent and is not a new job. Every
      such step is a `Justfile` recipe or a `reachyctl` invocation that
      continuous integration already runs on every pull request: `just check`,
      `just contracts-check`, `just image-verify`, `just wheel-verify`,
      `just provision-idempotency`, `just bench`. A runbook step whose command
      broke would turn a merge gate red before anybody read the runbook. What
      would *not* be caught is a step whose recorded output has merely drifted,
      and the answer to that is the same one this change applied to the
      remediation text: where a document reproduces something the code decides,
      something compares them — the managed-region contract test, the
      `.env.example` test, `test_checks_runbook.py`, the drift gate. A general
      "re-run every transcript and diff it" job was rejected: most of these
      transcripts carry timings, container names and identifiers that differ
      every run, so the job would be permanently red or permanently ignored.

      The hardware half stays manual and is not automatable here: it needs a
      robot in the loop, and no runner has one. It is a release checklist item
      rather than a job, and this change writes the checklist —
      [`docs/tasks.md`](../tasks.md) carries the standing list, and the
      `⏳ PENDING HARDWARE VERIFICATION` markers are how a reader tells which
      steps are on it.
- [x] **Whether a Hugging Face Space is published as an additional distribution
      channel. Resolved: no, and nothing here forecloses it.** The lean was
      followed, and implementing the whole stack did not disturb the reasoning.
      The daemon discovers applications through a standard Python entry point
      whether they arrived from a Space or from a wheel, so a Space would be a
      second publishing path to the same mechanism — with a second account, a
      second set of credentials in the release workflow, and a second artifact
      whose version has to be kept equal to the seven this repository already
      publishes under one tag. `just wheel-verify` proves the wheel carries the
      `reachy_mini_apps` entry point the daemon looks for, which is the property
      a Space would have had to reproduce. Adding one later is a job in
      `release.yml` and a credential; nothing in the layout, the packaging or
      the runbooks assumes it does not exist.

## References

- Spec: [Architecture](../specs/architecture/)
- Related changes: [0013-satellite-behaviour-and-ui](./0013-satellite-behaviour-and-ui.md),
  [0014-benchmarks-and-gates](./0014-benchmarks-and-gates.md),
  [0008-reachyctl-doctor](./0008-reachyctl-doctor.md)
