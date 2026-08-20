# 0006: Groundstation container images

## Summary

Package the groundstation as a multi-architecture container image published to
the GitHub Container Registry, with a CUDA variant and a compose file that makes
it runnable without an orchestrator.

**Spec:** [Groundstation](../specs/groundstation/)
**Status:** draft
**Depends On:** 0002, 0005

## Motivation

The service is only useful once it can be deployed somewhere, and the image is
where several of the spec's guarantees actually become true: models are present
rather than fetched, size is bounded, and both architectures are covered.

The predecessor's image was 483 MB against roughly 2 GB for the alternative
packaging, which is the difference between an image that can be pulled onto a
modest host and one that cannot. Keeping that property is a packaging decision,
not an application one.

## Requirements

### Testing Requirements

This change MUST satisfy the project's standing testing rules (see
[Testing conventions](../specs/architecture/index.md#testing-conventions)). CI
enforces these as merge gates:

- Tests run with `pytest`, with async strict mode enabled.
- Coverage MUST be gated on the diff rather than on the whole tree.
- Type checking MUST run in strict mode for new modules.
- A lint or type suppression MUST carry the rule identifier and a justification.

Skipping or weakening any of these rules to land the PR MUST be treated as a bug
in the PR, not in the rule.

Packaging is verified by running the built artifact, not by inspecting the build
file: CI MUST start the built image, wait for it to report ready, and drive a
session through it. A Dockerfile that builds successfully and produces a service
that cannot start is a passing build and a broken release.

### Functional requirements

The [groundstation spec](../specs/groundstation/) owns what the deployed service
guarantees, particularly
[REQ-023](../specs/groundstation/index.md#req-023-model-files-are-present-in-the-image)
on model presence and
[REQ-031](../specs/groundstation/index.md#req-031-images-are-published-for-both-robot-adjacent-architectures)
on architectures. Those scenarios are this change's acceptance criteria. What
implementing them requires of this change:

- Models are fetched and hash-verified during the build and baked into the
  image. A build with no network access to the model source fails at build time,
  which is the correct place for it to fail.
- The image runs as a non-root user and contains no build toolchain.
- Two tags per release from one source: a default CPU variant and a CUDA
  variant. Both carry the repository-wide version from 0002.
- The compose file runs the service standalone and includes a metrics scrape
  configuration, because the predecessor exposed metrics that nothing collected.
- Image size is recorded as a build output so 0014 can gate on its growth.
- The published image is verified by starting it in CI and running a real
  session against it.

#### Scenario: The image is started with no network access to the model source

- **GIVEN** a built image on a host that cannot reach the model source
- **WHEN** the container starts
- **THEN** the service reaches readiness, because the models are already in the
  image

## Design

### Approach

This change depends on 0005 rather than only on the service in 0004, because
there is nothing to bake in until the model registry exists and nothing to drive
a session through until a capability does. It depends on 0002 for the
repository-wide version the published tags carry.

A multi-stage build: dependencies and models resolved in a builder stage, then
copied into a slim runtime stage containing no compiler and no package manager.
Both architecture variants come from the same definition through cross-platform
build support.

The CUDA variant differs only in its base image and its runtime provider
selection, so it is a build argument rather than a second Dockerfile.

### Decisions

- **Decision**: Models are baked in, not mounted or fetched.
  - **Why**: [REQ-023](../specs/groundstation/index.md#req-023-model-files-are-present-in-the-image)
    requires the service to start without internet access, and a mounted model
    directory makes every deployment responsible for populating it correctly.
  - **Alternatives considered**: A model volume, which is smaller per image and
    turns a start-up failure into a deployment-time surprise.
- **Decision**: CPU is the default tag; CUDA is explicit.
  - **Why**: The measurements support it — the GPU available during the original
    work had 2.0 GB free and CPU inference won outright. A default that requires
    hardware most hosts lack is a bad default.
  - **Alternatives considered**: A single image detecting available hardware at
    run time, which drags CUDA runtime weight into every deployment.
- **Decision**: CI starts the image and drives a session through it.
  - **Why**: Every interesting packaging failure — a missing model, a wrong
    user, an absent shared library — is invisible to a build that only checks
    the build succeeded.

### Non-Goals

- No image signing or bill of materials; both are open questions in the
  [architecture spec](../specs/architecture/index.md#open-questions) and neither
  has a consumer yet.
- No Kubernetes manifests or Helm chart. Compose covers standalone deployment,
  and anything cluster-shaped would need environment-specific values this
  repository cannot hold.
- No CUDA performance tuning.

## Tasks

- [ ] Write the image build
  - [ ] Multi-stage Dockerfile with a toolchain-free runtime stage
  - [ ] Build-time model fetch with hash verification
  - [ ] Non-root runtime user
  - [ ] CUDA variant as a build argument over the same definition
- [ ] Publish from CI
  - [ ] Multi-architecture build for 64-bit ARM and x86
  - [ ] Push both variants to the registry on a version tag
  - [ ] Record image size as a build output
- [ ] Verify the artifact
  - [ ] Start the built image in CI, wait for readiness
  - [ ] Drive a real session through the running container
  - [ ] Assert the service starts with the model source unreachable
- [ ] Ship the standalone deployment
  - [ ] Compose file running the service with sane defaults
  - [ ] Metrics scrape configuration alongside it
  - [ ] `.env.example` documenting every setting the compose file reads

## Open Questions

- [ ] Whether the CUDA variant is built on every release or only on demand. It
      roughly doubles build time and nothing currently deploys it. Current lean:
      every release, so it cannot rot unnoticed.
- [ ] Whether ARM images are built natively or by emulation. Emulation is
      simpler and slow enough to be irritating for a model-heavy build. Current
      lean: emulation until it becomes painful.

## References

- Spec: [Groundstation](../specs/groundstation/)
- Related changes: [0004-groundstation-session](./0004-groundstation-session.md),
  [0005-perception-capability](./0005-perception-capability.md),
  [0014-benchmarks-and-gates](./0014-benchmarks-and-gates.md)
