# Benchmarks

## Overview

The benchmark suite measures the quantities that decide whether this stack is
usable: how long a detection takes, how long it takes for a movement of a face
to become a movement of the head, how much of the robot's CPU is left for
anything else, and how large the deployed artifacts are.

It exists because the predecessor's numbers were measured once, by hand, and
then held in someone's memory. Turning them into a suite makes them a
regression gate instead.

Nothing is implemented yet.

## Background

The predecessor stack was measured carefully, and those measurements are the
baseline this rebuild is accountable to. They are recorded here because a
rewrite that quietly loses ground is the most likely way this project fails, and
the only defence is knowing the numbers before starting.

### Recorded baseline

| Measurement | Baseline | Notes |
|---|---:|---|
| Face pass, on robot | 1199 ms | The original arrangement, before offloading |
| Face pass, off robot | 38 ms | Inference runtime, four threads |
| Frame decode | 2 ms | |
| Gesture pass | 5 ms | Sampled every fourth frame |
| Result delivery | 54 ms | Per-request, with connection reuse enabled |
| Photon to head | 150–250 ms | End to end |
| Robot CPU while tracking at 10 fps | 1.52 of 4 cores | Detection offloaded |
| Application resident memory | 205 MB | Training framework never imported |
| Service image size | 483 MB | Against roughly 2 GB for the alternative |

Two further figures shape how the suite is designed. Inference thread count was
measured at 93 ms, 51 ms and 55 ms for one, four and six threads, so four was
the knee on that hardware and the curve is worth reproducing rather than
assuming. And a cold connection to the robot cost 378 ms at p50, which is the
measurement that drove the [robot link](../robot-link/) design.

### The network underneath

Every latency figure above was measured over a WLAN with a 100–170 ms idle
round-trip time and occasional 700 ms spikes, on 2.4 GHz only. That is not a
property of this stack and it is not something the stack can fix, but it sits
underneath every end-to-end number, so a comparison against the baseline is only
meaningful when the network context is comparable.

## Requirements

### REQ-067: Results are structured and machine-readable

Every benchmark run MUST emit its results in a structured format that another
program can consume without parsing human-facing output.

#### Scenario: Results are compared across runs

- **GIVEN** two benchmark runs from different commits
- **WHEN** a comparison tool reads both result files
- **THEN** it can compare each measurement without screen-scraping

### REQ-068: Every result records the context it was measured in

Each benchmark result MUST record the hardware, the software versions, and the
configuration it was produced under.

#### Scenario: An unexpectedly fast result appears

- **GIVEN** a result substantially faster than the previous run
- **WHEN** an engineer inspects it
- **THEN** the host, the thread count, and the model in use are readable from
  the result itself, so the improvement can be distinguished from a changed
  measurement condition

### REQ-069: Latency is reported as a distribution

Timing measurements MUST report at least a median and a high percentile rather
than a mean alone.

#### Scenario: A stage becomes intermittently slow

- **GIVEN** a pipeline stage that is usually fast but occasionally very slow
- **WHEN** the benchmark reports its timings
- **THEN** the high percentile reflects the slow cases rather than being
  averaged away

### REQ-070: Stages are measured separately

The suite MUST report the duration of each pipeline stage individually in
addition to any end-to-end measurement.

#### Scenario: End-to-end latency regresses

- **GIVEN** a run whose end-to-end figure has worsened
- **WHEN** an engineer reads the result
- **THEN** the responsible stage is identifiable from the per-stage figures
  without a further instrumented run

### REQ-071: Regression is judged against a baseline recorded in the repository

Continuous integration MUST compare benchmark results against a baseline stored
in version control and fail when a measurement regresses beyond a stated
tolerance.

#### Scenario: A change slows detection substantially

- **GIVEN** a pull request that makes the detection pass materially slower
- **WHEN** the benchmark gate runs
- **THEN** the check fails, naming the measurement that regressed and by how
  much

### REQ-072: Benchmarks requiring hardware are opt-in

Any benchmark that requires a physical robot MUST be excluded from the default
suite and selectable explicitly.

