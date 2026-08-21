# The task surface for this repository.
#
# Every command a contributor runs is a recipe here, and continuous integration
# calls these recipes rather than restating the commands, so the two cannot
# drift apart. If a command is worth running twice, it belongs in this file.
#
# `--locked` installs exactly what `uv.lock` describes and fails when the
# lockfile no longer matches the manifests, so a dependency added without
# relocking is a red run instead of a silent difference between one machine and
# the next. It is deliberately not `--frozen`: that flag skips the freshness
# check entirely and runs happily against a stale resolution, which is the
# failure this recipe file exists to make impossible. Neither flag ever
# re-resolves. `--all-packages` puts every workspace member in the environment,
# which is what makes one lint, one type-check and one test run cover the whole
# tree.

set shell := ["bash", "-euo", "pipefail", "-c"]

#:= docs/specs/architecture/index.md#req-001-single-resolved-dependency-set
#:% The repository MUST resolve all workspace members against one committed lockfile,
#:% and continuous integration MUST install from that lockfile without re-resolving.
uv := "uv run --locked --all-packages"

# List the available recipes.
default:
    @just --list

# Install the workspace exactly as the lockfile describes it.
sync:
    uv sync --locked --all-packages

# Run the full test suite with coverage measurement.
test:
    {{ uv }} pytest

# Check formatting and lint rules. Fails without modifying anything.
lint: lint-boundary lint-behaviour-boundary lint-capability-boundary
    {{ uv }} ruff check .
    {{ uv }} ruff format --check .

# Prove the satellite behaviour layer's purity boundary still fires.
#
# ha-satellite REQ-042 says the logic that maps voice-pipeline events and
# detections to motion intents performs no input or output. What makes that true
# rather than merely intended is that `behaviour/` cannot reach anything that
# does any: not an adapter, not the vendored protocol layer, not the composition
# root, not the settings interface, not the daemon entry point — which is the one
# module that imports the SDK, so reaching it would import the SDK by another
# name — and not the SDK itself. Time is a parameter to every method that needs
# it, so the layer never reads a clock and never sleeps either.
#
# The mechanism is ruff's `flake8-tidy-imports` `banned-api`, configured on the
# invocation rather than in `pyproject.toml` — exactly as the groundstation's
# capability boundary is, and for the same reason. `banned-api` is a single
# global list and `pyproject.toml` spends it on the vendored ESPHome boundary,
# whose negated `per-file-ignores` entry switches TID251 off everywhere outside
# that directory; an entry added there would therefore be dead in the one package
# it needs to guard. TID253 is spent on the pydantic ban. So the ban lives here,
# with its scope, and this recipe is what runs it.
#
# Four checks, and the first two are what make it real. The failing case is a
# committed fixture full of the imports and the clock reads the boundary forbids,
# fed to ruff on standard input under a pretended path — `--stdin-filename` only
# tells ruff which per-file rules apply, so no probe file is ever left in the
# tree. The same fixture is then run under a path inside `adapters/`, where all
# of it is ordinary, which proves the ban is scoped to `behaviour/` rather than
# global. Only then is the real tree checked.
#
# The last two are the backstops TID251 cannot be. It inspects import statements
# only, so a dynamic import slips past it, and it says nothing whatever about a
# clock. Two greps close both, and the fixture carries a dynamic import and a
# clock read so each half is proved to fire.
#
# TID251 does more of the first half than it looks. It resolves a *relative*
# import against the file's own path, so `from ..adapters import daemon` inside
# `behaviour/` trips it exactly as the absolute spelling does; that shape needs
# no grep and the fixture does not carry one.
#
# What the grep half owns is every import the name of which is a string rather
# than a token, and there the pattern is written for the class rather than for
# a spelling — the same decision the clock grep makes below:
#
#   * The dynamic branch matches the SDK's **bare** module name as well as its
#     submodules. `import_module("reachy_mini")` is the sharpest form of the
#     hole, not a corner of it: importing the SDK's top level alone executes its
#     `__init__`, which transitively imports `reachy_mini.vision.face_tracking`,
#     which does `import gi` — so the one spelling a `reachy_mini\.` prefix
#     missed is the spelling that drags in the whole GStreamer stack. The quote
#     character is part of the match, which is also what keeps
#     `reachy_mini_ha_satellite.behaviour` — this layer importing itself, which
#     is fine — from colliding with it.
#   * `importlib` is banned outright, not just its `import_module`. A layer that
#     is handed everything it needs has no use for the module at all, so banning
#     it costs nothing and closes `importlib.util.spec_from_file_location`,
#     `importlib.machinery` and whatever else it grows next. The
#     `spec_from_file_location` name is matched on its own as well, because it
#     is importable directly from `importlib.util` and this repository loads a
#     module by path elsewhere on purpose — so the name is one somebody here has
#     a habit of reaching for.
#
# Two shapes remain out of reach of a grep, and they are named here rather than
# left for somebody to discover: an import whose module name is computed at run
# time (`__import__(name)`), and one reached through an alias bound earlier
# (`imp = import_module` then `imp("reachy_mini")`). Both need the name to be a
# value rather than a literal. Banning `importlib` shrinks the second to
# `__import__` alone, which is a builtin and cannot be taken away; neither has a
# reason to appear in a layer that computes nothing but poses, and both are
# review's to catch. That is the documented edge of this rule, not a gap in it.
#
# The clock grep bans `asyncio` outright, not just `asyncio.sleep`, and that is
# the difference between the rule and a rule about one spelling. `from asyncio
# import sleep` followed by `await sleep(...)` reads no attribute called
# `asyncio.sleep` and would have slipped past; so would a clock read through
# `loop.time()`. Banning the module closes the whole class at once, and it costs
# nothing: this layer is synchronous by construction — it is handed a moment and
# hands back intents, and there is nothing in it for an event loop to do.
#
# The greps are restricted to `*.py`. Left unrestricted they also read `.pyc`
# files under `__pycache__`, where they report `Binary file … matches` and fail
# the recipe over stale bytecode — and a boundary check that fails for reasons
# that have nothing to do with the boundary is one people learn to route around.
lint-behaviour-boundary:
    #!/usr/bin/env bash
    set -euo pipefail
    probe='apps/ha-satellite/tests/fixtures/behaviour_boundary_probe.py.txt'
    src='apps/ha-satellite/src/reachy_mini_ha_satellite'
    guarded="$src/behaviour"
    reached='^[[:space:]]*(from|import)[[:space:]]+(reachy_mini[[:space:].]|reachy_mini$|importlib[[:space:].]|importlib$|reachy_mini_ha_satellite\.(adapters|daemon_app|esphome|main|web))|(import_module|__import__)[[:space:]]*\([[:space:]]*[\"'"'"'](reachy_mini[\"'"'"'.]|reachy_mini_ha_satellite\.(adapters|daemon_app|esphome|main|web))|spec_from_file_location'
    clock='^[[:space:]]*(from|import)[[:space:]]+(time|datetime|asyncio)[[:space:].]|^[[:space:]]*(from|import)[[:space:]]+(time|datetime|asyncio)$|(time|datetime)\.(monotonic|perf_counter|time|now|utcnow|sleep)|asyncio\.sleep'
    ban='lint.flake8-tidy-imports.banned-api = { "reachy_mini_ha_satellite.adapters" = { msg = "The behaviour layer decides; adapters act. Everything it needs arrives as an argument. See ha-satellite REQ-042." }, "reachy_mini_ha_satellite.esphome" = { msg = "The behaviour layer has no opinions about protobuf; pipeline events reach it through adapters/pipeline_events.py. See ha-satellite REQ-042." }, "reachy_mini_ha_satellite.main" = { msg = "The composition root imports the behaviour layer, never the reverse." }, "reachy_mini_ha_satellite.daemon_app" = { msg = "daemon_app is the one module that imports the Reachy Mini SDK; reaching it from the behaviour layer would import the SDK by another name." }, "reachy_mini_ha_satellite.web" = { msg = "The settings interface reads the behaviour layer through the application; the behaviour layer does not know it exists." }, "reachy_mini" = { msg = "The behaviour layer must not import the Reachy Mini SDK; the robot is reached through the ports it is handed." } }'
    scope='lint.per-file-ignores = { "!apps/ha-satellite/src/reachy_mini_ha_satellite/behaviour/**" = ["TID251"] }'

    if [ ! -f "$probe" ]; then
        echo "lint-behaviour-boundary: FAILED — the fixture $probe is missing, so the rule is not proved to fire." >&2
        exit 1
    fi
    if [ ! -d "$guarded" ]; then
        echo "lint-behaviour-boundary: FAILED — $guarded is not a directory. The layout moved and this recipe no longer checks it." >&2
        exit 1
    fi

    # `--isolated` is what makes this a check rather than a formality: the root
    # configuration switches TID251 off outside the vendored directory, so a run
    # that loaded it would pass over any input at all.
    check=({{ uv }} ruff check --no-cache --isolated --select TID251 --output-format concise --config "$ban" --config "$scope")

    # ruff exits non-zero both when the rule fires and when the invocation is
    # broken, so the output has to actually name TID251 for this to prove anything.
    fired="$("${check[@]}" --stdin-filename "$guarded/probe.py" - < "$probe" 2>&1)" && status=0 || status=$?
    if [ "$status" -eq 0 ]; then
        printf '%s\n' "$fired"
        echo 'lint-behaviour-boundary: FAILED — an adapter import inside behaviour/ did not trip TID251.' >&2
        exit 1
    fi
    if ! printf '%s\n' "$fired" | grep -q 'TID251'; then
        printf '%s\n' "$fired"
        echo "lint-behaviour-boundary: FAILED — ruff exited $status without reporting TID251, so it failed for some other reason and proves nothing." >&2
        exit 1
    fi
    printf '%s\n' "$fired" | sed 's/^/    /'

    if ! "${check[@]}" --stdin-filename "$src/adapters/probe.py" - < "$probe"; then
        echo 'lint-behaviour-boundary: FAILED — the same imports are banned outside behaviour/, so the rule is not scoped to it.' >&2
        exit 1
    fi

    if ! "${check[@]}" "$guarded"; then
        echo 'lint-behaviour-boundary: FAILED — the behaviour layer imports something that performs input or output.' >&2
        exit 1
    fi

    if ! grep -qE "$reached" "$probe"; then
        echo 'lint-behaviour-boundary: FAILED — the fixture no longer reaches past the boundary, so the grep half proves nothing.' >&2
        exit 1
    fi

    # Matching the fixture *somewhere* proves only that one line still reaches,
    # and the fixture has many. Each shape only the grep can see is checked on
    # its own line, so a widened pattern cannot quietly stop covering one of
    # them and an edited fixture cannot quietly stop proving it. The bare SDK
    # name is here because a `reachy_mini\.` prefix missed exactly this.
    for shape in 'import_module("reachy_mini")' '__import__("reachy_mini")' 'import_module("reachy_mini_ha_satellite.adapters' 'spec_from_file_location'; do
        if ! grep -F -- "$shape" "$probe" | grep -qE "$reached"; then
            echo "lint-behaviour-boundary: FAILED — the fixture no longer proves the grep fires on: $shape" >&2
            exit 1
        fi
    done
    if grep -rnE --include='*.py' "$reached" "$guarded"; then
        echo 'lint-behaviour-boundary: FAILED — the behaviour layer reaches an adapter, the vendored protocol, the entry point, the settings interface or the SDK.' >&2
        exit 1
    fi

    if ! grep -qE "$clock" "$probe"; then
        echo 'lint-behaviour-boundary: FAILED — the fixture no longer reads a clock, so the second grep proves nothing.' >&2
        exit 1
    fi
    if grep -rnE --include='*.py' "$clock" "$guarded"; then
        echo 'lint-behaviour-boundary: FAILED — the behaviour layer reads a clock or sleeps. Time is a parameter to it.' >&2
        exit 1
    fi
    echo 'lint-behaviour-boundary: TID251 fires inside behaviour/ and nowhere else, and no adapter import, dynamic import or clock read survives there.'

