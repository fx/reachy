# Provisioning

## Overview

Provisioning takes a Reachy Mini running a stock image and brings it to a known
configured state: the daemon environment set, the
[satellite](../ha-satellite/) installed, the
[groundstation](../groundstation/) link configured, and the result verified.

It is declarative and idempotent, so the robot's state is something readable in
version control rather than something an operator remembers having typed.
[`reachyctl`](../reachyctl/) operates a robot that provisioning has already
brought to this state.

Nothing is implemented yet.

## Background

The predecessor's robot configuration was a systemd drop-in edited in place over
a remote shell. It worked, and it had the two properties that make hand-edited
machine state expensive: nobody could tell what was set without logging in to
look, and rebuilding the robot meant reconstructing it from memory.

The drop-in also had to be attached to the daemon unit rather than to the
application, because the daemon is what the application inherits its environment
from, and applying it required a daemon restart. That is exactly the kind of
detail that is obvious while doing it and gone three months later — which is an
argument for encoding it rather than documenting it.

## Requirements

### REQ-060: Applying twice changes nothing the second time

A provisioning run against an already-provisioned robot MUST report that it
changed nothing.

#### Scenario: A run is repeated

- **GIVEN** a robot that has just been provisioned successfully
- **WHEN** provisioning runs again with the same inputs
- **THEN** it completes reporting zero changed steps

### REQ-061: Idempotency is enforced automatically

Continuous integration MUST apply the provisioning twice against a test target
and fail if the second application reports any change.

#### Scenario: A step is written non-idempotently

- **GIVEN** a change introducing a step that reports a change on every run
- **WHEN** the idempotency check runs
- **THEN** the second application reports a changed step and the check fails

### REQ-062: A stock image is sufficient

Provisioning MUST succeed against an unmodified stock robot image without any
manual preparation of the target beyond network access and credentials.

#### Scenario: A freshly imaged robot is provisioned

- **GIVEN** a robot flashed with a stock image and reachable over the network
- **WHEN** provisioning runs
- **THEN** it completes successfully without a preparatory step having been
  performed by hand

### REQ-063: The managed configuration is fully owned

Configuration under provisioning's control MUST converge to exactly what is
declared, including removing values that were previously declared and no longer
are.

#### Scenario: A setting is removed from the declaration

- **GIVEN** a provisioned robot carrying a setting that provisioning previously
  applied
- **WHEN** that setting is removed from the declaration and provisioning runs
- **THEN** the setting is removed from the robot rather than left behind

### REQ-064: The robot can be returned to stock behaviour

Provisioning MUST offer a supported path that removes everything it applied and
restores the robot's stock behaviour.

#### Scenario: An operator reverts a robot

- **GIVEN** a fully provisioned robot
- **WHEN** the operator runs the removal path
- **THEN** the robot behaves as it did before provisioning, with the managed
  configuration gone

### REQ-065: Changes are previewable

Provisioning MUST support reporting the changes a run would make without making
any of them.

#### Scenario: An operator inspects a pending change

- **GIVEN** a robot whose declaration has been edited
- **WHEN** provisioning runs in preview mode
- **THEN** the steps that would change are listed and the robot is unmodified

### REQ-066: A run asserts the end state it created

A provisioning run MUST verify the robot reaches a working end state before
reporting success.

#### Scenario: Configuration applies but the application does not start

- **GIVEN** a run where every configuration step succeeds but the application
  fails to start
- **WHEN** the run reaches its verification step
- **THEN** the run fails rather than reporting a successful provision of a
  non-working robot

## Design

### Structure

```
provisioning/ansible/
├─ inventory.example.ini   # tracked; the real inventory is not
├─ site.yml                # tagged plays
├─ group_vars/all.yml      # neutral defaults only
└─ roles/
   ├─ daemon_env/          # the systemd drop-in and its environment
   ├─ app_install/         # the satellite wheel
   ├─ groundstation_link/  # endpoint and credential
   └─ verify/              # asserts the end state
```

### Why Ansible

The work here is a remote shell session and a set of files that need to end up
in a known condition. Ansible is agentless, so it runs against a stock image
with nothing installed on it; it is idempotent by construction, which is what
REQ-060 asks for; and it supports a dry run natively, which is REQ-065.

Terraform was considered and does not fit: it models API-backed resources with a
state file, and there is no API here. Using it would mean wrapping shell steps
in a resource model that describes nothing, and carrying state for a machine
whose real state is readable directly.

### Daemon environment

The environment belongs on the daemon unit rather than on the application,
because the application inherits it from the daemon, and applying a change
requires the daemon to restart. Both facts are encoded in the role.

REQ-063 exists because the natural way to write this — appending settings — is
wrong in a way that only shows up later. A drop-in that is added to but never
pruned means removing a setting from the declaration leaves it on the robot, and
the robot then diverges from the file that is supposed to describe it. The
managed region is owned wholesale.

REQ-064 follows from the same reasoning. Deleting the managed configuration
restores stock behaviour, which makes the provisioning reversible and makes
"what does this actually change?" answerable by reverting it.

### Verification

The verification role asserts the same conditions `reachyctl doctor` asserts,
from one shared definition, as required by
[reachyctl REQ-056](../reachyctl/index.md#req-056-diagnosis-and-provisioning-agree-on-what-healthy-means).
Provisioning checks the state it just created; `doctor` checks it later. Two
independent notions of "healthy" would drift, and the drift would surface as a
robot that provisioning calls fine and diagnosis calls broken.

### Inventory and secrets

The tracked inventory is an example. Real addresses and credentials are supplied
locally and are untracked, under
[architecture REQ-003](../architecture/index.md#req-003-no-environment-specific-values-in-version-control).
Credentials reach the robot through Ansible's own secret handling rather than
being written into variables files.

### Continuous integration

Idempotency is verified against a container standing in for the robot. It cannot
exercise hardware, and it does not try to: what it verifies is that the steps
converge, which is the property that decays silently as roles are edited.

## Constraints

- The target runs a stock image whose contents are set by the robot vendor, so
  provisioning adapts to it rather than the other way round.
- Applying daemon environment changes restarts the daemon, interrupting whatever
  the robot is doing.
- The container used for idempotency testing is not a robot, so any step
  requiring real hardware is exercised only against a real one.

## Open Questions

- **Whether provisioning manages the robot's network configuration.** It is the
  largest remaining piece of hand-held state, and getting it wrong makes the
  robot unreachable — which is also why it is the least safe thing to automate
  against a device on a desk. Current default: out of scope.
- **Whether several robots are provisioned from one declaration.** The inventory
  structure allows it and nothing has been tested with more than one. Current
  default: single robot.

## References

- [architecture](../architecture/) — repository hygiene and secret handling
- [ha-satellite](../ha-satellite/) — what is installed
- [reachyctl](../reachyctl/) — day-two operations and the shared checks
- [groundstation](../groundstation/) — the link being configured

## Changelog

| Date | Change | Document |
|------|--------|----------|
| 2026-08-20 | Initial spec created | — |
