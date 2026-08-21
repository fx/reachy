# 0001: Workspace skeleton

## Summary

Create the uv workspace, the pinned toolchain, the task surface, and the agent
documentation entry points, so every later change has a place to land and one
way to be built and tested.

**Spec:** [Architecture](../specs/architecture/)
**Status:** complete
**Depends On:** —

## Motivation

The repository currently contains a README, the docs tree, and the duvet
configuration. Nothing can be implemented until there is a workspace to
implement it in, and the shape of that workspace decides several things that are
expensive to change later: whether members share one resolution, whether tests
can run without hardware, and whether an agent arriving cold can find out what
the repository expects of it.

## Requirements

### Testing Requirements

This change MUST satisfy the project's standing testing rules (see
[Testing conventions](../specs/architecture/index.md#testing-conventions)). CI
enforces these as merge gates:

- Tests run with `pytest`, with async strict mode enabled.
- Unit tests MUST perform no input or output — no sockets, no filesystem, no
  sleeping.
- Coverage MUST be gated on the diff rather than on the whole tree.
- Type checking MUST run in strict mode for new modules; relaxations are
  per-module and carry a comment naming the reason.
- A lint or type suppression MUST carry the rule identifier and a justification.

Skipping or weakening any of these rules to land the PR MUST be treated as a bug
in the PR, not in the rule.

This change establishes the harness those rules run in, so it MUST also
demonstrate them: the workspace ships at least one member with a real test that
the suite actually executes, rather than an empty scaffold that reports success
by having nothing to run.

### Functional requirements

The [architecture spec](../specs/architecture/) owns the workspace's observable
guarantees —
[REQ-001](../specs/architecture/index.md#req-001-single-resolved-dependency-set)
on single resolution,
[REQ-005](../specs/architecture/index.md#req-005-behaviour-is-testable-without-hardware)
on hardware-free testability, and
[REQ-006](../specs/architecture/index.md#req-006-quality-gates-block-merge) on
gating. Those scenarios are this change's acceptance criteria and are not
restated here. What implementing them requires of this change:

- The workspace root declares every member; members depend on one another by
  path, never by version range.
- `uv.lock` is committed, and CI installs with `--locked` so a stale lockfile
  fails rather than being silently re-resolved. See the decision below on why
  that flag and not `--frozen`.
- `mise.toml` already pins `rust` and `cargo:duvet` from duvet adoption. This
  change adds `python` and `uv` to the same `[tools]` table rather than creating
  a second one.
- `Justfile` is the single task surface: `just test`, `just lint`, `just
  typecheck`, `just check`. CI calls these recipes rather than duplicating the
  commands, so a contributor and CI cannot diverge.
- Root `AGENTS.md` is expanded from the setup stub into the real entry point: a
  repository map, the cross-cutting invariants, and read-before-touch rules.
  `CLAUDE.md` remains a one-line import of it.
- Each member directory carries its own `AGENTS.md`.
- `.gitignore` is extended beyond the duvet entries to cover Python artifacts,
  virtual environments, and the untracked local files whose `.example` siblings
  are added in 0002.

## Design

### Approach

Create the directory skeleton for all six members with a minimal
`packages/reachy-contracts` that actually builds, imports, and is tested — the
others get their `pyproject.toml`, `AGENTS.md` and package directory but no
implementation. That gives the lockfile something real to resolve and the test
harness something real to run, without pre-empting the design work in later
changes.

Shared tool configuration (ruff, mypy, pytest, coverage) lives in the workspace
root `pyproject.toml` so members inherit it and cannot drift into per-member
lint dialects.

### Decisions

- **Decision**: One lockfile at the workspace root, no per-member lockfiles.
  - **Why**: The four artifacts deploy together, so a version skew between them
    is a deployment bug that a single resolution makes impossible.
  - **Alternatives considered**: Independent per-member resolution, which is
    more flexible and reintroduces exactly the skew this repository cannot
    tolerate.
- **Decision**: `Justfile` is the only documented command surface.
  - **Why**: An agent reading the repository should learn every available
    command from one file. Commands that exist only inside workflow YAML are
    discoverable only by reading CI.
  - **Alternatives considered**: Documenting raw `uv run` invocations in
    `AGENTS.md`, which drifts from CI the first time either changes.
- **Decision**: Installs use `uv --locked`, not `uv --frozen`.
  - **Why**: Only `--locked` produces the failure this change is after.
    Measured against this workspace: with a dependency added to a member and
    the lockfile left alone, `uv run --frozen` installs the old resolution and
    exits 0, while `uv run --locked` exits non-zero with "The lockfile at
    `uv.lock` needs to be updated". Neither flag re-resolves, so `--frozen`
    reads as safe and is in fact the one that lets a stale lockfile through.
  - **Alternatives considered**: `--frozen` plus a separate `uv lock --check`
    step, which is the same check spelled twice and only in the places someone
    remembered to add it.
- **Decision**: `python` and `uv` join the existing `[tools]` table.
  - **Why**: A second `[tools]` header is a TOML duplicate-key error and would
    stop mise loading any tool for the repository.
  - **Alternatives considered**: A separate config file, which splits the
    toolchain across two places.

### Non-Goals

- No CI workflows beyond what already exists for duvet — that is 0002.
- No implementation of the groundstation, satellite, CLI, or provisioning.
- No container build, no release automation, no published artifacts.

## Tasks

- [x] Create the uv workspace root
  - [x] Root `pyproject.toml` declaring all six members and shared tool
        configuration
  - [x] Add `python` and `uv` to the existing `mise.toml` `[tools]` table
  - [x] Extend `.gitignore` for Python artifacts and virtual environments
  - [x] Generate and commit `uv.lock`
- [x] Scaffold the member directories
  - [x] `packages/reachy-contracts` with a real module, a real test, and its
        `pyproject.toml`
  - [x] `services/groundstation`, `apps/ha-satellite`, `cli/reachyctl`,
        `bench` — package directory and `pyproject.toml` only
  - [x] `provisioning/ansible` directory skeleton
- [x] Create the task surface
  - [x] `Justfile` with `test`, `lint`, `typecheck`, `check`
  - [x] Verify each recipe runs green against the scaffold
- [x] Write the agent documentation
  - [x] Expand root `AGENTS.md` into the real entry point
  - [x] Per-member `AGENTS.md` for each of the six members
  - [x] Confirm `CLAUDE.md` still imports `AGENTS.md` and nothing else

## Open Questions

- [x] **Resolved: `bench` is a workspace member that is never published.** It
      needs the shared resolution and it imports the other members, which is
      what a member is for; the thing it does not do is produce an artifact.
      Those are separable, so both hold: it is listed in
      `[tool.uv.workspace] members`, and its `pyproject.toml` carries the
      `Private :: Do Not Upload` classifier, which makes an accidental publish
      fail at the upload rather than succeed quietly. A plain directory of
      scripts was rejected because its dependencies would then resolve outside
      the single lockfile, which is the skew REQ-001 exists to prevent.

## References

- Spec: [Architecture](../specs/architecture/)
- Related changes: [0002-ci-and-hygiene-gates](./0002-ci-and-hygiene-gates.md)
- [uv workspaces](https://docs.astral.sh/uv/concepts/projects/workspaces/)
