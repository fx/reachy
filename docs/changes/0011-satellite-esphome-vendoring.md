# 0011: Vendor the ESPHome satellite core

## Summary

Bring the ESPHome protocol layer into `apps/ha-satellite` as a vendored module
with full attribution and per-file provenance, trimmed to what this application
needs and cut at the seams the Reachy adapters will replace.

**Spec:** [HA Satellite](../specs/ha-satellite/)
**Status:** complete
**Depends On:** 0001

## Motivation

The ESPHome native API is what makes Home Assistant treat the robot as a voice
device rather than as something to be scripted at. Reimplementing it would be
substantial work with a large surface for subtle incompatibility, and the Home
Assistant project publishes a Linux voice satellite under Apache-2.0 that
already does it.

It is not published to a package index, so it cannot be a dependency. Vendoring
is the remaining option, and doing it as a discrete change keeps the
third-party code and its attribution reviewable on its own rather than buried
inside a larger feature branch.

This is also the change that closes out the licensing problem. The application
being replaced is a fork of a project that ships no licence file while declaring
one in package metadata; none of it is carried forward.

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

Vendored code is an exception to the strict-typing rule and MUST be recorded as
one: it carries a module-level relaxation with a comment naming it as vendored,
rather than being silently excluded from the type check. Its upstream tests are
carried over where they exercise code that is kept.

### Functional requirements

The [ha-satellite spec](../specs/ha-satellite/) owns the application's
behaviour, and
[architecture REQ-007](../specs/architecture/index.md#req-007-vendored-third-party-code-is-attributed-in-place)
owns the attribution rule. What implementing them requires of this change:

- The vendored directory carries the upstream licence text and a notice naming
  the upstream project, the files derived from it, and the commit they were
  taken at. Each file carries a header recording its own upstream path and
  commit.
- What is carried is the protocol, entity model, wake-word handling and
  discovery. What is discarded is the desktop audio capture, the media-player
  playback path, and the command-line entry point.
- The audio seams are cut and left as explicit interfaces to be satisfied in
  0012. This change does not implement them; it makes the shape of the hole
  deliberate rather than incidental.
- The vendored module imports nothing Reachy-specific, enforced by a lint rule
  on import direction. The dependency runs one way, so upstream code never grows
  a robot dependency.
- No code from the previously forked application is carried over in any form.

#### Scenario: A Reachy import is added inside the vendored module

- **GIVEN** a change adding an import of a Reachy adapter inside the vendored
  directory
- **WHEN** the lint check runs
- **THEN** it fails, keeping the dependency direction one-way

## Design

### Approach

Copy the modules that are kept, then trim. The upstream is roughly 4.9k lines of
which about 3.5k is carried: protocol, API server, entity model, message types,
wake word, discovery. The command-line entry point is roughly 850 lines of
argument parsing and wiring and is discarded entirely — the application is
started by the robot daemon, not from a shell.

The two audio seams are the substantive edit. Upstream captures through a
desktop sound library and plays back through a media-player process; on this
robot both belong to the daemon. Each becomes a narrow interface at the point
the vendored code used the library directly.

### Decisions

- **Decision**: Vendored in place, not a separate workspace member with an
  upstream sync workflow.
  - **Why**: A syncable subtree only pays off when local patches stay small.
    Both audio seams are replaced and the entry point is discarded, so every
    sync would be a conflict resolution wherever the code sits — a separate
    member would buy the appearance of a mirror without the property.
  - **Alternatives considered**: A `packages/reachy-esphome` member with a
    scheduled upstream-diff workflow, which was the initial plan and does not
    survive counting the patches.
- **Decision**: Per-file provenance headers, not just a directory notice.
  - **Why**: Files get moved and refactored. A header travels with the file;
    a directory-level notice does not.
- **Decision**: Upstream drift is reported, not merged.
  - **Why**: This is a derivation, not a mirror, and machinery that implies
    otherwise would mislead. A scheduled job that reports divergence in the
    files actually derived from upstream gives the useful half without the
    false promise.
- **Decision**: The lint rule on import direction ships with the vendoring.
  - **Why**: The boundary erodes the first time something is convenient, and it
    is much harder to restore than to establish.
- **Decision**: The asset licence gate is owned by this change, not shared with
  the model registry in 0005.
  - **Why**: Sharing the registry would make the satellite's vendoring wait on
    the whole groundstation line, which it is otherwise independent of. The two
    registries deliberately take the same shape so they can be unified later if
    that is ever worth a dependency; today it is not.
  - **Alternatives considered**: Depending on 0005 for its allowlist test, which
    orders phase-three work behind phase-one work for no benefit.

### Non-Goals

- No adapters, no audio, no motion — 0012.
- No behaviour logic, settings interface, or packaging — 0013.
- No functional changes to the vendored protocol beyond cutting the seams.

## Tasks

- [x] Vendor and attribute
  - [x] Copy the protocol, entity, message, wake-word and discovery modules
  - [x] Upstream licence text and notice in the vendored directory
  - [x] Per-file provenance headers naming upstream path and commit
  - [x] Module-level type-check relaxation with a comment naming it as vendored
- [x] Trim to what is needed
  - [x] Discard the command-line entry point and its wiring
  - [x] Discard the desktop capture and media-player playback paths
  - [x] Carry over upstream tests covering retained code
- [x] Cut the seams
  - [x] Define the capture interface at the point capture was performed
  - [x] Define the playback interface at the point playback was performed
  - [x] Verify the module imports and its tests pass with both seams unfilled
- [x] Gate the shipped assets on licence
  - [x] Record every wake-word model and sound asset's source and licence in an
        asset registry owned by this change
  - [x] Add a licence-allowlist test over that registry, so an asset with
        unacceptable terms fails CI rather than shipping in a wheel
- [x] Enforce the boundary
  - [x] Lint rule forbidding Reachy imports inside the vendored directory
  - [x] Scheduled job reporting upstream drift in the derived files

## Open Questions

- [x] **Which wake-word assets ship in the wheel.** Resolved: two microWakeWord
      models, `okay_nabu` and `stop`. That is the smallest set that works — a
      default wake word, and the stop word the protocol needs to interrupt a
      response or silence a ringing timer — and it leaves roughly four megabytes
      of upstream's model collection out of both the wheel and this repository's
      history. Nothing is lost by it: Home Assistant can activate a wake word
      the robot does not have, and the vendored protocol downloads it on demand
      into the download directory, where it is not a shipped asset and so not
      the registry's concern. Both models are Apache-2.0 and both pass the
      licence gate; so do the nine sounds, under CC BY 4.0 with the attribution
      the licence requires carried beside them.
- [x] **Whether the entity model is trimmed or carried whole.** Resolved:
      carried whole. Trimming would have meant editing `entity.py` and
      `satellite.py` to remove entities — precisely the kind of edit that makes
      every future upstream comparison a manual reconciliation, in exchange for
      a surface reduction that 0013 can achieve without touching upstream code
      at all, by choosing what it constructs. The peripheral WebSocket API came
      across for the same reason: `satellite.py` imports its event enumeration,
      so removing it would have meant rewriting the protocol layer rather than
      declining to start it.

## References

- Spec: [HA Satellite](../specs/ha-satellite/)
- Related changes: [0012-satellite-ports-and-adapters](./0012-satellite-ports-and-adapters.md)
- [Linux voice assistant](https://github.com/OHF-Voice/linux-voice-assistant) — Apache-2.0 upstream
