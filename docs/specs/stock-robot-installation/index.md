# Stock Robot Installation

## Overview

This specification owns what it takes to put the robot-side satellite onto a
**stock** Reachy Mini and configure it there: installed through the daemon's own
application-install path, brought up before anybody has told it who it is, and
running against a released daemon build that answers fewer questions than the
one this repository's development loop assumes.

It extends the [HA Satellite](../ha-satellite/),
[Home Assistant Configuration and Camera Feed](../home-assistant-configuration-and-camera-feed/)
and [reachyctl](../reachyctl/) contracts without changing the announced entity
model, the robot-link wire format, the deployment topology or the shape of the
robot's durable machine state. The behavior described here is proposed and not
yet implemented.

## Background

A stock robot is one running the image its manufacturer shipped, with the
released daemon and its released Python packages, reached only through the
surfaces that image exposes: the daemon's own dashboard, an installed
application's own web interface, and the robot's network. Nothing has been
copied onto it by hand, no file the image ships has been edited, and no shell
session has been opened on it.

Three things currently stand between that robot and a working satellite, and all
three were observed on real hardware rather than reasoned about.

**Motion is refused outright.** The satellite's motor-group coordinator asks the
daemon to correlate each grouped torque request with an acknowledgement and a
physical read-back, which is the confirmation
[REQ-093](../home-assistant-configuration-and-camera-feed/index.md#req-093-home-assistant-configuration-reports-effective-state)
requires before it will announce a motor switch. No released daemon offers it.

The application already notices that honestly: a daemon with no such surface
produces a confirmation marked *unavailable*, which is a different value from
the one a confirmation that ran and failed produces. The distinction is drawn
and it is correct. What is missing is the consequence — an unavailable
confirmation is still not a confirmation, so every group's command gate stays
shut, every motion command is rejected, and the satellite tracks a face and
streams frames while never moving at all. The missing switches are the correct
outcome; the frozen robot is not. A daemon that cannot report torque has nothing
to gate, and gating it anyway is what stops the robot.

**There is no first-time configuration path.** The announced Home Assistant
identity has no default, deliberately, because a derived default is silently
destructive on an upgrade — the reasoning is
[REQ-040](../ha-satellite/index.md#req-040-the-announced-device-identity-is-configuration)'s
and it still holds. What does not hold is the consequence: the application
refuses to start without that value, so its settings interface never comes up,
so the only surface that could supply the value is the one the missing value
prevents. The remaining route is hand-writing configuration onto the robot over
a shell, which is exactly what a stock installation excludes. The groundstation
address and its credential are unresolved for the same reason on a fresh
installation and have the same consequence.

**Distribution assumes a shell.** The wheel is published on GitHub Releases and
installed into the robot's shared application environment. On a stock robot,
getting it there is a file copy and a package install typed at a prompt. The
daemon has its own installation path for applications, and using it makes the
copying unnecessary. That path is currently a recorded non-goal — the
[architecture](../architecture/index.md#versioning-and-distribution) and
[HA Satellite](../ha-satellite/index.md#packaging-and-deployment) design notes
both say no application source is published for it, on the grounds that a wheel
is sufficient. It is sufficient for someone with a shell. This contract is the
reversal of that decision, and reconciling those two design notes is a separate
proposal rather than something an implementing change does in passing.

Diagnosis makes the same shell-shaped assumption from the other side.
`reachyctl` reads the robot's daemon service unit and treats the program it
starts as the daemon's Python interpreter. On the released image that program is
a shell launcher, so handing it a Python argument starts a **second daemon**,
which was watched on real hardware contending for the daemon's network port, its
serial device and its camera before dying. The tool then reports the robot's
health as broken, and the thing it was diagnosing was itself.

## Requirements

### REQ-099: Motion survives a daemon without torque confirmation

The satellite MUST command motion on a robot whose daemon offers no correlated
grouped-torque confirmation capability, treating that absence as nothing to gate
rather than as a motor group whose torque state could not be confirmed.

#### Scenario: A stock robot tracks a face

- **GIVEN** a robot whose daemon offers no correlated grouped-torque
  confirmation capability, with face tracking enabled and a face in view
- **WHEN** the satellite runs
- **THEN** the head follows the face, no controller fault is raised for a closed
  command gate, and the motor switches stay absent as the unconfirmed-group
  contract requires of them

#### Scenario: A confirming daemon is unchanged

- **GIVEN** a robot whose daemon does offer correlated grouped-torque
  confirmation
- **WHEN** the satellite runs
- **THEN** the confirmed path is in force with its gating, its switches and its
  transition safety exactly as their own contract states, and nothing about this
  one applies

#### Scenario: Confirmation exists and fails

- **GIVEN** a robot whose daemon offers the confirmation capability and a group
  whose confirmation is refused, contradicted or absent
- **WHEN** the satellite constructs its motor state
- **THEN** that group is treated as an unconfirmed group rather than as an
  unconfirmable daemon, and its commands stay gated

### REQ-100: The motion-gating mode in force is reported

The satellite MUST report which motion-gating mode is in force and why, so that
an operator can tell an ungated stock robot from a confirmed one without
inferring it from whether the robot moved.

#### Scenario: An operator inspects a stock robot

- **GIVEN** a running satellite on a robot whose daemon offers no confirmation
  capability
- **WHEN** the operator reads the application's health or diagnostics surface
- **THEN** it names the ungated mode and the absent daemon capability that put
  it there, without naming any credential or installation identifier

#### Scenario: An operator inspects a confirming robot

- **GIVEN** a running satellite on a robot whose daemon offers the capability
- **WHEN** the operator reads the same surface
- **THEN** it names the confirmed mode, and the two robots are distinguishable
  from that report alone

### REQ-101: An unresolved identity starts the application rather than stopping it

The satellite MUST start and serve its settings interface when no announced
identity has been configured, reporting the identity as unresolved rather than
refusing to start.

#### Scenario: A freshly installed application is started

- **GIVEN** a robot with the application installed and no identity configured
  anywhere
- **WHEN** the daemon starts the application
- **THEN** it starts, its settings interface answers, and that interface states
  that the identity is unresolved and that nothing is announced until it is set

#### Scenario: An operator resolves the identity from a browser

- **GIVEN** a running application reporting an unresolved identity
- **WHEN** the operator supplies one through the settings interface
- **THEN** it becomes the announced identity without a shell session on the
  robot and without reinstalling the application

#### Scenario: An invalid identity is submitted

- **GIVEN** a running application reporting an unresolved identity
- **WHEN** the operator submits a value the identity contract does not accept
- **THEN** the submission is refused with the constraint stated, the identity
  stays unresolved, and the interface remains usable for another attempt

### REQ-102: Nothing is announced while the identity is unresolved

The satellite MUST NOT announce itself to Home Assistant, or serve a Home
Assistant connection, while its announced identity is unresolved.

#### Scenario: Home Assistant looks for an unconfigured robot

- **GIVEN** a running application whose identity is unresolved
- **WHEN** Home Assistant discovers devices on the network
- **THEN** no device is registered for this robot, and no entity, history or
  automation target is created that a later identity would have to displace

#### Scenario: The identity is resolved while Home Assistant is watching

- **GIVEN** a running application whose identity has just been set through its
  settings interface
- **WHEN** Home Assistant next discovers devices
- **THEN** exactly one device appears, under exactly the identity that was
  entered, with the entity identifiers that identity implies

#### Scenario: An unconfigured application is restarted

- **GIVEN** an application that has been started and stopped several times with
  its identity still unresolved
- **WHEN** it starts again
- **THEN** nothing has been announced by any of those runs, and the first
  announcement still waits for an identity

### REQ-103: Remote perception is optional at first start

The satellite MUST start with an unresolved groundstation address or credential
and run on local detection until both are supplied through a configuration
surface.

#### Scenario: A robot is started with no groundstation

- **GIVEN** a robot with neither a groundstation address nor a credential
  configured
- **WHEN** the application starts
- **THEN** it runs, its detection source is the local one, and its health
  reports the remote source as unconfigured rather than as failed

#### Scenario: A groundstation is supplied later

- **GIVEN** a running application with no groundstation configured
- **WHEN** the operator supplies an address and a credential through a
  configuration surface
- **THEN** the remote source is adopted under the existing replacement contract,
  without restarting or reinstalling the application

### REQ-104: The application installs through the daemon's own path

The satellite MUST be installable onto an unmodified robot through the daemon's
own application-installation path, without copying files onto the robot, opening
a shell on it, or editing any file its image ships.

#### Scenario: An operator installs the satellite on a stock robot

- **GIVEN** a stock robot reached only through its daemon's own surfaces
- **WHEN** the operator installs the published application source through the
  daemon's application-installation path
- **THEN** the daemon lists the application as installed and can start it, with
  no file copied onto the robot and no shipped file changed

#### Scenario: An installed application is upgraded

- **GIVEN** a robot running a previously installed version of the application
- **WHEN** the operator installs a later version through the same path
- **THEN** the later version is what the daemon starts, the announced identity
  and the operator's settings survive the upgrade, and no shipped file is edited

#### Scenario: The published source is inspected before installation

- **GIVEN** an operator deciding whether to install
- **WHEN** they read the published application source
- **THEN** it names the version it installs and carries no credential, no
  address and no identifier belonging to anybody's installation

### REQ-105: The daemon's start program is not assumed to be an interpreter

`reachyctl` MUST NOT execute the program named by a robot's daemon service unit
as though it were a Python interpreter.

#### Scenario: The unit starts a wrapper

- **GIVEN** a robot whose daemon service unit starts a shell wrapper rather than
  a Python interpreter
- **WHEN** a command needs to run Python inside the daemon's environment
- **THEN** it resolves an actual interpreter for that environment, or fails
  saying it could not, and never passes Python source to the wrapper

#### Scenario: The unit starts an interpreter directly

- **GIVEN** a robot whose daemon service unit does start a Python interpreter
- **WHEN** the same command runs
- **THEN** it uses that interpreter, so a robot that worked before keeps working

### REQ-106: Diagnosis and deployment start no second daemon

`reachyctl` MUST NOT start another instance of the robot's daemon, or take any
device, port or lock from the running one, as a side effect of diagnosing or
deploying.

#### Scenario: Diagnosis runs against a stock robot

- **GIVEN** a stock robot with its daemon running and holding its camera, its
  serial device and its network port
- **WHEN** the operator runs diagnosis
- **THEN** exactly one daemon is running afterwards, it still holds all three,
  and each check reports what it found rather than what a competing process did
  to it

#### Scenario: Diagnosis reports an unreachable daemon

- **GIVEN** a robot whose daemon is genuinely not running
- **WHEN** the operator runs diagnosis
- **THEN** the failing check names the daemon as the broken link and starts
  nothing in its place

## Design

### Acting on a distinction that is already drawn

The application's daemon boundary answers two separate questions and already
answers them separately: whether the robot's daemon offers grouped-torque
confirmation at all, and what a particular confirmation attempt returned. An
absent surface produces an *unavailable* result rather than a failed one, so the
fact REQ-099 turns on is present in the process and correct.

What no consumer does is act on it. Unavailable and failed both mean "not
confirmed", and "not confirmed" closes the gate, so a daemon that was never
capable of confirming anything is treated exactly like a group whose
confirmation went wrong. REQ-099 is that consequence and nothing more: where the
capability is absent there is no torque state to protect and no gate worth
holding, so the satellite commands motion.

The decision belongs at composition, once per process, rather than per group or
per command. Two modes follow from it. The confirmed mode is the one that exists
today and nothing about it changes. The ungated mode is the command path the
application had before confirmation existed at all — motion issued directly,
with no gate and no motor switch, which is what the unconfirmed-group half of
REQ-093 already requires and is not restated here.

A capability that is present and answers badly stays a failed confirmation. That
is the third scenario, and it is what keeps the degradation from becoming a way
to switch the safety contract off by breaking something.

### Saying which mode is in force

The mode belongs beside the bounded, identifier-free motor diagnostics that
REQ-093 already requires, because an operator reading "no motor switches" needs
the next sentence to say whether that is the daemon's doing or a group's. It is
a static fact per process, so it costs one field rather than a stream of events.

### An identity-shaped hole rather than a refusal

The identity stays mandatory in the sense that matters: nothing is announced
without it. What changes is when the refusal happens. Configuration resolves to
a state in which the identity is either set or explicitly unresolved, and only
the announcing surface treats unresolved as fatal — by not existing yet. The
settings interface, the health surface and the daemon's own view of the
application all come up regardless.

That keeps REQ-040's hazard intact. The destructive outcome it guards against is
announcing under a *wrong* identity; announcing under none is not a lesser
version of that, it is the absence of it. An operator who starts the application
unconfigured has created no Home Assistant device, so there is nothing for the
eventual correct identity to collide with, split from or orphan.

The groundstation address and credential follow the same shape for a smaller
reason: they were never fatal, and a fresh installation should reach the
settings page with the local detector already running rather than reach it at
all only after somebody has guessed a URL. Replacing a configured groundstation
at run time is REQ-095's contract and is unchanged; arriving at the first one is
this contract's.

### Distribution through the daemon's own path

The daemon installs an application by fetching a published source directory and
installing it into the robot's shared application environment, then discovering
what it installed through the standard entry point group that
[REQ-041](../ha-satellite/index.md#req-041-the-application-is-discoverable-by-the-robot-daemon)
already covers. Publishing such a source is therefore additive: the same wheel,
reached a second way. GitHub Releases stays the artifact of record, and the
published source names a version rather than carrying a copy of the code.

The source is authored in this repository and published from it, so what an
operator installs is reviewable here. It carries no credential, no address and
no identity — those are what the settings interface is for, and REQ-101 is what
makes that sequence work on a robot with nothing configured.

### Asking the robot for its interpreter

The tool already refuses to assume machine state and asks systemd instead, which
is the right instinct pointed at the wrong property: the unit's start program is
the daemon's *entry point*, and only on some images is that also a Python
interpreter. The question to ask is which interpreter owns the daemon's package
environment, and the robot can answer it without the unit being consulted at
all.

The failure this prevents is worse than a wrong answer. Passing Python source to
a launcher runs the launcher, which starts a second daemon, which then competes
for the hardware the first one holds — so a diagnosis that was meant to observe
the robot perturbs it, and the checks that follow measure the perturbation.
REQ-106 states the property directly rather than leaving it implied by REQ-105,
because "resolve the interpreter correctly" and "never start a second daemon"
fail independently and the second is the one an operator notices.

## Constraints

- The three Home Assistant motor switches stay absent on a robot whose daemon
  cannot confirm torque. Announcing them there is outside this contract, and
  their behavior on a confirming daemon belongs to REQ-093 and REQ-094.
- No change to the robot-link wire format, the ESPHome entity model, the
  announced identity's role as the Home Assistant device key, or the durable
  machine state provisioning owns.
- No dependency on an unreleased or forked daemon build from any manifest. The
  released dependency range stays as it is, and the ungated mode is what a robot
  gets until an official release carries the confirmation capability.
- Nothing is published to a Python package index by this contract.
- The application-installation path is the daemon's own; no file the robot image
  ships is edited and no shell session on the robot is required at any point.
- Hardware-free tests use fakes for the robot, its daemon and Home Assistant.
  Steps that need a real robot are marked pending verification rather than given
  invented output.

## Open Questions

- **Whether the ungated mode should be operator-visible as a setting.** It is
  currently derived from the daemon and not chosen, which means an operator
  cannot ask a confirming robot to behave like a stock one for comparison.
  Current default: derived, not configurable.
- **Whether an unresolved identity should expire.** An application left
  unconfigured announces nothing indefinitely, which is safe and silent. Current
  default: no expiry, with the state visible on the health surface.

## References

- [HA Satellite REQ-040, REQ-041, REQ-049](../ha-satellite/index.md#requirements)
- [Home Assistant Configuration and Camera Feed REQ-093–095](../home-assistant-configuration-and-camera-feed/index.md#requirements)
- [reachyctl REQ-051, REQ-054, REQ-055](../reachyctl/index.md#requirements)
- [Architecture REQ-005 and REQ-009](../architecture/index.md#req-005-behaviour-is-testable-without-hardware)
- [Provisioning](../provisioning/) — the owner of durable machine state
- [Groundstation](../groundstation/)

## Changelog

| Date | Change | Document |
|------|--------|----------|
| 2026-09-06 | Initial spec created | [0021-stock-robot-installation](../../changes/0021-stock-robot-installation.md) |
