# packages/reachy-contracts

Shared wire types and golden fixtures. Distribution `reachy-contracts`, import
name `reachy_contracts`.

**Spec:** [robot-link](../../docs/specs/robot-link/) owns the wire contract.
**Fills this in:** [0003](../../docs/changes/0003-contracts-package.md).

Read the root [`AGENTS.md`](../../AGENTS.md) first — it holds the invariants
that apply here.

## Local rules

- **This package is the single definition of every wire type.** A type
  duplicated in a consumer is the drift this package exists to prevent. If the
  groundstation and `reachyctl` both need a shape, it lives here.
- **No dependencies on other members.** Everything else depends on this package;
  a dependency in the other direction is a cycle.
- **Keep it dependency-light.** It is installed on the robot, in the
  groundstation image and in the CLI wheel alike, so anything added here is
  added to all three.
- **This package holds the repository-wide version.**
  `src/reachy_contracts/version.py` declares `__version__` as a bare assignment
  because the build backend reads that line with a regular expression to derive
  the distribution metadata. An annotation on it breaks the build.
- **Golden fixtures are contract, not test data.** Once a fixture pins a wire
  shape, changing it is a protocol change and belongs in a change document.
