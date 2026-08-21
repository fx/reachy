# The benchmark suite

What this stack costs, measured rather than remembered, and judged against
numbers committed in this repository.

The [benchmarks spec](../docs/specs/benchmarks/) says what has to be true.
This file says how to run it, how to read the result, and — most importantly —
what changing a recorded number means.

## Running it

```
just models          # once: the benchmarks load the pinned face model
just bench           # the default selection, into bench-results.json
just bench-compare   # judge that result against the committed baseline
```

`just bench` runs the four benchmarks that need nothing but a container and
reports the two that need a robot as **excluded**, with the reason. Naming a
benchmark is what selects it, hardware or not:

```
just bench detect                      # just the thread sweep
just bench --iterations 200            # more passes per timing
just bench --artifact-size image-size/  # with sizes to record
```

Against a real installation, the two that need a robot go through `reachyctl`,
which already knows how to open a link to one:

```
reachyctl bench --benchmark robot-load --robot user@host
reachyctl bench --benchmark photon-to-head --observation 180 --observation 210
```

## What is measured

| Benchmark | Measures | Needs a robot |
|---|---|---|
| `detect` | The face pass, across a sweep of inference thread counts | no |
| `pipeline` | Decode, detection and result emission, per stage and end to end | no |
| `session` | Establishing a session, a frame's round trip, and reconnecting | no |
| `footprint` | Resident memory, and the size of every published artifact | no |
| `photon-to-head` | Stimulus to head movement, end to end | yes |
| `robot-load` | The robot's processor load while it tracks | yes |

Two of those need reading carefully.

**`detect` reproduces a curve, not a number.** The predecessor's inference was
fastest at four threads; the knee moves with the host, and a suite that measured
only the configured value would never reveal that it had moved. The knee is
reported as a note rather than gated, because a knee that moved is a property of
the machine.

**`pipeline` reports no gesture timing**, and that is deliberate. This build
wires no gesture model — the perception spec's recorded decision — so the
capability answers every frame with an empty payload in microseconds. Timing
that and putting it beside the predecessor's 5 ms would report an absent model
as a three-order improvement.

## What the gate decides

`just bench-compare` reads a result document and `baseline.json`, and fails when:

- a measurement is **worse than its recorded figure by more than the stated
  tolerance** — the line names the measurement and by how much;
- a measurement has **no recorded figure at all**, because a benchmark added
  without recording what it costs is one nothing will ever compare;
- a **recorded timing was not measured** by a run that took timings, because a
  measurement that disappears is otherwise indistinguishable from one that is
  fine;
- a **benchmark failed**.

It does *not* fail on an improvement, on an excluded benchmark, or on a class of
machine nobody has recorded — the last is reported as unbaselined, and the run
prints the profile to commit.

`just bench-sizes` is the narrow half, for the workflows that build an artifact
and time nothing. It reads the JSON `just image-size` and `just wheel-size`
already emit and compares `size_bytes` against the recorded size. It runs in
`images.yml` and in `release.yml` rather than beside the benchmarks, because
sizes are collected from the change that produces each artifact — rebuilding an
image somewhere else to weigh it would weigh a different build.

## The baseline, and what changing it means

`baseline.json` is committed data, and updating it is a pull request. That is
the whole point of it being data: a change to a recorded number is visible as a
diff of numbers, so accepting a regression is something somebody said out loud
in a review rather than something an automatically refreshed baseline absorbed.

Nothing writes to it. `just bench-record` prints the block to paste and says so.

It has three parts.

**`tolerances`** — how far a measurement of each unit may drift before it is a
regression. They are stated here rather than in code so that widening one is as
visible in a diff as changing a number.

**`artifacts`** — every published artifact's size, in bytes. Host-independent: an
image is the same number of bytes whichever machine weighed it, so these are one
flat set and are always gated.

**`profiles`** — the timings, keyed by the class of machine they were measured
on. Continuous integration hardware is not deployment hardware and varies
between runs, so a timing is only comparable against a baseline taken on the
same class. Two profiles ship today:

- `predecessor` — the stack this one replaces, measured once by hand on hardware
  that no longer exists. **Never gated against**: comparing a run to it would be
  a comparison between two machines as much as between two implementations. It
  is committed because the rebuild is accountable to it, and `bench-compare`
  prints a run beside it.
- `linux-x86_64-32c` — a real class of machine, gated.

### Adding a class of machine

Run the suite on it, read the numbers, and commit the block:

```
just bench
just bench-record --description "what the machine is"
```

A class with no profile is reported as unbaselined and gates nothing about its
timings; its sizes are gated regardless. Pass `--require-profile` to
`bench-compare` on a machine whose class *is* recorded — that is what stops the
timing half of the gate quietly becoming advisory if the label ever moves.

### Where the tolerances came from

