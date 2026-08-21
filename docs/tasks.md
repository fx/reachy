# Tasks

Catch-all task list for work not tracked in a specific [change document](changes/).

## Backlog

- [ ] Correct the [benchmarks spec](specs/benchmarks/) Overview, which still
      says "Nothing is implemented yet." Change 0014 implemented the suite, the
      committed baseline and the gate, so the sentence is now false. It is here
      rather than in that change because a spec edit is its own proposal, made
      through `/spec-writer`, and 0014 was allowed one changelog row in the
      perception spec and nothing else.

## Completed

- [x] Record a baseline profile for the `github-ubuntu-latest` runner class in
      `bench/baseline.json`, so the timing half of the benchmark gate judges
      something rather than reporting the class as unbaselined. Recorded in
      [0014](changes/0014-benchmarks-and-gates.md) from the first real run of
      `bench.yml`, which is the only way to get figures for a pool nobody can
      run on locally, and the job now passes `--require-profile` so the class
      cannot silently stop being recorded.
