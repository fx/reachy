# reachy

Monorepo for the Reachy Mini robot: the robot-side Home Assistant voice
satellite, the off-robot groundstation service, the `reachyctl` CLI, and
reproducible provisioning.

Start at [`docs/index.md`](docs/index.md) for specs and change documents.

Nothing is implemented yet. The specs describe the intended end state and the
change documents sequence the work; `docs/changes/0001-workspace-skeleton.md` is
the only one with no dependencies.

⚠️ **The "Requirements traceability" check currently passes vacuously.** No spec
is registered in `.duvet/config.toml`, so duvet loads zero specifications and
exits 0 having checked nothing. A green run is not evidence that any requirement
is traced. The header comment in that file explains why they are deliberately
unregistered and when to register them.

## Task Tracking

**You MUST load the `/project-management` skill before creating, modifying, or completing any task.** It owns all task-tracking rules and knows where tasks belong. Do not manage tasks without it.

## Code Review Rules

Read `REVIEW.md` at the repository root and apply it in full as the review rules for this repo. It is the canonical review-conventions file.
