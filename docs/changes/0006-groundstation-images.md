# 0006: Groundstation container images

## Summary

Package the groundstation as a multi-architecture container image published to
the GitHub Container Registry, with a CUDA variant and a compose file that makes
it runnable without an orchestrator.

**Spec:** [Groundstation](../specs/groundstation/)
**Status:** complete
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

- [x] Write the image build
  - [x] Multi-stage Dockerfile with a toolchain-free runtime stage
  - [x] Build-time model fetch with hash verification
  - [x] Non-root runtime user
  - [x] CUDA variant as a build argument over the same definition
- [x] Publish from CI
  - [x] Multi-architecture build for 64-bit ARM and x86
  - [x] Push both variants to the registry on a version tag
  - [x] Record image size as a build output
- [x] Verify the artifact
  - [x] Start the built image in CI, wait for readiness
  - [x] Drive a real session through the running container
  - [x] Assert the service starts with the model source unreachable
- [x] Ship the standalone deployment
  - [x] Compose file running the service with sane defaults
  - [x] Metrics scrape configuration alongside it
  - [x] `.env.example` documenting every setting the compose file reads

## Open Questions

- [x] Whether the CUDA variant is built on every release or only on demand. It
      roughly doubles build time and nothing currently deploys it. Current lean:
      every release, so it cannot rot unnoticed.
      **Resolved: every release, and every pull request as well.** The lean was
      right and understated. Building it only on release would have shipped a
      CUDA image that is not accelerated: the model runtime wheel in the
      lockfile links `libcublasLt.so.13` and needs a CUDA 13 base, and the
      obvious 12.6 base produced an image that builds, starts, serves and
      silently falls back to the CPU provider. That was caught by running the
      artifact, so the variant is verified on every pull request too, and the
      check is specifically that the provider library loads rather than that the
      container starts.
- [x] Whether ARM images are built natively or by emulation. Emulation is
      simpler and slow enough to be irritating for a model-heavy build. Current
      lean: emulation until it becomes painful.
      **Resolved: emulation.** It is one action against a second runner pool, a
      second cache and a manifest assembled by hand, and the emulated work is
      dependency installation rather than compilation — every dependency
      resolves to an `aarch64` wheel, so nothing is built from source under
      QEMU. Revisit when the ARM leg becomes the slowest merge gate; the change
      is confined to the two workflow jobs that name `--platform`.

## Completion notes

**The image.** `services/groundstation/Dockerfile`, built from the repository
root by `just image [variant] [tag] [buildx arguments…]`. Dependencies and the
models are resolved in a builder stage, and neither variant's runtime stage
installs anything: the interpreter, the environment and the weights arrive as
`COPY --from` instructions and both run as uid 65532.

The **default** variant's runtime base is `gcr.io/distroless/cc-debian12`, which
has no shell, no package manager and no compiler — and `just image-verify`
asserts that from inside the running container rather than taking the base
image's word for it. The **accelerated** variant's base is NVIDIA's CUDA runtime
image, which is an Ubuntu and therefore does carry a shell and a package
manager; NVIDIA publishes no smaller one, and assembling a CUDA runtime from
parts is not this change's job. That is the one property the two variants do not
share, and it is why the toolchain assertion is made for the default variant
only.

Python is a self-contained interpreter uv installs into the builder and both
stages copy, which is what makes the runtime base a free variable at all —
NVIDIA's image ships no Python, and installing one into it would have put a
package manager into the default variant too.

**Measured sizes**, `linux/amd64`, uncompressed, as `just image-size` reports
them:

| Variant | Bytes | |
|---|---:|---|
| default (CPU) | 458,501,043 | 437.3 MiB |
| accelerated (CUDA) | 3,659,895,758 | 3,490.3 MiB |

The default variant is under the predecessor's 483 MB, which was the packaging
property worth keeping. The accelerated variant is what a CUDA 13 runtime plus
cuDNN plus a 202 MB `onnxruntime-gpu` wheel costs; nothing about it is
compressible by choosing differently, which is the reason CPU is the default
tag.

**How the size is recorded**, since change 0014 gates on its growth:
`just image-size <tag> <variant>` prints one line of JSON —
`{"image": …, "variant": …, "platform": …, "size_bytes": …, "size_mib": …}` —
read from `docker image inspect .Size`, the uncompressed on-disk size a host
needs room for. The `Images` workflow writes it to the job summary and uploads
it as the artifact `groundstation-image-size-<variant>`, one JSON file named
`<variant>.json`.

**What the CUDA variant actually is:** the same Dockerfile with three build
arguments — `RUNTIME_BASE`, `RUNTIME_EXTRA=cuda` and `INFERENCE_PROVIDERS`. The
extra is `onnxruntime-gpu`, declared as an optional dependency of
`reachy-groundstation`; the build asks for it and leaves the CPU wheel out with
`--no-install-package onnxruntime`, because both distributions install the same
module.

**Verification** is `just image-verify <tag> [isolated|bridge] [variant]`. It
starts the image on an `--internal` Docker network with no route off the host,
proves the isolation by resolving every model source out of the registry inside
the container and failing if any answers, asserts the runtime stage carries no
toolchain, asserts it does not run as root, and then drives a real session from
a sibling container: negotiate, send a committed one-face fixture as a frame,
and require a face back. The service answered with one face at 0.909 confidence
on both variants.

**What has not been exercised on hardware.** No GPU was available, so the
accelerated variant is verified as far as "its CUDA provider library's
dependencies all resolve, apart from the driver the container runtime injects".
Whether inference on it is faster than the CPU path is change 0014's question,
and the measurements that made CPU the default say it may well not be.

## References

- Spec: [Groundstation](../specs/groundstation/)
- Related changes: [0004-groundstation-session](./0004-groundstation-session.md),
  [0005-perception-capability](./0005-perception-capability.md),
  [0014-benchmarks-and-gates](./0014-benchmarks-and-gates.md)
