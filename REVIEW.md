# PR Review

## PR Review Checklist (CRITICAL)

### This repository is public

Reject any change introducing an identifier that belongs to **someone's
environment or private infrastructure** — a hostname, IP address, account name,
username, internal URL, credential, or email address — into a tracked file,
including commit messages, which no later edit can retract. Such values belong
in a repository secret or an untracked local file with a tracked `.example`
sibling, and documentation examples use reserved ranges (RFC 5737) and
placeholder names.

This does **not** forbid public identifiers of third-party dependencies. A
pinned action (`actions/checkout@…`), an upstream project cited by its public
repository name, or a link to a standard are all fine — they describe a
dependency, not an environment.

Note that the automated leak scan matches **shapes** and cannot catch this whole
class: a private tool or account name has no distinguishing shape, and a
denylist of the real names would itself publish them. That residue is caught
here, at review time, which is why this rule is first.

### Pin GitHub Actions by commit SHA

Every `uses:` reference MUST name an immutable commit SHA, with the release it
corresponds to in a trailing comment:

```yaml
uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4
```

A tag is a mutable ref. Retargeting one upstream runs changed third-party code
in CI with no diff in this repository to review. Request changes on any `uses:`
pinned to a tag or a branch.

### Specs are written in duvet mode

`docs/specs/*/index.md` carries machine-traced requirements, which constrains
their shape. Do NOT report any of the following as defects:

- RFC 2119 keywords (MUST, SHOULD, MAY) appear **only** inside
  `### REQ-NNN:` sections. Their absence from Overview, Background, Design,
  Constraints, Open Questions and GIVEN/WHEN/THEN scenario bodies is required —
  a keyword in any of those becomes an extracted requirement with no citable
  anchor and fails CI permanently.
- Exactly one normative sentence per requirement section, stated as a single
  self-contained sentence rather than a bullet list. An annotation quotes it
  byte for byte.
- Requirements describe observable behaviour and deliberately do not name
  library APIs, component names, method names, or file layout.

Renaming a `### REQ-NNN:` heading is a breaking change: the heading is the
anchor every annotation cites. Flag any rename that is not accompanied by
re-pointed annotations.

### Change documents must not restate their spec

`docs/changes/NNNN-*.md` owns sequencing, migrations, design decisions and task
breakdown. Behaviour, defaults and scenarios are owned by the spec it links.
A change document that restates a rule its spec already owns has created a
second copy that will drift — report the duplication, not the drift.

Every change document's `## Requirements` section MUST open with
`### Testing Requirements` sourced from
`docs/specs/architecture/index.md#testing-conventions`.

### Change dependencies must cover what the tasks consume

A change document's `Depends On` field MUST list every earlier change producing
an artifact its tasks consume, and MUST agree with `docs/index.yml` and
`docs/index.md`. A change that cannot meet its own acceptance criteria from its
declared dependencies is unimplementable as sequenced.

The check is mechanical: every four-digit change number appearing in a
**work-specifying** section — Summary, Motivation, Requirements, Design
Approach, Tasks — MUST lie inside that change's transitive dependency closure.
`### Decisions`, `### Non-Goals`, `## Open Questions` and `## References` are
excluded, because a change number appears there precisely to record that
something is *not* a dependency, or is deferred. A reference in an excluded
section that is in fact consumed by a task is still a defect — read the task,
not the section heading.

### Secrets never appear in self-reported configuration

Components emit their resolved configuration at startup and expose it at run
time, which is deliberate — silently-inert configuration is a defect class this
project has already been bitten by. Every such surface MUST report a secret as
set or unset without its value. Request changes on any requirement, task, or
implementation that would log, return, or display a credential in full.

---

## Task Cross-Reference

Cross-reference every PR against task lists in `docs/changes/` and `docs/tasks.md`. If the PR completes work tracked in those files, the task checkboxes MUST be updated in this same PR. Request changes if missing.
