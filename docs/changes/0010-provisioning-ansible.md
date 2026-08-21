# 0010: Ansible provisioning

## Summary

Implement the Ansible roles that bring a stock Reachy Mini image to a configured
state, with idempotency enforced in CI and verification sharing its check
definitions with `reachyctl doctor`.

**Spec:** [Provisioning](../specs/provisioning/)
**Status:** complete
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

- [x] Build the playbook skeleton
  - [x] `site.yml` with per-concern tags
  - [x] `inventory.example.ini` and neutral `group_vars/all.yml`
  - [x] Secret handling wired through Ansible's own mechanism
- [x] Implement `daemon_env`
  - [x] Managed drop-in region on the daemon unit, owned wholesale
  - [x] Pruning of withdrawn settings
  - [x] Daemon restart on change, and only on change
- [x] Implement `app_install` and `groundstation_link`
  - [x] Install a wheel from a configured source into the robot's application
        environment, without hard-coding which application it is
  - [x] Groundstation endpoint and credential configuration
- [x] Implement `verify`
  - [x] Invoke the shared check registry from 0008
  - [x] Fail the run when the end state is not working
- [x] Implement the removal path
  - [x] Remove the managed configuration and installed application
  - [x] Assert stock behaviour is restored
- [x] Wire the CI gate
  - [x] Container target approximating the stock layout
  - [x] Apply twice; fail on any changed step in the second application

## Open Questions

- [x] How faithfully the container target needs to model the robot.
      **Resolved: the filesystem and unit layout only, and the list is written
      down.** The target runs real systemd as PID 1 and is reached over SSH,
      because `daemon-reload`, `restart` and `systemctl show` are what the roles
      read and write — a stubbed `systemctl` would make the gate a test of the
      stub, and a container connection plugin would exercise a transport no
      robot ever sees. Above that it carries only what the roles touch: the
      daemon unit, an application environment whose interpreter that unit
      declares, a `reachy-mini` distribution in it, and an application control
      reached as `python -m reachy_mini.apps`. No hardware, no groundstation, no
      aarch64, and no vendor daemon. What made the "too faithful" half of the
      question answerable was writing the boundary down rather than judging it:
      `provisioning/ci/README.md` is a table of what is modelled, what is not,
      and where each unmodelled thing is proved instead — so the gate's silence
      about hardware is a recorded fact rather than an assumption a reader has
      to make.
- [x] Whether `reachyctl provision` wraps the playbook in this change or later.
      **Resolved: here.** The playbook is the thing being wrapped, and a wrapper
      landing later would mean an interval in which the tool's help describes a
      smaller thing than the spec does. It is thin on purpose — it finds the
      playbook, spells preview as `--check --diff`, names the removal path, and
      translates Ansible's exit status into this tool's — and it is the one
      command that shells out, because Ansible's own output is the report and
      reproducing its progress rendering would make the wrapped run harder to
      read than the unwrapped one.

## References

- Spec: [Provisioning](../specs/provisioning/)
- Related changes: [0008-reachyctl-doctor](./0008-reachyctl-doctor.md),
  [0009-reachyctl-deploy-and-config](./0009-reachyctl-deploy-and-config.md)
