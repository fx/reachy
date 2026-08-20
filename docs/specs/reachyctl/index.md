# reachyctl

## Overview

`reachyctl` is the command-line tool for operating a Reachy Mini running this
stack: deploying the [satellite](../ha-satellite/) to the robot, managing its
configuration, diagnosing the whole chain end to end, and exercising the
[groundstation](../groundstation/) directly.

It owns day-two operations. Machine state — the systemd drop-in, package
installation, the shape of the robot's configuration — is owned by
[provisioning](../provisioning/), and `reachyctl` defers to it rather than
reimplementing it.

Nothing is implemented yet.

## Background

Operating the predecessor meant a sequence of remote copies, package installs
and service restarts typed by hand, with no way to check afterwards whether the
result was what was intended. Two failures came directly out of that.

Environment overrides were silently inert for months, because nothing reported
what configuration was actually in effect. And the robot's systemd drop-in was
edited in place over a remote shell, which made the robot's state a thing
someone remembered rather than a thing anyone could read.

Both are diagnosis problems before they are deployment problems, which is why
`doctor` is specified as carefully here as `deploy`.

## Requirements

### REQ-051: Deployment verifies its own result

The deploy command MUST confirm that the intended version is installed and
running before it reports success.

#### Scenario: An install silently fails to take effect

- **GIVEN** a deploy where the package installs but the daemon continues running
  the previous version
- **WHEN** the deploy command completes its steps
- **THEN** it reports failure and names the version actually running, rather
  than reporting success on the strength of the install having exited zero

### REQ-052: Configuration changes can be previewed without being applied

Every command that modifies robot state MUST support a mode that reports the
changes it would make and makes none of them.

#### Scenario: An operator checks a change before making it

- **GIVEN** a robot with an existing configuration
- **WHEN** the operator runs a configuration change in preview mode
- **THEN** the differences are printed and the robot is left byte-identical

### REQ-053: Configuration values are validated before they are sent

The tool MUST reject a configuration value that the receiving component would
not accept, before applying it to the robot.

#### Scenario: An operator sets an out-of-range value

- **GIVEN** a setting with a constrained range
- **WHEN** the operator sets a value outside it
- **THEN** the command fails locally with the constraint stated, and the robot
  is not contacted

### REQ-054: Diagnosis covers the whole chain and names the failing link

The doctor command MUST report the status of every link between the operator and
a working robot individually, and MUST identify which link is broken when one
is.

#### Scenario: The groundstation is unreachable

- **GIVEN** a robot whose application is running and a groundstation that is
  down
- **WHEN** the operator runs the doctor command
- **THEN** the daemon and application checks pass, the groundstation check
  fails, and the output identifies the groundstation as the broken link

#### Scenario: Everything is healthy

- **GIVEN** a fully working installation
- **WHEN** the operator runs the doctor command
- **THEN** every check reports success, including the negotiated capabilities
  and the measured round-trip time

### REQ-055: A failed check states how to fix it

Every diagnostic check that fails MUST report a remediation.

#### Scenario: The application is installed but not running

- **GIVEN** a robot with the application installed and stopped
- **WHEN** the doctor command runs
- **THEN** the failing check names the command that starts it

### REQ-056: Diagnosis and provisioning agree on what healthy means

The checks performed by the doctor command and by the provisioning verification
step MUST be defined once and used by both.

#### Scenario: A new check is added

- **GIVEN** a new health check added to the shared definitions
- **WHEN** either the doctor command or the provisioning verification runs
- **THEN** both perform the new check, without it having been added twice

### REQ-057: The probe exercises the real session protocol

The probe command MUST establish a session using the same protocol
implementation the robot application uses.

#### Scenario: A protocol change breaks compatibility

- **GIVEN** a change to the session protocol that the groundstation implements
  incorrectly
- **WHEN** the probe runs against it
- **THEN** the probe fails in the same way the robot application would, because
  no separate protocol path exists for testing

### REQ-058: Output is machine-readable on request

Every command that reports results MUST offer a structured output format
suitable for consumption by another program.

#### Scenario: Diagnosis runs from a script

- **GIVEN** an operator scripting a health check
- **WHEN** the doctor command runs with structured output requested
- **THEN** the result parses without screen-scraping, and the process exit
  status reflects whether the checks passed

### REQ-059: Secrets are never written to output

The tool MUST NOT write credentials to its output, its logs, or its error
messages.

