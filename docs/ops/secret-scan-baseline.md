# Secret-scan baseline

The one-off full-history secret scan, recorded because a scan nobody wrote down
is a scan nobody can tell was run. From here on the `Secret scan` job in
`.github/workflows/hygiene.yml` covers what arrives; this file covers what was
already there when the gate went up.

The repository was two commits old when the gate was wired, so the full history
was cheap to scan and never gets cheaper. Reproduce any row with the command
beside it — `just secret-scan` takes `git log` options, so the range is the
argument.

## Result

| | |
|---|---|
| **Date** | 2026-08-21 |
| **Tool** | gitleaks 8.30.1, default rule set, `--redact` |
| **Invocation** | `just secret-scan "<range>"` |
| **Findings** | **0** |

| Range scanned | Command | Commits | Bytes | Findings |
|---|---|---|---|---|
| Full history of the default branch | `just secret-scan "main"` | 3 | 412.60 KB | 0 |
| The change that wired the gates | `just secret-scan "main..HEAD"` | 1 | 72.26 KB | 0 |

```
INF 3 commits scanned.
INF scanned ~412603 bytes (412.60 KB) in 120ms
INF no leaks found
```

## What this does and does not establish

It establishes that gitleaks' default rule set found no credential in the
history it was pointed at, on that date, with that version. It does not
establish that the history contains no secret: a rule set only matches the
shapes it knows, and a credential with no distinguishing shape — a password that
looks like a word, an account name — passes any scanner. That class is caught in
review, which is why `REVIEW.md` names it explicitly.

It also does not cover the environment-leak class at all. Hostnames, private
addresses and email addresses are not secrets and gitleaks does not look for
them; the `Leak scan` job does, and it runs on the same events.

## Re-running it

Redo the full-history scan whenever the rule set moves significantly — a
gitleaks major release, or a new credential type entering the repository — and
replace the table above rather than appending to it. A baseline is a statement
about now; a list of historical statements is a changelog nobody reads.

```
mise install                 # gets the pinned gitleaks
just secret-scan "main"      # full history of the default branch
just secret-scan             # every commit reachable in the clone
```
