# 0014: Benchmark suite and performance gates

## Summary

Implement the benchmark suite, record the predecessor baseline in version
control, and turn performance into a merge gate rather than something noticed
after a release.

**Spec:** [Benchmarks](../specs/benchmarks/)
**Status:** complete
**Depends On:** 0006, 0009, 0013

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
- Artifact sizes are collected from the change that produces each artifact, not
  from a single build: the container image from 0006, the `reachyctl` wheel from
  0009, and the satellite wheel from 0013. Each is gated on growth against the
  committed baseline.
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

- [x] Build the harness
  - [x] Result structure recording hardware, versions and configuration
  - [x] Distribution statistics reporting median and a high percentile
  - [x] Structured result output
  - [x] Comparison against a committed baseline with tolerances
  - [x] Tests for statistics, serialisation and comparison
- [x] Commit the baseline
  - [x] Machine-readable baseline from the spec's recorded table
  - [x] Document what updating it means
- [x] Implement the hardware-free benchmarks
  - [x] `detect` across a thread-count sweep
  - [x] `pipeline` per stage
  - [x] `session` round-trip and reconnection cost
  - [x] `footprint` resident memory, plus artifact sizes collected from each
        producing change — image from 0006, `reachyctl` wheel from 0009,
        satellite wheel from 0013
  - [x] Committed fixture frames with recorded licences
- [x] Implement the hardware benchmarks
  - [x] `photon-to-head`, invoked deliberately
  - [x] `robot-load` at a configured frame rate
  - [x] Exclusion from the default selection, reported as excluded
- [x] Wire the gate and settle the model question
  - [x] Per-pull-request benchmark job gating on the comparison
  - [x] Run the YuNet-versus-baseline face comparison
  - [x] Record the result in the perception spec changelog

## Completion notes

- **`complete` here means implemented, tested and gated in continuous
  integration.** It does not mean validated on a robot. `photon-to-head` and
  `robot-load` are implemented and were never run: nothing in this repository
  has a Reachy Mini attached, so they are excluded from the default selection
  and reported as excluded — which is REQ-072 rather than a shortcut. The
  session that runs them is sequenced by
  [0015](./0015-docs-and-runbooks.md).
- **The spec is registered in `.duvet/config.toml`.** All seven of its
  requirements are annotated and traced. It is registered by this change because
  this change is the only one that touches it, and because a gate registered
  before its baseline existed would be a green check over a comparison against
  nothing.
- **The numbers this repository actually has**, measured on a
  `linux-x86_64-32c` host over a 640 by 480 committed fixture, beside the
  predecessor's hand-measured figures:

  | Measurement | This build | Predecessor |
  |---|---:|---:|
  | Face pass, four threads | 1.9 ms | 38 ms |
  | Face pass, one thread | 7.8 ms | 93 ms |
  | Frame decode | 0.46 ms | 2 ms |
  | Result round trip | 4.2 ms | 54 ms |
  | Establishing a session | 1.0 ms | 378 ms |
  | Service resident memory | 119 MiB | 205 MiB |
  | Service image, amd64 | 437.3 MiB | "483 MB" |

  Two of those rows are not like-for-like and the result document says so in a
  note: the round trip and the connection crossed a loopback interface here and
  a 2.4 GHz WLAN there, and the predecessor's 205 MiB was its robot
  application's rather than a groundstation's. The face figures are the same
  amount of work on different hardware.
- **The thread-count curve was reproduced and the knee did not move.** 7.8, 3.0,
  1.9, 2.5 and 2.7 ms at one, two, four, six and eight threads: four is still
  the knee, on a machine with thirty-two cores rather than four. That is worth
  knowing precisely because it was not assumed.
- **No gesture timing is reported at all**, and the result carries a note saying
  why. This build wires no gesture model — the perception spec's recorded
  decision — so the capability answers every frame with an empty payload in
  microseconds, and putting that beside the predecessor's 5 ms would report an
  absent model as a three-order improvement.
- **The gate's timing half does not cover the runner class yet.** The committed
  baseline holds a profile for the machine these numbers were measured on;
  nothing has been recorded for `github-ubuntu-latest`, because that needs a run
  on one. Until it is, `bench.yml` reports its numbers, reports the class as
  unbaselined, and prints the profile block to commit into the job summary —
  and the *size* half gates regardless, in `images.yml` and `release.yml`, since
  a size does not depend on the machine that weighed it. Committing that block
  is the follow-up, and it is one reviewable diff.
- **A latent import cycle in the groundstation was fixed here**, because this
  change is what tripped over it. `reachy_groundstation.pipeline` could not be
  imported before `reachy_groundstation.session`: `pipeline.runner` imports
  `MessageKind` from `session.framing`, which initialises the session package,
  which imports `session.runner`, which came back for `FramePipeline` while
  `pipeline.runner` was still running its own imports. Every path through the
  service itself reaches session first, so the cycle was invisible until the
  benchmarks imported the pipeline directly. `session/runner.py` now binds the
  module rather than the class, and says why.
- **The benchmarks spec's Overview still says "Nothing is implemented yet."**
  Editing it is out of scope here — a spec change is its own proposal — but it
  is now false, and correcting it belongs in the next pass over that spec.

## Open Questions

- [x] **How photon-to-head is stimulated repeatably.** Resolved as: it is not
      automated, and the benchmark owns the *reporting* rather than the
      stimulus. Both candidates measure slightly different things and building
      either is disproportionate to the project as it stands, so the
      measurement stays an operator's: `photon-to-head` validates the intervals
      it is given, reports them as a distribution, records the method in the
      result's configuration, and refuses to report anything without them —
      because a number it invented would be worse than no number. Recording the
      method is what makes "two runs are only comparable when the same method
      produced both" checkable rather than hoped for.
- [x] **What tolerance the detection gate uses.** Resolved from measured
      variance rather than chosen, and revised once when the first choice fired
      on an unchanged tree. Ten consecutive runs of the whole default selection
      on an idle machine moved every artifact size and the resident memory not
      at all, moved five latency figures by under a tenth, and moved one — the
      sweep point at the knee — by 89%. The five stable ones were given a
      tighter bar of 35%; an eleventh run, taken while the machine was also
      running the test suite, moved one of them by 72% and every other latency
      figure with it. A shared machine
      moves every latency figure together, which is the condition a continuous
      integration runner is permanently in, so the tighter bar was removed. The
      answer is: sizes 2%, resident memory 10%, latency 100% uniformly, and
      200% on `pipeline.emit` alone, whose four microseconds are within an order
      of the clock's own granularity. Robot processor load is 25% and is the one
      number still a judgement — nothing has run on a robot yet, and it is
      re-argued from data the first time `robot-load` does. The tolerance is
      loose on purpose: a threefold regression in the detection pass reports
      every sweep point at between +191% and +227%, and every delta is printed
      whether it failed or not, so drift towards the bar is visible on a pull
      request that passed. The observations are tabulated in `bench/README.md`.

## References

- Spec: [Benchmarks](../specs/benchmarks/)
- Related changes: [0005-perception-capability](./0005-perception-capability.md),
  [0006-groundstation-images](./0006-groundstation-images.md),
  [0015-docs-and-runbooks](./0015-docs-and-runbooks.md)
