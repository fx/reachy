# cli/reachyctl

The command-line tool for deploying, configuring and diagnosing a robot.
Distribution and import name `reachyctl`.

**Spec:** [reachyctl](../../docs/specs/reachyctl/).
**Filled in by:** [0007](../../docs/changes/0007-reachyctl-probe.md),
[0008](../../docs/changes/0008-reachyctl-doctor.md) and
[0009](../../docs/changes/0009-reachyctl-deploy-and-config.md). Every command the
spec names exists except `bench`, which is [0014](../../docs/changes/0014-benchmarks-and-gates.md).

Read the root [`AGENTS.md`](../../AGENTS.md) first — it holds the invariants
that apply here.

## Local rules

- **`bench` is a registered name with no body.** 0014 gives it one. Everything
  else the spec's command table names is implemented. Do not add implementation
  ahead of the change that owns it.
- **`deploy` is defined over a WHEEL, not over the satellite.** It builds a
  named workspace member or accepts one by path, which is what let it be
  written — and exercised, against a fixture wheel with no application in it —
  before the satellite existed. Do not hard-code an application into it.
- **A deploy's answer is the version the robot reports afterwards.** Never an
  install's exit status: the failure this command exists to catch is a package
  that installed successfully into an environment the running daemon was not
  using, and every step of that deploy exits zero. The same reasoning governs
  `app start`, `app stop` and `config apply` — each ends by asking the shared
  check registry what the robot is actually doing.
- **Every command that modifies robot state takes `--preview`**, and a preview
  issues no mutating command at all. The test for one asserts the robot's
  after-state, never that a plan was printed: a command that printed a perfect
  plan and then applied it anyway would pass the second test.
- **Configuration is validated locally, before anything is contacted.** The
  vocabulary is `reachy_contracts.settings`, shared with provisioning, so a
  value the robot would refuse costs no round trip. A second copy of a
  constraint here is a tool that accepts what the robot rejects.
- **The managed drop-in is owned in full and its shape is a contract.**
  `reachyctl.managed` holds it and
  [`docs/ops/managed-daemon-environment.md`](../../docs/ops/managed-daemon-environment.md)
  quotes it byte for byte, because change 0010's Ansible role writes the same
  file. A contract test compares the two. Changing one without the other is how
  two tools start reverting each other's applies.
- **The robot is reached in process, over one connection.** `reachyctl.ssh` is
  the transport and `reachyctl.robot` is the seam it sits behind; nothing above
  that seam knows what an SSH error looks like. A command is a list of
  arguments, quoted once by the transport — never a shell line assembled here.
- **Text a robot wrote is never transformed before it is scrubbed.** Not
  truncated, not joined, not shortened. Every one of those can cut a credential
  in half, after which the redactor matches nothing and reports success while
  the secret goes out in pieces. `CommandOutcome.complaint` quotes verbatim and
  says why.
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
