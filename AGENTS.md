# reachy

Monorepo for the Reachy Mini robot: the robot-side Home Assistant voice
satellite, the off-robot groundstation service, the `reachyctl` CLI, and
reproducible provisioning.

This file is the entry point. It maps the repository, states the invariants that
hold across all of it, and says what to read before touching what. Every member
directory carries its own `AGENTS.md` for rules local to it. `CLAUDE.md` is a
one-line import of this file and holds no content of its own.

## Repository map

| Path | What it is |
|---|---|
| `docs/` | Specs, change documents, runbooks and generated contracts |
| `packages/reachy-contracts/` | Shared wire types and golden fixtures (`reachy_contracts`) |
| `apps/ha-satellite/` | Robot-side ESPHome voice satellite (`reachy_mini_ha_satellite`) |
| `services/groundstation/` | Off-robot capability host (`reachy_groundstation`) |
| `cli/reachyctl/` | Command-line tool (`reachyctl`) |
| `bench/` | Performance suite (`reachy_bench`); a member, never published |
| `tools/repo-hygiene/` | The repository's own leak scanner (`reachy_hygiene`); a member, never published |
| `provisioning/ansible/` | Stock image to configured robot; not a Python member |
| `scripts/` | Helpers the `Justfile` calls; not a Python member |
| `pyproject.toml` | Workspace root and all shared tool configuration |
| `uv.lock` | One lockfile for every member |
| `mise.toml` | The pinned toolchain |
| `Justfile` | The task surface |
| `.github/workflows/` | The merge gates: checks, hygiene, release, traceability |
| `release-please-config.json` | Where the derived version is written, artifact by artifact |

`packages/reachy-contracts`, `apps/ha-satellite` and `tools/repo-hygiene` have
implementations today. The other three members are scaffolds: a
`pyproject.toml`, an `AGENTS.md` and a package directory, waiting for the change
that fills them in.

## Read before touching

- **Before any code change**, read `docs/index.md` to find the spec that owns
  the behaviour and the change document that sequences the work. The specs are
  the authority; a change document sequences, it does not redefine.
- **Before editing `docs/changes/`**, load the `/project-management` skill. It
  owns all task-tracking rules and knows where tasks belong.
- **Before opening a pull request**, read `REVIEW.md`. It is the canonical
  review-conventions file for this repository and your change is judged against
  it.
- **Before adding a dependency**, read the invariants below — the lockfile
  travels in the same pull request.
- **Do not edit `docs/specs/`** as part of implementing a change. A spec change
  is its own proposal, made through `/spec-writer`.

## Cross-cutting invariants

### This repository is public

No hostname, IP address, account name, username, internal URL, credential or
email address belonging to anyone's environment goes into a tracked file — or
into a commit message, which no later edit can retract. Examples use RFC 5737
reserved ranges and placeholder names. Real values live in an untracked local
file or a repository secret, each with a tracked `.example` sibling documenting
its shape. Public identifiers of third-party dependencies are fine.

### One resolution, one lockfile

Members depend on one another by path (`{ workspace = true }`), never by version
range, and `uv.lock` at the root is the only lockfile. Adding a dependency to any
member means committing the regenerated lockfile in the same pull request,
because every install runs `--locked` and a lockfile that no longer matches the
manifests fails the run rather than being silently re-resolved. Not `--frozen`:
that flag skips the freshness check and runs against the stale resolution.

### One version for the whole repository

Every member carries the same version. `packages/reachy-contracts` declares it in
`src/reachy_contracts/version.py` and derives its distribution metadata from
that line, so the module and the metadata cannot disagree.

That version is derived from conventional commits by release-please, which
maintains a release pull request on the default branch and, when it is merged,
writes the derived version to every place that declares one and creates the tag.
`release-please-config.json` is where that list lives; a new member that
declares a version adds itself there in the same pull request, or it ships the
version of whenever it was last edited by hand. Nothing is published by the
release workflow — the publishing steps arrive with the artifacts.

### The Justfile is the only command surface

`just test`, `just lint`, `just typecheck`, `just check`, plus `just fmt`,
`just sync`, `just coverage-diff`, `just duvet`, `just leak-scan`,
`just secret-scan`, `just contracts`, `just contracts-check`,
`just lint-boundary`, `just check-assets` and `just vendored-drift`. Continuous
integration calls these recipes rather than restating the commands. A command
worth running twice belongs in the `Justfile`, not in workflow YAML and not in
prose here.

### Behaviour is testable without hardware

No test may require a robot, a camera or a microphone. Anything that needs one
is reached through an interface and exercised with a fake. Unit tests perform no
input or output at all: no sockets, no filesystem, no sleeping.

Sockets are the part of that rule the harness enforces: `pytest` runs with
`--disable-socket`, so a test that opens one errors. An integration test that
exercises a real transport in-process declares it with
`@pytest.mark.enable_socket`. The filesystem and sleeping halves have no
equivalent guard and are enforced in review — a test that genuinely must read a
committed file says so with `@pytest.mark.filesystem`, which makes the
dependency visible in the test rather than leaving it to be discovered.

