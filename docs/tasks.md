# Tasks

Catch-all task list for work not tracked in a specific [change document](changes/).

## Backlog

- [ ] Correct the [benchmarks spec](specs/benchmarks/) Overview, which still
      says "Nothing is implemented yet." Change 0014 implemented the suite, the
      committed baseline and the gate, so the sentence is now false. It is here
      rather than in that change because a spec edit is its own proposal, made
      through `/spec-writer`, and 0014 was allowed one changelog row in the
      perception spec and nothing else.
- [ ] Record a baseline profile for the `github-ubuntu-latest` runner class in
      `bench/baseline.json`. Until one exists, the benchmark workflow reports
      its numbers and reports the class as unbaselined, so the *timing* half of
      the gate cannot fail — the size half, in the image and release workflows,
      is unaffected. The first run of `bench.yml` on the default branch prints
      the block to commit into its job summary; committing it is one reviewable
      diff, and `bench-compare --require-profile` can be added to the job in the
      same pull request so the class cannot silently stop being recorded.

## Completed
