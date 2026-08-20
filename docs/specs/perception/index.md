# Perception

## Overview

Perception is the first [groundstation](../groundstation/) capability: it turns
a camera frame into face and gesture detections that the
[robot app](../ha-satellite/) uses to aim the head and react to hand signals.

It is specified separately from the groundstation because it is one capability
among several to come, and because it carries two concerns the service itself
does not — model licensing and detection accuracy.

Nothing is implemented yet.

## Background

The predecessor ran a face model and a hand-gesture pair, and it worked: a face
pass took 38 ms off the robot against 1199 ms on it, and the robot's own CPU
load fell from saturation to 1.52 of four cores.

Two problems came with it. The face model derived from an AGPL-3.0 codebase,
which a public repository and a published container image cannot carry without
consequences. And the gesture classifier reported hand signals at 0.9 confidence
in an empty room — a model problem that thresholds do not fix and that the
predecessor never solved.

The licence problem is settled below. The accuracy problem is specified here as
a measurable gate rather than declared solved.

## Requirements

### REQ-032: Detection models are permissively licensed

Every model shipped in the published artifact MUST be redistributable under a
licence that places no obligation on the licensing of the code that runs it.

#### Scenario: A model is proposed for inclusion

- **GIVEN** a candidate model whose weights derive from a copyleft codebase
- **WHEN** its inclusion is reviewed
- **THEN** it is rejected, regardless of its accuracy or speed

### REQ-033: Model licence and provenance are recorded beside the model

