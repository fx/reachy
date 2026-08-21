# bench

The performance suite and the baseline it is judged against. Distribution
`reachy-bench`, import name `reachy_bench`.

**Spec:** [benchmarks](../docs/specs/benchmarks/).
**Fills this in:** [0014](../docs/changes/0014-benchmarks-and-gates.md).

Read the root [`AGENTS.md`](../AGENTS.md) first — it holds the invariants that
apply here.

## Local rules

- **This is a scaffold.** It has a `pyproject.toml`, this file and an empty
  package. Do not add implementation ahead of the change that owns it.
- **A workspace member that is never published.** It needs the shared resolution
  and it imports the other members, which is what makes it a member; it produces
  no artifact this repository distributes. The `Private :: Do Not Upload`
  classifier in `pyproject.toml` is what makes an accidental publish fail at the
  upload. Do not remove it, and do not add publishing steps here.
- **Benchmarks are not tests.** They are excluded from the default `pytest` run
  by the change that adds them, and anything requiring a physical robot is
  opt-in on top of that, so `just test` never needs hardware.
- **Every result records its context** — hardware, software versions and the
  conditions measured under — because a result without them cannot be compared
  to the recorded baseline.
- **Thresholds are relative to the committed baseline**, never absolute numbers
  pasted from one machine.
