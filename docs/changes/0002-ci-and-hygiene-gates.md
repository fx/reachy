# 0002: CI and hygiene gates

## Summary

Stand up the continuous integration workflows and the repository-hygiene checks
that keep this public repository publishable: quality gates, leak detection,
secret scanning, and the release machinery that produces one version for every
artifact.

**Spec:** [Architecture](../specs/architecture/)
**Status:** draft
**Depends On:** 0001

## Motivation

This repository is public from its first commit, and its history is not
practically rewritable once pushed. Anything that keeps environment-specific
values out of it has to work on the way in, including in commit messages, which
no later file edit can retract.

The gates also have to exist before there is much code, because retrofitting a
leak scan onto a repository that has already leaked does not help.

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

The leak scan is itself testable and MUST be tested: this change ships fixtures
covering both a string that must be caught and a string that must not, so a
later tightening of the patterns cannot silently start failing legitimate
content.

### Functional requirements

The [architecture spec](../specs/architecture/) owns what the gates guarantee —
[REQ-004](../specs/architecture/index.md#req-004-automated-leak-detection-on-every-change)
on leak detection,
[REQ-006](../specs/architecture/index.md#req-006-quality-gates-block-merge) on
gating, and
[REQ-002](../specs/architecture/index.md#req-002-one-version-for-the-whole-repository)
on versioning. Those scenarios are this change's acceptance criteria. What
implementing them requires of this change:

- The leak scan matches **shapes only**. A denylist of the real hostnames and
  accounts being kept out would itself publish them, so the pattern set covers
  RFC 1918 ranges, internal hostname suffixes, and email address shapes, with
  documentation-reserved ranges excluded.
- The scan runs over the pull request diff **and** over commit messages in the
  range, because a message cannot be corrected by a later file edit.
- CI workflow steps call `Justfile` recipes rather than restating commands.
- Release automation derives one version from conventional commits and applies
  it to every artifact. No artifact is published by this change; the machinery
  is wired and the publishing steps arrive with the components in 0006, 0009 and
  0013.
- The duvet workflow from adoption already exists and is not modified here,
  beyond confirming it runs alongside the new jobs.
- `.example` siblings are added for every untracked local file the repository
  will use, so the tracked tree documents their shape.

## Design

### Approach

Three workflows, kept separate so each can be named as a required check:

| Workflow | Jobs |
|---|---|
| `checks.yml` | lint, typecheck, test with diff coverage, contract drift |
| `hygiene.yml` | leak scan over diff and commit messages, secret scanning |
| `release.yml` | version derivation from conventional commits, tag creation |

The contract-drift job regenerates schemas and fails on difference, satisfying
[REQ-008](../specs/architecture/index.md#req-008-generated-contract-artifacts-cannot-drift).
It has nothing to regenerate until 0003 introduces the contracts package, so it
lands here as a job that is correct-and-trivial and becomes meaningful then.

The hygiene workflow is annotated for duvet, since the workflow is the
implementation of the requirements it satisfies — this is what the
`type = "implication"` source block in `.duvet/config.toml` exists for.

### Decisions

- **Decision**: Leak patterns describe shapes, never names.
  - **Why**: A denylist containing the real values would publish them in the
    repository whose purpose is to exclude them.
  - **Alternatives considered**: A denylist held in a repository secret and
    injected at scan time, which works but makes the check unreviewable and
    untestable by contributors.
- **Decision**: Commit messages are scanned, not just diffs.
  - **Why**: A leaked value in a file can be deleted in a follow-up; a leaked
    value in a commit message cannot, short of rewriting history.
  - **Alternatives considered**: Diff-only scanning, which misses the one case
    that is genuinely unrecoverable.
- **Decision**: Secret scanning runs over the full history once, at set-up.
  - **Why**: The repository is new, so full history is cheap now and never gets
    cheaper.

### Non-Goals

- No artifact publishing — the image, wheels and their workflows belong to the
  changes that create those artifacts.
- No branch protection configuration; making a check required is a repository
  setting, not a file, and is reported rather than applied.
- No changes to the duvet workflow created during adoption.

## Tasks

- [ ] Add the quality-gate workflow
  - [ ] `checks.yml` calling `just lint`, `just typecheck`, `just test`
  - [ ] Diff-scoped coverage reporting and its threshold
  - [ ] Contract-drift job, trivial until 0003
- [ ] Add the hygiene workflow
  - [ ] Generic leak-pattern scan over the diff
  - [ ] Extend the scan to commit messages in the pull request range
  - [ ] Fixtures for a caught string and an allowed string, with a test
  - [ ] Secret scanning on pull requests
  - [ ] One-off full-history secret scan, with the result recorded
- [ ] Add release automation
  - [ ] Conventional-commit version derivation producing one repository version
  - [ ] Tag creation; no publishing steps yet
- [ ] Add tracked `.example` files for every untracked local file

## Open Questions

- [ ] Whether the leak scan should also run on pushes to the default branch, or
      only on pull requests. Direct pushes are possible until branch protection
      is configured. Current lean: both.
- [ ] Which checks become required — a repository setting this change can
      recommend but not apply.

## References

- Spec: [Architecture](../specs/architecture/)
- Related changes: [0001-workspace-skeleton](./0001-workspace-skeleton.md),
  [0003-contracts-package](./0003-contracts-package.md)
- [RFC 1918](https://datatracker.ietf.org/doc/html/rfc1918)
- [RFC 5737](https://datatracker.ietf.org/doc/html/rfc5737) — documentation-reserved address ranges