#### Scenario: A connection fails with a credential configured

- **GIVEN** a groundstation credential configured locally
- **WHEN** a connection attempt fails and the error is reported
- **THEN** the credential does not appear in the message, in verbose output, or
  in any log file the tool writes

## Design

### Command surface

| Command | Purpose |
|---|---|
| `deploy` | Build the wheel, transfer it, install into the robot's application environment, restart the daemon, start the application, verify the running version |
| `config get` / `set` / `diff` / `apply` | Read and change the robot's configuration and the daemon environment, with preview |
| `doctor` | Walk the chain: daemon, application, groundstation, session, capabilities, round-trip time, model files, effective configuration |
| `app start` / `stop` / `logs` | Application lifecycle and log access |
| `probe` | Open a real session to the groundstation and feed it live or recorded frames, with no robot involved |
| `bench` | Run the [benchmark](../benchmarks/) suite against a live installation |

### Interaction model

Commands are non-interactive by default so they compose in scripts, with richer
rendering when attached to a terminal. Long operations — deployment, probing,
benchmarking — present live progress; everything else prints and exits.

The structured output in REQ-058 is what keeps the two modes honest: if the
human-facing rendering is the only way to get a result, the tool has become
unscriptable, and diagnosis is exactly the thing people want to run on a timer.

### The probe

`probe` exists because the groundstation needs real traffic long before the
robot application exists, and because a synthetic client that speaks its own
dialect of the protocol proves nothing about the protocol.

REQ-057 is therefore about sharing an implementation, not about behaving
similarly. The probe is a second consumer of the same session client, which is
also why the [robot link](../robot-link/) contract is specified independently of
either component.

Its second life is diagnostic. When face tracking misbehaves, the question is
whether the groundstation is producing bad results or the robot is applying good
ones badly, and a probe fed a recorded frame answers it in one command.

### Division with provisioning

Provisioning brings a robot from a stock image to a configured state and is
declarative and idempotent. `reachyctl` operates a robot that is already in that
state. The dividing line is durability: anything that should survive a rebuild
belongs to provisioning.

`reachyctl` wraps rather than reimplements the provisioning run, so there is one
description of machine state. The shared check definitions in REQ-056 are the
other half of that: provisioning asserts the end state it just created, and
`doctor` asserts the same thing later, from the same source.

### Transport

The robot is reached over its own remote-access and daemon interfaces, in
process rather than by invoking command-line clients, so failures arrive as
structured errors that progress reporting can reflect rather than as text to be
parsed out of a subprocess.

### Decision Records

#### Deployment verifies rather than assumes

The predecessor's deployment failure mode was a package that installed
successfully into an environment the running daemon was not using, which looks
identical to success at every step. REQ-051 makes the check part of the command
because an operator who has to remember to verify separately eventually will
not. Rejected alternative: reporting success on exit status, with verification
left to `doctor`.

#### Preview mode on every mutating command

Robot state was previously edited in place with no way to see the change first.
Requiring preview on everything that mutates, rather than on the commands that
seemed risky, avoids having to predict which ones those are.

## Constraints

- The robot is reached over a WLAN measured at 100–170 ms idle round-trip with
  700 ms spikes, so anything that performs many small round trips feels slow;
  operations batch where they can.
- Deployment restarts the daemon, which interrupts whatever the robot is doing.
  The tool cannot make that invisible and does not pretend to.
- The robot's application environment is shared with the daemon and any other
  installed application, so installation cannot assume exclusive ownership of it.

## Open Questions

- **Whether `deploy` can roll back.** The previous wheel is recoverable in
  principle, and nothing currently keeps it. Doing this properly means retaining
  versions on the robot, which costs space on a device that has little. Current
  default: no rollback; recovery is redeploying a known-good version.
- **Whether `doctor` runs unattended on a schedule.** The structured output in
  REQ-058 makes it possible, and nothing consumes it yet. Current default:
  operator-invoked.

## References

- [architecture](../architecture/) — repository conventions
- [ha-satellite](../ha-satellite/) — what `deploy` installs
- [groundstation](../groundstation/) — what `probe` exercises
- [provisioning](../provisioning/) — the owner of durable machine state
- [robot-link](../robot-link/) — the protocol the probe shares with the robot
- [benchmarks](../benchmarks/) — what `bench` runs

## Changelog

| Date | Change | Document |
|------|--------|----------|
| 2026-08-20 | Initial spec created | — |
