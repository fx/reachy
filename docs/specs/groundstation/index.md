# Groundstation

## Overview

The groundstation is the off-robot service that carries computation the robot
cannot afford. It terminates the [robot link](../robot-link/) session, routes
each frame to the capabilities agreed for that session, and returns results.

It is named for its relationship to the robot rather than for any particular
job. Vision is its first capability, not its definition — the structure exists
so that audio processing, planning, or direct control can be added without
touching the transport or the pipeline.

Nothing is implemented yet.

## Background

The predecessor was a single-purpose vision service: it pulled the robot's video
stream, ran two models, and posted results back. It worked, and it produced the
numbers in [benchmarks](../benchmarks/) — a face pass fell from 1199 ms on the
robot to 38 ms off it.

Two things about it do not survive into this design. It was named and shaped for
perception alone, so anything else would have arrived as a second service or as
an awkward tenant. And its configuration was read by a function that nothing
called, so every environment override was silently inert.

The naming decision drives the architecture. A service that will host more than
vision needs its session multiplexed by capability from the beginning, which is
why that requirement sits in the [robot link](../robot-link/) spec rather than
being deferred.

## Requirements

### REQ-022: Capabilities register without transport changes

Adding a capability MUST NOT require modification to the session layer, the
transport, or any other capability.

#### Scenario: A new capability is added

- **GIVEN** a groundstation offering face detection
- **WHEN** a developer adds an audio capability by implementing the capability
  interface and registering it
- **THEN** the new capability is offered during negotiation, and no file
  belonging to the transport, the session layer, or the face capability was
  changed

### REQ-023: Model files are present in the image

The service MUST load every model from a file already present in its deployed
artifact, and MUST NOT fetch model weights over the network at run time.

#### Scenario: The service starts without internet access

- **GIVEN** a groundstation container on a host with no outbound internet access
- **WHEN** the service starts
- **THEN** every configured capability loads its model and becomes ready

### REQ-024: Model provenance is recorded and verified

Every model file MUST be pinned by content hash, and the build MUST fail when a
fetched file's hash does not match the pinned value.

#### Scenario: An upstream model file is replaced

- **GIVEN** a pinned model whose upstream URL now serves different bytes
- **WHEN** the image is built
- **THEN** the build fails on the hash mismatch rather than shipping unknown
  weights

### REQ-025: A failed capability does not take down the service

When a capability fails to initialise, the service MUST continue serving the
capabilities that initialised successfully.

#### Scenario: One model file is corrupt

- **GIVEN** a deployment where the gesture model fails to load and the face
  model loads normally
- **WHEN** a session is negotiated
- **THEN** face detection is offered and used, the gesture failure is reported
  as unhealthy, and sessions continue to work

### REQ-026: Readiness is distinct from liveness

The service MUST report itself ready only once every capability it will offer
has completed its warm-up.

#### Scenario: An orchestrator waits for readiness

- **GIVEN** a starting service whose models are still warming up
- **WHEN** the orchestrator polls the readiness endpoint
- **THEN** the service reports not ready until warm-up completes, so no session
  arrives before the first inference would be slow

### REQ-027: Inference parallelism is bounded by configuration

The service MUST constrain each model runtime to a configured number of threads
rather than letting it size itself against the host.

#### Scenario: The service runs on a large host

- **GIVEN** a groundstation configured for four inference threads, deployed on a
  host with many more cores
- **WHEN** inference runs
- **THEN** it uses the configured number of threads, leaving the rest of the
  host undisturbed

### REQ-028: Work is attributable end to end

Every log line and metric emitted while handling a frame MUST carry the session
identifier and the frame's sequence number.

#### Scenario: One frame behaves strangely

- **GIVEN** a result that took far longer than usual to produce
- **WHEN** an operator searches the logs by that frame's sequence number
- **THEN** every stage that touched the frame is retrievable, including which
  session it belonged to

### REQ-029: Per-stage timings are measured and exposed

The service MUST record the duration of each pipeline stage separately and
expose those durations as metrics.

#### Scenario: Latency regresses after a change

- **GIVEN** a deployment whose end-to-end latency has increased
- **WHEN** an operator inspects the metrics
- **THEN** decode, each capability, and result emission are separately visible,
  so the responsible stage is identifiable without instrumenting further

### REQ-030: The effective configuration is retrievable at run time

The service MUST expose its fully resolved configuration over its own interface
while running, with every value marked secret replaced by a redacted
placeholder.

#### Scenario: An operator doubts a setting took effect

- **GIVEN** a running groundstation whose configuration was changed by
  environment variable
- **WHEN** the operator queries the configuration endpoint
- **THEN** the value in effect is returned, without restarting the service or
  reading its startup log

#### Scenario: The configuration includes a secret

- **GIVEN** a running groundstation holding a credential in its configuration
- **WHEN** the configuration endpoint is queried
- **THEN** the credential is reported as set but its value is not returned,
  because the endpoint is reachable by anything that can reach the service

