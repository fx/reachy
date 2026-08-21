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

# Everything that gates a merge.
check: lint typecheck test

# Coverage of the lines this branch changed, rather than of the whole tree, so
# a large untested area cannot mask a new one. Requires `just test` to have
# written coverage.xml first; the continuous integration job that enforces the
# threshold on pull requests is wired in change 0002.
coverage-diff base="origin/main":
    {{ uv }} diff-cover coverage.xml --compare-branch={{ base }} --fail-under=90

# Verify that every requirement annotation still agrees with the specs.
duvet:
    duvet report --ci
