# 0021: Stock robot installation

## Summary

Implement the proposed
[Stock Robot Installation](../specs/stock-robot-installation/) contract in four
reviewable pull requests: graceful motor degradation on a released daemon,
browser-reachable first-time configuration, an interpreter resolution that does
not start a second daemon, and then the installable application source, its
release wiring, the runbooks and final traceability.

**Spec:** [Stock Robot Installation](../specs/stock-robot-installation/)
**Status:** draft
**Depends On:** 0020

## Approval

The operator asked:

> the satellite/daemon thing ...how would we install this on a stock robot? I
> can't assume people want to copy files around or modify stock files or whatever

and, when told what it would take:

> ok uh, I trust you're right, do it end to end and if we really need a HF repo
> to cleanly install onto the robot (it's a HF robot after all), we can create
> one for all I care

That is an approval of the product and distribution decisions now owned by
[REQ-099–106](../specs/stock-robot-installation/index.md#requirements),
including publishing an application source for the daemon's own installation
path, which reverses a recorded non-goal. This document records the
implementation sequencing and rationale; it does not repeat the observable
contract.

## Motivation

The satellite is installable today by someone with a shell on the robot. On a
robot that is only reached through the surfaces its shipped image exposes, it is
not installable at all — and where it has been installed by hand, it does not
move.

**The evidence is measured, not inferred.** A real Reachy Mini running
ReachyMiniOS v0.2.3 with the released `reachy-mini` 1.9.0 was driven with a
hand-installed satellite build. It tracked faces and streamed frames for the
whole session and **never moved once**. Every motor group's command gate stayed
shut because the daemon offers no correlated grouped-torque read-back, the
controller reported a command fault, and safe hold stayed engaged. The absent
switches were correct — that is what
[0020](./0020-home-assistant-configuration-and-camera-feed.md) says happens
without the capability, and what `README.md` records — but the frozen robot was
not, and nothing in that change's completion notes claims it should be.

The same session produced the other two failures. A first-time configuration
cannot be reached, because the announced identity has no default and its absence
stops the application before its settings interface is served; the only
remaining route is writing configuration onto the robot over a shell, which is
the thing being removed. And `reachyctl` reads the daemon's service unit and
treats the program it starts as a Python interpreter — on that image it is a
shell launcher, so the tool **started a second daemon**, which was watched
contending for the daemon's network port, its serial device and its camera
before dying. Deployment failed at its first step and three diagnostic checks
reported the robot as broken when what was broken was the diagnosis.

Distribution is the last piece. The daemon can install an application from a
published source and then discover it through the same standard entry point the
wheel already declares, so publishing one is additive rather than a second
packaging story. That it has not been done is a recorded decision — the
[architecture](../specs/architecture/index.md#versioning-and-distribution) and
[HA Satellite](../specs/ha-satellite/index.md#packaging-and-deployment) design
notes both say a wheel is sufficient — and it is sufficient only for someone
with a shell. The operator has authorised reversing it.

## Requirements

### Testing Requirements

This change MUST satisfy the project's standing testing rules (see
[Testing conventions](../specs/architecture/index.md#testing-conventions)). CI
enforces these as merge gates:

- Tests MUST run with `pytest`, with async strict mode enabled.
- Unit tests MUST perform no input or output: no sockets, no filesystem access,
  no wall-clock sleeping. The robot, its daemon, its capability surface, time
  and Home Assistant MUST be supplied through deterministic fakes.
- Integration tests that exercise HTTP or the ESPHome transport MUST use real
  in-process transports and carry the repository's socket marker.
- No test may require a robot, a camera, a microphone or a Home Assistant
  instance, and none may require a Hugging Face account or network access.
- Coverage MUST be gated on the diff, and new modules MUST pass strict type
  checking.
- Every lint or type suppression MUST carry its rule identifier and a reason.

Skipping or weakening any of these rules to land a pull request MUST be treated
as a bug in that pull request, not in the rule.

Each implementation task runs its focused suites plus `just check`,
`just contracts-check`, `just leak-scan` and `just secret-scan`. The final task
also runs `just duvet` after registration and snapshot regeneration.

### Functional requirements

The
[Stock Robot Installation requirements](../specs/stock-robot-installation/index.md#requirements)
own graceful motor degradation and its observability, startup with an unresolved
identity, the announcement embargo that goes with it, optional remote
perception, installation through the daemon's own path, and the two `reachyctl`
properties. Their scenarios are this change's acceptance criteria and are not
restated here. What implementing them requires of this change:

- The first task MUST separate "this daemon cannot confirm torque" from "this
  group's confirmation failed" at the daemon boundary, and select the mode once
  per process rather than per command, so a broken confirmation cannot open the
  gate that an absent capability opens.
- The first task MUST NOT weaken the serialization, quiescing, reseeding or
  terminal-release guarantees that
  [0020](./0020-home-assistant-configuration-and-camera-feed.md) delivered for
  the confirmed path, and MUST leave that path byte-for-byte in force where the
  capability exists.
- The second task MUST make the unresolved identity a resolved configuration
  state rather than a startup failure, and MUST keep the announcing surface —
  not the settings interface, the health surface or the daemon's view of the
  application — the only thing that treats it as fatal.
- The second task MUST reach the same outcome for an unresolved groundstation
  address and credential without duplicating the replacement owner
  [0020](./0020-home-assistant-configuration-and-camera-feed.md) built; arriving
  at a first groundstation goes through the same owner as replacing one.
- The third task MUST resolve the daemon's interpreter from the daemon's own
  environment, MUST keep working on an image whose unit does start an
  interpreter, and MUST fail with a named reason rather than falling back to a
  path that might not be one.
- The fourth task MUST publish an application source that names a released
  version rather than carrying a second copy of the code, and MUST NOT add a
  dependency on an unreleased or forked daemon build to any manifest.
- The fourth task MUST extend the runbooks and deployment reference delivered by
  [0015](./0015-docs-and-runbooks.md) and updated by
  [0020](./0020-home-assistant-configuration-and-camera-feed.md) rather than
  creating parallel instructions, and MUST correct the design notes'
  distribution claim wherever it is not owned by a spec.
- Runbooks MUST contain output actually observed. Any step needing a real robot
  remains marked pending hardware verification until it is run; invented
  transcripts are not acceptance evidence.

## Design

### Approach

#### Task 1 — graceful motor degradation

The daemon boundary in `apps/ha-satellite` currently catches the absence of the
confirmation methods and returns an `unavailable` confirmation, which every
caller reads as a failed confirmation. Split that into a capability probe
performed once where the boundary is constructed, and the per-request result it
already returns. The probe's outcome selects the coordinator's mode for the
process's lifetime.

The confirmed mode is the existing one and is not touched. The ungated mode
keeps the coordinator — its per-group serialization is what stops two producers
commanding the same hardware and is unrelated to torque — and holds the gate
open, so `ReachyMotion` commands succeed and `command_gaze` no longer raises.
Registration of the three switches stays exactly where it is: an unconfirmed
group gets none, which is
[REQ-093](../specs/home-assistant-configuration-and-camera-feed/index.md#req-093-home-assistant-configuration-reports-effective-state)'s
contract and not this change's to restate or relax.

The mode goes into the bounded, identifier-free motor diagnostics that change
already publishes, as one static field beside the per-group records, and into
the health surface the settings interface reads.

#### Task 2 — browser-reachable bootstrap configuration

`device_name` stops being a field with no default and becomes a field whose
unresolved state is representable. Startup validation then has three outcomes
rather than two: valid, invalid, and unresolved — and only the first announces.
`daemon_app.main` stops exiting on the third; the ESPHome server is what does
not start, while the settings interface, the health surface and the application
lifecycle come up.

The settings interface gains an explicit unconfigured presentation: it states
that nothing is announced, names what is missing, and accepts the value. Setting
an identity brings the announcing surface up without a restart if that is
achievable within the existing lifecycle, and otherwise restarts the announcing
surface alone — the observable contract is REQ-101's and REQ-102's, and neither
requires a process restart nor forbids one, so the task picks whichever keeps
the announcement embargo provable.

The groundstation address and credential take the same shape and route their
first resolution through the replacement owner rather than a second path.

#### Task 3 — `reachyctl` daemon-interpreter resolution

`cli/reachyctl/src/reachyctl/daemon.py::interpreter` stops returning the unit's
`ExecStart` path. It asks the robot for the interpreter that owns the daemon's
package environment, which is a question about the environment rather than about
the unit, and reports what it found. The configured `--python` override stops
being consulted only when the unit declares no `ExecStart` at all and becomes an
explicit answer an operator can supply.

Failure to resolve is a named error, not a fallback: a path that might not be an
interpreter is what produced the second daemon. The change is behind the same
boundary `doctor` and `deploy` already call, so the three affected checks and
the deploy step are fixed by fixing it once.

#### Task 4 — installable app source, release wiring, runbooks and registration

Add a published application-source directory to `apps/ha-satellite/`, containing
the metadata the daemon's installation path expects and a dependency on the
released wheel by version. It carries no credential, no address and no identity.
A `Justfile` recipe publishes it, and the release workflow bumps the version it
names on a version tag, alongside the wheel it points at.

The runbooks then get the stock-robot route as a first-class path beside the
existing one: install through the daemon, open the settings interface, resolve
the identity, then the groundstation. `docs/ops/satellite-deployment.md` and the
`README.md` claim that no application source is published stop being true and
are corrected. The two design notes that say the same thing live in
`docs/specs/architecture/` and `docs/specs/ha-satellite/`, which an implementing
change may not edit; the spec's Background records the conflict and reconciling
it is a separate `/spec-writer` proposal.

This task registers the spec in `.duvet/config.toml`, regenerates the snapshot
and flips this document's Status.

### Decisions

- **Decision:** Detect the daemon's confirmation capability once and degrade to
  ungated motion, rather than treating an absent capability as a failed
  confirmation.
  - **Why:** The two are different facts and only one of them is a reason to
    stop moving. Collapsing them made a robot that cannot confirm torque a robot
    that cannot move, which is a strictly worse outcome than the ungated motion
    every build before the confirmed path shipped already performed.
  - **Alternatives considered:** Keeping the refusal and requiring the forked
    daemon on every robot; a configuration flag to disable the gate; treating
    any confirmation failure, including a real one, as licence to open the gate.
- **Decision:** Keep the motor switches absent in the ungated mode.
  - **Why:** A switch whose state nothing can confirm is the optimistic state
    0020 refused to ship, and the operator's decision there has not changed.
    Moving is not the same claim as reporting torque.
  - **Alternatives considered:** Announcing optimistic switches in the ungated
    mode; announcing read-only switches; announcing them as unavailable, which
    the existing switch wire cannot express.
- **Decision:** Make the unresolved identity a startup state rather than a
  startup failure, and put the embargo on the announcing surface.
  - **Why:** The hazard REQ-040 guards is announcing under the *wrong* identity.
    Announcing under none is the absence of that hazard, not a weaker form of
    it, and a refusal that hides the only surface capable of fixing it is a
    deadlock rather than a safeguard.
  - **Alternatives considered:** A derived default from the package or host
    name; a first-run wizard on the daemon's dashboard; requiring an operator to
    supply the identity at install time through the installation path.
- **Decision:** Ask the robot which interpreter owns the daemon's environment
  instead of reading the unit's start program.
  - **Why:** The unit names the daemon's entry point, and only on some images is
    that also an interpreter. Executing it with Python arguments starts a second
    daemon that competes for the hardware the first holds, so the diagnosis
    perturbs what it measures.
  - **Alternatives considered:** Detecting a shell wrapper and parsing it;
    requiring `--python` on every invocation; hard-coding the released image's
    interpreter path, which is the assumption the tool exists to avoid.
- **Decision:** Publish an application source for the daemon's installation path
  and keep the wheel on GitHub Releases as the artifact of record.
  - **Why:** The daemon discovers an application through the same standard entry
    point either way, so the source is a second route to one artifact rather
    than a second packaging story. It is what removes the file copying the
    operator objected to.
  - **Alternatives considered:** Publishing to a Python package index; a
    one-line installation command the operator pastes into a shell, which still
    needs a shell; asking to be added to the manufacturer's curated application
    list, which is not ours to schedule.

### Non-Goals

- No upstreaming of the forked correlated torque read-back and no upstream pull
  request. It is a daemon-side change no packaging can carry, it is already a
  standing item in [`docs/tasks.md`](../tasks.md), and nothing here substitutes
  for it.
- No replacement of the daemon's own environment on any robot, and no dependency
  on the fork from any manifest.
- No publication to a Python package index.
- No motor switches on a robot whose daemon cannot confirm torque. Their absence
  there is the contract, not a gap.
- No change to the robot-link wire format, the ESPHome entity model, the gaze
  and trajectory calibration, or the durable machine state provisioning owns.
- No edit to `docs/specs/architecture/` or `docs/specs/ha-satellite/`. Their
  distribution design notes are contradicted by this contract and correcting
  them is a separate proposal.

## Prerequisites and Risks

- **Publishing the application source needs credentials this repository does not
  have.** There is no Hugging Face token in the development environment, so the
  deliverable is the source in-repo, a one-command publish recipe and a runbook.
  The one-time repository creation and the authentication are the operator's,
  and the runbook step that needs them is marked pending until it is run.
- **The ungated mode is a real behaviour change on a real robot.** A robot that
  has been standing still since the confirmed path shipped will start moving
  when this lands. That is the intent, and the staged verification below brings
  it up one group at a time with an abort path rather than all at once.
- **The confirmed path must stay exactly as it is.** The degradation is
  selected by a capability probe, so a bug in the probe is a bug that silently
  disables the safety contract on a robot that has it. The acceptance matrix
  drives both modes and the capability-present-but-failing case explicitly.
- **A second daemon may already have been started on a robot under diagnosis.**
  An operator who has run the current `deploy` or `doctor` against a stock robot
  may have an orphaned process or a wedged device. The runbook says how to check
  and how to recover, and the fix removes the cause.

## Tasks

Tasks 1, 2 and 3 are **independent of one another and run in parallel**: they
touch disjoint code — the motor path, the configuration and announcement path,
and the CLI's daemon boundary — and none needs another's output. **Task 4
depends on all three** and is the **final pull request**: it is the one that
makes the stock-robot route real end to end, and registering the spec before its
requirements have implementations to annotate would be a red traceability job
waiting on work that does not exist.

- [ ] Task 1 — Degrade to ungated motion on a daemon without torque
      confirmation (`apps/ha-satellite`)
  - [ ] Probe the daemon's grouped-torque confirmation capability once where the
        daemon boundary is constructed, and distinguish its absence from a
        confirmation that ran and failed
  - [ ] Select the coordinator's mode from that probe for the process's
        lifetime, keeping per-group serialization in both modes and holding the
        command gate open only in the ungated one
  - [ ] Leave the confirmed path's gating, quiescing, reseeding, ownership and
        terminal-release behaviour unchanged, and leave switch registration
        governed by the unconfirmed-group contract it already has
  - [ ] Report the mode in force and the reason for it on the bounded
        identifier-free motor diagnostics and the health surface, with no
        credential or installation identifier
  - [ ] Cover both modes, a present-but-failing capability, a partially
        answering capability, motion under the ungated mode, the absence of
        switches under it, and shutdown and safe-hold behaviour in both, with
        fakes and no hardware
  - [ ] Run the focused satellite suites and the repository checks required
        above

- [ ] Task 2 — Start and configure without a shell on the robot
      (`apps/ha-satellite`)
  - [ ] Make an unresolved announced identity a representable configuration
        state rather than a validation failure, keeping every other identity
        constraint as it is
  - [ ] Start the application, its settings interface and its health surface
        with an unresolved identity, and start no announcing surface
  - [ ] Present the unconfigured state explicitly in the settings interface,
        naming what is missing and that nothing is announced until it is set
  - [ ] Adopt an identity supplied through the settings interface without a
        shell session and without reinstalling, announcing exactly it
  - [ ] Give an unresolved groundstation address and credential the same
        treatment, routing their first resolution through the existing
        replacement owner rather than a second path, and report the remote
        source as unconfigured rather than failed
  - [ ] Cover unresolved, invalid and resolved identities, repeated restarts
        while unresolved, the announcement embargo, adoption, and first-time
        groundstation resolution with local fallback, with fakes and no Home
        Assistant instance
  - [ ] Update the configuration and settings-interface documentation for the
        new startup states
  - [ ] Run the focused satellite suites and the repository checks required
        above

- [ ] Task 3 — Resolve the daemon's interpreter without starting a second daemon
      (`cli/reachyctl`)
  - [ ] Resolve the interpreter that owns the daemon's package environment from
        the environment itself rather than from the unit's start program
  - [ ] Keep an image whose unit does start an interpreter working, and make the
        configured override an explicit answer rather than a last resort
  - [ ] Fail with a named, remediable error when no interpreter can be resolved,
        never falling back to a path that might not be one
  - [ ] Confirm the three affected checks and the deploy step report what they
        found once the boundary is fixed, without each carrying its own
        workaround
  - [ ] Cover a wrapper-started unit, an interpreter-started unit, an
        unresolvable environment, an uninstalled unit and the override, proving
        no Python source is ever handed to the unit's program
  - [ ] Update the troubleshooting entries for the affected checks
  - [ ] Run the focused CLI suites and the repository checks required above

- [ ] Task 4 — Publish the installable application source and complete the
      change (**FINAL**, depends on tasks 1, 2 and 3)
  - [ ] Add the application-source directory to `apps/ha-satellite/`, naming the
        released wheel by version and carrying no credential, address or
        identity
  - [ ] Add the `Justfile` recipe that publishes it and the release wiring that
        moves its version with everything else, adding it to
        `release-please-config.json` if it declares one
  - [ ] Cover the source's contents and the recipe's refusals without a network,
        an account or a token
  - [ ] Add the stock-robot route to the setup and operations runbooks — install
        through the daemon, resolve the identity in a browser, then the
        groundstation — marking every step that needs a real robot pending
        hardware verification rather than inventing output
  - [ ] Correct the "no application source is published" claim in
        `docs/ops/satellite-deployment.md` and `README.md`, and record that the
        two design notes under `docs/specs/` say otherwise and need their own
        proposal
  - [ ] Record how to detect and recover from a second daemon started by an
        earlier diagnosis
  - [ ] Add exact annotations for REQ-099 through REQ-106, register
        `docs/specs/stock-robot-installation/index.md` in `.duvet/config.toml`
        with `format = "markdown"`, regenerate the snapshot from the repository
        root and run `just duvet`
  - [ ] Correct the spec Overview's "proposed and not yet implemented" sentence
        and add its `## Changelog` row. **This document authorises exactly those
        two edits under `docs/specs/stock-robot-installation/` and nothing
        else** — leaving the sentence stale is the defect
        [`docs/tasks.md`](../tasks.md) already tracks across eight specs, and
        repeating it here deliberately would be worse than the narrow exception
  - [ ] Update the spec and requirement counts in `AGENTS.md`, `REVIEW.md`, the
        member `AGENTS.md` files and `.duvet/config.toml`'s header, mark 0021
        complete and synchronise `docs/index.yml` and `docs/index.md`
  - [ ] Run the repository checks required above

## Verification Stages

1. **Deterministic motor acceptance:** drive REQ-099 and REQ-100 through an
   absent capability, a present one, and a present one that fails, asserting
   that motion happens in the first, that the confirmed path is untouched in the
   second, and that the third is treated as an unconfirmed group.
2. **Deterministic configuration acceptance:** drive REQ-101 to REQ-103 through
   unresolved, invalid and resolved identities, repeated restarts, the
   announcement embargo, browser adoption and first-time groundstation
   resolution, with no Home Assistant instance.
3. **Deterministic CLI acceptance:** drive REQ-105 and REQ-106 over a
   wrapper-started unit, an interpreter-started unit and an unresolvable
   environment, proving nothing hands Python source to the unit's program.
4. **Deterministic packaging acceptance:** drive REQ-104's inspectable-source
   scenario against the committed source, and the publish recipe's refusals,
   without a network or an account.
5. **Staged live verification:** on a stock robot, install through the daemon's
   path, resolve the identity in a browser, confirm nothing was announced
   before, then bring motion up one motor group at a time with an abort path.
   Run diagnosis and confirm exactly one daemon remains.
6. **Evidence:** scrub every recorded outcome per the repository's runbook
   convention, and leave unrun hardware steps marked pending rather than giving
   them invented output.

## Open Questions

None for implementation approval. Whether the ungated mode should become an
operator-visible setting, and whether an unresolved identity should expire, are
recorded as open questions in the spec and neither blocks this work.

## References

- Spec: [Stock Robot Installation](../specs/stock-robot-installation/)
- Dependencies:
  [0020-home-assistant-configuration-and-camera-feed](./0020-home-assistant-configuration-and-camera-feed.md)
- Parent contracts:
  [HA Satellite](../specs/ha-satellite/),
  [reachyctl](../specs/reachyctl/),
  [Home Assistant Configuration and Camera Feed](../specs/home-assistant-configuration-and-camera-feed/),
  [Architecture](../specs/architecture/)
- Outstanding elsewhere: the upstream contribution of the correlated torque
  read-back, tracked in [`docs/tasks.md`](../tasks.md)
