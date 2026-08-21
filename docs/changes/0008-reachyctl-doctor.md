# 0008: reachyctl doctor and shared checks

## Summary

Implement `doctor`: an end-to-end diagnosis of the chain from the operator's
machine to a working robot, built on a shared check definition that
[provisioning](../specs/provisioning/) verification will also consume.

**Spec:** [reachyctl](../specs/reachyctl/)
**Status:** complete
**Depends On:** 0007

## Motivation

Two of the predecessor's most expensive failures were diagnosis failures.
Environment overrides were silently inert for months because nothing reported
what was actually in effect, and the robot's configuration lived in a systemd
drop-in that nobody could inspect without logging in.

`doctor` lands before `deploy` deliberately. It is the tool used to find out
whether a deployment worked, so building it second would mean the first
deployments are the ones with no way to check them.

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

Every check MUST be tested in both its passing and its failing state. A
diagnosis tool tested only against a healthy system is tested against the case
nobody runs it in.

### Functional requirements

The [reachyctl spec](../specs/reachyctl/) owns the diagnosis behaviour —
[REQ-054](../specs/reachyctl/index.md#req-054-diagnosis-covers-the-whole-chain-and-names-the-failing-link),
[REQ-055](../specs/reachyctl/index.md#req-055-a-failed-check-states-how-to-fix-it),
and
[REQ-056](../specs/reachyctl/index.md#req-056-diagnosis-and-provisioning-agree-on-what-healthy-means).
Those scenarios are this change's acceptance criteria. What implementing them
requires of this change:

- Checks are declared as data — identifier, description, the probe that runs it,
  and the remediation text — in a shared module. This is what makes
  [REQ-056](../specs/reachyctl/index.md#req-056-diagnosis-and-provisioning-agree-on-what-healthy-means)
  achievable when 0010 consumes the same definitions.
- Checks are independent, and one failing does not prevent the rest from
  running. An operator with a broken groundstation still learns whether the
  daemon is healthy.
- A failing check reports remediation as a runnable command wherever one exists,
  not as prose describing what to do.
- The Home Assistant device identity check compares what the satellite announces
  against what is configured, because a silent change there detaches entity
  history — see
  [ha-satellite REQ-040](../specs/ha-satellite/index.md#req-040-the-announced-device-identity-is-configuration).
- Round-trip time is measured and reported, since the link is the component most
  likely to be the real problem and the least likely to be suspected.
- Structured output uses the conventions established in 0007.

## Design

### Approach

The check registry is a workspace module importable by both the CLI and the
provisioning verification role. Each check declares what it needs — a daemon
connection, a groundstation session, a local file — so the runner can skip
checks whose prerequisites are absent and report them as skipped rather than
failed.

The chain runs in dependency order for readability, but no check depends on
another's result.

### Decisions

- **Decision**: Checks are data, not functions scattered across commands.
  - **Why**: [REQ-056](../specs/reachyctl/index.md#req-056-diagnosis-and-provisioning-agree-on-what-healthy-means)
    requires provisioning and diagnosis to agree, and two independently written
    notions of "healthy" drift into a robot that provisioning calls fine and
    diagnosis calls broken.
  - **Alternatives considered**: Provisioning shelling out to `doctor`, which
    couples the provisioning run to a CLI installation on the control machine.
- **Decision**: `doctor` before `deploy`.
  - **Why**: Deployment verification in
    [REQ-051](../specs/reachyctl/index.md#req-051-deployment-verifies-its-own-result)
    is a check, so the check infrastructure has to exist first.
  - **Alternatives considered**: Deploy first with ad-hoc verification, later
    refactored — which means writing the verification twice.
- **Decision**: Skipped is a distinct outcome from failed.
  - **Why**: An operator with no groundstation configured is not in an error
    state, and reporting one trains people to ignore the output.
- **Decision**: The registry is a workspace member, `packages/reachy-checks`,
  imported as `reachy_checks`.
  - **Why**: [0010](./0010-provisioning-ansible.md)'s verification role imports
    these declarations from a control machine that may have no CLI installed, so
    a module inside `cli/reachyctl` would force provisioning either to install
    the CLI or to write the checks a second time. A sibling of
    `packages/reachy-session-client` is importable by both and depends on
    neither.
  - **Alternatives considered**: A module inside `reachyctl`, which fails the
    control-machine test; a package under `provisioning/`, which would make the
    CLI depend on the provisioning tree.
- **Decision**: The groundstation's pinned model registry is an optional extra
  of the checks package, imported where it is used rather than at module level.
  - **Why**: The digests and the hashing are
    `reachy_groundstation.models` and re-deriving either would be a second
    opinion about which weights are the right ones. But that service also pulls
    OpenCV, onnxruntime and an ASGI stack, and putting all of it into every CLI
    install to read a directory of hashes is not a trade worth making. The files
    live inside the groundstation's artifact, so the machine that has files to
    check is the machine that has the service installed; anywhere else the check
    reports the registry as missing rather than raising.
- **Decision**: A failed check exits `FAILURE`, not `UNREACHABLE`.
  - **Why**: `probe` exits `UNREACHABLE` for a groundstation that is not there
    because it has learned nothing. `doctor` was asked to find that out, so the
    same groundstation is a negative answer rather than an aborted run. The
    statuses that still mean "nothing was learned" are the ones about the
    invocation: an address that is not a session URL, an unreadable credential
    file, a declaration document that is not one.
- **Decision**: Skipped checks do not make a run negative; the counts are
  reported instead.
  - **Why**: An all-skipped run exits zero and says in its summary that not
    everything was checked. A monitor that wants a complete diagnosis rather
    than a clean one asserts `skipped == 0` from the structured output, which is
    a decision the consumer makes rather than one the registry makes for it.

### Non-Goals

- No remediation execution — `doctor` reports the command, it does not run it.
- No scheduled or unattended operation.
- No provisioning role; 0010 consumes these definitions.

## Tasks

- [x] Build the shared check registry
  - [x] Check declaration structure: identifier, description, probe, remediation
  - [x] Prerequisite declaration and the skipped outcome
  - [x] Runner executing checks independently and collecting results
- [x] Implement the checks
  - [x] Daemon reachable and responding
  - [x] Application installed, and the installed version
  - [x] Application running
  - [x] Groundstation reachable; session established; capabilities negotiated
  - [x] Round-trip time measured
  - [x] Model files present and matching their pinned hashes
  - [x] Effective configuration matches intent
  - [x] Announced Home Assistant identity matches configuration
- [x] Implement the command
  - [x] Human-facing rendering with per-check status
  - [x] Structured output and exit status per the 0007 conventions
  - [x] Passing-and-failing tests for every check

## Open Questions

- [x] **Whether `doctor` can query Home Assistant directly: no. Announced is
      compared against declared, and the Home Assistant side is a manual
      check.** The lean was followed. Holding Home Assistant credentials would
      widen this tool's blast radius for one comparison, and the credential
      would then be a second secret every output path has to be scrubbed
      against. What the check catches without them is the case that actually
      happens — the satellite quietly announcing something other than what was
      declared — and the `home-assistant.identity` remediation says in so many
      words that whether Home Assistant already holds a stale device is a look
      in its device list. Nothing forecloses the richer check: the identity is
      already a field of the declared intent, so a Home Assistant client would
      be a second source to compare against rather than a rewrite.
- [x] **Whether check results are retained between runs: no.** The lean was
      followed. A `doctor` run reads nothing and writes nothing but its output,
      which is what lets it run from a script, from a provisioning play and from
      a laptop without any of them sharing state. The one number worth trending
      — the round trip — is promoted into the run's scalar fields, so a
      consumer that wants a history has one field to record and
      [0014](./0014-benchmarks-and-gates.md) is where a stored baseline is
      already a requirement.

## References

- Spec: [reachyctl](../specs/reachyctl/)
- Related changes: [0007-reachyctl-probe](./0007-reachyctl-probe.md),
  [0009-reachyctl-deploy-and-config](./0009-reachyctl-deploy-and-config.md),
  [0010-provisioning-ansible](./0010-provisioning-ansible.md)
