# PR Review

## PR Review Checklist (CRITICAL)

### Known false positives — do NOT report these

**Wrapped list continuations are not code blocks.** Task lists in
`docs/changes/` wrap at 80 columns with the continuation indented to 8 spaces.
This renders as normal text — verified against GitHub's own renderer, which
emits `<br>` and no `<pre>` — because CommonMark forbids an indented code block
from interrupting a paragraph. Do not report it, and do not suggest joining the
lines.

**`.duvet/snapshot.txt` records annotations, not coverage.** Four specs are
registered in `.duvet/config.toml` — perception, groundstation, robot-link and
reachyctl — so `duvet report --ci` and `duvet query -c implementation` check
those 39 requirements and nothing else. The snapshot is nonetheless larger than
that: duvet loads a specification an annotation points at whether or not it is
registered, so the file lists the requirement text every annotation cites —
including the annotations that already point at architecture and provisioning,
neither of which is registered. Regeneration is
byte-identical, so CI is deterministic — and a green run is evidence about those
three specs only. A spec is registered by the change that implements it. See
`.duvet/config.toml` for why the rest are unregistered, and for why annotations
are written `#:=`/`#:%` rather than duvet's documented `#=`/`#%`.

An anchor duvet resolves is **not** always the anchor GitHub renders. Duvet
derives its section identifier from the heading with its own rules, and an
apostrophe becomes a separator rather than disappearing: ha-satellite REQ-043's
heading gives duvet `req-043-hardware-access-goes-through-the-daemon-s-media-layer`
where a markdown link in a change document uses `…-the-daemons-media-layer`. The
two spellings are both correct for their own reader. Do not "fix" one to match
the other.

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

**A `pyfakefs` test carries no marker, and that is not an omission.** The marker
declares real input or output. `pyfakefs` performs none — it is an in-memory
filesystem, which is exactly why it is a development dependency here — so a test
using the `fs` fixture is an ordinary unit test and marking it would say
something untrue. A test using pytest's `tmp_path` writes real files and does
carry it. The dividing line is whether anything reaches a disk, not whether the
word "filesystem" appears in the test.

The criterion is **the bytes on disk being the thing under test**; the golden
corpus is the first example, not the list. The second is the deployment files —
`test_groundstation_deployment.py` reads the Dockerfile, the compose files, the
scrape configuration and `.env.example` and compares them with the settings
model, `mise.toml` and the member list, where a fake would compare the
documentation with itself. Do not report those. A marker on a test that merely
*used* a real path for convenience still is a finding.

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

The same distinction governs a **timeout under test**. A unit test that
configures a real delay and waits for it to elapse is a finding, however short
the delay; a unit test that configures a timeout already expired by the time the
event loop looks at it is not, because nothing waits and no outcome can turn on
how loaded the runner is. The groundstation's three such tests use
`_ALREADY_ELAPSED`.

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

### Seeding a redactor is the one legitimate reveal outside the offer

A credential is held in `Credential`, whose `repr` and `str` render a
placeholder. Inside `reachy-session-client` it is revealed at exactly one call
site — building the session offer — and a second reveal there IS a finding.

A **consumer** may have one of its own, for one reason: handing the value to
whatever scrubs its output. A redactor cannot remove a string it was never
given, so that call is what makes reachyctl REQ-059 hold on the paths nobody
controls — the text of an exception raised three libraries down. `reachyctl`
does it once, in `cli/reachyctl/src/reachyctl/cli.py`, seeding its `Redactor`.
Do not report it as a leak or as a second reveal site; deleting it removes the
protection rather than tightening it. A reveal that feeds anything other than a
redactor or the offer is still a finding.

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

**`### Testing Requirements` is not a restatement, and neither is a Functional
requirements list that delegates.** The first is mandatory above and every one of
the 15 change documents carries it in the same shape: it cites
[Testing conventions](docs/specs/architecture/index.md#testing-conventions) and
then says which of those standing rules this change's pull request is judged
against, which is sequencing rather than a second normative copy. The second is
the documented shape too — the section names the spec that owns the behaviour,
says its scenarios are the acceptance criteria, and lists what implementing them
requires *of this change*. Do not report either as a violation of this rule. What
IS a violation is a change document stating a behavioural rule the spec does not,
or contradicting one the spec does.

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