Each model MUST have a record naming its upstream source, its licence, and the
retrieval location, stored alongside the pinned hash required by
[groundstation REQ-024](../groundstation/index.md#req-024-model-provenance-is-recorded-and-verified).

#### Scenario: A licence audit is performed

- **GIVEN** a released image containing two models
- **WHEN** an auditor asks what each model is and under what terms it ships
- **THEN** both are answerable from the repository without network access or
  archaeology

### REQ-034: Face detections report a normalised centre and a confidence

Each face detection MUST report the face's centre in normalised image
coordinates together with a confidence value.

#### Scenario: A face is detected off-centre

- **GIVEN** a frame with one face in the upper-left quadrant
- **WHEN** the frame is processed
- **THEN** the reported centre has a negative horizontal and a positive vertical
  component, and a confidence accompanies it

### REQ-035: Detection output is independent of input resolution

The same scene captured at different resolutions MUST produce detections whose
reported positions agree within a stated tolerance.

#### Scenario: Capture resolution is halved

- **GIVEN** a fixture image and the same image at half its dimensions
- **WHEN** both are processed
- **THEN** the reported centres agree within the stated tolerance

### REQ-036: Post-processing is verified against a reference implementation

The hand-written pre- and post-processing MUST be verified against an
independent reference implementation on a fixture set, asserting agreement on
detection count, position, and confidence within stated tolerances.

#### Scenario: A decoding change is introduced

- **GIVEN** a change to bounding-box decoding or overlap suppression
- **WHEN** the parity test runs
- **THEN** it fails if detection counts differ or positions move beyond
  tolerance, catching the error before it reaches a robot

### REQ-037: Gesture accuracy is measured against a negatives fixture set

The gesture capability MUST be evaluated against a fixture set containing scenes
with no hands present, and its false-positive rate on that set MUST be reported
by the test suite.

#### Scenario: A gesture model is evaluated

- **GIVEN** a candidate gesture model and a fixture set of empty scenes
- **WHEN** the evaluation runs
- **THEN** the proportion of empty scenes yielding a gesture above threshold is
  reported as a number

### REQ-038: A capability can be disabled without disabling the session

Each detector MUST be independently switchable at run time, and disabling one
MUST leave the others operating.

#### Scenario: Gestures are switched off

- **GIVEN** a running session with face and gesture detection both active
- **WHEN** gesture detection is disabled
- **THEN** face results continue uninterrupted and results carrying no gesture
  are delivered normally

### REQ-039: Detection thresholds are configuration

The confidence threshold for each detector MUST be settable without rebuilding
the artifact.

#### Scenario: An operator tunes sensitivity

- **GIVEN** a deployment reporting too many low-confidence faces
- **WHEN** the operator raises the face threshold and restarts the service
- **THEN** the new threshold takes effect, and the value in effect is visible
  through the configuration endpoint

## Design

### Face detection

Face detection uses YuNet, a compact detector distributed under the MIT licence
through the OpenCV Zoo. It reports a bounding box and five facial landmarks; the
box centre is what the robot's motion layer consumes.

Two properties matter beyond the licence. The model accepts a dynamic input
shape, so frames are fed at their own dimensions rounded to the model's stride
rather than letterboxed onto a fixed canvas — the letterbox step, and the class
of coordinate bugs that come with reversing it, disappears. And the same model
ships inside the Reachy Mini SDK, which means the on-robot fallback path and the
groundstation path run identical weights.

That second property is worth more than it first appears. It gives the parity
test in REQ-036 a reference implementation that is already maintained and
already runs on the target hardware, and it removes the accuracy discontinuity
that would otherwise make falling back to local detection a visible change in
behaviour.

### Gesture recognition

Gesture recognition is a two-stage arrangement: a hand detector followed by a
classifier over the detected crop. Its cost was 5 ms against a 39 ms face pass,
so it is not a latency concern.

Its accuracy is a concern. The predecessor's classifier reported `fist` and
`mute` at 0.9 confidence in an empty room, which means the failure is not a
threshold that needs raising — a confident wrong answer survives any threshold
that keeps the true positives. REQ-037 turns this into a measured quantity so a
replacement model can be compared rather than argued about, and the model choice
itself remains open below.

The predecessor sampled gestures on every fourth frame to bound their cost. That
rate is configuration here rather than a constant, because the right value
depends on the classifier eventually chosen.

### Coordinates

Positions are normalised as defined in
[robot link REQ-021](../robot-link/index.md#req-021-detection-geometry-is-resolution-independent).
Perception produces them; the link contract owns their meaning.

### Decision Records

#### YuNet rather than a YOLO-derived face model

The predecessor used `yolo11n-face`, which is Ultralytics-derived. Ultralytics
licenses its work under AGPL-3.0 and states that the licence covers both the
training code and the models produced by it, requiring anyone deploying over a
network to publish the complete corresponding source of the entire derivative
work. Applied to a public repository and a published image, that makes the
service AGPL-3.0 rather than permissively licensed, and propagates the same
obligation to everyone who deploys the image.

The chain of title is also unclear. The weights in use trace to a redistributor
whose repository carries a GPL-3.0 licence file predating Ultralytics' move to
AGPL, while pointing at an upstream that is now AGPL — so the terms actually
granted are ambiguous.

Attribution does not resolve any of this. AGPL is copyleft: carrying the licence
text is necessary and not sufficient, and the obligation attaches to a published
artifact rather than to source that could quietly be dropped later.

YuNet removes the question. It is MIT-licensed, roughly 340 KB, scores 0.834,
0.824 and 0.708 average precision on the easy, medium and hard WIDER Face
splits, and is already the detector the Reachy Mini SDK itself uses. Rejected
alternatives: keeping the YOLO-derived model and accepting AGPL for the service,
or purchasing a commercial licence — both cost more than a model swap that
[benchmarks](../benchmarks/) will confirm is close to free.

#### Gesture model selection is deferred, not assumed

The existing classifier's false-positive behaviour is a known defect, and
carrying it forward silently because it is what exists would repeat the
predecessor's mistake. Its weights also need the same provenance and licence
check REQ-032 and REQ-033 apply to everything else, which has not been done.
REQ-037 makes the defect measurable so the replacement decision has evidence
behind it.

## Constraints

- Inference runs on CPU by default, on whatever host the groundstation occupies.
- The face pass has to stay far enough below the frame interval that the
  pipeline does not shed frames in normal operation; the measured predecessor
  budget was 39 ms of a 100 ms interval.
- Every model ships inside a public image, so redistribution rights are a
  precondition rather than a preference.

## Open Questions

- **Which gesture model replaces the current classifier.** Candidates need
  evaluating against the negatives fixture set required by REQ-037, and against
  the same licence bar as the face model. Current default: none chosen; the
  capability is specified so it can be disabled independently until one is.
- **Whether facial landmarks are exposed beyond the centre.** YuNet reports five
  of them, and gaze or head-pose behaviour would want them. Nothing consumes
  them today. Current default: report the centre and confidence; extend the
  message type when something needs more.
- **Whether the on-robot fallback path is enabled by default.** Sharing one
  model across both paths removes the accuracy argument against falling back,
  but the robot pays real CPU for it. Current default: remote only, with local
  available by configuration — see [ha-satellite](../ha-satellite/).

## References

- [groundstation](../groundstation/) — the service hosting this capability
- [robot-link](../robot-link/) — coordinate and result contracts
- [benchmarks](../benchmarks/) — the latency baseline and the model comparison
- [OpenCV Zoo — YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet)
- [WIDER Face](http://shuoyang1213.me/WIDERFACE/) — the benchmark the reported precision figures come from

## Changelog

| Date | Change | Document |
|------|--------|----------|
| 2026-08-20 | Initial spec created | — |
