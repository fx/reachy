# cli/reachyctl

The command-line tool for deploying, configuring and diagnosing a robot.
Distribution and import name `reachyctl`.

**Spec:** [reachyctl](../../docs/specs/reachyctl/).
**Fills this in:** [0007](../../docs/changes/0007-reachyctl-probe.md),
[0008](../../docs/changes/0008-reachyctl-doctor.md) and
[0009](../../docs/changes/0009-reachyctl-deploy-and-config.md).

Read the root [`AGENTS.md`](../../AGENTS.md) first — it holds the invariants
that apply here.

## Local rules

- **This is a scaffold.** It has a `pyproject.toml`, this file and an empty
  package. Do not add implementation ahead of the change that owns it.
- **Never reimplement the session protocol.** `probe` is a second client of the
  same contract the groundstation speaks, and it uses the shared session client
  built on `reachy-contracts`. A second implementation is the drift the
  contracts package exists to prevent — and the reason this tool stays in
  Python rather than becoming a compiled binary.
- **Diagnosis and provisioning agree on what healthy means.** `doctor` asserts
  against a shared check registry, not against its own private list; the Ansible
  verification role asserts the same conditions.
- **`reachyctl` wraps provisioning, it does not replace it.** Anything
  declarative belongs in `provisioning/ansible/`.
- **No robot address is ever tracked.** Targets come from arguments or from an
  untracked local file with a tracked `.example` sibling.
