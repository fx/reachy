# 0005: Perception capability

## Summary

Implement the perception capability: the model runtime, the pinned model store,
YuNet face detection with its parity test, and gesture recognition with the
negatives evaluation that makes its accuracy a measured quantity.

**Spec:** [Perception](../specs/perception/)
**Status:** draft
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

#### Scenario: The pinned face model is swapped for a differently licensed one

- **GIVEN** a change that repoints the face model registry entry at a model
  whose recorded licence is copyleft
- **WHEN** the licence check runs
- **THEN** the check fails, because the recorded licence is not on the permitted
  list

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

- [ ] Implement the model runtime
  - [ ] Runtime session management with provider selection and thread limits
  - [ ] Warm-up, wired to the readiness signal from 0004
  - [ ] Thread-count configuration with its documented default
- [ ] Implement the model store
  - [ ] Registry file recording source, licence, location and hash per model
  - [ ] Build-time fetch with hash verification and failure on mismatch
  - [ ] Licence-allowlist test over the registry
- [ ] Implement face detection
  - [ ] YuNet inference using dynamic input shape, no letterbox
  - [ ] Output decoding and overlap suppression
  - [ ] Parity test against the SDK decoder with stated tolerances
  - [ ] Resolution-independence test across two scales of one fixture
- [ ] Implement gesture recognition
  - [ ] Hand detection and crop classification
  - [ ] Configurable sampling rate
  - [ ] Negatives fixture set and false-positive rate reporting
- [ ] Wire run-time switching
  - [ ] Per-detector enable and disable
  - [ ] Configurable thresholds, visible through the configuration endpoint
  - [ ] Test that disabling every detector yields empty results, not errors

## Open Questions

- [ ] Which gesture model replaces the current classifier. Blocked on the
      negatives evaluation this change produces.
- [ ] Whether the gesture capability ships enabled by default before a
      replacement model is chosen. Current lean: disabled by default, with the
      false-positive number in its documentation.
- [ ] What the parity tolerances should be. The predecessor's hand comparison
      found centres within 2.2 px and confidence within 0.011 against a
      different model pair. Current lean: start there and tighten once the real
      distribution is known.

## References

- Spec: [Perception](../specs/perception/)
- Related changes: [0004-groundstation-session](./0004-groundstation-session.md),
  [0006-groundstation-images](./0006-groundstation-images.md),
  [0014-benchmarks-and-gates](./0014-benchmarks-and-gates.md)
- [OpenCV Zoo — YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet)