# Prove the groundstation's capability boundary still fires.
#
# Groundstation REQ-022 says adding a capability changes no file belonging to the
# transport, the session layer, or another capability. What makes that true rather
# than merely intended is that nothing under `api/`, `session/` or `pipeline/` may
# import `reachy_groundstation.capabilities` at all: they hold a
# `CapabilityRegistryPort` handed to them by `reachy_groundstation.service`, which
# is the one module outside the package that composes it.
#
# The rule is ruff's `flake8-tidy-imports` `banned-api`, exactly as the vendored
# ESPHome boundary above is — but configured on the invocation rather than in
# `pyproject.toml`, and that is forced rather than chosen. `banned-api` is a single
# global list, and `pyproject.toml` already spends it on the vendored boundary,
# whose negated `per-file-ignores` entry switches TID251 off everywhere outside
# that directory. An entry added there would therefore be dead in the three
# packages it needs to guard, and it would also ban `reachy_contracts` here, which
# is the one import the session layer exists to carry. TID253 is spent on the
# pydantic ban and cannot take a second target without a per-file ignore that would
# switch the pydantic ban off inside `capabilities/` — the one place a wire type
# would most plausibly be redeclared. So the ban lives here, with its scope, and
# this recipe is what runs it.
#
# Three checks, and the first two are the ones that make it real. A rule nobody has
# watched fail is a rule that does not exist, so the failing case is a committed
# fixture full of the imports the boundary forbids, fed to ruff on standard input
# under a pretended path — `--stdin-filename` only tells ruff which per-file rules
# apply, so no probe file is ever left in the tree. The same fixture is then run
# under a path inside `capabilities/`, where all of it is ordinary, which proves
# the ban is scoped to the three packages rather than global. Only then is the real
# tree checked.
#
# The fourth check is the backstop TID251 cannot be: it inspects import statements
# only, so a dynamic import slips past it. A grep for the package's name in the
# three guarded directories closes that, and the fixture carries two dynamic
# imports so this half is proved to fire too.
lint-capability-boundary:
    #!/usr/bin/env bash
    set -euo pipefail
    probe='services/groundstation/tests/fixtures/capability_boundary_probe.py.txt'
    src='services/groundstation/src/reachy_groundstation'
    guarded="$src/api $src/session $src/pipeline"
    dynamic='(import_module|__import__)[[:space:]]*\([[:space:]]*[\"'"'"']reachy_groundstation\.capabilities'
    ban='lint.flake8-tidy-imports.banned-api = { "reachy_groundstation.capabilities" = { msg = "The transport, the session layer and the pipeline route by capability name against a CapabilityRegistryPort handed to them; only reachy_groundstation.service composes the registry. See groundstation REQ-022." } }'
    scope='lint.per-file-ignores = { "services/groundstation/src/reachy_groundstation/capabilities/**" = ["TID251"], "services/groundstation/src/reachy_groundstation/service.py" = ["TID251"], "services/groundstation/tests/**" = ["TID251"] }'

    if [ ! -f "$probe" ]; then
        echo "lint-capability-boundary: FAILED — the fixture $probe is missing, so the rule is not proved to fire." >&2
        exit 1
    fi
    for directory in $guarded; do
        if [ ! -d "$directory" ]; then
            echo "lint-capability-boundary: FAILED — $directory is not a directory. The layout moved and this recipe no longer checks it." >&2
            exit 1
        fi
    done

    # `--isolated` is what makes this a check rather than a formality: the root
    # configuration switches TID251 off outside the vendored directory, so a run
    # that loaded it would pass over any input at all.
    check=({{ uv }} ruff check --no-cache --isolated --select TID251 --output-format concise --config "$ban" --config "$scope")

    # ruff exits non-zero both when the rule fires and when the invocation is
    # broken, so the output has to actually name TID251 for this to prove anything.
    fired="$("${check[@]}" --stdin-filename "$src/session/probe.py" - < "$probe" 2>&1)" && status=0 || status=$?
    if [ "$status" -eq 0 ]; then
        printf '%s\n' "$fired"
        echo 'lint-capability-boundary: FAILED — a capability import inside the session layer did not trip TID251.' >&2
        exit 1
    fi
    if ! printf '%s\n' "$fired" | grep -q 'TID251'; then
        printf '%s\n' "$fired"
        echo "lint-capability-boundary: FAILED — ruff exited $status without reporting TID251, so it failed for some other reason and proves nothing." >&2
        exit 1
    fi
    printf '%s\n' "$fired" | sed 's/^/    /'

    if ! "${check[@]}" --stdin-filename "$src/capabilities/probe.py" - < "$probe"; then
        echo 'lint-capability-boundary: FAILED — the same imports are banned inside capabilities/, so the rule is not scoped to the three guarded packages.' >&2
        exit 1
    fi

    if ! "${check[@]}" services/groundstation; then
        echo 'lint-capability-boundary: FAILED — a guarded package imports reachy_groundstation.capabilities.' >&2
        exit 1
    fi

    if ! grep -qE "$dynamic" "$probe"; then
        echo 'lint-capability-boundary: FAILED — the fixture no longer contains a dynamic capability import, so the grep half proves nothing.' >&2
        exit 1
    fi
    if grep -rnE --include='*.py' "$dynamic" $guarded; then
        echo 'lint-capability-boundary: FAILED — a guarded package reaches a capability module through a dynamic import.' >&2
        exit 1
    fi
    echo 'lint-capability-boundary: TID251 fires inside api/, session/ and pipeline/ and nowhere else, and no dynamic capability import survives there.'