### Wire types are declared once

Every type that crosses the robot link is declared in
`packages/reachy-contracts` and imported from `reachy_contracts` by everything
else. A second copy in a consumer is free to drift from the first, and the
failure shows up as a robot behaving oddly rather than as a test going red.

That import direction is a lint rule, not a convention: ruff's `TID251` bans
`pydantic.BaseModel` and `pydantic.RootModel` outside the contracts package, so
a member that starts declaring its own wire type fails `just lint` with a
message naming where the type belongs. A component that needs a validated model
for something that is *not* a wire type — its own configuration, say — uses
`pydantic_settings.BaseSettings`, which the ban does not touch.

### Tool configuration is shared, never per-member

ruff, mypy, pytest and coverage are configured once, in the root
`pyproject.toml`. A member does not carry its own copy — per-member dialects
drift, and a reviewer should learn the rules from one file.

### Suppressions carry their rule and a reason

`# type: ignore[code]  # why` and `# noqa: RULE  # why`. A mypy relaxation is a
per-module override with a comment naming the reason, never a global loosening.
Vendored third-party code is the one standing exception, and it is recorded the
same way: the modules are named in a `[[tool.mypy.overrides]]` block and in
ruff's `per-file-ignores`, both carrying the reason, and neither excludes the
code from being checked at all. The suppressions *inside* those files are
upstream's and stay as upstream wrote them — the reason is the directory's
`NOTICE`, and annotating each one would be an unlisted edit to a derived file.
This rule governs the suppressions this repository adds.

The two halves are enforced differently, and the difference is deliberate. The
**rule identifier** is enforced by the tooling: mypy runs with
`ignore-without-code` enabled and ruff selects `PGH003`/`PGH004`, so a blanket
suppression is an error. The **justification** is enforced in review — no tool
can judge whether a trailing comment explains anything, and a check that only
demanded some text after the code would be satisfied by `# noqa: F401  # noqa`.
A suppression without a reason is a change request, not a lint failure.

### Test module names are globally unique

Test files live in each member's `tests/` directory, which is not a package, so
two members with a `test_config.py` would collide during type checking. Prefix
the file with what it covers: `test_contracts_version.py`, not `test_version.py`.

## Toolchain

`mise.toml` pins `python`, `uv`, `just`, `rust`, `duvet` and `gitleaks`, and it
is the only place any of them is pinned: continuous integration reads the first
three out of that file rather than restating them, so a runner and a contributor
cannot end up on versions that merely look alike. Python 3.12 is the floor,
matching the robot image, and the robot itself is an aarch64 Raspberry Pi CM4 —
anything shipped to it is built and tested for that architecture.

```
mise install     # once, to get the pinned versions
just sync        # install the workspace exactly as uv.lock describes it
just check       # lint, typecheck, test — three of the merge gates
```

## Merge gates

Four workflows, and they do not all run on the same events. Each job calls a
`Justfile` recipe, so every one of them reproduces locally with the command in
its step.

| Workflow | Runs on | Jobs | Local equivalent |
|---|---|---|---|
| `checks.yml` | pull requests, pushes to `main` | `Lint`, `Type check`, `Test` (diff-scoped coverage), `Contract drift` | `just check`, `just contracts-check` |
| `hygiene.yml` | pull requests, pushes to `main` | `Leak scan` (diff, paths and commit messages), `Secret scan` | `just leak-scan`, `just secret-scan` |
| `release.yml` | pushes to `main` only | Version derivation and tag creation; publishes nothing | — |
| `duvet.yml` | pull requests, pushes to `main` | Requirements traceability — vacuous today, see below | `just duvet` |

`release.yml` never runs on a pull request, which is why it is not in the set of
checks to require below.

⚠️ **A gate only blocks a merge once it is a required status check**, which is a
repository setting rather than a file and cannot be committed. The settings to
enable, and why direct pushes to the default branch matter, are recorded in the
completion notes of
[`docs/changes/0002-ci-and-hygiene-gates.md`](docs/changes/0002-ci-and-hygiene-gates.md).

## Requirements traceability

⚠️ **The "Requirements traceability" check currently passes vacuously.** No spec
is registered in `.duvet/config.toml`, so duvet loads zero specifications and
exits 0 having checked nothing. A green run is not evidence that any requirement
is traced. The header comment in that file explains why they are deliberately
unregistered and when to register them.

Annotations already in the tree still resolve, and they are written `#:=` for the
meta line and `#:%` for the quoted requirement — not duvet's documented `#=` and
`#%`. The colon is what stops `ruff format` inserting a space after the `#` and
silently breaking the citation; the same file's header explains it.

A `#:%` line reproduces the requirement's sentence byte for byte, including its
original line wrapping. Reflow it to fit and `duvet report` exits 1 with `could
not find text in section`.

## Task Tracking

**You MUST load the `/project-management` skill before creating, modifying, or completing any task.** It owns all task-tracking rules and knows where tasks belong. Do not manage tasks without it.

## Code Review Rules

Read `REVIEW.md` at the repository root and apply it in full as the review rules for this repo. It is the canonical review-conventions file.
