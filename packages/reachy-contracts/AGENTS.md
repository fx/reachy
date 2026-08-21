# packages/reachy-contracts

Shared wire types and golden fixtures. Distribution `reachy-contracts`, import
name `reachy_contracts`.

**Spec:** [robot-link](../../docs/specs/robot-link/) owns the wire contract.
**Filled in by:** [0003](../../docs/changes/0003-contracts-package.md).

Read the root [`AGENTS.md`](../../AGENTS.md) first — it holds the invariants
that apply here.

## Layout

| Path | What it holds |
|---|---|
| `src/reachy_contracts/session.py` | Negotiation, the frame header, the result envelope, errors and close |
| `src/reachy_contracts/values.py` | The wire model base, normalised coordinates, the capture token, and the per-capability payloads |
| `src/reachy_contracts/fixtures.py` | The corpus manifest and the loader every consumer reads it through |
| `src/reachy_contracts/golden/` | The golden fixtures themselves, one file per message type |
| `src/reachy_contracts/contracts_export.py` | The registry `just contracts` renders `docs/contracts/` from |

## Local rules

- **This package is the single definition of every wire type.** A type
  duplicated in a consumer is the drift this package exists to prevent. If the
  groundstation and `reachyctl` both need a shape, it lives here. The rule is
  enforced rather than advised: ruff's `TID253` bans importing `pydantic` at
  module level everywhere else in the workspace, and this package is the only
  path exempted from it in the root `pyproject.toml`. Naming the module rather
  than the model bases covers every way to declare one, and it leaves `TID251`
  free for the vendored ESPHome boundary, which scopes that rule to a single
  directory and would otherwise switch a pydantic ban off everywhere.
- **A capability is data, never a type.** `Capability` is a name and a version;
  `ResultEnvelope` is generic in its payload. Adding one means adding a payload
  type and a row in `CAPABILITY_PAYLOADS`, and changing nothing in `session.py`.
- **The capture timestamp is an opaque token.** `CaptureTimestamp` stores
  characters and offers no way to parse, order or compare them, because only the
  machine that minted the value has a clock it means anything against. Anything
  that turns it into an instant on the way through breaks robot-link REQ-016.
- **Every message type has a golden fixture, and the round-trip is
  byte-identical.** Add a message type, add a file in `golden/`, add its row to
  `FIXTURES`, and add its schema to `_MESSAGE_TYPES` in `contracts_export.py`.
  Never generate a fixture from the code it pins.
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
