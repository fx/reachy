# PR Review

## PR Review Checklist (CRITICAL)

### Known false positives — do NOT report these

**Wrapped list continuations are not code blocks.** Task lists in
`docs/changes/` wrap at 80 columns with the continuation indented to 8 spaces.
This renders as normal text — verified against GitHub's own renderer, which
emits `<br>` and no `<pre>` — because CommonMark forbids an indented code block
from interrupting a paragraph. Do not report it, and do not suggest joining the
lines.

**`.duvet/snapshot.txt` records annotations, not coverage.** No spec is
registered in `.duvet/config.toml`, so duvet extracts 0 requirements and both
`duvet report --ci` and `duvet query -c implementation` exit 0 having checked
nothing. The snapshot is nonetheless non-empty: duvet loads a specification an
annotation points at whether or not it is registered, so the file lists the
requirement text each annotation cites. Regeneration is byte-identical, so CI is
deterministic — and a green run is still not evidence that any requirement is
traced. See `.duvet/config.toml` for why the specs are unregistered, and for why
annotations are written `#:=`/`#:%` rather than duvet's documented `#=`/`#%`.

**A vendored file stays as upstream wrote it.** A file carrying a provenance
header is a derived work, and its directory's `NOTICE` enumerates the complete
intended diff from upstream. Its style, its partial typing, its suppressions and
its latent bugs are inherited, and the reason is recorded once per directory — in
that `NOTICE`, and in the `[[tool.mypy.overrides]]` and `per-file-ignores` blocks
that name those files. Do not report any of it, and do not ask for a
suppression's justification there: annotating one would itself be an unlisted
edit. The rule that a suppression carries a reason governs code this repository
authors, and a suppression *added* here without one is still a finding.

**`@pytest.mark.filesystem` is a declaration, not a licence.** It does not
permit a unit test to touch the filesystem — it states that the test is not a
unit test, exactly as `@pytest.mark.enable_socket` does for a test that opens a
socket. The no-input-or-output rule constrains unit tests, and a contract test
reading the golden fixture corpus is not one: those bytes are the contract, so a
fake would pin whatever the fake was told to return. Do not report the marker as
weakening the rule. A *unit* test wearing it is a finding; the marker is not.

**Some standing rules are review-enforced on purpose.** Tooling decides what a
tool can: `--disable-socket`, and `ignore-without-code`/`PGH003`/`PGH004` for a
suppression's rule identifier. Whether its comment *explains* anything, and
whether a unit test touches the filesystem or sleeps, are review judgements.
Report a violation, never the absence of a tool.

**`await asyncio.sleep(0)` is a yield, not sleeping.** It reads no clock,
schedules no timer and adds no wall time — it hands control to the event loop
and resumes on its next pass, which is how a test drives another task to its
next await point deterministically. The no-sleeping rule exists so the suite is
neither slow nor flaky, and a zero-delay yield is the fix for both, not an
instance of the problem. A test that yields in an *unbounded* loop is a finding
— it can hang the suite — so bound the turns; the groundstation's tests call
`hand_control_to_the_event_loop`, which does. A non-zero `asyncio.sleep` in a
unit test is still a finding. In an integration test it is not: those poll a
real server, and the bounded wait is what makes them deterministic.

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