They are argued from measured run-to-run variance, not chosen. Ten consecutive
runs of the whole default selection on an **idle** `linux-x86_64-32c` host, with
nothing changed between them, gave:

| Measurement | Worst single-run deviation from the median |
|---|---:|
| Every artifact size | 0.0% |
| Resident memory | 0.0% |
| `detect.face.threads.2` | 3.0% |
| `detect.face.threads.1` | 4.3% |
| `session.reconnect` | 9.4% |
| `pipeline.decode` | 9.6% |
| `session.connect` | 9.8% |
| `detect.face.threads.8` | 17.4% |
| `session.round_trip` | 36.3% |
| `detect.face.threads.6` | 38.1% |
| `pipeline.emit` | 50.0% |
| `pipeline.end_to_end` | 58.2% |
| `pipeline.capability.face` | 75.7% |
| `detect.face.threads.4` | 88.9% |

The five figures that never moved by a tenth looked stable enough to carry a
tighter bar of their own, so they were given one — 35% — and it fired on an
unchanged tree the first time a run happened while the machine was also running
the test suite. `detect.face.threads.2`, which had not moved by more than 3.0%
over ten idle runs, moved 72.3% on that one; every other latency figure moved
with it, between 10% and 77%. **A shared machine moves every latency figure
together**, which is exactly the condition a continuous integration runner is
permanently in and exactly what the benchmarks spec means by "continuous
integration runners are shared and variable". The tighter bar was removed.

So:

- **Sizes: 2%.** Eleven runs moved them not at all, because a size only changes
  when the artifact does. Two per cent is room for a base image's patch bump and
  nothing like room for a dependency that pulls in a large transitive tree.
- **Resident memory: 10%.** Also unmoved, idle or loaded; the margin is for
  allocator and page-size differences on another machine of the same class, and
  it is far below what importing a training framework costs.
- **Latency: 100%,** uniformly. A timing has to double before the gate fires.
  The worst honest movement observed is 89% idle and 77% under load, and a
  tolerance under those fails honest changes. It still catches what this project
  is actually afraid of: losing the offload is twentyfold, dropping to one
  thread is fourfold, and importing a training stack is not subtle. Watched
  failing, a threefold regression in the detection pass reports every sweep
  point at between +191% and +227%.
- **`pipeline.emit`: 200%,** stated on the entry itself. At four microseconds
  the figure is within an order of the clock's own granularity, so it moves by
  half its value over a difference of one tick — what is being measured is
  smaller than what is measuring it.
- **Robot processor load: 25%,** and that one is a judgement rather than a
  measurement — nothing has run on a robot yet. It is re-argued from data the
  first time `robot-load` runs against one.

The gate is loose, and deliberately so: a gate that fails on an unchanged tree
is one people learn to route around, and a gate that never fails is one nobody
notices has stopped working. What keeps it honest between those two is that
every delta is printed whether it failed or not, so drift towards the bar is
visible on a pull request that passed.

## Reading a result

One JSON document per run, and everything a comparison needs is in it:

- `context.host` — the class of machine, and **never its identity**. There is no
  hostname, user or address in a result document; `ALLOWED_HOST_FIELDS` in
  `reachy_bench.context` names every field that may appear and a test holds the
  document to exactly that set.
- `context.software` — the interpreter, the commit, and the versions that change
  what a timing means.
- `benchmarks[].configuration` — what *that* benchmark was configured with: the
  thread count, the model, the frame. Between them these answer REQ-068's
  question — whether a surprising number is a real improvement or a changed
  measurement condition — from the file alone.
- `benchmarks[].measurements[].distribution` — a median, a 95th percentile and
  the spread. The median is the figure the gate compares; the percentile is
  there because a stage that is usually fast and occasionally very slow has a
  mean that looks fine.
- `benchmarks[].notes` — what a reader needs in order not to misread the
  numbers.

## The network

Every latency figure the spec records crossed a 2.4 GHz WLAN at 100-170 ms idle
round-trip with 700 ms spikes. That is not a property of this stack and not
something a runner reproduces, so the suite **records** the network rather than
controlling it: `--network` puts the operator's description into the result, and
`session` runs over the loopback interface and says so in a note. A loopback
figure compared against a WLAN one is a comparison of two networks.

## Fixture frames

The detection benchmarks measure over the perception fixtures change 0005
generates into `services/groundstation/tests/fixtures/perception/`, reused rather
than duplicated — so the frames the benchmark measures are the frames the
perception tests assert on, and there is one provenance question rather than two.
They are drawn by `scripts/generate_perception_fixtures.py` from seeded random
draws rather than photographed, so there is no licence to check; see the `NOTICE`
beside them. The default is `scene_full.jpg`, because it is 640 by 480 — the
resolution the predecessor captured at, and the only one at which a face pass
here and the recorded 38 ms are measurements of the same amount of work.
