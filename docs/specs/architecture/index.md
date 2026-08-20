# Architecture

## Overview

This repository is a public monorepo for the Reachy Mini robot. It holds four
deployable components — a robot-side Home Assistant voice satellite, an
off-robot groundstation service, a `reachyctl` CLI, and Ansible provisioning —
plus one shared contracts package and a benchmark suite.

This spec covers project-level concerns only: workspace layout, tooling,
versioning and distribution, CI gates, testing conventions, repository hygiene,
and documentation conventions. Component behaviour lives in the feature specs
listed under [References](#references).

Nothing in this repository is implemented yet. Every requirement here describes
the intended end state, and the change documents in `docs/changes/` sequence the
work that gets there.

## Background

The components grew out of a working but hand-assembled stack: a vision service
that scraped the robot's MJPEG stream and posted detections back, plus a forked
third-party Home Assistant app running on the robot. That stack produced the
performance baseline recorded in [benchmarks](../benchmarks/), and it exposed
three problems this repository exists to fix.

The first was structural. Detections travelled over per-request HTTP
connections to a listener on the robot, and a cold TCP handshake to the robot
measured 378 ms at p50 — so connection reuse was the difference between 22 ms
and 1241 ms per post. Reuse was achieved by convention, which meant any future
caller could silently reintroduce the cost. The [robot-link](../robot-link/)
protocol replaces that with a single long-lived session.

The second was licensing. The forked app carried no LICENSE file despite
declaring one in its package metadata, and the face model in use derived from an
AGPL-3.0 codebase. Neither is publishable. Both are resolved by rewriting from
permissively licensed sources — see the decision records in
[ha-satellite](../ha-satellite/) and [perception](../perception/).

The third was configuration. Environment overrides in the old app were read by a
function nothing ever called, so every value that looked tuned was in fact a
dataclass default. That failure was silent for months. Configuration handling in
this repository is designed so the equivalent mistake cannot stay quiet.

## Requirements

### REQ-001: Single resolved dependency set

The repository MUST resolve all workspace members against one committed lockfile,
and continuous integration MUST install from that lockfile without re-resolving.

#### Scenario: A member adds a dependency

- **GIVEN** a pull request that adds a dependency to one workspace member
- **WHEN** continuous integration installs the workspace
- **THEN** the run fails unless the committed lockfile already contains that
  dependency, so the lockfile update travels in the same pull request

#### Scenario: Two members disagree on a version

- **GIVEN** two members that transitively require incompatible versions of a
  shared dependency
- **WHEN** the lockfile is regenerated
- **THEN** resolution fails locally rather than producing two different resolved
  versions across members

### REQ-002: One version for the whole repository

Every artifact published from this repository MUST carry the same version
string, derived from a single repository-wide version.

#### Scenario: A release is cut

- **GIVEN** a release that changes only the groundstation
- **WHEN** the release is published
- **THEN** the container image, every wheel, and the git tag all carry the same
  version, so the deployed combination is identifiable from the tag alone

### REQ-003: No environment-specific values in version control

Hostnames, IP addresses, account names, credentials, and deployment-target
identifiers MUST NOT appear in tracked files, and each such value MUST be
supplied at run time from an untracked local file or a repository secret with a
tracked `.example` sibling documenting its shape.

#### Scenario: A developer configures a robot address

- **GIVEN** a developer setting up against their own robot
- **WHEN** they copy the tracked example inventory and fill in a real address
- **THEN** the filled-in file is ignored by version control and the example
  remains the only tracked copy

### REQ-004: Automated leak detection on every change

Continuous integration MUST reject a change whose diff or commit messages match
a generic environment-leak pattern, where the pattern set describes shapes such
as private IP ranges, internal hostname suffixes, and email addresses rather
than any specific name.

#### Scenario: A private address is committed

- **GIVEN** a pull request that adds a line containing an RFC 1918 address
- **WHEN** the leak scan runs
- **THEN** the check fails and names the file and line

#### Scenario: A private address appears only in a commit message

- **GIVEN** a pull request whose file changes are clean but one commit message
  contains an RFC 1918 address
- **WHEN** the leak scan runs
- **THEN** the check fails and names the offending commit, because a value in a
  message cannot be retracted by a later file edit

#### Scenario: A public address is committed

- **GIVEN** a pull request that adds a documentation example using a
  documentation-reserved address
- **WHEN** the leak scan runs
- **THEN** the check passes, because the reserved ranges are excluded by shape

### REQ-005: Behaviour is testable without hardware

Every workspace member MUST expose its behaviour through interfaces that allow
its full test suite to run without a robot, a camera, or a microphone attached.

#### Scenario: Continuous integration runs the suite

- **GIVEN** a continuous integration runner with no Reachy Mini attached
- **WHEN** the full test suite runs
- **THEN** it completes without skipping any test on the grounds of missing
  hardware

### REQ-006: Quality gates block merge

Continuous integration MUST run linting, type checking, and the test suite on
every pull request, and a failure of any of them MUST block merge.

#### Scenario: A type error is introduced

- **GIVEN** a pull request introducing a type error in a workspace member
- **WHEN** continuous integration runs
- **THEN** the type-check job fails and the pull request cannot be merged

### REQ-007: Vendored third-party code is attributed in place

Any directory containing code derived from a third-party project MUST carry that
project's licence text and a notice recording the upstream project, the files
derived from it, and the upstream commit they were taken at.

#### Scenario: An auditor reads the tree

- **GIVEN** a directory holding code derived from an external project
- **WHEN** an auditor opens that directory
- **THEN** the licence and the upstream provenance are readable without leaving
  it, and without consulting a root-level manifest

### REQ-008: Generated contract artifacts cannot drift

Continuous integration MUST regenerate every published schema and interface
description from source and fail when the regenerated output differs from the
committed copy.

#### Scenario: A wire type changes without regeneration

- **GIVEN** a pull request that changes a shared wire type but leaves the
  committed schema untouched
- **WHEN** the contract-generation check runs
- **THEN** the check fails and shows the difference

### REQ-009: Configuration is validated and self-reporting

Every component that reads configuration from its environment MUST fail to start
when it encounters a variable matching its own prefix that it does not
recognise, and MUST emit its fully resolved configuration at startup.

#### Scenario: A variable is misspelled

- **GIVEN** an operator who sets a variable with a typo in its name
- **WHEN** the component starts
- **THEN** startup fails naming the unrecognised variable, rather than silently
  running on the default

#### Scenario: An operator checks what is in effect

- **GIVEN** a running component
- **WHEN** the operator reads its startup log
- **THEN** every configuration value in effect is present, including those left
  at their defaults

## Design

### Workspace layout

```
reachy/
├─ AGENTS.md                  # entry point for coding agents
├─ pyproject.toml             # workspace root + shared tool configuration
├─ uv.lock                    # one lockfile, every member
├─ mise.toml                  # pinned toolchain
├─ Justfile                   # the task surface
│
├─ packages/
│  └─ reachy-contracts/       # shared wire types and golden fixtures
│
├─ apps/
│  └─ ha-satellite/           # robot app  → reachy_mini_ha_satellite
│
├─ services/
│  └─ groundstation/          # off-robot service → reachy_groundstation
│
├─ cli/
│  └─ reachyctl/              # Typer + Textual + Rich
│
├─ provisioning/ansible/      # stock image → configured robot
├─ bench/                     # performance suite
├─ docs/                      # specs, changes, runbooks, generated contracts
└─ .github/workflows/
```

A uv workspace is the mechanism: members depend on each other by path and share
one resolution, which is what REQ-001 describes in observable terms.

### Toolchain

`mise.toml` pins the toolchain so a contributor and a continuous integration
runner get identical versions. `Justfile` is the task surface — a contributor or
an agent reads one file to learn every command the repository supports, rather
than reconstructing them from workflow YAML.

### Versioning and distribution

Versioning is driven from conventional commits, producing one repository-wide
version. Independent per-member versions were considered and rejected: the four
artifacts are only ever deployed as a set, and a shared version makes "which app
goes with which groundstation" answerable from a tag instead of a compatibility
matrix.

Distribution is entirely through GitHub:

| Artifact | Destination |
|---|---|
| Groundstation image | GitHub Container Registry, multi-architecture, plus a CUDA variant |
| Robot app wheel | GitHub Releases |
| `reachyctl` wheel | GitHub Releases |

There is deliberately no Hugging Face Space. The Reachy Mini daemon can install
apps from a Space, but it discovers them through a standard Python entry point,
so a wheel installed into the robot's application environment is sufficient.
Publishing to a Space is a possible later addition and nothing in this layout
forecloses it.

### Testing conventions

These are the standing rules every change document's Testing Requirements
section refers back to.

- Tests run with `pytest`. Async tests use strict mode, so an un-awaited
  coroutine fails rather than passing silently.
- Unit tests perform no input or output: no sockets, no filesystem, no sleeping.
  Behaviour that needs those is reached through an interface and exercised with
  a fake, which is what makes REQ-005 achievable.
- Integration tests exercise real transports in-process rather than mocking
  them, so wire behaviour is genuinely covered.
- Contract tests run the shared golden fixtures from
  [`reachy-contracts`](../robot-link/) against both the producing and consuming
  side of every wire type.
- Coverage is gated on the diff rather than on the whole tree, so a large
  untested legacy area cannot mask an untested new one, and new code cannot land
  uncovered.
- Type checking runs in strict mode for new modules. Relaxations are per-module
  and carry a comment naming the reason.
- A suppression comment for a lint or type rule carries the rule identifier and
  a justification. A bare suppression fails review.

Weakening any of these to land a pull request is a defect in the pull request,
not in the rule.

### Documentation conventions

Documentation is written primarily for language models, which mostly means being
more explicit than a human reader needs.

- `AGENTS.md` at the root is the entry point: a map of the repository, the
  invariants that hold across it, and rules about what to read before touching
  what. `CLAUDE.md` imports it so the two cannot diverge.
- Each workspace member carries its own `AGENTS.md` for local rules.
- Runbooks under `docs/setup/` and `docs/ops/` are imperative. Every step is a
  command with the output to expect, so a reader can tell whether a step worked
  without asking.
- `docs/contracts/` holds generated schema and interface descriptions, kept
  honest by REQ-008.
- Decisions with reasoning live in the specs that own them, under a Decision
  Records heading. A future session reading only the code cannot reconstruct why
  onnxruntime rather than ultralytics, or why one session rather than per-request
  connections, and re-litigating settled ground is the most expensive thing an
  agent does.

### Repository hygiene

The leak scan in REQ-004 matches shapes, never names. A denylist of the real
hostnames and accounts to be kept out of the repository would itself publish
them, which is why the requirement is written in terms of generic patterns.
Secret scanning runs alongside it, over each pull request and over the full
history once at set-up.

### Decision Records

#### Python throughout, with a native escape hatch

Every component here is Python. The question of whether the compute-bound parts
would be better in a compiled language was asked deliberately rather than
answered by default, and the measurements settle it.

The groundstation is the obvious candidate and does not survive inspection. Its
per-pass budget — 2 ms decode, 39 ms face, 5 ms gesture — is spent almost
entirely in C++ and C: the JPEG decoder, the inference runtime, and the array
library. Those stages account for essentially the whole pass, which leaves no
room for interpreter time worth reclaiming, and the pass itself sits behind
54 ms of result delivery on a network measured at 100–170 ms idle. The
concurrency argument does not apply either, because both the inference runtime
and the array library release the interpreter lock during their work.

The robot application has no choice to make. The daemon launches applications as
Python modules discovered through a Python entry point, and the ESPHome protocol
library is Python. A reimplementation would track Home Assistant's protocol
changes forever in exchange for nothing measurable, since the application's own
CPU goes to audio processing, wake-word inference, and an SDK that already
delegates its real-time paths to Rust.

The command-line tool is the only component where the answer is close. A
compiled binary would start faster and distribute without a Python installation.
It would also need a second implementation of the session protocol and a second
copy of every wire type, which is precisely the drift the shared contracts
package exists to prevent — a large structural cost to save a fraction of a
second on an operator tool.

What stays open is the narrow case: a future capability that is compute-bound
*outside* a model, where profiling shows interpreter overhead dominating. The
answer there is a native extension module for that specific loop rather than a
rewrite of anything containing it. The capability boundary in the
[groundstation](../groundstation/) and the ports in the
[satellite](../ha-satellite/) are the seams where such an implementation drops
in without disturbing its neighbours, and keeping them clean is part of what
those boundaries are for.

## Constraints

- The repository is public from its first commit. History is not rewritable in
  practice once pushed, so hygiene has to hold on the way in rather than being
  cleaned up afterwards — including in commit messages, which no later file edit
  can retract.
- The robot is an aarch64 Raspberry Pi CM4 with four cores. Anything shipped to
  it is built and tested for that architecture.
- The robot's application environment is shared and managed by the Reachy Mini
  daemon. Components installed there cannot assume an isolated virtual
  environment of their own.
- Python 3.12 is the floor, matching the robot image.

## Open Questions

- **Whether `reachyctl` is published to PyPI.** GitHub Releases plus
  `uv tool install` from a release artifact covers the known users. PyPI would
  make installation one command shorter at the cost of another publishing
  credential and another namespace to hold. Current default: GitHub Releases
  only.
- **Whether the groundstation image is signed and accompanied by a bill of
  materials.** Both are cheap to add in the publishing workflow and neither is
  needed by any known consumer yet. Current default: defer until an external
  consumer exists.

## References

- [robot-link](../robot-link/) — the wire contract between robot and groundstation
- [groundstation](../groundstation/) — the off-robot service
- [perception](../perception/) — the first groundstation capability
- [ha-satellite](../ha-satellite/) — the robot-side voice satellite
- [reachyctl](../reachyctl/) — the command-line tool
- [provisioning](../provisioning/) — Ansible roles against a stock image
- [benchmarks](../benchmarks/) — the performance suite and its baseline
- [RFC 2119](https://datatracker.ietf.org/doc/html/rfc2119) — requirement keywords
- [RFC 1918](https://datatracker.ietf.org/doc/html/rfc1918) — private address ranges

## Changelog

| Date | Change | Document |
|------|--------|----------|
| 2026-08-20 | Initial spec created | — |