# Prove the vendored ESPHome directory's import-direction boundary still fires.
#
# A lint rule nobody has watched fail is a rule that does not exist, and this one
# guards something a single convenient import would quietly undo. So the failing
# scenario is a fixture — a file full of Reachy imports — and the recipe runs both
# halves of the boundary against it and fails if either stays quiet.
#
# The first half is ruff's TID251, run twice over the fixture: once under a path
# inside the vendored directory, where every import in it is banned, and once
# under a path beside the adapters, where all of them are ordinary. Neither path
# exists on disk — `--stdin-filename` only tells ruff which per-file rules apply —
# so no probe file is ever left in the tree to be imported by mistake.
#
# The second half exists because TID251 alone cannot express the whole rule. Ruff
# resolves a relative import to its absolute path before matching, so banning the
# package root would ban the vendored modules' own `from .entity import ...`; the
# ban therefore names Reachy-specific modules, and `import
# reachy_mini_ha_satellite` on its own would slip through. TID251 also inspects
# only import statements, so it never sees a dynamic import. A grep for any
# absolute `reachy`-prefixed import closes both, qualified or not — the fixture
# carries a bare root import and two dynamic ones, so this half is proved to fire.
#
# It is a backstop against the convenient accident, not a sandbox: an author
# determined to load a Reachy module from in here can always assemble the name at
# run time, and no lint rule stops that. What this makes impossible is doing it
# without meaning to, and without a reviewer seeing it.
#
# The grep is restricted to `*.py`. Left unrestricted it also reads `.pyc` files
# under `__pycache__`, where it reports `Binary file … matches` and fails the
# recipe over stale bytecode — and a boundary check that fails for reasons that
# have nothing to do with the boundary is one people learn to route around.
lint-boundary:
    #!/usr/bin/env bash
    set -euo pipefail
    probe='apps/ha-satellite/tests/fixtures/vendored_boundary_probe.py.txt'
    vendored='apps/ha-satellite/src/reachy_mini_ha_satellite/esphome'
    inside="$vendored/boundary_probe.py"
    outside='apps/ha-satellite/src/reachy_mini_ha_satellite/adapters/boundary_probe.py'
    reachy_import='^[[:space:]]*(from|import)[[:space:]]+reachy|(import_module|__import__)[[:space:]]*\([[:space:]]*[\"'"'"']reachy'

    if [ ! -f "$probe" ]; then
        echo "lint-boundary: FAILED — the fixture $probe is missing, so neither half of the boundary is proved." >&2
        exit 1
    fi
    if [ ! -d "$vendored" ]; then
        echo "lint-boundary: FAILED — $vendored is not a directory. The vendored code moved and this recipe no longer checks it." >&2
        exit 1
    fi

    # ruff exits non-zero both when the rule fires and when the invocation is
    # broken, so a bad config or an unreadable fixture would otherwise read as
    # proof. The output has to actually name TID251.
    fired="$({{ uv }} ruff check --no-cache --select TID251 --output-format concise --stdin-filename "$inside" - < "$probe" 2>&1)" && status=0 || status=$?
    if [ "$status" -eq 0 ]; then
        printf '%s\n' "$fired"
        echo 'lint-boundary: FAILED — a Reachy import inside the vendored directory did not trip TID251.' >&2
        exit 1
    fi
    if ! printf '%s\n' "$fired" | grep -q 'TID251'; then
        printf '%s\n' "$fired"
        echo "lint-boundary: FAILED — ruff exited $status without reporting TID251, so it failed for some other reason and proves nothing." >&2
        exit 1
    fi
    printf '%s\n' "$fired" | sed 's/^/    /'

    if ! {{ uv }} ruff check --no-cache --select TID251 --output-format concise --stdin-filename "$outside" - < "$probe"; then
        echo 'lint-boundary: FAILED — the same imports are banned outside the vendored directory, so the rule is not scoped to it.' >&2
        exit 1
    fi

    if ! grep -qE "$reachy_import" "$probe"; then
        echo 'lint-boundary: FAILED — the fixture no longer contains an absolute Reachy import, so the grep half proves nothing.' >&2
        exit 1
    fi
    if grep -rnE --include='*.py' "$reachy_import" "$vendored"; then
        echo 'lint-boundary: FAILED — the vendored directory imports a Reachy module by absolute name.' >&2
        exit 1
    fi
    echo 'lint-boundary: TID251 fires inside the vendored directory and nowhere else, and no absolute Reachy import survives there.'

# Fetch every model the groundstation ships and verify it against its pinned
# hash. Writes to `.models/` by default, which is gitignored — weights are never
# committed.
#
# This is the build-time half of groundstation REQ-023 and REQ-024: models are
# fetched and hash-verified while the artifact is being built, and the running
# service loads them from a file already in place. It is also what continuous
# integration runs before the test suite, because the perception integration
# tests run real inference and there is nothing to run it against otherwise. A
# fetched file whose digest disagrees with the registry fails the run and is
# deleted rather than left where a later stage could find it.
models directory=".models":
    {{ uv }} python -m reachy_groundstation.models.fetch {{ directory }}

# Print the repository-wide version every artifact carries.
#
# Read out of the contracts package, which is where release automation writes it
# and where the distribution metadata is derived from — see the root AGENTS.md
# on one version for the whole repository. Anything that needs to label an
# artifact reads it from here rather than growing a second copy.
version:
    @{{ uv }} python -c 'from reachy_contracts import __version__; print(__version__)'

