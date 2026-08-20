# 0010: Ansible provisioning

## Summary

Implement the Ansible roles that bring a stock Reachy Mini image to a configured
state, with idempotency enforced in CI and verification sharing its check
definitions with `reachyctl doctor`.

**Spec:** [Provisioning](../specs/provisioning/)
**Status:** draft
**Depends On:** 0009

## Motivation

The robot's configuration currently exists as a systemd drop-in edited in place
over a remote shell. Nobody can tell what is set without logging in to look, and
rebuilding the robot means reconstructing it from memory.

There is also a detail that decays silently: the drop-in has to attach to the
daemon unit rather than to the application, because the application inherits its
environment from the daemon, and applying it needs a daemon restart. That is
obvious while doing it and gone three months later, which is an argument for
encoding it rather than writing it down.

## Requirements

### Testing Requirements

This change MUST satisfy the project's standing testing rules (see
[Testing conventions](../specs/architecture/index.md#testing-conventions)). CI
enforces these as merge gates:

- Tests run with `pytest`, with async strict mode enabled.
- Coverage MUST be gated on the diff rather than on the whole tree, for any
  Python shipped with this change.
- Type checking MUST run in strict mode for new modules.
- A lint or type suppression MUST carry the rule identifier and a justification.

Skipping or weakening any of these rules to land the PR MUST be treated as a bug
in the PR, not in the rule.

Provisioning adds a gate of its own, required by
[REQ-061](../specs/provisioning/index.md#req-061-idempotency-is-enforced-automatically):
CI MUST apply the playbook twice against a container target and fail if the
second application reports any changed step. Idempotency is the property that
decays first as roles are edited, and it is invisible without this check.

### Functional requirements

The [provisioning spec](../specs/provisioning/) owns the observable guarantees —
idempotency, stock-image sufficiency, full ownership of managed configuration,
reversibility, preview, and end-state verification. Those scenarios are this
change's acceptance criteria. What implementing them requires of this change:

- The daemon environment role owns its managed region wholesale, including
  pruning settings that were previously declared and no longer are.
- The role encodes that the drop-in attaches to the daemon unit and that
  applying it restarts the daemon, rather than leaving either as documentation.
- The `app_install` role installs a wheel from a configured source — a release
  artifact or a local path — rather than hard-coding the satellite. That is what
  keeps this change implementable before 0013 exists, matching the same choice
  in [0009](./0009-reachyctl-deploy-and-config.md).
- The verification role consumes the check registry from 0008. It does not
  define its own checks and does not shell out to the CLI, since that would
  require a CLI installation on the control machine.
- The tracked inventory is an example only. Real addresses and credentials are
  untracked, under
  [architecture REQ-003](../specs/architecture/index.md#req-003-no-environment-specific-values-in-version-control).
- Credentials reach the robot through Ansible's own secret handling, never
  through variables files.
- A removal path undoes everything the roles applied and restores stock
  behaviour.

#### Scenario: The playbook runs against a container standing in for a robot

- **GIVEN** a container image approximating the stock robot filesystem layout
- **WHEN** CI applies the playbook twice
- **THEN** the first application reports changes and the second reports none

## Design

### Approach

Four roles behind a tagged `site.yml`: `daemon_env`, `app_install`,
`groundstation_link`, `verify`. Tags let an operator apply one concern without
the others, which is what makes the playbook usable during development rather
than only at first setup.

The verification role reaches the shared check registry as a Python module
invoked from the play, which keeps one definition of healthy without coupling
the playbook to a CLI installation.

CI runs against a container that approximates the stock filesystem layout. It
cannot exercise hardware and does not try to — what it verifies is convergence,
which is the property that rots as roles are edited.

### Decisions

- **Decision**: Ansible, not Terraform.
  - **Why**: The work is a remote shell session and a set of files that need to
    end up in a known condition. Terraform models API-backed resources with a
    state file, and there is no API here; using it would mean wrapping shell
    steps in a resource model describing nothing, and carrying state for a
    machine whose real state is readable directly.
  - **Alternatives considered**: Terraform with a shell provider; a bespoke
    deployment script, which is where this started.
- **Decision**: The managed region is owned wholesale rather than appended to.
  - **Why**: An append-only drop-in means a setting removed from the declaration
    stays on the robot, so the file that is supposed to describe the robot
    stops doing so.
  - **Alternatives considered**: Append-with-markers, which is the same problem
    with more machinery.
- **Decision**: Verification imports the check registry rather than invoking the
  CLI.
  - **Why**: Shelling out makes provisioning depend on a CLI installed on
    whatever machine runs the playbook.
- **Decision**: The idempotency target is a container, not a robot.
  - **Why**: CI has no robot. The convergence property is testable without one,
    and pretending otherwise would mean no check at all.

### Non-Goals

- No image building; this configures a stock image rather than producing one.
- No network configuration management — recorded as an open question in the
  [spec](../specs/provisioning/index.md#open-questions), and the fastest way to
  make a robot unreachable.
- No multi-robot orchestration.
- No hardware-dependent verification in CI.

## Tasks

- [ ] Build the playbook skeleton
  - [ ] `site.yml` with per-concern tags
  - [ ] `inventory.example.ini` and neutral `group_vars/all.yml`
  - [ ] Secret handling wired through Ansible's own mechanism
- [ ] Implement `daemon_env`
  - [ ] Managed drop-in region on the daemon unit, owned wholesale
  - [ ] Pruning of withdrawn settings
  - [ ] Daemon restart on change, and only on change
- [ ] Implement `app_install` and `groundstation_link`
  - [ ] Install a wheel from a configured source into the robot's application
        environment, without hard-coding which application it is
  - [ ] Groundstation endpoint and credential configuration
- [ ] Implement `verify`
  - [ ] Invoke the shared check registry from 0008
  - [ ] Fail the run when the end state is not working
- [ ] Implement the removal path
  - [ ] Remove the managed configuration and installed application
  - [ ] Assert stock behaviour is restored
- [ ] Wire the CI gate
  - [ ] Container target approximating the stock layout
  - [ ] Apply twice; fail on any changed step in the second application

## Open Questions

- [ ] How faithfully the container target needs to model the robot. Too rough
      and the gate passes on roles that fail against real hardware; too faithful
      and it becomes its own maintenance burden. Current lean: model the
      filesystem and unit layout only.
- [ ] Whether `reachyctl provision` wraps the playbook in this change or later.
      The spec calls for a thin wrapper. Current lean: here, since the playbook
      is the thing being wrapped.

## References

- Spec: [Provisioning](../specs/provisioning/)
- Related changes: [0008-reachyctl-doctor](./0008-reachyctl-doctor.md),
  [0009-reachyctl-deploy-and-config](./0009-reachyctl-deploy-and-config.md)
