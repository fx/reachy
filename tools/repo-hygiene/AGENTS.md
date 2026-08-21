# tools/repo-hygiene

The repository's own hygiene gates. Distribution `reachy-hygiene`, import name
`reachy_hygiene`.

**Spec:** [architecture](../../docs/specs/architecture/) owns
[REQ-003](../../docs/specs/architecture/index.md#req-003-no-environment-specific-values-in-version-control)
and
[REQ-004](../../docs/specs/architecture/index.md#req-004-automated-leak-detection-on-every-change).
**Fills this in:** [0002](../../docs/changes/0002-ci-and-hygiene-gates.md).

This member is REQ-004 — the automated detection. **REQ-003 is the prohibition
and the untracked-file-plus-tracked-`.example` rule, and its duvet citation is in
`.gitignore`**, where the ignore rules and their negations are the mechanism the
requirement describes. Detecting a leak and being arranged so there is nothing to
detect are two different things, and they are cited from the two different files
that do them.

Read the root [`AGENTS.md`](../../AGENTS.md) first — it holds the invariants
that apply here.

## Local rules

- **Patterns describe shapes, never names.** A list of the real hostnames and
  accounts this repository keeps out would itself publish them in the
  repository whose purpose is to exclude them. Anything added to
  `patterns.py` must be a shape — an address range, a hostname suffix, an
  address form — that is recognisable without knowing anyone's environment.
- **Every pattern change moves the corpus with it.**
  `src/reachy_hygiene/corpus.py` holds one tuple of strings that MUST be caught
  and one tuple that MUST NOT be, and the test suite asserts both directions.
  Tightening a pattern without extending the corpus is how a gate silently
  starts failing legitimate content.
- **The corpus is the single self-scan exemption.** It is listed by exact path
  in `patterns.EXEMPT_PATHS`, not by directory, because a directory rule would
  quietly exempt every file added beside it later. Anything else that must
  carry a leak-shaped string uses the inline `leak-scan:allow` marker, which is
  visible on the line a reviewer is already reading.
- **The scanner is pure; the process boundary is one function.** Pattern
  matching, diff parsing and commit-log parsing take strings and return values,
  which is what lets the whole tool be tested without touching a filesystem or
  a subprocess. The single `git` invocation takes an injectable runner so its
  callers stay testable too.
- **A finding never prints the value it found.** Continuous integration logs on
  a public repository are public, so a scan that echoed the leak would publish
  it while reporting it. Findings name the file, the line and the rule, and
  redact the match — in the reported path as well as in the excerpt, because
  the filename is sometimes the leak.
- **This member is never published.** Do not remove the
  `Private :: Do Not Upload` classifier and do not add publishing steps here.