# Build the groundstation container image, from the repository root.
#
#     just image                       # the default CPU variant
#     just image cuda                  # the accelerated variant
#     just image cpu ghcr.io/…:1.2.3 --platform linux/amd64,linux/arm64 --push
#
# The variant is the whole difference between the two published tags: which base
# image ships, which model runtime is installed, and which execution providers
# the service prefers. There is one Dockerfile, and everything variant-specific
# is here, so a third variant is three lines rather than a second build file.
#
# Anything after the tag is passed to `docker buildx build` unchanged, which is
# how continuous integration adds `--platform`, `--push` and its cache flags
# without this recipe growing a parameter per flag. Nothing here names an
# architecture: `--platform` is the whole of building for both.
image variant="cpu" tag="reachy-groundstation:dev" *buildx_args:
    #!/usr/bin/env bash
    set -euo pipefail

    case '{{ variant }}' in
        cpu)
            # The default. `RUNTIME_BASE` and the provider list are the
            # Dockerfile's own defaults, so nothing is overridden here.
            args=()
            ;;
        cuda)
            # NVIDIA's CUDA runtime with cuDNN, pinned by the digest of the
            # multi-architecture index. The CUDA MAJOR VERSION is not free: the
            # `onnxruntime-gpu` wheel in the lockfile links `libcublasLt.so.13`
            # and says so — "Require cuDNN 9.* and CUDA 13.*" — so a 12.x base
            # produces an image whose CUDA provider library fails to load and a
            # service that silently falls back to the CPU provider. That is
            # exactly the "builds fine, is not what it claims" failure this
            # change's verification exists to catch, and
            # `just image-verify <tag> <network> cuda` is what catches it.
            #
            # `onnxruntime-gpu` is what actually carries the CUDA execution
            # provider — the stock wheel does not have it, so a variant that
            # changed only the provider list would fall back to the CPU
            # provider and quietly not be a CUDA variant.
            args=(
                --build-arg 'RUNTIME_BASE=nvidia/cuda:13.0.2-cudnn-runtime-ubuntu24.04@sha256:14d94b039cb94bbd5da559f303b46bc4b0d5d6c24ab1a9d7b186e566ed3400dc'
                --build-arg 'RUNTIME_EXTRA=cuda'
                --build-arg 'INFERENCE_PROVIDERS=CUDAExecutionProvider,CPUExecutionProvider'
            )
            ;;
        *)
            echo "just image: unknown variant '{{ variant }}'; expected cpu or cuda" >&2
            exit 1
            ;;
    esac

    # Where the result goes. A `docker` driver builds straight into the local
    # daemon; a `docker-container` one — which `docker buildx create` makes, and
    # which continuous integration uses because it is what builds for another
    # architecture — writes nowhere unless told to, so `just image` followed by
    # `just image-verify` would verify whatever image was there before. Asking
    # for `--load` covers both.
    #
    # It is dropped in exactly two cases, and naming an architecture is not one
    # of them: a single-platform build for the OTHER architecture is precisely
    # the one that has to be loaded, because it is the one `just image-verify`
    # then runs under emulation. The two are a caller who already said where the
    # result goes, since buildx refuses two destinations, and a genuinely
    # multi-platform build, which buildx cannot load into a daemon at all.
    # Every spelling buildx accepts for a destination, because two exporters is
    # an error rather than a preference: the long flags, their boolean
    # assignment forms, and the `-o` shorthand with or without its value
    # attached.
    output=(--load)
    platforms=''
    previous=''
    for argument in {{ buildx_args }}; do
        case "$argument" in
            --load|--load=*|--push|--push=*|--output|--output=*|-o|-o=*|-o?*)
                output=()
                ;;
            --platform=*) platforms="${argument#--platform=}" ;;
        esac
        if [ "$previous" = '--platform' ]; then
            platforms="$argument"
        fi
        previous="$argument"
    done
    case "$platforms" in
        *,*) output=() ;;
    esac

    docker buildx build \
        --file services/groundstation/Dockerfile \
        --tag '{{ tag }}' \
        "${args[@]}" \
        "${output[@]}" \
        {{ buildx_args }} \
        .

# Report how large a built image is, as one line of JSON.
#
# This is the build output groundstation change 0014 gates on: the predecessor
# shipped 483 MB against roughly 2 GB for the alternative packaging, and keeping
# that property is a packaging decision rather than an application one. The
# number is the uncompressed on-disk size of the image in the local daemon,
# which is what `docker images` reports and what a host needs room for; the
# compressed transfer size depends on the registry and is not comparable across
# one.
#
# JSON on standard output rather than a table, because the consumer is a gate.
#
# ⚠️ `{{{{` below is not a typo and the braces are not unbalanced. `docker
# --format` takes a Go template, whose delimiters are the same `{{ }}` this file
# interpolates its own parameters with — so a literal `{{` has to be escaped as
# `{{{{`, while `}}` outside an interpolation is already literal and is written
# once. `'{{{{ .Size }}'` therefore reaches the shell as `'{{ .Size }}'`, quoted
# and balanced. Check with `just --dry-run image-size`, which prints what will
# actually run. The same escape appears in `image-verify` below, twice.
#
# This comment sits ABOVE the recipe rather than inside it, and that is forced: a
# comment inside a recipe body is a template line like any other, so writing an
# unescaped doubled brace in one opens an interpolation that never closes and the
# whole file stops parsing.
image-size tag="reachy-groundstation:dev" variant="cpu":
    #!/usr/bin/env bash
    set -euo pipefail
    bytes="$(docker image inspect '{{ tag }}' --format '{{{{ .Size }}')"
    platform="$(docker image inspect '{{ tag }}' --format '{{{{ .Os }}/{{{{ .Architecture }}')"
    printf '{"image":"%s","variant":"%s","platform":"%s","size_bytes":%s,"size_mib":%s}\n' \
        '{{ tag }}' '{{ variant }}' "$platform" "$bytes" \
        "$(awk -v b="$bytes" 'BEGIN { printf "%.1f", b / 1048576 }')"

