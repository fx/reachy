# The managed daemon environment

Two independent implementations write one file on the robot: `reachyctl config
apply` and the Ansible `daemon_env` role. This document is what they are both
written against. It is not a description of what the tool happens to do — a
contract test renders the tool's own output and compares it with the block
below, so a change to one without the other is a red run.

## Where it is

```
/etc/systemd/system/reachy-mini-daemon.service.d/10-reachy-managed.conf
```

- The unit is the **daemon**, not the application. The application inherits its
  environment from the daemon, which is also why applying a change requires the
  daemon to restart — see the
  [provisioning spec](../specs/provisioning/index.md#daemon-environment).
- The drop-in directory is created if it is missing (`mkdir --parents`).
- The file is installed with mode `0644`, owner `root`, group `root`.
- Other drop-ins in the same `.d/` directory belong to whoever put them there.
  Neither implementation reads them and neither writes them.
- After writing, `systemctl daemon-reload` runs; putting the change **in force**
  additionally requires `systemctl restart reachy-mini-daemon.service`.

## Ownership rule

**The whole file is owned.** Every line of it is written by whichever side
applied last. Nothing in it is preserved, merged with, or appended to.

That is [provisioning REQ-063](../specs/provisioning/index.md#req-063-the-managed-configuration-is-fully-owned):
a setting removed from the declaration is removed from the robot rather than
left behind. An implementation that appends works perfectly until the first time
somebody deletes a setting, and then the file that is supposed to describe the
robot no longer does.

An operator who edits this file by hand loses the edit on the next apply. That is
what the header says, in the file, in those words.

## Exact shape

Reproduce these bytes. The markers are literal, the section header is literal,
the header comment is literal, and the file ends with a newline.

```ini
# This file is generated and is owned in full by the Reachy tooling.
# `reachyctl config apply` and the Ansible daemon_env role both rewrite it
# whole, so an edit made here by hand is lost on the next apply, and a
# setting removed from the declaration is removed from the robot rather
# than left behind. See docs/ops/managed-daemon-environment.md.
[Service]
# >>> reachy managed environment >>>
Environment="REACHY_GROUNDSTATION_URL=ws://192.0.2.10:8000/v1/session"
Environment="REACHY_SATELLITE_LOG_LEVEL=info"
# <<< reachy managed environment <<<
```

The two settings above are an example. What is normative is everything else: the
five header lines, `[Service]`, the two markers, and the form of an
`Environment=` line.

## Rules the format imposes

- **One `Environment=` line per setting**, with the whole `NAME=value`
  assignment inside one pair of double quotes. A name assigned twice makes the
  region unreadable — taking either one silently discards a value.
- **Settings appear in name order.** Two applies of the same declaration
  therefore produce byte-identical files, which is what makes
  [REQ-060](../specs/provisioning/index.md#req-060-applying-twice-changes-nothing-the-second-time)
  a property of the format rather than something each implementation has to
  remember.
- **Inside the quotes, `\` becomes `\\` and `"` becomes `\"`.** The backslash is
  escaped first. Nothing else is escaped.
- **A value may not contain a control character.** A line break would end the
  directive it is on, and the rest of the value would become a directive of its
  own. `reachy_contracts.validate_settings` refuses one before anything is
  written; the writer does not re-check, so the validator is not optional.
- **A reader accepts only what it could have written, and checks that in closed
  form.** `reachyctl` parses the region and then re-renders it: unless the result
  is the file it was given, byte for byte, the file is refused. So every rule on
  this page is load-bearing for a reader as well as a writer — the name order,
  the escaping, the `\n` line endings, one line per setting, no blank lines
  inside the region, the exact header. **A `daemon_env` role whose output differs
  from this page in any respect produces a region `reachyctl` will refuse**, and
  that is the intended direction: a reader that tolerated a disagreement would
  let the two implementations overwrite each other's files instead of agreeing.
  The specific refusals below still exist, because they say *which* line is
  wrong; the round trip is what makes the check complete.
- **Absent, empty and unreadable are three states, and an implementation must
  answer with the one it found.** This is the rule the whole ownership model
  rests on, because a region read as ours is a region the next apply rewrites.

  | State | What it means | What to do |
  |---|---|---|
  | The file does not exist | Nothing has been applied to this robot | Proceed; write it |
  | The file exists and carries a readable region | This tooling wrote it | Proceed; converge it |
  | The file exists and is empty, or its markers are missing, unpaired or out of order | Something else wrote it, or emptied it | **Refuse**, naming the path |

  **An existing empty file belongs in the third row, not the first.** This format
  never writes an empty file: withdrawing every setting still writes the header,
  `[Service]` and both markers, so a region with nothing in it is nine lines
  long. A blank file is therefore one something else blanked, and collapsing it
  into "never written" means the next apply silently replaces it — in exactly
  the case where an operator most needs to be told.
- **The markers delimit the region a reader parses.** They are not how ownership
  is decided — ownership is the file — but a file whose markers are missing,
  unpaired, or out of order is reported as unreadable rather than silently
  treated as empty, per the table above.
- **A line between the markers that is not an `Environment=` assignment this
  format writes** makes the region unreadable, for the same reason. That includes
  a blank one: this format does not write them.

## The vocabulary

The setting names and the values each will accept are declared once, in
`reachy_contracts.settings` (`ROBOT_SETTINGS`). Both implementations validate
against it before writing, so a value the robot would refuse costs no round trip
— [reachyctl REQ-053](../specs/reachyctl/index.md#req-053-configuration-values-are-validated-before-they-are-sent).
A setting marked `secret` there is never rendered into any output by value; it is
reported as set or unset.

## Reading the effective environment

The file is what was declared. What is **in force** is
`systemctl show reachy-mini-daemon.service --property=Environment --value`,
which is the whole environment the unit ended up with, whichever drop-in or unit
file put it there. The two are compared rather than assumed equal: a setting that
is in the file and not in force is the silently-inert configuration the reachyctl
spec's background names, and only the comparison finds it.
