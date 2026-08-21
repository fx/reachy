# The groundstation image: one definition, two variants, two architectures.
#
# Build it from the REPOSITORY ROOT, because the workspace is one resolution and
# `uv.lock` sits there:
#
#     just image                      # the default CPU variant
#     just image cuda                 # the accelerated variant
#
# ------------------------------------------------------------------------------
# What this build guarantees, and how
# ------------------------------------------------------------------------------
#
# **Models are in the image, not fetched at run time.** The builder stage runs
# the same model fetch `just models` runs, into `/opt/reachy/models`, and the
# runtime stage copies the result. Groundstation REQ-023 requires the service to
# start on a host with no outbound internet access; groundstation REQ-024
# requires the build to fail when a fetched file's hash is not the pinned one,
# and `reachy_groundstation.models.fetch` is what fails it. A build with no
# network access to the model source therefore fails HERE, at build time, which
# is the correct place for it to fail — a mounted model directory would move
# that failure to whichever deployment forgot to populate it.
#
# **The runtime stage has no compiler, no package manager and no shell.** It is
# not a slimmed Debian with the tools deleted afterwards: it is a distroless
# base that never had them. Nothing is installed here — the venv, the
# interpreter and the models arrive as three `COPY --from` instructions, and the
# only executable in the image that this repository put there is a Python
# interpreter. `just image-verify` asserts that from inside the running
# container, so the property is checked rather than asserted in a comment.
#
# **Python comes from the builder, not from the base.** uv installs a
# self-contained CPython into `/opt/reachy/python` and the venv points at it, so
# both stages copy to the same absolute path and the runtime base only has to
# provide a C library. That is what lets the CUDA variant be a base-image
# argument rather than a second Dockerfile: NVIDIA's runtime image ships no
# Python, and installing one into it would put a package manager back.
#
# **Both architectures come from this one definition.** Nothing below names an
# architecture; `docker buildx build --platform linux/amd64,linux/arm64` is the
# whole of it. See `docs/changes/0006-groundstation-images.md` for why they are
# built by emulation.

# ------------------------------------------------------------------------------
# Pinned inputs
# ------------------------------------------------------------------------------
# Every base is pinned by digest, for the same reason every GitHub action is: a
# tag is a mutable ref, so a rebuilt base changes what ships with no diff in this
# repository to review. Each digest is the multi-architecture index's, so one
# pin covers both platforms.

# uv, as the distroless image that holds only the binary.
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1

# Where the dependencies are resolved and the models are fetched. Discarded.
ARG BUILDER_BASE=debian:bookworm-slim@sha256:abd67ffcfa541b485a3dff59865ab629aa048a6c613e639d36e7456b0b229241

# What ships. `cc-debian12` is glibc, libgcc and libstdc++ and nothing else: no
# shell, no apt, no dpkg, no compiler. The CUDA variant overrides this with
# NVIDIA's runtime image — see `just image`, which sets it — and that image is
# an Ubuntu with a package manager, because NVIDIA publishes no smaller one and
# building CUDA from parts is not this change's job.
ARG RUNTIME_BASE=gcr.io/distroless/cc-debian12:latest@sha256:6e1871c34683dc9ee996d13084497783fd98ac0200213d0826625f4e9d4be1d0

# The interpreter the image runs. Kept in step with `mise.toml` by a test —
# `test_groundstation_deployment.py` reads both — so a toolchain bump does not
# leave the image on the version of whenever this file was last edited.
ARG PYTHON_VERSION=3.12.14

# Which model runtime is installed, as the extra that declares it. Empty for the
# CPU variant, which takes the `onnxruntime` the service depends on outright.
ARG RUNTIME_EXTRA=

# Which providers the service tries, best first. `resolve_providers` drops
# anything this build has not got and always ends at the CPU provider, so this
# is a preference rather than a requirement.
ARG INFERENCE_PROVIDERS=CPUExecutionProvider

FROM ${UV_IMAGE} AS uv

# ------------------------------------------------------------------------------
# Builder: resolve the dependencies, fetch the models, verify them.
# ------------------------------------------------------------------------------
FROM ${BUILDER_BASE} AS builder

ARG PYTHON_VERSION
ARG RUNTIME_EXTRA

