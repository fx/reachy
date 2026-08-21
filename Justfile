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
lint:
    {{ uv }} ruff check .
    {{ uv }} ruff format --check .

# Apply formatting and the lint fixes that are safe to apply automatically.
fmt:
    {{ uv }} ruff check --fix .
    {{ uv }} ruff format .

# Type-check every member in strict mode.
typecheck:
    {{ uv }} mypy

# The three gates a contributor runs before pushing. Not every merge gate: the
# contract-drift check writes to the index and the leak scan needs a commit
# range, so both stay their own recipe rather than making this one mutate
# anything or guess what to compare against.
check: lint typecheck test

# Coverage of the lines this branch changed, rather than of the whole tree, so
# a large untested area cannot mask a new one. Requires `just test` to have
# written coverage.xml first.
coverage-diff base="origin/main":
    {{ uv }} diff-cover coverage.xml --compare-branch={{ base }} --fail-under=90

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