### REQ-031: Images are published for both robot-adjacent architectures

Every release MUST publish the service image for both 64-bit ARM and 64-bit x86
architectures under the same tag.

#### Scenario: The service is deployed on ARM hardware

- **GIVEN** an operator deploying the published tag on a 64-bit ARM host
- **WHEN** the image is pulled
- **THEN** the ARM variant is selected automatically and runs

## Design

### Structure

```
services/groundstation/src/reachy_groundstation/
├─ api/            # session endpoint, health, readiness, metrics, config
├─ session/        # authentication, capability negotiation, routing
├─ capabilities/
│  ├─ perception/  # the first capability — see the perception spec
│  └─ …            # audio, planning, control: each self-contained
├─ pipeline/       # bounded async stages, drop-oldest under pressure
├─ runtime/        # model runtime sessions: providers, thread caps, warm-up
├─ models/         # pinned files, hashes, provenance and licence records
├─ obs/            # structured logging, tracing, metrics
└─ config.py       # settings, validated at import
```

Transport at the edge, session in the middle, capabilities as plugins. The
capability boundary is what REQ-022 describes: a capability declares the message
types it consumes and produces, and the session layer routes to it by name
without knowing anything else about it.

### Pipeline

Each session owns a bounded queue. Frames enter, are decoded once, and are
handed to every agreed capability. Results are collected and emitted against the
frame's sequence number.

Decoding once per frame rather than once per capability matters at the measured
numbers: decode was 2 ms against a 39 ms face pass, so a second decode is small
but a third and fourth would not be. Capabilities receive the decoded frame and
never the compressed bytes.

Backpressure is drop-oldest, specified in
[robot link REQ-015](../robot-link/index.md#req-015-overload-drops-frames-rather-than-queueing-them).
The pipeline implements it; the contract owns it.

### Model runtime

Models run on a general-purpose inference runtime rather than a training
framework. The measured difference was substantial: 483 MB of image against
roughly 2 GB, and 38 ms per face pass against 51 ms.

Thread counts are configuration because the correct value was found by
measurement and differs per host — on the hardware measured, four threads was
the knee, with 93 ms, 51 ms and 55 ms at one, four and six.

### Images

Two variants per release. The default targets CPU inference, which is what the
measurements support: the GPU available during the original work had 2.0 GB free
and CPU inference won outright. A CUDA variant is published alongside it so that
heavier future capabilities are not blocked on repackaging.

A compose file ships beside the image so the service is runnable without any
orchestrator, and it includes a metrics scrape configuration — the predecessor
exposed metrics that nothing ever collected.

### Configuration

Configuration behaviour is owned by
[architecture REQ-009](../architecture/index.md#req-009-configuration-is-validated-and-self-reporting):
unrecognised variables under the service's own prefix fail startup, and the
resolved configuration is emitted at boot. REQ-030 adds the run-time endpoint,
which is what makes the setting checkable without shell access to the container.

### Decision Records

#### An inference runtime, not a training framework

Measured on the same hardware and the same model: 38 ms against 51 ms, and a
483 MB image against roughly 2 GB. The hand-written pre- and post-processing
this requires was validated against a reference implementation — identical box
counts, centres within 2.2 px, confidence within 0.011 — and that validation is
now a standing test rather than a one-off. Rejected alternative: the training
framework's own inference path, which is slower, four times the image, and drags
in a copyleft licence.

#### CPU by default

The available GPU had 2.0 GB free, and CPU inference was faster than the
alternative under test. Publishing a CUDA variant keeps the option open without
making the common deployment depend on hardware it does not need.

## Constraints

- The service is expected to run on whatever host is available, including one
  without a GPU and one with no outbound internet access.
- Latency budget is set by the link, not by the service: results are useful only
  while the frame they describe is recent, on a network measured at 100–170 ms
  idle round-trip.
- The image is a public artifact, so every model it contains has to be
  redistributable — see [perception](../perception/) for the licence analysis.

## Open Questions

- **Whether capabilities may be added without redeploying.** Loading a
  capability from outside the image would make experimentation faster and would
  conflict directly with REQ-023's guarantee that everything needed is already
  present. Current default: capabilities ship in the image.
- **Whether one groundstation serves several robots concurrently.** Nothing in
  the session design prevents it, and nothing has been measured for it — model
  runtimes are shared, so thread limits interact. Current default: one robot,
  with the multi-robot case left unmeasured rather than claimed.

## References

- [architecture](../architecture/) — repository conventions and configuration rules
- [robot-link](../robot-link/) — the session contract this service terminates
- [perception](../perception/) — the first capability
- [benchmarks](../benchmarks/) — measurements cited throughout this spec

## Changelog

| Date | Change | Document |
|------|--------|----------|
| 2026-08-20 | Initial spec created | — |
