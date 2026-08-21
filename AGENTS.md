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
| `provisioning/ansible/` | Stock image to configured robot; not a Python member |
| `pyproject.toml` | Workspace root and all shared tool configuration |
| `uv.lock` | One lockfile for every member |
| `mise.toml` | The pinned toolchain |
| `Justfile` | The task surface |

Only `packages/reachy-contracts` has an implementation today. The other members
are scaffolds: a `pyproject.toml`, an `AGENTS.md` and a package directory,
waiting for the change that fills them in.

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
that line, so the module and the metadata cannot disagree. Deriving that version
from conventional commits is wired in change 0002.

### The Justfile is the only command surface

`just test`, `just lint`, `just typecheck`, `just check`, plus `just fmt`,
`just sync`, `just coverage-diff` and `just duvet`. Continuous integration calls
these recipes rather than restating the commands. A command worth running twice
belongs in the `Justfile`, not in workflow YAML and not in prose here.

### Behaviour is testable without hardware

No test may require a robot, a camera or a microphone. Anything that needs one
is reached through an interface and exercised with a fake. Unit tests perform no
input or output at all: no sockets, no filesystem, no sleeping.

Sockets are the part of that rule the harness enforces: `pytest` runs with
`--disable-socket`, so a test that opens one errors. An integration test that
exercises a real transport in-process declares it with
`@pytest.mark.enable_socket`. The filesystem and sleeping halves have no
equivalent guard and are enforced in review.

### Tool configuration is shared, never per-member

ruff, mypy, pytest and coverage are configured once, in the root
`pyproject.toml`. A member does not carry its own copy — per-member dialects
drift, and a reviewer should learn the rules from one file.

### Suppressions carry their rule and a reason

`# type: ignore[code]  # why` and `# noqa: RULE  # why`. A mypy relaxation is a
per-module override with a comment naming the reason, never a global loosening.

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

`mise.toml` pins `python`, `uv`, `rust` and `duvet`. Python 3.12 is the floor,
matching the robot image, and the robot itself is an aarch64 Raspberry Pi CM4 —
anything shipped to it is built and tested for that architecture.

```
mise install     # once, to get the pinned versions
just sync        # install the workspace exactly as uv.lock describes it
just check       # lint, typecheck, test — what gates a merge
```

## Requirements traceability

⚠️ **The "Requirements traceability" check currently passes vacuously.** No spec
is registered in `.duvet/config.toml`, so duvet loads zero specifications and
exits 0 having checked nothing. A green run is not evidence that any requirement
is traced. The header comment in that file explains why they are deliberately
unregistered and when to register them.

## Task Tracking

**You MUST load the `/project-management` skill before creating, modifying, or completing any task.** It owns all task-tracking rules and knows where tasks belong. Do not manage tasks without it.

## Code Review Rules

Read `REVIEW.md` at the repository root and apply it in full as the review rules for this repo. It is the canonical review-conventions file.
