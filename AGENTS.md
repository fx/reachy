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
| `docs/` | Specs, change documents, runbooks and generated contracts. `docs/index.md` is the map |
| `docs/setup/` | The task-ordered setup runbooks: the groundstation, the robot, Home Assistant. Every step is a command with the output to expect |
| `docs/ops/` | Running one: `deploy.md` updates an installation, `troubleshooting.md` is keyed to `doctor`'s check identifiers, `managed-daemon-environment.md` is the byte-level contract for the robot's managed drop-in, `satellite-deployment.md` is the application's own reference |
| `docs/contracts/` | Generated, never edited. `just contracts` writes it and the drift gate compares it |
| `packages/reachy-contracts/` | Shared wire types and golden fixtures (`reachy_contracts`) |
| `packages/reachy-checks/` | The one definition of what a healthy installation is (`reachy_checks`) |
| `packages/reachy-session-client/` | The one client half of the robot link (`reachy_session_client`) |
| `apps/ha-satellite/` | Robot-side ESPHome voice satellite (`reachy_mini_ha_satellite`) |
| `services/groundstation/` | Off-robot capability host (`reachy_groundstation`) |
| `cli/reachyctl/` | Command-line tool (`reachyctl`) |
| `bench/` | Performance suite, the committed baseline and the regression gate (`reachy_bench`); a member, never published |
| `tools/repo-hygiene/` | The repository's own leak scanner (`reachy_hygiene`); a member, never published |
| `provisioning/ansible/` | Stock image to configured robot: four tagged roles, a removal path, and the filter plugins they reach their Python through. Not a Python member, and its plugins are nonetheless linted, type-checked and covered with everything else |
| `provisioning/ci/` | The container target the idempotency gate applies the playbook against, and the list of what it does and does not model |
| `scripts/` | Helpers the `Justfile` calls; not a Python member, and linted, type-checked and tested with everything else. `export_contracts.py` is the driver that writes `docs/contracts/` from both registries |
| `pyproject.toml` | Workspace root and all shared tool configuration |
| `uv.lock` | One lockfile for every member |
| `mise.toml` | The pinned toolchain |
| `Justfile` | The task surface |
| `.github/workflows/` | The merge gates: checks, hygiene, images, release, traceability, provisioning, benchmarks |
| `release-please-config.json` | Where the derived version is written, artifact by artifact |

Every member has an implementation today.

There is exactly one definition of what a healthy installation is, and it is
`packages/reachy-checks`. `reachyctl doctor` runs those declarations and the
provisioning verification role imports the same ones — see reachyctl REQ-056.
A check written into either consumer rather than into the registry is a check
the other will never perform, and the failure shows up as a robot that
provisioning calls fine and diagnosis calls broken.

There is exactly one implementation of the session protocol's client half, and
it is `packages/reachy-session-client`. `reachyctl probe` and the robot's
groundstation adapter both import it, which is what makes a probe run evidence
about the protocol rather than about a probe — see reachyctl REQ-057. A second
client, however lightweight and however clearly labelled as being for testing,
is the change that makes that requirement false.

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
  is its own proposal, made through `/spec-writer`. The one exception is a
  `## Changelog` row in the spec a change settles, and only where that change
  document asks for one: a measurement that decides a question the spec argued
  is what its decision record is for, and recording it anywhere else is how that
  record goes stale. Everything else about a spec still goes through
  `/spec-writer`.

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
version of whenever it was last edited by hand. The release workflow publishes
the `reachyctl` wheel on a version tag, alongside the three unpublished wheels it
depends on; the container image is published from `images.yml` on the same tag,
and the robot application's wheel arrives with the change that builds it.

### The Justfile is the only command surface

`just test`, `just lint`, `just typecheck`, `just check`, plus `just fmt`,
`just sync`, `just coverage-diff`, `just duvet`, `just leak-scan`,
`just secret-scan`, `just contracts`, `just contracts-check`,
`just lint-boundary`, `just lint-behaviour-boundary`,
`just lint-capability-boundary`, `just check-assets`, `just vendored-drift`, the
wheel trio `just wheels`, `just wheel-size` and `just wheel-verify`, the
benchmark set `just bench`, `just bench-compare`, `just bench-sizes` and
`just bench-record`, and the provisioning set `just provision-lint`,
`just provision-target-up`, `just provision-run`, `just provision-target-down`
and `just provision-idempotency`. Continuous integration calls these recipes
rather than restating the commands. A command worth running twice belongs in the
`Justfile`, not in workflow YAML and not in prose here.

### Behaviour is testable without hardware

No test may require a robot, a camera or a microphone. Anything that needs one
is reached through an interface and exercised with a fake. Unit tests perform no
input or output at all: no sockets, no filesystem, no sleeping.

Sockets are the part of that rule the harness enforces: `pytest` runs with
`--disable-socket`, so a test that opens one errors. An integration test that
exercises a real transport in-process declares it with
`@pytest.mark.enable_socket`. The filesystem and sleeping halves have no
equivalent guard and are enforced in review.

