# 0014: Benchmark suite and performance gates

## Summary

Implement the benchmark suite, record the predecessor baseline in version
control, and turn performance into a merge gate rather than something noticed
after a release.

**Spec:** [Benchmarks](../specs/benchmarks/)
**Status:** draft
**Depends On:** 0006, 0013

## Motivation

The predecessor's numbers exist because someone measured them once, by hand. A
face pass fell from 1199 ms to 38 ms, the robot went from saturated to 1.52 of
four cores, and the image was 483 MB rather than roughly 2 GB. Those figures are
what justify the whole architecture, and they currently live in a report rather
than in anything that would notice them changing.

There is a specific claim to settle here too. The face model was changed from a
YOLO-derived detector to YuNet for licensing reasons, and that swap has been
argued rather than measured. This suite is where it is either confirmed or found
to have cost something.

## Requirements

### Testing Requirements

This change MUST satisfy the project's standing testing rules (see
[Testing conventions](../specs/architecture/index.md#testing-conventions)). CI
enforces these as merge gates:

- Tests run with `pytest`, with async strict mode enabled.
- Unit tests MUST perform no input or output.
- Coverage MUST be gated on the diff rather than on the whole tree.
- Type checking MUST run in strict mode for new modules.
- A lint or type suppression MUST carry the rule identifier and a justification.

Skipping or weakening any of these rules to land the PR MUST be treated as a bug
in the PR, not in the rule.

The benchmark harness itself MUST be tested, separately from the measurements it
takes: statistics, result serialisation and the regression comparison are
ordinary code with ordinary tests. A harness whose comparison logic is wrong
reports green through a real regression.

### Functional requirements

The [benchmarks spec](../specs/benchmarks/) owns what is measured and how it is
reported, including the recorded baseline. Its scenarios are this change's
acceptance criteria. What implementing them requires of this change:

- The baseline table from the spec is committed as machine-readable data, not
  only as prose, so the comparison has something to read.
- Hardware-requiring benchmarks are excluded from the default selection, and a
  run that excludes them reports them as excluded rather than as passing.
- The regression gate compares against a baseline measured on the same class of
  runner, with a stated tolerance, and updating that baseline is a reviewable
  change rather than an automatic one.
- Image and wheel sizes are recorded from the 0006 build outputs and gated on
  growth.
- The thread-count curve is reproduced rather than a single configured value
  measured, because the knee moves with the host — it was at four threads on the
  hardware originally measured, with 93 ms, 51 ms and 55 ms at one, four and six.
- Every result records its hardware, software versions and configuration.
- The YuNet-versus-baseline face comparison runs as part of this change and its
  result is written into the [perception spec](../specs/perception/) changelog.

#### Scenario: The recorded baseline is updated

- **GIVEN** a pull request that changes the committed baseline
- **WHEN** it is reviewed
- **THEN** the change is visible as a diff of recorded numbers, so accepting a
  regression is an explicit decision

## Design

### Approach

`bench/` holds the harness and the benchmark definitions. Each benchmark emits a
structured result; a comparison step reads a result and the committed baseline
and reports per-measurement deltas.

CI runs the hardware-free benchmarks on every pull request and gates on the
comparison. Hardware benchmarks are invoked deliberately through
`reachyctl bench` against a real installation.

That second group is why this change depends on 0013 rather than only on the
groundstation work: `photon-to-head` and `robot-load` measure a robot running
the satellite, so neither is implementable until the satellite exists. The
hardware-free group needs only the groundstation and its image.

Fixture frames are committed so detection benchmarks are reproducible; they are
small, synthetic or permissively licensed, and subject to the same licence bar
as everything else in the repository.

### Decisions

- **Decision**: Relative gating against a committed baseline, not absolute
  thresholds.
  - **Why**: CI hardware is not deployment hardware and varies between runs. An
    absolute threshold is either loose enough to catch nothing or tight enough
    to fail randomly.
  - **Alternatives considered**: Absolute thresholds; or no gate, which is where
    the predecessor was.
- **Decision**: The baseline is committed data, updated by pull request.
  - **Why**: A regression should require someone to say so in a review, rather
    than being absorbed by an automatically refreshed baseline.
  - **Alternatives considered**: Rolling baselines from recent runs, which
    absorb slow drift precisely because they are automatic.
- **Decision**: The thread curve is measured, not assumed.
  - **Why**: Four threads was the knee on one host. A suite that measured only
    the configured value would never reveal that it had moved.
- **Decision**: Network conditions are recorded rather than controlled.
  - **Why**: Normalising them away needs test infrastructure disproportionate to
    the project, and produces end-to-end numbers that do not describe the real
    installation — which is measured at 100–170 ms idle round-trip with 700 ms
    spikes.

### Non-Goals

- No historical result storage beyond baseline and current run.
- No automated photon-to-head stimulus; that remains an open question and the
  measurement stays manual.
- No performance work; this change measures, it does not optimise.

## Tasks

- [ ] Build the harness
  - [ ] Result structure recording hardware, versions and configuration
  - [ ] Distribution statistics reporting median and a high percentile
  - [ ] Structured result output
  - [ ] Comparison against a committed baseline with tolerances
  - [ ] Tests for statistics, serialisation and comparison
- [ ] Commit the baseline
  - [ ] Machine-readable baseline from the spec's recorded table
  - [ ] Document what updating it means
- [ ] Implement the hardware-free benchmarks
  - [ ] `detect` across a thread-count sweep
  - [ ] `pipeline` per stage
  - [ ] `session` round-trip and reconnection cost
  - [ ] `footprint` image, wheel and resident memory, from the 0006 outputs
  - [ ] Committed fixture frames with recorded licences
- [ ] Implement the hardware benchmarks
  - [ ] `photon-to-head`, invoked deliberately
  - [ ] `robot-load` at a configured frame rate
  - [ ] Exclusion from the default selection, reported as excluded
- [ ] Wire the gate and settle the model question
  - [ ] Per-pull-request benchmark job gating on the comparison
  - [ ] Run the YuNet-versus-baseline face comparison
  - [ ] Record the result in the perception spec changelog

## Open Questions

- [ ] How photon-to-head is stimulated repeatably. A person moving is not
      reproducible; a screen showing a moving face is, and measures something
      slightly different. Current lean: unresolved, manual until settled.
- [ ] What tolerance the detection gate uses. Too tight and CI variance fails
      honest changes; too loose and it catches nothing. Current lean: set it
      from observed run-to-run variance once there is some.

## References

- Spec: [Benchmarks](../specs/benchmarks/)
- Related changes: [0005-perception-capability](./0005-perception-capability.md),
  [0006-groundstation-images](./0006-groundstation-images.md),
  [0015-docs-and-runbooks](./0015-docs-and-runbooks.md)