# Start the built image and prove it is a working deployment, not just a
# successful build.
#
# Every interesting packaging failure — a missing model, a wrong user, an absent
# shared library, an interpreter that segfaults on a base image with no
# `/etc/machine-id` — is invisible to a build that only checks the build
# succeeded. All four of those have happened to this Dockerfile.
#
# `network` decides what the container can reach, and the default is the
# interesting one:
#
#   isolated  an `--internal` Docker network, which has no route off the host.
#             This is groundstation REQ-023's actual scenario — a container on a
#             host with no outbound internet access — and the recipe proves the
#             isolation is real by resolving every model source out of the
#             registry inside the container and failing if any of them can be
#             reached.
#   bridge    ordinary container networking, so the same session is driven
#             against a service that could have reached the network and did not
#             need to.
#
# `variant` selects the two checks that are not the same for both images. Only
# the default variant is toolchain-free — NVIDIA publishes no CUDA runtime image
# smaller than an Ubuntu, so the accelerated one inherits a package manager and
# a shell — and only the accelerated one has a CUDA provider library to load.
#
# The session is driven from a SIBLING container on the same network rather than
# from the host, because an `--internal` network publishes no ports. The sibling
# runs the image under test, which already carries the interpreter, `websockets`
# and the contracts; the script and the fixture frame are mounted read-only.
image-verify tag="reachy-groundstation:dev" network="isolated" variant="cpu":
    #!/usr/bin/env bash
    set -euo pipefail

    # A placeholder, and never anybody's: this credential exists for the length
    # of one container. See the root AGENTS.md on what may enter a tracked file.
    credential='example-credential'
    name="reachy-image-verify-$$"
    net="${name}-net"

    case '{{ network }}' in
        isolated) create=(docker network create --internal "$net") ;;
        bridge)   create=(docker network create "$net") ;;
        *)
            echo "just image-verify: unknown network '{{ network }}'; expected isolated or bridge" >&2
            exit 1
            ;;
    esac

    # Read-only, at a path nothing in the image looks at: the probe scripts and
    # the fixture frame are harness, not artifact. Nothing is mounted over
    # anything the image ships.
    mounts=(
        --volume "$PWD/scripts:/verify/scripts:ro"
        --volume "$PWD/services/groundstation/tests/fixtures:/verify/fixtures:ro"
    )

    # The one client implementation of the session protocol, mounted for the
    # container that drives the session and for no other. Reachyctl REQ-057
    # makes `reachy_session_client` the only client, so the alternative is a
    # second one written to test an image, which would pass its own
    # expectations and prove nothing about what a robot meets.
    #
    # It is mounted rather than installed because it is not a groundstation
    # dependency and putting it in the published image to test the published
    # image would be the wrong trade. It runs on the image's own interpreter:
    # the client is pure Python and needs `reachy_contracts` and `websockets`,
    # both of which the service already installs. If it ever needs a third, the
    # driver fails with an import error naming it.
    driver=(
        --volume "$PWD/packages/reachy-session-client/src:/verify/lib:ro"
        --env PYTHONPATH=/verify/lib
    )

    # The log is the only account of why a container that failed failed, and by
    # the time anybody looks the container is gone. So it is dumped on the way
    # out of ANY failure — a container that never started, a probe that refused
    # it, a session that was never answered — rather than at the one call site
    # where somebody remembered to.
    cleanup() {
        status=$?
        if [ "$status" -ne 0 ]; then
            echo '--- the service log follows ---' >&2
            # Both streams together: the resolved configuration goes to standard
            # output and a refusal to start goes to standard error, and the one
            # that explains a failure is usually the second.
            logs="$(docker logs "$name" 2>&1 || true)"
            printf '%s\n' "$logs" >&2
        fi
        docker rm --force "$name" >/dev/null 2>&1 || true
        docker network rm "$net" >/dev/null 2>&1 || true
        return "$status"
    }
    trap cleanup EXIT

    "${create[@]}" >/dev/null
    # Two timeouts are widened for the harness, and only for the harness. An
    # image built for the other architecture is run here under emulation, where
    # one detection pass costs seconds rather than tens of milliseconds — so the
    # service's own warm-up bound and its per-frame bound would both expire and
    # the run would report a packaging failure that is really a QEMU
    # measurement. What is being checked is that the artifact starts and
    # answers, not how quickly; how quickly is change 0014's question, measured
    # on hardware rather than through an emulator.
    docker run --detach --name "$name" --network "$net" "${mounts[@]}" \
        --env "REACHY_GROUNDSTATION_CREDENTIAL=${credential}" \
        --env 'REACHY_GROUNDSTATION_WARM_UP_TIMEOUT_SECONDS=600' \
        --env 'REACHY_GROUNDSTATION_CAPABILITY_TIMEOUT_SECONDS=120' \
        '{{ tag }}' >/dev/null

    # A container that refused its configuration or could not load its model has
    # already exited by now, and every check below would then fail with
    # something that reads like a network fault. Say what actually happened
    # instead, and say it in a second rather than after the readiness deadline.
    sleep 5
    # The quadrupled brace below is just's escape for the doubled brace a Go
    # template needs — see the note above `image-size`.
    if [ "$(docker inspect "$name" --format '{{{{ .State.Running }}')" != 'true' ]; then
        echo "just image-verify: the container exited instead of starting; its log follows" >&2
        exit 1
    fi

    # These run in the service's OWN container, which is the only place any of
    # them can be answered: whether this network really has no route off the
    # host, whether the runtime stage really carries no toolchain, and whether
    # the accelerated variant's CUDA provider would actually load.
    probe=()
    if [ '{{ network }}' = 'isolated' ]; then
        probe+=(--unreachable-sources)
    fi
    case '{{ variant }}' in
        cpu)  probe+=(--no-toolchain) ;;
        cuda) probe+=(--cuda-provider) ;;
        *)
            echo "just image-verify: unknown variant '{{ variant }}'; expected cpu or cuda" >&2
            exit 1
            ;;
    esac
    docker exec "$name" /opt/reachy/venv/bin/python \
        /verify/scripts/probe_groundstation_container.py "${probe[@]}"

    # It must not run as root, whatever the base image's own default is. The
    # brace escape below is the one explained above `image-size`.
    user="$(docker inspect "$name" --format '{{{{ .Config.User }}')"
    case "$user" in
        ''|0|0:0|root|root:*)
            echo "just image-verify: the image runs as '${user:-root}'" >&2
            exit 1
            ;;
    esac
    echo "image-verify: runs as ${user}"

    # The session is driven from a sibling on the same network. An `--internal`
    # network publishes no ports, so there is no way to reach the service from
    # the host — and driving it from another container is closer to a robot
    # opening a session than a loopback connection would be anyway. The sibling
    # runs the image under test because it already carries the interpreter,
    # `websockets` and the contracts, which is what the shared session client
    # mounted above needs to run on it.
    docker run --rm --network "$net" "${mounts[@]}" "${driver[@]}" \
        --entrypoint /opt/reachy/venv/bin/python \
        '{{ tag }}' /verify/scripts/verify_groundstation_image.py \
            --base-url "http://${name}:8080" \
            --credential "$credential" \
            --frame /verify/fixtures/perception/face_single.jpg \
            --ready-timeout 900

    docker logs "$name"

# Build every wheel this repository releases.
#
# Five members, and three of them are not padding. `reachyctl` declares
# `reachy-contracts`, `reachy-checks` and `reachy-session-client` as
# requirements and nothing publishes them to an index, so a wheel released on
# its own installs nowhere: the resolver looks for three distributions that do
# not exist. They are built beside it and released beside it, and
# `just wheel-verify` installs the set into an empty environment to prove that
# is enough.
#
# The fifth is the robot application. It is released for a different reason —
# the daemon installs it and discovers it through the `reachy_mini_apps` entry
# point, ha-satellite REQ-041 — and it is built here rather than by a recipe of
# its own so that one command produces everything a release carries and
# `just wheel-size` reports on all of it in one format.
#
# One version for the whole repository, so every wheel here carries the same one
# — see the root AGENTS.md.
wheels out_dir="dist":
    #!/usr/bin/env bash
    set -euo pipefail
    rm --recursive --force '{{ out_dir }}'
    for member in reachy-contracts reachy-checks reachy-session-client reachyctl \
                  reachy-mini-ha-satellite; do
        uv build --package "$member" --wheel --out-dir '{{ out_dir }}'
    done
    ls -1 '{{ out_dir }}'

# Report how large a built wheel is, as one line of JSON.
#
# The same shape as `just image-size`, and for the same consumer: change 0014
# gates on artifact growth and should read one format rather than two. The unit
# differs because the artifacts do — an image is measured in mebibytes and a
# wheel in kibibytes — and `size_bytes` is the field a gate compares, in both.
#
# JSON on standard output rather than a table, because the consumer is a gate.
wheel-size wheel:
    #!/usr/bin/env bash
    set -euo pipefail
    name="$(basename '{{ wheel }}')"
    bytes="$(stat --format=%s '{{ wheel }}')"
    # `{distribution}-{version}-{python}-{abi}-{platform}.whl`, and a wheel's
    # file name spells a hyphen as an underscore.
    artifact="$(echo "${name%%-*}" | tr '_' '-')"
    version="$(echo "$name" | cut -d- -f2)"
    printf '{"artifact":"%s","wheel":"%s","version":"%s","size_bytes":%s,"size_kib":%s}\n' \
        "$artifact" "$name" "$version" "$bytes" \
        "$(awk -v b="$bytes" 'BEGIN { printf "%.1f", b / 1024 }')"

# Install the built wheels into an empty environment and drive the tool.
#
# A wheel that builds and cannot be installed is a passing build and a broken
# release, which is the same reasoning `just image-verify` is written from: the
# artifact is verified by running it, not by inspecting how it was made. Three
# things are checked, and the first is the one that catches a missing sibling —
# an environment with nothing in it resolves the whole dependency set or fails.
#
# The environment is built in a temporary directory and the install runs from
# there, so nothing about this checkout — its `.venv`, its workspace sources —
# is on the path. `--find-links` supplies the four unpublished wheels; the
# third-party dependencies come from the index, exactly as they would for
# somebody installing a release.
wheel-verify out_dir="dist":
    #!/usr/bin/env bash
    set -euo pipefail
    expected="$(just version)"
    wheels="$(cd '{{ out_dir }}' && pwd)"
    scratch="$(mktemp --directory)"
    trap 'rm --recursive --force "$scratch"' EXIT
    cd "$scratch"
    uv venv --quiet env
    VIRTUAL_ENV="$scratch/env" uv pip install --quiet --find-links "$wheels" reachyctl

    reported="$("$scratch/env/bin/reachyctl" --version)"
    if [ "$reported" != "reachyctl $expected" ]; then
        echo "wheel-verify: the installed tool reports '$reported', not 'reachyctl $expected'" >&2
        exit 1
    fi
    echo "wheel-verify: $reported"

    "$scratch/env/bin/reachyctl" --help >/dev/null

    # A real command, run as a standalone tool with nothing configured: every
    # check is skipped, the run exits zero, and the result parses. That is
    # reachyctl REQ-058's scenario, and it is also what proves the console
    # script's whole import graph resolves in an environment that has only the
    # release in it.
    "$scratch/env/bin/reachyctl" --output json doctor > result.json
    "$scratch/env/bin/python" - <<'PYTHON'
    import json
    import pathlib

    document = json.loads(pathlib.Path("result.json").read_text(encoding="utf-8"))
    assert document["command"] == "doctor", document
    assert document["ok"] is True, document
    assert document["data"]["skipped"] == document["data"]["checks"], document
    print(f"wheel-verify: doctor reported {document['data']['checks']} checks, all skipped")
    PYTHON

    # The satellite wheel is released by the same job and asked a different
    # question: not "does this install" but "does it carry what makes the daemon
    # find it". A missing `reachy_mini_apps` entry point installs perfectly and
    # never appears in the daemon's application list, and an asset shipping
    # without its registry entry ships somebody else's file under terms nobody
    # agreed to. Neither is visible to a build that merely succeeded.
    cd - >/dev/null
    satellite=("$wheels"/reachy_mini_ha_satellite-*.whl)
    if [ "${#satellite[@]}" -ne 1 ] || [ ! -f "${satellite[0]}" ]; then
        echo "wheel-verify: expected exactly one satellite wheel in $wheels" >&2
        exit 1
    fi
    {{ uv }} python scripts/verify_satellite_wheel.py "${satellite[0]}"