COPY --from=uv /uv /usr/local/bin/uv

# The one thing installed anywhere in this build, and it is installed in the
# stage that is thrown away. The model source is HTTPS and
# `reachy_groundstation.models.fetch` refuses any other scheme, so without a
# trust store this stage cannot fetch a model at all — and the slim base ships
# none. It is deliberately not `ca-certificates` plus a package manager left
# behind: this layer never reaches the runtime stage.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# `only-managed` is what makes the interpreter reproducible rather than whatever
# the base happens to carry: uv fetches the exact patch release from the same
# standalone builds `mise install` uses, into a directory both stages agree on.
#
# `UV_LINK_MODE=copy` because the cache and the environment are on different
# layers, where uv's default hardlinking cannot work and warns about it on every
# install. `UV_COMPILE_BYTECODE=1` pays for the `.pyc` files once, here, rather
# than on the first request of every container start.
ENV UV_PYTHON_INSTALL_DIR=/opt/reachy/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_PROJECT_ENVIRONMENT=/opt/reachy/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /src

# The manifests first, then the sources. A source edit then re-runs the second
# sync — which installs two path dependencies — rather than re-resolving and
# re-downloading the model runtime, which is most of the image.
#
# The list mirrors `[tool.uv.workspace] members` in the root `pyproject.toml`: uv
# reads every member's manifest to resolve against the lockfile, so a member
# missing here is a build that fails. `test_groundstation_deployment.py` checks
# the two lists agree, so a member added later is a red test rather than a red
# build somebody has to diagnose.
COPY pyproject.toml uv.lock ./
COPY packages/reachy-contracts/pyproject.toml packages/reachy-contracts/
COPY apps/ha-satellite/pyproject.toml apps/ha-satellite/
COPY services/groundstation/pyproject.toml services/groundstation/
COPY cli/reachyctl/pyproject.toml cli/reachyctl/
COPY bench/pyproject.toml bench/
COPY tools/repo-hygiene/pyproject.toml tools/repo-hygiene/

RUN uv python install "${PYTHON_VERSION}"

# `--locked` installs exactly what `uv.lock` describes and fails when the
# lockfile no longer matches the manifests, so an image can never be built from
# a resolution nobody committed. `--no-default-groups` drops `dev` and `parity`:
# the test tools and the parity reference are not deployed.
#
# `--no-install-package onnxruntime` is what the CUDA variant is made of. Both
# runtimes install the module `onnxruntime`, so installing both would leave one
# overwriting the other's files; the extra brings `onnxruntime-gpu` in and this
# leaves the CPU wheel out of that one build. Empty for the CPU variant, where
# `RUNTIME_EXTRA` is empty and this whole expansion is nothing.
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv sync --locked --no-default-groups --no-install-workspace \
        --package reachy-groundstation \
        ${RUNTIME_EXTRA:+--extra $RUNTIME_EXTRA --no-install-package onnxruntime}

COPY packages/reachy-contracts/ packages/reachy-contracts/
COPY services/groundstation/ services/groundstation/

# `--no-editable` is what makes the environment self-contained. uv installs a
# workspace member in editable mode by default, which leaves a path file in
# site-packages pointing at the source tree — and the source tree is in the
# builder stage, so the runtime stage would start and report no such module.
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv sync --locked --no-default-groups --no-editable \
        --package reachy-groundstation \
        ${RUNTIME_EXTRA:+--extra $RUNTIME_EXTRA --no-install-package onnxruntime}

#:= docs/specs/groundstation/index.md#req-023-model-files-are-present-in-the-image
#:% The service MUST load every model from a file already present in its deployed
#:% artifact, and MUST NOT fetch model weights over the network at run time.
#
# The same module `just models` runs, against the same registry, with the same
# refusal: a file whose digest is not the pinned one is deleted and this layer
# fails. It is invoked directly rather than through `just` because `just models`
# runs it under `uv run --locked --all-packages`, which would install the test
# and parity groups into an image that ships neither.
# `test_groundstation_deployment.py` checks that both name the same entry point.
RUN /opt/reachy/venv/bin/python -m reachy_groundstation.models.fetch \
    /opt/reachy/models

