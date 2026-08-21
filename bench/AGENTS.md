# bench

The performance suite and the baseline it is judged against. Distribution
`reachy-bench`, import name `reachy_bench`.

**Spec:** [benchmarks](../docs/specs/benchmarks/).
**Filled in by:** [0014](../docs/changes/0014-benchmarks-and-gates.md).

Read the root [`AGENTS.md`](../AGENTS.md) first — it holds the invariants that
apply here. [`README.md`](README.md) beside this file says how to run the suite,
how to read a result, and what changing a recorded number means; read it before
touching `baseline.json`.

## Local rules

- **A workspace member that is never published.** It needs the shared resolution
  and it imports the other members, which is what makes it a member; it produces
  no artifact this repository distributes. The `Private :: Do Not Upload`
  classifier in `pyproject.toml` is what makes an accidental publish fail at the
  upload. Do not remove it, and do not add publishing steps here.
  - That is also why `reachyctl bench` imports this package **inside the
    command** rather than at module level. `reachyctl` is released as a wheel; a
    requirement on an unpublishable distribution would make that wheel
    uninstallable, and `just wheel-verify` would catch it. An installation
    without the suite is told so in a sentence.
- **Benchmarks are not tests.** Nothing under `src/` is collected by `pytest`.
  `bench/tests/` holds the harness's own tests, and every one of them is a unit
  test that performs no input or output — the statistics, the serialisation and
  the comparison are ordinary code, and they are tested as such.
  - The functions that do the expensive thing — open a model, start a server,
    read a robot's processor time — are excluded from coverage with a
    `# pragma: no cover` and a docstring saying why. A unit test of one of them
    would be a unit test of ONNX Runtime, uvicorn or the operating system. What
    exercises them is `just bench`, which the benchmark workflow runs on every
    pull request. Everything around them takes them as an argument and is
    tested against a fake.
- **A comparison that is wrong reports green through a real regression**, which
  is worse than having no gate at all. `compare.py` therefore has a test for
  every verdict it can reach, including the ones a passing run never produces.
  Do not add a branch to it without one.
- **Every result records its context** — hardware, software versions and the
  configuration measured under — because a result without them cannot be
  compared to the recorded baseline.
  - And **never the host's identity**. This repository is public and a benchmark
    result is exactly the artifact a hostname leaks through.
    `ALLOWED_HOST_FIELDS` names every field a host record may carry and a test
    holds the rendered document to exactly that set; a field added later is a
    red run rather than a leak nobody noticed.
- **Thresholds are relative to the committed baseline**, never absolute numbers
  pasted from one machine. Timings are keyed by the class of machine they were
  measured on; sizes are not, because a size does not depend on the machine that
  weighed it.
- **Nothing here writes to `baseline.json`.** `just bench-record` prints the
  block to paste. Changing a recorded number is a pull request, so that
  accepting a regression is a decision somebody made in a review.
- **Measurement names are namespaced by their benchmark.** The first dotted
  segment is the benchmark's own name, because the comparison attributes a
  recorded figure back to the benchmark that would have taken it — an excluded
  benchmark's recorded figures are left alone rather than reported as missing.
  `benchmark_name_problems` holds the suite to it and a test runs it.