# Redraw the committed perception fixture images.
#
# They are drawn rather than photographed, so their provenance is the script and
# there is no licence to check. Every random draw is seeded, so a rerun that
# changes a file means the drawing changed rather than the noise.
perception-fixtures directory="services/groundstation/tests/fixtures/perception":
    {{ uv }} python scripts/generate_perception_fixtures.py {{ directory }}

# Verify that the assets shipped in the satellite wheel are exactly the ones the
# registry records, unmodified. The half of the licence gate that has to read the
# directory; the half that judges the terms is an ordinary unit test.
check-assets:
    {{ uv }} python -m reachy_mini_ha_satellite.assets.verify

# Report how far the vendored ESPHome core has drifted from the upstream it was
# derived from, as a markdown table on standard output.
#
# It reports; it never merges, and it never opens a pull request. This is a
# derivation, not a mirror: both audio seams are replaced and the command-line
# entry point is discarded, so an automatic sync would be a conflict resolution
# every time and machinery that implied otherwise would mislead.
#
# The file list is not written down here. Every vendored file records its own
# upstream path and commit in its header, so those headers are the single source
# of truth and `scripts/vendored_provenance.py` reads them back out — a file that
# moves inside this repository keeps being tracked, and a file whose header goes
# missing fails the run rather than dropping out of the report.
#
# What that script hands back, and what the table below reports, are UPSTREAM
# paths: the comparison diffs upstream against itself, so the local file each one
# was derived from never enters it. The pairing lives in the local file's header
# and in its directory NOTICE.
#
# Exits 0 even when upstream has moved. Drift is news about somebody else's
# repository, and a check that went red on it would be permanently red and
# quickly ignored. What does fail is a broken provenance record: a recorded
# commit that no longer resolves, or a header naming a path upstream has not got.
# That is a defect here.
#
# The scheduled workflow runs this recipe and puts its output in the job summary.
vendored-drift clone_dir=".upstream-drift":
    #!/usr/bin/env bash
    set -euo pipefail

    provenance="$(python3 scripts/vendored_provenance.py)"
    url="$(printf '%s\n' "$provenance" | sed -n 's/^upstream-url=//p')"
    recorded="$(printf '%s\n' "$provenance" | sed -n 's/^upstream-commit=//p')"
    upstream_paths="$(printf '%s\n' "$provenance" | sed -n 's/^upstream-paths=//p')"

    # An absent key parses as an empty string, and an empty path list would walk
    # zero files and report a clean comparison over nothing — the one outcome
    # this whole recipe exists to make impossible. Each is named separately so
    # the message says which key went missing.
    for pair in "upstream-url=$url" "upstream-commit=$recorded" "upstream-paths=$upstream_paths"; do
        if [ -z "${pair#*=}" ]; then
            echo "vendored-drift: scripts/vendored_provenance.py printed no ${pair%%=*}. Its output and this recipe have gone out of step." >&2
            exit 1
        fi
    done

    if [ -d '{{ clone_dir }}/.git' ]; then
        # A cached clone of a DIFFERENT upstream would be compared against
        # happily, and every path would appear to have vanished.
        cached="$(git -C '{{ clone_dir }}' remote get-url origin)"
        # `git clone` records the URL verbatim, but a clone made by hand often
        # carries a trailing `.git` the headers do not. Compare what identifies
        # the repository, not the spelling.
        if [ "${cached%.git}" != "${url%.git}" ]; then
            echo "vendored-drift: {{ clone_dir }} is a clone of $cached, but the headers name $url. Remove it and run again." >&2
            exit 1
        fi
    else
        git clone --quiet --filter=blob:none --no-checkout "$url" '{{ clone_dir }}'
    fi
    cd '{{ clone_dir }}'
    git fetch --quiet --filter=blob:none origin

    if ! git cat-file -e "${recorded}^{commit}" 2>/dev/null; then
        git fetch --quiet --filter=blob:none origin "$recorded" || true
    fi
    if ! git cat-file -e "${recorded}^{commit}" 2>/dev/null; then
        echo "vendored-drift: the recorded upstream commit $recorded is not reachable in $url." >&2
        echo "vendored-drift: the provenance headers name a commit that no longer exists." >&2
        exit 1
    fi

    if ! head_commit="$(git rev-parse --verify --quiet origin/HEAD)"; then
        echo "vendored-drift: $url has no origin/HEAD in {{ clone_dir }}, so there is no upstream tip to compare against." >&2
        exit 1
    fi
    echo '## Vendored ESPHome core'
    echo
    echo '| | |'
    echo '|---|---|'
    echo "| Upstream | \`$url\` |"
    echo "| Recorded at | \`$recorded\` |"
    echo "| Upstream head | \`$head_commit\` |"
    echo

    if [ "$head_commit" = "$recorded" ]; then
        echo 'No drift: upstream has not moved since the vendored files were taken from it.'
        exit 0
    fi

    drifted=0
    rows=''
    for upstream_path in $upstream_paths; do
        if ! git cat-file -e "$recorded:$upstream_path" 2>/dev/null; then
            echo "vendored-drift: a provenance header names $upstream_path, which does not exist upstream at $recorded." >&2
            exit 1
        fi
        stat="$(git diff --numstat "$recorded" "$head_commit" -- "$upstream_path")"
        [ -z "$stat" ] && continue
        drifted=$((drifted + 1))
        rows+="| \`$upstream_path\` | $(echo "$stat" | cut -f1) | $(echo "$stat" | cut -f2) |"$'\n'
    done

    if [ "$drifted" -eq 0 ]; then
        echo 'Upstream has moved, but none of the files this repository derives from changed.'
        exit 0
    fi

    echo '| Upstream file | Lines added | Lines removed |'
    echo '|---|---:|---:|'
    printf '%s' "$rows"
    echo
    echo "$drifted upstream file(s) that this repository derives from have changed."
    echo 'Each row above is an UPSTREAM path; the local file derived from it is named'
    echo 'in its own header and in its directory NOTICE. Review the changes and decide'
    echo 'deliberately whether to carry any of them across; nothing here merges'
    echo 'anything. Re-vendoring means updating the files, every per-file header,'
    echo 'and the commit recorded in the NOTICE.'

# Take the benchmark measurements and write a result document.
#
# With no names, the default selection runs: everything that needs no hardware.
# The two that need a robot are reported as excluded rather than left out of the
# document, which is what benchmarks REQ-072 asks for — a suite that simply
# omitted them would look like one that had lost them. Naming a benchmark is
# what selects it, hardware or not:
#
#     just bench                                    # the default selection
#     just bench detect                             # one of them
#     just bench --artifact-size image-size/        # with sizes to record
#
# Needs `just models` to have run: `detect`, `pipeline`, `session` and
# `footprint` all load the pinned face model, and a benchmark with no model is a
# benchmark that fails rather than one that is fast.
bench *args:
    {{ uv }} python -m reachy_bench run {{ args }}

