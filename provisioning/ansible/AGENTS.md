# provisioning/ansible

Idempotent provisioning from a stock Reachy Mini image to a configured robot.

**Spec:** [provisioning](../../docs/specs/provisioning/).
**Implemented by:** [0010](../../docs/changes/0010-provisioning-ansible.md).

Read the root [`AGENTS.md`](../../AGENTS.md) first — it holds the invariants
that apply here.

This is not a Python workspace member: it has no `pyproject.toml` and nothing
here is installed by `uv sync`. It is a directory of Ansible content, plus the
filter plugins under `plugins/filter/` that the roles reach their Python
through — those are ordinary modules, linted, type-checked, tested and
diff-covered with the rest of the repository, and the root `pyproject.toml` says
where each tool picks them up.

```
site.yml     apply           remove.yml   undo           ansible.cfg   the run's own configuration
roles/daemon_env             the managed drop-in on the daemon unit
roles/app_install            a wheel from a configured source
roles/groundstation_link     the endpoint and the credential
roles/verify                 the shared checks, and the failure when they fail
plugins/filter/              the region's format, the wheel's name, the check run
```

```
just provision-lint          # ansible-lint over the playbooks and roles
just provision-target-up     # build and start the container target
just provision-run site.yml --check
just provision-idempotency   # apply twice; fail on any change in the second
just provision-target-down
```

Ansible runs from this workspace's own environment — `ansible-core` is the
`provisioning` dependency group in the root `pyproject.toml` — which is what
lets the plugins import `reachy_checks` and `reachy_contracts` rather than
reimplementing what they hold.

## Local rules

- **The only tracked inventory is an example.** `inventory.example.ini` is the
  shape; the filled-in `inventory.ini` is ignored by version control and is
  never committed. Addresses in the example use RFC 5737 reserved ranges and
  placeholder names.
- **`group_vars/all.yml` holds neutral defaults**, never a real endpoint,
  account or credential. The groundstation credential reaches the robot through
  Ansible's own secret handling — `--extra-vars @secrets.yml` over an
  `ansible-vault` file, never a `NAME=VALUE` argument — and every task whose
  output could carry the daemon's environment or the daemon's own free text
  carries `no_log`.
- **The managed drop-in is one file with two writers.**
  [`docs/ops/managed-daemon-environment.md`](../../docs/ops/managed-daemon-environment.md)
  is the byte-level contract, `reachyctl config apply` is the other
  implementation, and `provisioning/tests/` renders both and compares them.
  Neither imports the other, and a test asserts that: two implementations that
  shared a renderer would agree about everything and prove nothing. **Change
  this format only by changing that document, and expect to change both sides.**
- **`daemon_env` owns its region in full.** Nothing in the file is preserved,
  merged with or appended to, which is what takes a withdrawn setting off the
  robot. A file the format could not have written is refused by name rather than
  replaced.
- **Every play is idempotent.** A second run changes nothing, and
  `just provision-idempotency` enforces that with a repeat run against a
  container rather than trusting it. Anything that reads is `changed_when:
  false`; anything that writes compares first.
- **Verification is shared with `reachyctl doctor`.** The `verify` role imports
  `reachy_checks` and runs `CHECKS`. It declares no check of its own and does
  not shell out to the CLI — a role that did either would need a CLI
  installation on the control machine, or would hold a second notion of healthy.
- **A failed verification fails the run.** Provisioning does not report success
  over a robot that does not work.
- **Two rules are switched off in `.ansible-lint`**, each with its identifier
  and its reason, the same standard the root `AGENTS.md` sets for a `# noqa`.
