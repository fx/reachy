# PR Review

## PR Review Checklist (CRITICAL)

### Known false positives — do NOT report these

**Wrapped list continuations are not code blocks.** Task lists in
`docs/changes/` wrap at 80 columns with the continuation indented to 8 spaces.
This renders as normal text — verified against GitHub's own renderer, which
emits `<br>` and no `<pre>` — because CommonMark forbids an indented code block
from interrupting a paragraph. Do not report it, and do not suggest joining the
lines.

**`.duvet/snapshot.txt` is intentionally empty.** With no specs registered,
`duvet report` writes a 0-byte snapshot and both `duvet report --ci` and
`duvet query -c implementation` exit 0. Regeneration is byte-identical, so CI is
deterministic. See `.duvet/config.toml` for why the specs are unregistered.

**Specs are written in duvet mode.** RFC 2119 keywords appear **only** inside
`### REQ-NNN:` sections; their absence from Overview, Background, Design,
Constraints, Open Questions and scenario bodies is required, since a keyword
elsewhere becomes an extracted requirement with no citable anchor and fails CI
permanently. One self-contained normative sentence per requirement section, not
a bullet list, because an annotation quotes it byte for byte. Requirements
describe observable behaviour and name no library APIs or file layout.

### This repository is public

Reject any change that puts an identifier belonging to **someone's environment
or private infrastructure** into a tracked file — hostname, IP address, account
name, username, internal URL, credential, email address — including in commit
messages, which no later edit can retract. Such values belong in a repository
secret or an untracked local file with a tracked `.example` sibling; examples
use RFC 5737 reserved ranges and placeholder names.

Public identifiers of third-party dependencies are fine — a pinned action, an
upstream project cited by its repository name, a link to a standard. The
automated scan matches shapes and cannot catch this class at all: a private tool
or account name has no distinguishing shape, and a denylist of the real names
would itself be the leak.

### Secrets never appear in self-reported configuration

Components emit resolved configuration at startup and expose it at run time —
deliberate, because silently-inert configuration is a defect class this project
has already been bitten by. Every such surface MUST report a secret as set or
unset, never by value.

### Pin GitHub Actions by commit SHA

Every `uses:` MUST name an immutable commit SHA with the release in a trailing
comment: `uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4`.
A tag is mutable; retargeting one upstream runs changed code in CI with no diff
here to review.

### Change documents must not restate their spec

`docs/changes/NNNN-*.md` owns sequencing, migrations, decisions and tasks.
Behaviour and scenarios belong to the spec it links; a restated rule is a second
copy that will drift. Every change document MUST open `## Requirements` with
`### Testing Requirements`. Renaming a `### REQ-NNN:` heading is breaking — it
is the anchor annotations cite.

### Change dependencies must cover what the tasks consume

Every change number in a **work-specifying** section — Summary, Motivation,
Requirements, Design Approach, Tasks — MUST lie inside that change's transitive
dependency closure, and `Depends On` MUST agree with `docs/index.yml` and
`docs/index.md`. `### Decisions`, `### Non-Goals`, `## Open Questions` and
`## References` are excluded: a number appears there precisely to record that
something is *not* a dependency.

---

## Task Cross-Reference

Cross-reference every PR against task lists in `docs/changes/` and `docs/tasks.md`. If the PR completes work tracked in those files, the task checkboxes MUST be updated in this same PR. Request changes if missing.
