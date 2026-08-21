# packages/reachy-checks

The one definition of what a healthy installation is. Distribution name
`reachy-checks`, import name `reachy_checks`.

**Spec:** [reachyctl](../../docs/specs/reachyctl/) owns the diagnosis
behaviour; [provisioning](../../docs/specs/provisioning/) owns the verification
that consumes the same definitions.
**Fills this in:** [0008](../../docs/changes/0008-reachyctl-doctor.md).
**Consumes it:** [0010](../../docs/changes/0010-provisioning-ansible.md) — the
Ansible verification role imports this package. It does not shell out to
`reachyctl`.

Read the root [`AGENTS.md`](../../AGENTS.md) first — it holds the invariants
that apply here.

## Local rules

- **This is a member, not a module inside the CLI, and that is the whole
  point.** reachyctl REQ-056 requires diagnosis and provisioning to assert the
  same conditions from one source. A control machine running an Ansible play
  may not have the CLI installed, so a registry living inside `reachyctl` would
  force provisioning to either install it or write the checks a second time —
  and two independently written notions of "healthy" drift into a robot that
  provisioning calls fine and diagnosis calls broken.
- **A check is data.** Identifier, description, what it needs, the probe that
  runs it, and the remediation. Nothing about how a result is rendered, which
  stream it goes to, or what a process exits with belongs here: those are the
  consumer's, and `reachyctl` already decided them in `output.py` and
  `exits.py`.
- **Identifiers and remediation strings are a published interface.** The
  troubleshooting runbook in
  [0015](../../docs/changes/0015-documentation-and-runbooks.md) is keyed to the
  identifiers and shares the remediation text rather than restating it, so
  renaming one is a breaking change and needs the runbook updated in the same
  pull request.
- **A remediation is a runnable command wherever one exists.** Where none
  exists, `Remediation.command` is empty and the explanation says what to do
  instead. Inventing a command that does not exist is worse than admitting
  there is not one.
- **Checks are independent.** The runner executes every check whatever the ones
  before it did, and one that raises becomes a failed result rather than a run
  that stopped. An operator with a broken groundstation still learns whether
  the daemon is healthy.
- **Skipped is not failed.** A check whose prerequisites are absent — no robot
  connection, no groundstation configured, no model directory — is skipped. An
  operator who has not configured a groundstation is not in an error state, and
  reporting one trains people to ignore the output.
- **Nothing here reads the environment, prints, or opens anything at import
  time.** The concrete adapters in `link.py` and `files.py` do input and output
  when they are called; everything else is pure and the probes reach the world
  only through the ports.
- **No value belonging to anyone's environment.** Remediation text, docstrings
  and examples are published in a public repository: addresses come from RFC
  5737 reserved ranges and names are placeholders.