# Judge a benchmark result against the baseline committed in `bench/baseline.json`.
#
# This is the gate. It exits non-zero when a measurement has regressed beyond
# its stated tolerance, naming the measurement and by how much, and it also
# fails when a measurement has no recorded figure at all — a benchmark added
# without recording what it costs is a measurement nothing will ever compare.
#
# Timings are judged against the profile for the class of machine the run
# happened on; a class nobody has recorded is reported as unbaselined rather
# than passed, and `just bench-record` prints the profile to commit.
bench-compare *args:
    {{ uv }} python -m reachy_bench compare {{ args }}

# Judge artifact sizes against the committed baseline. Benchmarks REQ-073.
#
# The narrow half of the gate, for the workflows that build an artifact and time
# nothing: it reads the JSON `just image-size` and `just wheel-size` already
# emit and compares `size_bytes` against the recorded size. Sizes are collected
# from the change that produces each artifact rather than from one build, so
# this runs in the image workflow and in the release workflow rather than beside
# the benchmarks.
#
#     just bench-sizes --artifact-size image-size/cpu-amd64.json
bench-sizes *args:
    {{ uv }} python -m reachy_bench sizes {{ args }}

# Print the baseline profile block that would record a result, and write nothing.
#
# How a new class of machine is adopted: run the suite on it, read the numbers,
# and paste the block into `bench/baseline.json` in a pull request. Nothing here
# edits the baseline, because accepting a change to the recorded numbers is a
# decision somebody makes in a review rather than one a command makes quietly.
bench-record *args:
    {{ uv }} python -m reachy_bench record {{ args }}

# Apply formatting and the lint fixes that are safe to apply automatically.
fmt:
    {{ uv }} ruff check --fix .
    {{ uv }} ruff format .

# Type-check every member in strict mode.
typecheck:
    {{ uv }} mypy

# The gates a contributor runs before pushing. Not every merge gate: the
# contract-drift check writes to the index and the leak scan needs a commit
# range, so both stay their own recipe rather than making this one mutate
# anything or guess what to compare against. `check-assets` is here because it
# only reads the tree, and because an unregistered asset is a licensing problem
# rather than a test failure — it should stop a contributor, not a release.
check: lint typecheck test check-assets

# Coverage of the lines this branch changed, rather than of the whole tree, so
# a large untested area cannot mask a new one. Requires `just test` to have
# written coverage.xml first.
#
# The nine vendored modules are excluded from this gate, and only from this gate:
# they are still measured, and `just test` still prints their coverage. The gate
# asks whether the work done on a branch was tested, and vendored code is not
# work done on a branch — it arrives with whatever tests its upstream wrote, and
# the rule for it is to carry those tests where they exercise retained code,
# which is a weaker guarantee than 90% of the lines and deliberately so. Holding
# it to the threshold leaves two ways out and both are worse: writing tests
# against a derived file, which forks it from upstream and turns every future
# comparison into a reconciliation, or deleting protocol the application needs.
#
# The nine are named one by one rather than globbed by directory, because
# `esphome/seams.py` sits among them and is ours — it stays gated, and adding a
# vendored file is a visible edit here rather than something a directory pattern
# swallows.
coverage-diff base="origin/main":
    {{ uv }} diff-cover coverage.xml --compare-branch={{ base }} --fail-under=90 \
        --exclude \
        '*/esphome/api_server.py' \
        '*/esphome/entity.py' \
        '*/esphome/models.py' \
        '*/esphome/peripheral_api.py' \
        '*/esphome/satellite.py' \
        '*/esphome/util.py' \
        '*/esphome/wake_word.py' \
        '*/esphome/webrtc.py' \
        '*/esphome/zeroconf.py'

# Verify that every requirement annotation still agrees with the specs.
duvet:
    duvet report --ci

# Reject values belonging to somebody's environment in a range's diff and in
# its commit messages alike. Shapes only — a list of the real hostnames and
# accounts would itself publish them. `head` defaults to the working revision,
# so `just leak-scan` checks the branch against the default branch.
leak-scan base="origin/main" head="HEAD":
    {{ uv }} python -m reachy_hygiene --base {{ base }} --head {{ head }}

# Scan for committed secrets. `log-opts` is passed to `git log`, so the default
# is the whole history and a range narrows it: `just secret-scan "main..HEAD"`.
# Needs `gitleaks` on PATH — `mise install` provides the pinned version. On a
# runner the pull request scan is the gitleaks action instead, which carries its
# own binary; this recipe is what reproduces a finding locally and what produced
# the full-history baseline in docs/ops/secret-scan-baseline.md.
secret-scan log-opts="":
    gitleaks git --no-banner --redact --log-opts="{{ log-opts }}" .

# Regenerate every published schema and interface description from source.
#
# Two registries feed one directory: `reachy_contracts.contracts_export` holds a
# JSON Schema per robot-link message type, and `reachy_checks.checks_export`
# holds the `doctor` check reference. They are separate because the dependency
# runs one way — `reachy-checks` depends on `reachy-contracts`, so a registry in
# the contracts package that imported the checks package would be a cycle — and
# `scripts/export_contracts.py` is the driver that hands both to one export, so
# the index it writes lists every artifact rather than half of them.
#
# Registering a further generator is a `Contract` in whichever registry owns its
# source, and nothing here or in the workflow changes.
contracts:
    {{ uv }} python scripts/export_contracts.py docs/contracts

# Fail when the regenerated contracts differ from the committed copies. The
# `--intent-to-add` is what makes a newly generated file show up as a
# difference: without it an artifact nobody committed is merely untracked, and
# `git diff` reports a clean tree over a drift the gate exists to catch.
contracts-check: contracts
    git add --intent-to-add -- docs/contracts
    git diff --exit-code -- docs/contracts

# --- Provisioning -------------------------------------------------------------
# Ansible runs under this workspace's own environment rather than a system
# install, and `ansible-core` is pinned in the `provisioning` dependency group so
# a runner and a contributor cannot end up on versions that merely look alike.
# The group is deliberately not installed by default: a lint, a type check and a
# test run have no use for an Ansible engine. `uv run --group provisioning` puts
# it there for the two recipes that do, from the same lockfile everything else
# resolves against — and, because it is the workspace environment, the roles'
# filter plugins can import `reachy_checks` and `reachy_contracts` as ordinary
# modules. That is what reachyctl REQ-056 asks for: one definition of what a
# healthy robot is, used by `doctor` and by the verification role alike.

ansible := "uv run --locked --all-packages --group provisioning"

# Where a running container target keeps its ephemeral key, inventory and
# declaration. Untracked; `just provision-target-down` removes it.
target_dir := ".provisioning-target"
target_name := "reachy-provisioning-target"
target_image := "reachy-provisioning-target:dev"

# Lint the playbooks and roles. `ansible-lint` carries its own opinions about
# role layout, task naming and module spelling, which is why it is pinned: an
# unpinned linter is a gate whose verdict changes with no diff here to review.
provision-lint:
    cd provisioning/ansible && {{ ansible }} ansible-lint --offline site.yml remove.yml