# ------------------------------------------------------------------------------
# Runtime: three copies, one user, one entry point.
# ------------------------------------------------------------------------------
FROM ${RUNTIME_BASE} AS runtime

ARG INFERENCE_PROVIDERS

# Two labels, and two that are deliberately absent. `image.source` is added by
# the workflow that pushes, from the repository it is running in, so this file
# names no account; `image.licenses` is left off because this repository states
# no licence, and a label asserting one would be an assertion nothing backs.
LABEL org.opencontainers.image.title="reachy-groundstation" \
      org.opencontainers.image.description="Off-robot capability host for the Reachy Mini"

# The interpreter, the environment, the weights. The first two are copied to the
# paths the builder created them at, because the venv records the interpreter's
# location in `pyvenv.cfg` by absolute path.
#
# The numeric owner is distroless' `nonroot` user. It is written as a number
# because there is no `useradd` here to consult and no shell to run it.
# The two shared libraries the wheels need and this base has not got. Every
# other `NEEDED` entry across the interpreter and the installed extension
# modules — libc, libm, libdl, libpthread, librt, libutil, libgcc_s, libstdc++,
# libgomp — is already here, which is what makes `cc-debian12` the right base
# rather than a full Debian.
#
# They are copied into `/usr/lib`, one of the loader's default trusted
# directories, rather than put on `LD_LIBRARY_PATH`, so a base image that
# carries its own copy in its architecture directory keeps using that one. The
# glob catches the versioned file the `.so.1` name is a symlink to; nothing here
# names an architecture, so the same two lines hold for ARM.
#
# If a dependency ever needs a third, the container fails to import it and
# `just image-verify` goes red — the service loads every one of these before it
# reports ready.
COPY --from=builder /lib/*-linux-gnu/libz.so.1* /lib/*-linux-gnu/libcrypt.so.1* /usr/lib/

COPY --from=builder --chown=65532:65532 /opt/reachy/python /opt/reachy/python
COPY --from=builder --chown=65532:65532 /opt/reachy/venv /opt/reachy/venv
COPY --from=builder --chown=65532:65532 /opt/reachy/models /opt/reachy/models

# Binding the loopback interface is the right default for a process started by
# hand and the wrong one for a container, where nothing outside the network
# namespace could then reach it. That is a deployment decision, so it is made
# here rather than in `config.py`.
#
# `PYTHONUNBUFFERED` because the logs are the operator's only view of a container
# that has no shell to attach to, and a buffered stream loses the last of them
# when the process is killed.
# `ORT_DISABLE_TELEMETRY` is a policy decision that also happens to be load
# bearing. The policy: this service is specified to run on a host with no
# outbound internet access, so a model runtime that reports usage anywhere is
# reporting it into a firewall at best. The load-bearing part: ONNX Runtime's
# telemetry reads `/etc/machine-id` for a device identifier and segfaults during
# `import onnxruntime` when there is none — and a distroless base has none.
# Writing a constant machine identifier into a public image instead would give
# every deployment in the world the same one, which is worse than having none.
ENV PATH=/opt/reachy/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    ORT_DISABLE_TELEMETRY=1 \
    REACHY_GROUNDSTATION_HOST=0.0.0.0 \
    REACHY_GROUNDSTATION_PORT=8080 \
    REACHY_GROUNDSTATION_MODELS_DIR=/opt/reachy/models \
    REACHY_GROUNDSTATION_INFERENCE_PROVIDERS=${INFERENCE_PROVIDERS}

USER 65532:65532
EXPOSE 8080

# Readiness, not liveness: groundstation REQ-026 makes them different questions,
# and an orchestrator that starts sending work before warm-up finished is the
# failure that distinction exists to prevent. There is no shell and no curl, so
# the check runs in the interpreter that is already here.
HEALTHCHECK --interval=10s --timeout=5s --start-period=120s --retries=3 \
    CMD ["/opt/reachy/venv/bin/python", "-c", "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('REACHY_GROUNDSTATION_PORT','8080')+'/readyz',timeout=4)"]

# The module, not a console script: `reachy_groundstation.service.main` is the
# whole of startup and `__main__` is three lines that call it, so this is the
# same entry point the tests drive.
ENTRYPOINT ["/opt/reachy/venv/bin/python", "-m", "reachy_groundstation"]
