# PR Review

## PR Review Checklist (CRITICAL)

### This repository is public

Reject any change that puts an identifier belonging to **someone's environment
or private infrastructure** into a tracked file — hostname, IP address, account
name, username, internal URL, credential, email address — including in commit
messages, which no later edit can retract. Such values belong in a repository
secret or an untracked local file with a tracked `.example` sibling.
Documentation examples use RFC 5737 reserved ranges and placeholder names.

Public identifiers of third-party dependencies are fine: a pinned action, an
upstream project cited by its public repository name, a link to a standard.
Those describe a dependency, not an environment.

The automated scan matches shapes and cannot catch this whole class — a private
tool or account name has no distinguishing shape, and a denylist of the real
names would itself be the leak. That residue is caught here, which is why this
rule is first.

### Secrets never appear in self-reported configuration

Components emit their resolved configuration at startup and expose it at run
time — deliberate, because silently-inert configuration is a defect class this
project has already been bitten by. Every such surface MUST report a secret as
set or unset, never by value. Request changes on any requirement, task, or code
that would log, return, or display a credential in full.

### Pin GitHub Actions by commit SHA

Every `uses:` MUST name an immutable commit SHA with the release in a trailing
comment: `uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4`.
A tag is mutable; retargeting one upstream runs changed third-party code in CI
with no diff here to review. Request changes on any `uses:` pinned to a tag or
branch.

### Specs are written in duvet mode

`docs/specs/*/index.md` carries machine-traced requirements. Do NOT report these
as defects:

- RFC 2119 keywords appear **only** inside `### REQ-NNN:` sections. Their
  absence from Overview, Background, Design, Constraints, Open Questions and
  GIVEN/WHEN/THEN scenario bodies is required — a keyword elsewhere becomes an
  extracted requirement with no citable anchor and fails CI permanently.
- Exactly one normative sentence per requirement section, self-contained rather
  than a bullet list, because an annotation quotes it byte for byte.
- Requirements describe observable behaviour and deliberately name no library
  APIs, methods, or file layout.

Renaming a `### REQ-NNN:` heading is breaking — it is the anchor annotations
cite. Flag any rename not accompanied by re-pointed annotations.

### Change documents must not restate their spec

`docs/changes/NNNN-*.md` owns sequencing, migrations, decisions and tasks.
Behaviour, defaults and scenarios belong to the spec it links; a restated rule
is a second copy that will drift. Report the duplication, not the drift. Every
change document MUST open `## Requirements` with `### Testing Requirements`
sourced from `docs/specs/architecture/index.md#testing-conventions`.

### Change dependencies must cover what the tasks consume

Every four-digit change number in a **work-specifying** section — Summary,
Motivation, Requirements, Design Approach, Tasks — MUST lie inside that change's
transitive dependency closure, and `Depends On` MUST agree with `docs/index.yml`
and `docs/index.md`. `### Decisions`, `### Non-Goals`, `## Open Questions` and
`## References` are excluded: a number appears there precisely to record that
something is *not* a dependency. A reference in an excluded section that a task
actually consumes is still a defect — read the task, not the heading.

---

## Task Cross-Reference

Cross-reference every PR against task lists in `docs/changes/` and `docs/tasks.md`. If the PR completes work tracked in those files, the task checkboxes MUST be updated in this same PR. Request changes if missing.
