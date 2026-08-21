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

- **`probe`, `doctor` and `bench` are the commands that exist.** `bench` is a
  registered name with no body: 0014 gives it one. `deploy`, `config` and `app`
  arrive in 0009. Do not add implementation ahead of the change that owns it.
- **`doctor` decides nothing about what healthy means.** The checks are
  declared in `reachy-checks`, which the provisioning verification role imports
  too — see reachyctl REQ-056. A check added to this command rather than to
  that registry is a check provisioning will never perform.
- **Never reimplement the session protocol.** `probe` is a second *consumer* of
  `reachy_session_client`, which holds the one implementation of the robot
  link's client half and is imported by the robot application too. A second
  implementation is the drift the contracts package exists to prevent — and the
  reason this tool stays in Python rather than becoming a compiled binary.
- **The output conventions are set once, in `output.py` and `exits.py`.** A new
  command builds a `Report` and returns a `Reporter`'s exit code; it does not
  print, does not choose a format and does not invent an exit status. Every
  string leaves through `Reporter`, which scrubs it — that is what makes
  REQ-059 a rule rather than a habit each command has to remember.
- **No option ever takes a credential.** An argument is visible in the process
  list and lands in the shell history. `--credential-file` takes a path, and
  `REACHYCTL_CREDENTIAL` and `REACHYCTL_CREDENTIAL_FILE` are the other two ways
  in.
- **Diagnosis and provisioning agree on what healthy means.** `doctor` asserts
  against a shared check registry, not against its own private list; the Ansible
  verification role asserts the same conditions.
- **`reachyctl` wraps provisioning, it does not replace it.** Anything
  declarative belongs in `provisioning/ansible/`.
- **No robot address is ever tracked.** Targets come from arguments or from an
  untracked local file with a tracked `.example` sibling.
