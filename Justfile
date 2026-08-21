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
lint: lint-boundary lint-capability-boundary
    {{ uv }} ruff check .
    {{ uv }} ruff format --check .

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

# Regenerate every published schema and interface description from source. The
# generators are registered in `reachy_contracts.contracts_export`; the registry
# is empty until the wire types exist, and the index it writes records that.
contracts:
    {{ uv }} python -m reachy_contracts.contracts_export docs/contracts

# Fail when the regenerated contracts differ from the committed copies. The
# `--intent-to-add` is what makes a newly generated file show up as a
# difference: without it an artifact nobody committed is merely untracked, and
# `git diff` reports a clean tree over a drift the gate exists to catch.
contracts-check: contracts
    git add --intent-to-add -- docs/contracts
    git diff --exit-code -- docs/contracts
