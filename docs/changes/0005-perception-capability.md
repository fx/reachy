# 0005: Perception capability

## Summary

Implement the perception capability: the model runtime, the pinned model store,
YuNet face detection with its parity test, and gesture recognition with the
negatives evaluation that makes its accuracy a measured quantity.

**Spec:** [Perception](../specs/perception/)
**Status:** complete
**Depends On:** 0004

## Motivation

This is the capability that justifies the groundstation existing: it moved a
face pass from 1199 ms on the robot to 38 ms off it, and dropped the robot from
saturation to 1.52 of four cores.

It is also where the repository's licensing exposure is decided. The
predecessor's face model derived from an AGPL-3.0 codebase, which a public
repository and a published image cannot carry without imposing that licence on
the service and on everyone who deploys it.

## Requirements

### Testing Requirements

This change MUST satisfy the project's standing testing rules (see
[Testing conventions](../specs/architecture/index.md#testing-conventions)). CI
enforces these as merge gates:

- Tests run with `pytest`, with async strict mode enabled.
- Unit tests MUST perform no input or output.
- Integration tests MUST run real inference against fixture images rather than
  mocking the runtime.
- Coverage MUST be gated on the diff rather than on the whole tree.
- Type checking MUST run in strict mode for new modules.
- A lint or type suppression MUST carry the rule identifier and a justification.

Skipping or weakening any of these rules to land the PR MUST be treated as a bug
in the PR, not in the rule.

The parity test required by
[REQ-036](../specs/perception/index.md#req-036-post-processing-is-verified-against-a-reference-implementation)
is a merge gate, not an advisory check. Hand-written decoding of model output is
the highest-risk code in this repository — it is silently wrong when it is
wrong, and its output goes to a motor.

### Functional requirements

The [perception spec](../specs/perception/) owns detection semantics, the
licence bar, and the accuracy requirements. Its scenarios are this change's
acceptance criteria. What implementing them requires of this change:

- The face model is YuNet, retrieved from the pinned source recorded in the
  model registry, with its hash verified at build time.
- The model registry records source, licence and retrieval location alongside
  the hash, satisfying
  [REQ-033](../specs/perception/index.md#req-033-model-licence-and-provenance-are-recorded-beside-the-model).
  It is a tracked file; weights are never committed.
- YuNet's dynamic input shape is used directly — frames are padded to the
  model's stride rather than letterboxed onto a fixed canvas. This removes the
  letterbox-and-reverse step entirely, along with the coordinate bugs that live
  in it.
- The parity reference is the Reachy Mini SDK's own YuNet decoder, which runs
  the same weights. Tolerances are stated in the test, not implied.
- Gesture recognition stays a two-stage detector-then-classifier arrangement,
  with its sampling rate as configuration rather than a constant.
- The negatives fixture set ships with this change and its false-positive rate
  is reported by the suite as a number, whether or not it is acceptable.
- Detectors are independently switchable at run time, and disabling one produces
  ordinary results from the others rather than an error.

## Design

### Approach

`runtime/` owns model-runtime sessions: provider selection, thread limits,
warm-up. `models/` owns the registry, the fetch-and-verify build step, and the
licence check. `capabilities/perception/` owns the detectors themselves behind
the capability interface from 0004.

Models are fetched during the image build and verified against their pinned
hashes, never at run time — the service has to start on a host with no outbound
internet access.

The licence check is a test over the registry, not a manual review step, so
adding a model with unacceptable terms fails in CI rather than in someone's
memory.

### Decisions

- **Decision**: YuNet rather than the predecessor's YOLO-derived face model.
  - **Why**: The rationale is recorded in full in the
    [perception spec's decision records](../specs/perception/index.md#decision-records).
    In short: AGPL-3.0 exposure on a published image, ambiguous chain of title,
    and a permissive alternative that the robot's own SDK already uses.
  - **Alternatives considered**: Accepting AGPL for the service, or a commercial
    licence — both cost more than a model swap.
- **Decision**: The parity reference is the SDK's decoder.
  - **Why**: It runs identical weights, is independently maintained, and already
    runs on the target hardware. A reference implementation nobody else
    maintains is a second thing to keep correct.
  - **Alternatives considered**: A framework's own inference path, which would
    reintroduce the dependency and the licence the model change removes.
- **Decision**: Gesture model selection is deferred, and the capability ships
  switchable.
  - **Why**: The existing classifier reports hand signals at 0.9 confidence in
    an empty room, and its weights have not had a provenance check. Carrying it
    forward silently would repeat the predecessor's mistake; blocking the whole
    capability on choosing a replacement would block the change.
  - **Alternatives considered**: Shipping the existing classifier as the
    default, which makes a known defect the out-of-box experience.

### Non-Goals

- No choice of replacement gesture model — this change makes the choice
  measurable and leaves it open.
- No facial landmark output beyond the centre and confidence.
- No CUDA-specific tuning; the CUDA image variant is 0006 and this change stays
  provider-agnostic.

## Tasks

- [x] Implement the model runtime
  - [x] Runtime session management with provider selection and thread limits
  - [x] Warm-up, wired to the readiness signal from 0004
  - [x] Thread-count configuration with its documented default
- [x] Implement the model store
  - [x] Registry file recording source, licence, location and hash per model
  - [x] Build-time fetch with hash verification and failure on mismatch
  - [x] Licence-allowlist test over the registry
- [x] Implement face detection
  - [x] YuNet inference using dynamic input shape, no letterbox
  - [x] Output decoding and overlap suppression
  - [x] Parity test against the SDK decoder with stated tolerances
  - [x] Resolution-independence test across two scales of one fixture
- [x] Implement gesture recognition — the capability, not a model. The model
      choice is a non-goal above and the perception spec's decision record
      defers it, so no gesture model is registered and neither stage has one
      behind it.
  - [x] Hand detection and crop classification — the two-stage arrangement, its
        two interfaces, the cropping and the thresholding, exercised end to end
        through scripted stages. No hand model and no classifier are wired.
  - [x] Configurable sampling rate
  - [x] Negatives fixture set and false-positive rate reporting
- [x] Wire run-time switching
  - [x] Per-detector enable and disable
  - [x] Configurable thresholds, visible through the configuration endpoint
  - [x] Test that disabling every detector yields empty results, not errors

## Open Questions

- [x] Which gesture model replaces the current classifier. **Decided: none, and
      the choice is now a measurement rather than a blocker.** Choosing one is
      follow-up work for the change that proposes a candidate; it is no longer
      an open question for this change, because what this change owed it was the
      evidence. No hand-signal classifier was found that clears
      [REQ-032](../specs/perception/index.md#req-032-detection-models-are-permissively-licensed)'s
      licence bar with a provenance chain this repository can record, so no
      gesture model is registered and the capability ships with neither stage
      wired. The evaluation harness is in place and reports a number for
      whatever is wired, so a candidate is compared rather than argued about.
      The question moves to the change that proposes one.
- [x] Whether the gesture capability ships enabled by default before a
      replacement model is chosen. **Disabled by default**, as the lean
      suggested. With no model wired it would answer every frame with an empty
      payload, so offering it would advertise a capability that does nothing.
      Switching it on is one environment variable, and it then negotiates,
      samples and answers normally.
- [x] What the parity tolerances should be. **Tightened, well below the lean.**
      The two implementations agree exactly: across 6 fixtures and 9 faces the
      maximum centre deviation is 0.000000 px and the maximum confidence
      deviation is 0.000000, because they are two spellings of the same
      arithmetic over the same session's float32 outputs. The stated tolerances
      are 0.5 px and 0.005 — not zero, so that a future refactor moving a result
      by an ulp does not fail a gate people would then route around, and tight
      enough that a real decoding error moves a detection by tens of pixels.
      Resolution independence is stated separately at 0.02 normalised units,
      against an observed worst case of 0.013.

## Completion notes

**What shipped.** `runtime/` bounds every model session by configured thread
counts and runs inference on a single worker thread off the event loop.
`models/` holds a tracked registry pinning the face model by digest, a
build-time fetch that refuses anything else, a run-time store that only ever
reads, and a licence gate that is an ordinary unit test.
`capabilities/perception/` holds the two detectors behind 0004's interface.

**The face model.** YuNet, MIT, retrieved from an immutable revision of the same
Hugging Face repository the Reachy Mini SDK downloads from — an unmodified
redistribution of the OpenCV Zoo model. Its digest, licence, attribution,
upstream project and retrieval URL are in
`services/groundstation/src/reachy_groundstation/models/registry.py`, and weights
are never committed. `just models` fetches and verifies them; continuous
integration runs it before the suite.

**Disabled is not unhealthy.** A capability switched off by configuration
declines to be built by raising `CapabilityDisabledError`, and the registry
records `CapabilityState.DISABLED`. It is offered to nobody and routed to by
nothing, and the health surface distinguishes it from a capability that failed —
so an operator can tell "I turned that off" from "that broke".

**What was deliberately not done.** No gesture model was chosen or wired, per the
first open question above. No CUDA-specific work: the provider list is
configuration, and the accelerated image variant is 0006. No facial landmarks,
which the spec leaves for the change that introduces a consumer.

## References

- Spec: [Perception](../specs/perception/)
- Related changes: [0004-groundstation-session](./0004-groundstation-session.md),
  [0006-groundstation-images](./0006-groundstation-images.md),
  [0014-benchmarks-and-gates](./0014-benchmarks-and-gates.md)
- [OpenCV Zoo — YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet)