`@pytest.mark.filesystem` is the filesystem's counterpart to
`@pytest.mark.enable_socket`, and it grants a unit test nothing. It **declares
that the test is not a unit test**: reading a committed data file is input, so a
test that does it is a contract test and the rule above no longer describes it.
The marker exists so that fact is visible in the test rather than discovered
later, and a unit test carrying it is a mislabelled test, not a licensed one. It
is warranted only where the bytes on disk are themselves the thing under test —
the golden fixture corpus — because there a fake would pin whatever the fake was
told to return.

### Wire types are declared once

Every type that crosses the robot link is declared in
`packages/reachy-contracts` and imported from `reachy_contracts` by everything
else. A second copy in a consumer is free to drift from the first, and the
failure shows up as a robot behaving oddly rather than as a test going red.

That import direction is a lint rule, not a convention: ruff's `TID253` bans
importing `pydantic` at module level outside the contracts package, so a member
that starts declaring its own wire type fails `just lint`. The ban names the
module rather than the model bases, which covers every construct at once —
`BaseModel`, `RootModel`, `create_model`, `pydantic.dataclasses` and the
`pydantic.v1` shim — and keeps covering whatever pydantic adds next.

It is `TID253` and not `banned-api` because `TID251` is already spoken for by
the vendored ESPHome boundary, whose negated `per-file-ignores` entry switches
that rule off everywhere outside the vendored directory. A `banned-api` entry
for pydantic would therefore be silently dead in every member it needed to
guard. The two rules carry the two bans precisely so their scopes stay
independent.

`pydantic_settings` is not banned, so validated configuration is unaffected. A
settings model that also wants pydantic's own `Field` suppresses `TID253` on
that one import, with a comment saying the model is configuration rather than a
wire type.

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

### Generated documents are generated, and a document that quotes code is checked

`docs/contracts/` is written by `just contracts` and compared by the
contract-drift gate. Do not edit a file in it: the edit is reverted by the next
run at best and blocks a merge at worst. Two registries feed it — the wire
schemas in `reachy_contracts.contracts_export` and the `doctor` check reference
in `reachy_checks.checks_export` — because `reachy-checks` depends on
`reachy-contracts` and a registry in the contracts package that imported the
checks package would be a cycle. `scripts/export_contracts.py` hands both to one
export, so the index lists every artifact rather than whichever half ran last.

The same rule extends past generation. Where a document reproduces something the
code decides, something compares them:

| Document | What holds it to the code |
|---|---|
| `docs/ops/managed-daemon-environment.md` | A contract test renders `reachyctl.managed`'s own output and requires the fenced block to be it |
| `docs/ops/troubleshooting.md` | `packages/reachy-checks/tests/test_checks_runbook.py` requires its sections to be the registered identifiers, in order, each quoting that check's remediation word for word |
| `services/groundstation/deploy/.env.example` | A test requires it to list exactly the settings the service's model declares, at their defaults |
| `docs/contracts/**` | The drift gate |

**A docstring or a comment asserting something the code does not do is a defect
here**, not a cosmetic issue, because several of this repository's rules are
enforced in review rather than by tooling and a reviewer reads those sentences as
evidence. The commonest form is a forward reference that has come true — "arrives
in change 0009", "until it lands" — left in place after the change landed.

### Runbooks record what a command printed, not what it should print

Every step in `docs/setup/` and `docs/ops/` is a command paired with the output
to expect, and the output is a transcript of running it. Expected output written
from memory is wrong in small ways that train a reader to ignore it, and an agent
following it cannot tell which parts to trust.

Nothing in this repository has a Reachy Mini attached, so the steps that need one
are **marked pending hardware verification** rather than being given invented
output. That marker is honest and useful; a fabricated transcript is the failure
the convention exists to prevent. `docs/tasks.md` carries the outstanding list.

**Scrub every transcript you paste.** A container id, a host name, a path with an
account in it or an address all reach a public repository through a runbook more
easily than through code. Cut what you cut, and say in the document that you cut
it.

### The model weights are never committed

`just models` fetches them into `.models/` and refuses anything whose digest is
not the one
`services/groundstation/src/reachy_groundstation/models/registry.py` pins. That
registry records each file's licence, attribution, upstream project and retrieval
URL, and it is what a licence audit reads instead of the bytes. Perception tests
that run real inference skip without the weights, saying so; on a runner they
fail instead, because a merge gate that skips when its inputs are missing is not
a merge gate.

### The Reachy Mini SDK is optional and imported lazily

Exactly one module imports it — the satellite's daemon entry point — and nothing
else may, including by another name. Importing the SDK's top level alone executes
an `__init__` that transitively reaches `import gi`, which drags in the whole
GStreamer stack; a test suite that did it would need a system GTK on every
runner. The root `pyproject.toml` declares the distribution's metadata statically
so the parity reference installs without that tree, and
`just lint-behaviour-boundary` proves the behaviour layer cannot reach it — by
import, by relative import, or by a dynamic one.

### Skipped is not failed

