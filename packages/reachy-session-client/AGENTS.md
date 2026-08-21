# packages/reachy-session-client

The client half of the [robot link](../../docs/specs/robot-link/) session
protocol. Distribution name `reachy-session-client`, import name
`reachy_session_client`.

**Spec:** [robot-link](../../docs/specs/robot-link/).
**Created by:** [0007](../../docs/changes/0007-reachyctl-probe.md).
**Also consumed by:** [0012](../../docs/changes/0012-satellite-ports-and-adapters.md).

Read the root [`AGENTS.md`](../../AGENTS.md) first — it holds the invariants
that apply here.

## Why this is a member and not CLI code

reachyctl REQ-057 requires `probe` to establish its session with the same
protocol implementation the robot application uses. A client living inside
`reachyctl` would either be imported by the robot — which would put a CLI on the
robot's application environment — or copied, and a copy that behaves similarly
to the real one tests the copy.

So there is exactly **one** implementation, it lives here, and both `reachyctl
probe` and the robot's groundstation adapter import it. There is no second
"lightweight client for testing" anywhere in this repository, and adding one is
the change that makes REQ-057 false.

## Local rules

- **Declare no wire type.** Every message on the link is declared in
  `reachy-contracts` and imported from `reachy_contracts`. The TID253 ban in the
  root `pyproject.toml` applies here in full, and this package carries no
  per-file ignore for it: a module here that needs pydantic is a module about to
  redeclare somebody else's contract.
- **Keep the dependency list short.** Everything installed here is installed on
  the robot, alongside the Reachy Mini SDK and the daemon, in an application
  environment this package does not own. Two dependencies today; adding a third
  is a decision, not a convenience.
- **No logging framework, no printing.** This package is a library used by a
  daemon and by a CLI with its own output conventions. What happened is
  observable through `SessionStats` and through the results themselves, so the
  consumer decides what to say and where.
- **The credential never reaches a string.** It is held in `Credential`, whose
  `repr` and `str` are redacted, and **inside this package** it is revealed at
  exactly one call site — building the offer. No exception raised here embeds a
  pydantic validation message from a model that carries one;
  `describe_validation` is what those paths report instead.

  A consumer may have one reveal site of its own, and exactly one reason for it:
  handing the value to whatever scrubs its output, which cannot remove a string
  it was never given. `reachyctl` does this once, in
  `cli/reachyctl/src/reachyctl/cli.py`, to seed its `Redactor`. That call is what
  makes reachyctl REQ-059 hold on the paths nobody controls — the text of an
  exception raised three libraries down — so it is not a leak to be tidied away,
  and deleting it would silently remove the protection rather than tighten it.
- **`framing.py` mirrors the groundstation's.** The two ends of one transport
  need the same packing, and the groundstation's copy predates this package.
  `test_session_client_framing.py` pins them together by asserting that both
  encoders produce identical bytes and that each side decodes the other's, so
  drift fails a test rather than a robot. Folding the two into one module is a
  change of its own — see the change document's open questions.