#### Scenario: Continuous integration runs the default suite

- **GIVEN** a continuous integration runner with no robot attached
- **WHEN** the default benchmark suite runs
- **THEN** it completes without attempting a measurement that needs hardware,
  and without reporting a skip as a failure

### REQ-073: Artifact size is measured as a tracked quantity

The suite MUST record the size of each published artifact and treat growth
beyond a stated tolerance as a regression.

#### Scenario: A dependency pulls in a large transitive tree

- **GIVEN** a change that adds a dependency substantially increasing image size
- **WHEN** the size measurement runs
- **THEN** the growth is reported and fails the tolerance, prompting a decision
  rather than passing unnoticed

## Design

### What is measured

| Benchmark | Measures | Needs a robot |
|---|---|---|
| `detect` | Per-model inference time at several thread counts | no |
| `pipeline` | Decode, detection, result emission, per stage | no |
| `session` | Round-trip time over an established session, and reconnection cost | no |
| `footprint` | Image size, wheel size, resident memory | no |
| `photon-to-head` | Physical movement to commanded head movement | yes |
| `robot-load` | Robot CPU while tracking at a given frame rate | yes |

The split is what REQ-072 formalises. Most of the value is in the measurements
that need nothing but a container, which is what makes a per-pull-request gate
possible at all.

### Relative rather than absolute thresholds

Continuous integration hardware is not the hardware anyone deploys on, and it
varies between runs. An absolute threshold would either be so loose it catches
nothing or so tight it fails randomly.

The gate in REQ-071 therefore compares against a baseline recorded in the
repository and measured on the same class of runner, and it tolerates a stated
margin. Updating the baseline is an explicit, reviewable change — which is the
point: a regression should require someone to say so in a pull request rather
than being absorbed silently.

### The thread-count curve

`detect` reproduces the curve rather than asserting a single number, because the
knee moves with the host. On the hardware originally measured it sat at four
threads; on a different host it will not, and a suite that only measured the
configured value would never reveal that.

### Photon to head

This is the measurement that matters to a person in the room and the hardest to
automate: it spans a physical stimulus, capture, transport, inference, transport
again, and a motor command. It needs a robot and a repeatable stimulus, so it
runs on demand rather than continuously.

It is specified anyway, because the per-stage figures can all improve while the
end-to-end experience does not — and the end-to-end experience is the thing
being bought.

### Decision Records

#### The baseline is recorded before the rewrite starts

The predecessor's numbers exist because someone measured them once. Writing them
into the repository before any replacement is built means the rewrite is
accountable to them, rather than being compared against a recollection that will
have drifted by the time there is anything to compare. The face figure in
particular is what the model change in [perception](../perception/) has to
defend.

#### Network conditions are recorded, not controlled

The suite records the network context rather than trying to normalise it away.
Anything else would either require test infrastructure disproportionate to the
project or produce end-to-end numbers that do not describe the real
installation.

## Constraints

- The robot is a four-core aarch64 device also running motion control and audio,
  so any on-robot measurement competes with real work.
- Continuous integration runners are shared and variable, which is why REQ-071
  is relative.
- Hardware benchmarks need physical access to one robot, so they are run
  deliberately rather than on every change.

## Open Questions

- **How photon-to-head is stimulated repeatably.** A person moving in front of
  the robot is not reproducible; a screen showing a moving face is, but measures
  a slightly different thing. Current default: unresolved, and the measurement
  is manual until it is.
- **Whether benchmark history is retained beyond the current baseline.** A
  series would show slow drift that a pairwise comparison misses, at the cost of
  somewhere to keep it. Current default: baseline plus the current run.

## References

- [architecture](../architecture/) — continuous integration and gating conventions
- [groundstation](../groundstation/) — the service under measurement
- [perception](../perception/) — the model change these numbers adjudicate
- [robot-link](../robot-link/) — the protocol whose latency is measured
- [reachyctl](../reachyctl/) — `bench` runs this suite against a live installation

## Changelog

| Date | Change | Document |
|------|--------|----------|
| 2026-08-20 | Initial spec created | — |