`reachy_checks` has three outcomes and the third is load-bearing. A check that
ran and found something wrong is `FAILED`; a check whose prerequisites were never
supplied is `SKIPPED`. Collapsing them makes the output worth ignoring — an
operator who has configured no groundstation would be told their installation is
broken, and after the third time they stop reading. `doctor` with nothing
configured exits 0 and says *nothing failed, but not everything was checked*.

The same distinction holds in the groundstation's capability health: a capability
switched off by configuration is `disabled`, not `failed`, so an operator can
tell "I turned that off" from "that broke". Gestures ship disabled with no model
wired, which is a recorded decision rather than a gap.

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

The one tool that is **not** pinned there is Ansible. `ansible-core` and
`ansible-lint` are a `provisioning` dependency group in the root
`pyproject.toml`, resolved by the same `uv.lock` as everything else, and they are
deliberately outside `default-groups`: a lint, a type check and a test run have
no use for an Ansible engine. `uv run --group provisioning` installs it for the
two recipes that do. The group is also what lets the roles' filter plugins import
`reachy_checks` and `reachy_contracts` — the playbook runs under this workspace's
interpreter, which is how one definition of a healthy robot serves both
`reachyctl doctor` and the verification role.

```
mise install     # once, to get the pinned versions
just sync        # install the workspace exactly as uv.lock describes it
just models      # fetch and hash-verify the pinned model weights, never committed
just check       # lint, typecheck, test — three of the merge gates
just bench       # measure this stack; `just bench-compare` judges the result
just image       # build the groundstation container image (needs docker)
just image-verify  # start it with no network and drive a real session through it
just provision-idempotency  # apply the playbook twice against a container (needs docker)
```

Model weights are not in the repository: `just models` fetches them into
`.models/` and refuses anything whose digest is not the one
`services/groundstation/src/reachy_groundstation/models/registry.py` pins. The
perception tests that run real inference skip without them, saying so; on a
runner they fail instead, because a merge gate that skips when its inputs are
missing is not a merge gate.

## Merge gates

Seven workflows, and they do not all run on the same events. Each job calls a
`Justfile` recipe, so every one of them reproduces locally with the command in
its step.

| Workflow | Runs on | Jobs | Local equivalent |
|---|---|---|---|
| `checks.yml` | pull requests, pushes to `main` | `Lint`, `Type check`, `Test` (diff-scoped coverage), `Contract drift` | `just check`, `just contracts-check` |
| `hygiene.yml` | pull requests, pushes to `main` | `Leak scan` (diff, paths and commit messages), `Secret scan` | `just leak-scan`, `just secret-scan` |
| `images.yml` | pull requests, pushes to `main`, version tags | `Verify <variant> on <architecture>`, one per published combination; `Publish` on a version tag only | `just image`, `just image-verify`, `just image-size` |
| `release.yml` | pushes to `main`, version tags | Version derivation and tag creation on `main`; on a tag, every released wheel — the `reachyctl` set and the robot application — built, installed into an empty environment, verified, measured, and attached to the release | `just wheels`, `just wheel-verify`, `just wheel-size` |
| `duvet.yml` | pull requests, pushes to `main` | Requirements traceability — all eight specs, all 73 requirements | `just duvet` |
| `provisioning.yml` | pull requests, pushes to `main` | `Provisioning lint`; `Idempotency`, which applies the playbook twice against a container target and fails on any changed step in the second application | `just provision-lint`, `just provision-idempotency` |
| `bench.yml` | pull requests, pushes to `main` | `Benchmark` — the hardware-free suite, judged against the committed baseline | `just bench`, `just bench-compare` |

`release.yml` never runs on a pull request, which is why it is not in the set of
checks to require below. Its two jobs never run on each other's event: version
derivation is gated on the default branch, and publishing on the tag that
derivation creates.

⚠️ **A gate only blocks a merge once it is a required status check**, which is a
repository setting rather than a file and cannot be committed. The settings to
enable, and why direct pushes to the default branch matter, are recorded in the
completion notes of
[`docs/changes/0002-ci-and-hygiene-gates.md`](docs/changes/0002-ci-and-hygiene-gates.md).
That list excluded `Requirements traceability` because it passed vacuously at the
time. It no longer does — see below — so it belongs in the required set now.

## Requirements traceability

**All eight specs are registered and all 73 requirements are traced.**
`.duvet/config.toml` registers every `docs/specs/*/index.md`, so a green
"Requirements traceability" run is evidence about the whole of this
repository's requirements rather than about a subset.

A spec is registered by the change that **completes** it, in that change's pull
request, alongside the annotations that make it pass — never by the change that
writes it. A registered spec whose requirements nothing implements is a red job
that stays red, and one that is going to stay red is one somebody switches off.
That rule is what put architecture last: six changes implemented its nine
requirements and 0015 registered it, adding the two that were still missing.
The header comment in `.duvet/config.toml` records which change registered which
spec and why.

Two requirements are cited from files that are neither Python nor a workflow, and
both have a `[[source]]` block of their own: REQ-001 from the `Justfile`, whose
`--locked` installs are what continuous integration actually runs, and REQ-003
from `.gitignore`, whose ignore rules and their tracked `.example` siblings are
the mechanism the requirement describes.

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
