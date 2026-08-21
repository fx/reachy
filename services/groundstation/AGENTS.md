# services/groundstation

The off-robot service that hosts heavy computation as pluggable capabilities.
Distribution `reachy-groundstation`, import name `reachy_groundstation`.

**Spec:** [groundstation](../../docs/specs/groundstation/), with
[perception](../../docs/specs/perception/) for the first capability.
**Fills this in:** [0004](../../docs/changes/0004-groundstation-session.md),
then [0005](../../docs/changes/0005-perception-capability.md) and
[0006](../../docs/changes/0006-groundstation-images.md).

Read the root [`AGENTS.md`](../../AGENTS.md) first — it holds the invariants
that apply here.

## Local rules

- **This is a scaffold.** It has a `pyproject.toml`, this file and an empty
  package. Do not add implementation ahead of the change that owns it.
- **Capabilities sit behind the capability boundary.** That seam is where a
  native implementation of a compute-bound loop would drop in, so it stays
  clean; see the Decision Records in the architecture spec.
- **Wire types come from `reachy-contracts`.** Never redefine one locally.
- **Configuration fails loud.** An unrecognised variable matching this
  component's prefix stops startup, and the resolved configuration is emitted at
  startup with every secret shown as set or unset, never by value.
- **No test may need a camera or a GPU.** Model runtimes are reached through an
  interface and exercised with a fake.
