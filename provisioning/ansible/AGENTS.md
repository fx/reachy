# provisioning/ansible

Idempotent provisioning from a stock Reachy Mini image to a configured robot.

**Spec:** [provisioning](../../docs/specs/provisioning/).
**Fills this in:** [0010](../../docs/changes/0010-provisioning-ansible.md).

Read the root [`AGENTS.md`](../../AGENTS.md) first — it holds the invariants
that apply here.

This is not a Python workspace member: it has no `pyproject.toml` and nothing
here is installed by `uv sync`. It is a directory of Ansible content, and today
it is an empty skeleton — `roles/` and `group_vars/` exist so the layout the
spec describes is visible before anything fills it.

## Local rules

- **The only tracked inventory is an example.** Change 0010 adds
  `inventory.example.ini`; neither it nor anything else here exists yet. The
  filled-in `inventory.ini` is already ignored by version control and is never
  committed. Addresses in the example use RFC 5737 reserved ranges and
  placeholder names.
- **`group_vars/all.yml`, when 0010 adds it, holds neutral defaults**, never a
  real endpoint, account or credential.
- **Every play is idempotent.** A second run changes nothing, and continuous
  integration enforces that with a repeat run rather than trusting it.
- **Verification is shared with `reachyctl doctor`.** The verify role asserts
  the same conditions the diagnostic command asserts, so the two cannot disagree
  about what a healthy robot is.
- **A failed verification fails the run.** Provisioning does not report success
  over a robot that does not work.