# Build the container target and start it, then leave behind everything an
# ordinary `ansible-playbook` run needs to reach it.
#
# The container runs real systemd as PID 1, which is what `--privileged` and the
# cgroup mount are for: `daemon-reload`, `restart` and `systemctl show` are what
# the roles read and write, and a stub `systemctl` would make the gate a test of
# the stub. It is reached over SSH rather than through a container connection
# plugin, so the gate differs from a run against a real robot in as few ways as
# it can.
#
# The key pair is generated per run and thrown away with the directory. Nothing
# resembling a credential is baked into the image or committed anywhere.
provision-target-up:
    #!/usr/bin/env bash
    set -euo pipefail

    docker build --tag '{{ target_image }}' provisioning/ci

    docker rm --force '{{ target_name }}' >/dev/null 2>&1 || true
    rm --recursive --force '{{ target_dir }}'
    mkdir --parents '{{ target_dir }}'
    chmod 0700 '{{ target_dir }}'

    docker run --detach --name '{{ target_name }}' \
        --privileged --cgroupns=host \
        --volume /sys/fs/cgroup:/sys/fs/cgroup:rw \
        --tmpfs /run --tmpfs /run/lock \
        --publish 127.0.0.1::22 \
        '{{ target_image }}' >/dev/null

    port="$(docker port '{{ target_name }}' 22/tcp | head -n 1 | sed 's/.*://')"

    # systemd reports `degraded` when a unit failed and `running` when none did.
    # Both mean it finished starting, which is all this waits for — the units
    # this cares about are checked by the run itself.
    for _ in $(seq 1 60); do
        state="$(docker exec '{{ target_name }}' systemctl is-system-running 2>/dev/null || true)"
        case "$state" in running|degraded) break ;; esac
        sleep 1
    done
    if [ "${state:-}" != running ] && [ "${state:-}" != degraded ]; then
        echo "just provision-target-up: systemd did not finish starting (${state:-no answer})" >&2
        docker logs '{{ target_name }}' >&2 || true
        exit 1
    fi

    ssh-keygen -t ed25519 -N '' -C 'reachy-provisioning-target' \
        -f '{{ target_dir }}/id' -q
    docker cp '{{ target_dir }}/id.pub' \
        '{{ target_name }}:/home/reachy/.ssh/authorized_keys' >/dev/null
    docker exec '{{ target_name }}' chown reachy:reachy /home/reachy/.ssh/authorized_keys
    docker exec '{{ target_name }}' chmod 0600 /home/reachy/.ssh/authorized_keys

    # Host-key verification stays on, exactly as it does against a robot. The
    # container's key is new every run, so the run records it rather than
    # switching the check off — there is no option in the playbook that does.
    #
    # Retried, and the file checked for content. `ssh-keyscan` exits 0 whether or
    # not it collected anything — its own documentation says the status only
    # reports usage errors — and `systemctl is-system-running` says the boot
    # finished, not that sshd is accepting. An empty known_hosts would fail every
    # later run on host-key verification, several minutes and one confusing
    # message away from the recipe that produced it.
    for _ in $(seq 1 30); do
        ssh-keyscan -p "$port" 127.0.0.1 > '{{ target_dir }}/known_hosts' 2>/dev/null || true
        [ -s '{{ target_dir }}/known_hosts' ] && break
        sleep 1
    done
    if [ ! -s '{{ target_dir }}/known_hosts' ]; then
        echo "just provision-target-up: no host key came back from the target on port $port, so nothing could verify it" >&2
        docker logs '{{ target_name }}' >&2 || true
        exit 1
    fi

    # A real wheel with nothing in it, built in memory by the helper `reachyctl`
    # already uses for the same purpose. Installing it is what exercises the
    # `app_install` role, and using a distribution that is obviously not the
    # satellite is the point: the role installs a wheel from a configured source
    # rather than a particular application.
    wheel="$({{ ansible }} python - '{{ target_dir }}' <<'PY'
    import sys
    from pathlib import Path

    sys.path.insert(0, "cli/reachyctl/tests/support")
    from reachyctl_fixture_wheel import (  # noqa: E402 - path set above
        FIXTURE_DISTRIBUTION,
        FIXTURE_VERSION,
        fixture_wheel,
    )

    name, body = fixture_wheel()
    Path(sys.argv[1], name).write_bytes(body)
    print(f"{name} {FIXTURE_DISTRIBUTION} {FIXTURE_VERSION}")
    PY
    )"
    set -- $wheel

    cat > '{{ target_dir }}/inventory.ini' <<EOF
    [reachy]
    target ansible_host=127.0.0.1 ansible_port=$port

    [reachy:vars]
    ansible_user=reachy
    ansible_become=true
    ansible_ssh_private_key_file=$PWD/{{ target_dir }}/id
    ansible_ssh_common_args=-o UserKnownHostsFile=$PWD/{{ target_dir }}/known_hosts -o StrictHostKeyChecking=yes
    EOF

    # The declaration the gate applies. Every value here is a placeholder — the
    # address is from RFC 5737's TEST-NET-1 range and the credential lasts as
    # long as one container. See the root AGENTS.md on what may enter a tracked
    # file; this one is untracked, and it is written here rather than committed
    # so that the same rule is visible in the recipe that produces it.
    cat > '{{ target_dir }}/declaration.yml' <<EOF
    ---
    reachy_settings:
      REACHY_HOME_ASSISTANT_IDENTITY: Reachy Mini Example
      REACHY_SATELLITE_LOG_LEVEL: info
      REACHY_SATELLITE_FRAME_INTERVAL_MS: 100
    reachy_groundstation_url: ws://192.0.2.10:8000/v1/session
    reachy_groundstation_credential: example-credential
    reachy_app_distribution: $2
    reachy_app_wheel_path: $PWD/{{ target_dir }}/$1
    EOF

    echo
    echo "The container target is up on 127.0.0.1:$port."
    echo "  just provision-run site.yml            # apply"
    echo "  just provision-run site.yml --check    # preview, change nothing"
    echo "  just provision-run remove.yml          # undo it"
    echo "  just provision-target-down             # stop and remove it"

# Run a playbook against the container target `just provision-target-up` left
# behind. Everything after the playbook name is passed to `ansible-playbook`, so
# `--check`, `--diff`, `--tags` and `-v` all work.
provision-run playbook="site.yml" *args:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ ! -f '{{ target_dir }}/inventory.ini' ]; then
        echo 'just provision-run: no container target; run `just provision-target-up` first' >&2
        exit 1
    fi
    cd provisioning/ansible && {{ ansible }} ansible-playbook \
        --inventory "$OLDPWD/{{ target_dir }}/inventory.ini" \
        --extra-vars "@$OLDPWD/{{ target_dir }}/declaration.yml" \
        '{{ playbook }}' {{ args }}

# Stop the container target and forget everything about it.
provision-target-down:
    -docker rm --force '{{ target_name }}' >/dev/null 2>&1
    rm --recursive --force '{{ target_dir }}'

# The idempotency gate: apply the playbook twice and fail on any changed step in
# the second application.
#
# This is provisioning REQ-061, and it is the check the whole change is built
# around. Idempotency is the property that decays first as roles are edited and
# the one that is invisible without a gate: a task written non-idempotently
# works, converges the robot, and then reports a change on every run forever
# after. Nobody notices until a run that was supposed to change nothing restarts
# the daemon in the middle of a conversation.
#
# The recap is what is read, because it is what an operator reads. A second
# application that reports `changed=0` for every host is the requirement, and
# `failed` and `unreachable` are checked alongside it so a run that fell over
# cannot be mistaken for a run that changed nothing.
provision-idempotency: provision-target-up
    #!/usr/bin/env bash
    set -euo pipefail

    echo '=== first application: this one is expected to change things ==='
    just provision-run site.yml

    echo
    echo '=== second application: this one must change nothing ==='
    second="$(just provision-run site.yml 2>&1 | tee /dev/stderr)"

    recap="$(printf '%s\n' "$second" | sed -n '/PLAY RECAP/,$p')"
    if [ -z "$recap" ]; then
        echo 'just provision-idempotency: the second application printed no recap' >&2
        exit 1
    fi

    printf '%s\n' "$recap" | awk '
        /changed=/ {
            for (i = 1; i <= NF; i++) {
                split($i, pair, "=")
                if (pair[1] == "changed" || pair[1] == "failed" || pair[1] == "unreachable") {
                    total[pair[1]] += pair[2]
                }
            }
            hosts++
        }
        END {
            if (hosts == 0) {
                print "just provision-idempotency: the recap named no host" > "/dev/stderr"
                exit 1
            }
            if (total["failed"] > 0 || total["unreachable"] > 0) {
                printf "just provision-idempotency: the second application failed (failed=%d unreachable=%d)\n", total["failed"], total["unreachable"] > "/dev/stderr"
                exit 1
            }
            if (total["changed"] > 0) {
                printf "just provision-idempotency: the second application reported changed=%d. This gate is provisioning REQ-061, and the property it enforces is REQ-060: a run against an already-provisioned robot changes nothing. Find the task the recap counted and make it compare before it writes\n", total["changed"] > "/dev/stderr"
                exit 1
            }
            printf "the second application changed nothing across %d host(s)\n", hosts
        }
    '
