# Setting up the robot

Bringing a stock Reachy Mini to a configured state: the satellite installed, the
groundstation link declared, the daemon's environment converged, and the whole
thing asserted rather than assumed.

**Read [the Home Assistant runbook's identity warning](home-assistant.md#-the-one-thing-that-cannot-be-undone-the-announced-identity)
before you deploy anything.** `REACHY_SATELLITE_DEVICE_NAME` has no default and
choosing it wrongly on an upgrade detaches every entity's history in Home
Assistant. There is no repair for that after the fact worth the name.

- **You need:** a robot on the network, an account on it with `sudo`, a
  [running groundstation](groundstation.md), and this repository checked out on
  the machine you are working from.
- **You get:** a robot running the satellite, with `doctor` reporting every link
  as passed.
- **Then go to:** [Home Assistant](home-assistant.md).

---

## ⏳ How much of this page has been executed

Nothing in this repository has a Reachy Mini attached, so the steps that talk to
a robot **have not been run against one**. They are marked, individually, like
this:

> **⏳ PENDING HARDWARE VERIFICATION.** No expected output is recorded for this
> step, because the command has never been run against a robot. Nothing below is
> a transcript.

What *has* been executed, and is transcribed verbatim, is every step that runs
off the robot: the playbook against the container target described in
[`provisioning/ci/README.md`](../../provisioning/ci/README.md), the wheel builds,
and every `doctor` and `probe` run that does not need `--robot`. The container
target runs real systemd, a real daemon unit, a real application environment and
a real SSH transport, so what it proves is that the roles converge and stay
converged; what it cannot prove is anything about hardware, the vendor's real
daemon, or aarch64.

The outstanding list is [`docs/tasks.md`](../tasks.md).

---

## Two paths, and when each is right

| | `reachyctl provision` | `reachyctl deploy` |
|---|---|---|
| What it is | The Ansible playbook: four roles, a declaration, a verification | One wheel, built and installed, then checked |
| Use it for | First setup, and any change you want reproducible | Iterating on the application |
| Idempotent | Yes, and a gate enforces it | The install is; it always restarts and re-verifies |
| Needs | An inventory and a declaration | `--robot` and a wheel or a member to build |

Do the first setup with `provision`. Reach for `deploy` when you are changing
the application and want the wheel on the robot in one step.

---

## Path A: provision from stock

### 1. Write an inventory

Copy the tracked example — it is the only tracked copy, and it never holds a real
value:

```
cp provisioning/ansible/inventory.example.ini provisioning/ansible/inventory.ini
```

`inventory.ini` is ignored by version control. Fill in the address and the
account:

```ini
[reachy]
robot ansible_host=192.0.2.10

[reachy:vars]
ansible_user=reachy
ansible_port=22
ansible_become=true
ansible_become_method=sudo
```

The address above is RFC 5737 TEST-NET-1. Substitute yours; it must not come
back into this repository.

### 2. Write a declaration

The declaration is what the robot is supposed to be. Every setting name in it is
one `reachy_contracts.ROBOT_SETTINGS` declares, and the roles validate the whole
of it before writing anything — so a value the robot would refuse costs no write
and no daemon restart.

**Executed** — the whole of that vocabulary, read out of the package:

```
uv run --locked --all-packages python -c \
  'from reachy_contracts.settings import ROBOT_SETTINGS
for setting in ROBOT_SETTINGS:
    print(f"{setting.name}\t{setting.kind.value}\t{setting.secret}")'
```

```
REACHY_GROUNDSTATION_URL	text	False
REACHY_GROUNDSTATION_CREDENTIAL	text	True
REACHY_HOME_ASSISTANT_IDENTITY	text	False
REACHY_SATELLITE_LOG_LEVEL	choice	False
REACHY_SATELLITE_FRAME_INTERVAL_MS	integer	False
REACHY_SATELLITE_JPEG_QUALITY	integer	False
REACHY_SATELLITE_RESULT_STALENESS_SECONDS	number	False
```

Seven names, and **a name outside that list is refused before anything is
written**. Executed, with the last line of the traceback it produces — the
frames above it name paths on the machine it ran on and are cut:

```
uv run --locked --all-packages python -c \
  'from reachy_contracts.settings import validate_settings
validate_settings({"REACHY_SATELLITE_DEVICE_NAME": "reachy-mini-1"})'
```

```
reachy_contracts.settings.SettingError: no setting named 'REACHY_SATELLITE_DEVICE_NAME' is declared; declared: REACHY_GROUNDSTATION_URL, REACHY_GROUNDSTATION_CREDENTIAL, REACHY_HOME_ASSISTANT_IDENTITY, REACHY_SATELLITE_LOG_LEVEL, REACHY_SATELLITE_FRAME_INTERVAL_MS, REACHY_SATELLITE_JPEG_QUALITY, REACHY_SATELLITE_RESULT_STALENESS_SECONDS
```

So a declaration looks like this:

```yaml
---
reachy_settings:
  REACHY_HOME_ASSISTANT_IDENTITY: reachy-mini-1
  REACHY_SATELLITE_LOG_LEVEL: info
reachy_groundstation_url: ws://192.0.2.10:8080/v1/session
reachy_groundstation_credential: the-credential-you-generated
reachy_app_distribution: reachy-mini-ha-satellite
reachy_app_wheel_path: dist/reachy_mini_ha_satellite-0.1.0-py3-none-any.whl
```

**The credential belongs in an encrypted file.** `reachyctl provision` accepts
`--extra-vars` only in the `@path` form, because an argument carrying a value is
visible in the process list to every user on the machine and lands in the shell
history:

```
ansible-vault encrypt declaration.yml
```

To fetch the wheel over the network instead of handing over a local path, use
`reachy_app_wheel_url` — and then `reachy_app_wheel_checksum` is required, as
`sha256:<64 hex digits>`. A wheel that arrived over the network is installed as
root into the environment the daemon runs, so what arrived has to be what was
reviewed. Every release publishes a `SHA256SUMS` beside its wheels.

### ⚠️ Known gap: this declaration does not configure the satellite

**Six of the seven names above are not settings the satellite application
reads**, and the settings it does need cannot be written through this path at
all. This is a real disagreement between the shared vocabulary the tooling
validates against and the application's own configuration, and it is recorded
as a follow-up in [`docs/tasks.md`](../tasks.md) rather than papered over
here.

| Declared name | What the satellite does with it |
|---|---|
| `REACHY_SATELLITE_LOG_LEVEL` | **Read.** This one works. |
| `REACHY_SATELLITE_FRAME_INTERVAL_MS` | Accepted and reported as having no effect; the application reads `REACHY_SATELLITE_FRAME_INTERVAL_SECONDS` |
| `REACHY_SATELLITE_JPEG_QUALITY` | Accepted and reported as having no effect; the application has no such setting |
| `REACHY_SATELLITE_RESULT_STALENESS_SECONDS` | Accepted and reported as having no effect; the application reads `REACHY_SATELLITE_STALENESS_SECONDS` |
| `REACHY_GROUNDSTATION_URL` | Not under the application's prefix, so it is never looked at; the application reads `REACHY_SATELLITE_GROUNDSTATION_URL` |
| `REACHY_GROUNDSTATION_CREDENTIAL` | The same; the application reads `REACHY_SATELLITE_GROUNDSTATION_CREDENTIAL` |
| `REACHY_HOME_ASSISTANT_IDENTITY` | The same. The application announces `REACHY_SATELLITE_DEVICE_NAME`. `doctor`'s `home-assistant.identity` check nonetheless reads this one and calls it "the identity the satellite announces" |

**What that means for you today.** A robot provisioned from this declaration
alone has a satellite that **refuses to start**, because
`REACHY_SATELLITE_DEVICE_NAME` has no default and nothing has set it. Set the
application's own variables in a **second drop-in of your own** beside the
managed one — the managed region is owned in full and rewritten whole, but
other drop-ins in the same directory belong to whoever put them there and
neither `reachyctl` nor Ansible reads or writes them:

```ini
# /etc/systemd/system/reachy-mini-daemon.service.d/20-satellite.conf
[Service]
Environment="REACHY_SATELLITE_DEVICE_NAME=reachy-mini-1"
Environment="REACHY_SATELLITE_GROUNDSTATION_URL=ws://192.0.2.10:8080/v1/session"
Environment="REACHY_SATELLITE_GROUNDSTATION_CREDENTIAL=the-credential-you-generated"
```

**That file holds a credential, so give it root-only permissions.** systemd
reads unit configuration as root and needs nothing more:

```
sudo install --owner root --group root --mode 600 \
  20-satellite.conf /etc/systemd/system/reachy-mini-daemon.service.d/
sudo systemctl daemon-reload
sudo systemctl restart reachy-mini-daemon.service
```

> **⏳ PENDING HARDWARE VERIFICATION.** No expected output is recorded for these
> commands against a robot. Nothing below is a transcript.

And set `REACHY_HOME_ASSISTANT_IDENTITY` in the managed declaration to the
**same string** as `REACHY_SATELLITE_DEVICE_NAME`, so that `doctor`'s identity
check compares two values that are supposed to be equal rather than reporting
on a variable nothing announces.

Everything else about the satellite is set the same way, or from
[its own settings page](../ops/satellite-deployment.md#the-settings-page) once
it is running.

### 3. Preview it

```
reachyctl provision --preview --extra-vars @declaration.yml
```

> **⏳ PENDING HARDWARE VERIFICATION.** No expected output is recorded for this
> step against a robot. Nothing below is a transcript.

`--preview` is `--check --diff`: it reports what would change and changes
nothing. Read the diff of the managed drop-in before applying — it is the file
the whole configuration story hangs on, and
[its format is a written contract](../ops/managed-daemon-environment.md).

### 4. Apply it

```
reachyctl provision --extra-vars @declaration.yml
```

> **⏳ PENDING HARDWARE VERIFICATION.** No expected output is recorded for this
> step against a robot. Nothing below is a transcript.

Four concerns run in order, behind four tags — `groundstation_link`,
`daemon_env`, `app_install`, `verify`. The link is validated before anything is
written, so a half-configured groundstation is refused rather than deployed. The
last thing the play does is assert the end state with the same checks
`reachyctl doctor` runs, which is reachyctl REQ-056: one definition of healthy,
used by diagnosis and by provisioning alike.

Applying one concern rather than all of them:

```
reachyctl provision --tags daemon_env --extra-vars @declaration.yml
```

### 5. What a successful verification looks like

**Executed** — against the container target, not a robot, with
`just provision-idempotency`. This is the tail of the second of two applications:

```
TASK [verify : Report every link between this run and a working robot] *********
ok: [target] => (item=daemon.reachable) => {
    "msg": "daemon.reachable: passed — the robot daemon answered, running 1.9.0"
}
ok: [target] => (item=application.installed) => {
    "msg": "application.installed: passed — the application is installed at version 1.2.3"
}
ok: [target] => (item=application.running) => {
    "msg": "application.running: passed — the application is running (running under the daemon at 1.2.3)"
}
ok: [target] => (item=groundstation.session) => {
    "msg": "groundstation.session: skipped — provisioning does not open a session to the groundstation: one opened from the machine running the playbook would measure that machine's route rather than the robot's. Run `reachyctl doctor --url ...` to exercise the link"
}
ok: [target] => (item=groundstation.capabilities) => {
    "msg": "groundstation.capabilities: skipped — provisioning does not open a session to the groundstation: one opened from the machine running the playbook would measure that machine's route rather than the robot's. Run `reachyctl doctor --url ...` to exercise the link"
}
ok: [target] => (item=groundstation.round-trip) => {
    "msg": "groundstation.round-trip: skipped — provisioning does not open a session to the groundstation: one opened from the machine running the playbook would measure that machine's route rather than the robot's. Run `reachyctl doctor --url ...` to exercise the link"
}
ok: [target] => (item=models.files) => {
    "msg": "models.files: skipped — the model files belong to the groundstation's artifact and are not on the robot; nothing here has a registry to judge them against"
}
ok: [target] => (item=configuration.effective) => {
    "msg": "configuration.effective: passed — all 5 declared setting(s) are in force"
}
ok: [target] => (item=home-assistant.identity) => {
    "msg": "home-assistant.identity: passed — the satellite announces 'Reachy Mini Example', as declared"
}

TASK [verify : Report that the end state was verified] *************************
ok: [target] => {
    "msg": "nothing failed, but not everything was checked (5 passed, 0 failed, 4 skipped)"
}

PLAY RECAP *********************************************************************
target                     : ok=29   changed=0    unreachable=0    failed=0    skipped=20   rescued=0    ignored=0

the second application changed nothing across 1 host(s)
```

Two things to read there. **The three groundstation checks report themselves
skipped, with a reason** — the play deliberately does not open a session,
because one opened from the control machine would measure the control machine's
route. Exercising the link is `reachyctl doctor --url`, in step 7 below.

**`changed=0` on a second application** is provisioning
[REQ-060](../specs/provisioning/index.md#req-060-applying-twice-changes-nothing-the-second-time),
and the gate that enforces it is
[REQ-061](../specs/provisioning/index.md#req-061-idempotency-is-enforced-automatically).
A run against an already-provisioned robot changes nothing — which matters
because the alternative is a run that restarts the daemon in the middle of a
conversation.

### 6. Undoing it

```
reachyctl provision --remove --extra-vars @declaration.yml
```

The removal path takes everything provisioning applied, or nothing: it does not
accept `--tags`.

---

## Path B: deploy a wheel

`deploy` builds a wheel, sends it, installs it into the robot's shared
application environment, restarts, and then asks the robot which version is
actually running — rather than trusting the install.

### Build the wheels first, if you are deploying from a checkout

**Executed:**

```
just wheels
```

```
Successfully built dist/reachy_checks-0.1.0-py3-none-any.whl
Building wheel...
Successfully built dist/reachy_session_client-0.1.0-py3-none-any.whl
Building wheel...
Successfully built dist/reachyctl-0.1.0-py3-none-any.whl
Building wheel...
Successfully built dist/reachy_mini_ha_satellite-0.1.0-py3-none-any.whl
reachy_checks-0.1.0-py3-none-any.whl
reachy_contracts-0.1.0-py3-none-any.whl
reachy_mini_ha_satellite-0.1.0-py3-none-any.whl
reachy_session_client-0.1.0-py3-none-any.whl
reachyctl-0.1.0-py3-none-any.whl
```

Five wheels, and the count is not padding: `reachyctl` requires three siblings
that nothing publishes to an index, so a wheel released on its own installs
nowhere. The fifth is the robot application.

`just wheel-verify` installs the set into an empty environment and drives the
tool, which is what proves that is enough. **Executed:**

```
wheel-verify: reachyctl 0.1.0
wheel-verify: doctor reported 9 checks, all skipped
satellite wheel: reachy_mini_ha_satellite-0.1.0-py3-none-any.whl carries 13 registered assets, their licence texts, and the reachy_mini_apps entry point
```

The third line is the question specific to the satellite wheel: a missing
`reachy_mini_apps` entry point installs perfectly and never appears in the
daemon's application list, and an asset shipping without its registry entry ships
somebody else's file under terms nobody agreed to. Neither is visible to a build
that merely succeeded.

### Preview, then deploy

```
reachyctl deploy --robot reachy@192.0.2.10 --member reachy-mini-ha-satellite --preview
reachyctl deploy --robot reachy@192.0.2.10 --member reachy-mini-ha-satellite
```

> **⏳ PENDING HARDWARE VERIFICATION.** No expected output is recorded for
> either command against a robot. Nothing below is a transcript.

`--wheel <path>` sends a wheel already built instead of building one.
`--application` defaults to the name the wheel itself carries, which is the only
name that cannot be wrong about what was installed.

**Host-key verification stays on.** There is no option that turns it off. Point
`--known-hosts` at a file if the robot is not already in your default one.

### Reading and changing the configuration

```
reachyctl config get   --robot reachy@192.0.2.10
reachyctl config diff  --robot reachy@192.0.2.10 --declaration declaration.json
reachyctl config apply --robot reachy@192.0.2.10 --declaration declaration.json --preview
reachyctl config apply --robot reachy@192.0.2.10 --declaration declaration.json
reachyctl config set   --robot reachy@192.0.2.10 REACHY_SATELLITE_LOG_LEVEL=debug
```

> **⏳ PENDING HARDWARE VERIFICATION.** No expected output is recorded for any of
> these against a robot. Nothing below is a transcript.

The declaration here is a **JSON** document with two keys, and it is the same
document `doctor --intent` reads — two documents describing one robot are two
documents that will disagree:

```json
{
  "configuration": {
    "REACHY_GROUNDSTATION_URL": "ws://192.0.2.10:8080/v1/session",
    "REACHY_HOME_ASSISTANT_IDENTITY": "reachy-mini-1",
    "REACHY_SATELLITE_LOG_LEVEL": "info"
  },
  "announced_identity": "reachy-mini-1"
}
```

`announced_identity` and `REACHY_HOME_ASSISTANT_IDENTITY` overlap in exactly one
place, and the loader **refuses a document that declares both differently**
rather than picking a winner: that document describes a robot that cannot exist,
and the failure it would otherwise produce — an apply that succeeds followed by a
`doctor` that fails — is one an operator would spend an afternoon on.

`apply` makes the managed region **exactly** the declaration: a setting withdrawn
from the declaration is removed from the robot, not left behind. `set` changes
some settings and leaves the rest of the region alone. `get` reports what the
daemon is actually running with, read from `systemctl show`, rather than what the
file says — the two are compared rather than assumed equal, because a setting
that is in the file and not in force is exactly the silently-inert configuration
this whole stack is written against.

The file's format is
[a written contract](../ops/managed-daemon-environment.md) that both `reachyctl`
and the Ansible role are implemented against, and a contract test holds the
document and the code together.

### Starting and stopping

```
reachyctl app start --robot reachy@192.0.2.10
reachyctl app stop  --robot reachy@192.0.2.10
reachyctl app logs  --robot reachy@192.0.2.10
```

> **⏳ PENDING HARDWARE VERIFICATION.** No expected output is recorded for any of
> these against a robot. Nothing below is a transcript.

Start and stop both confirm the robot reports the new state rather than
returning as soon as the command was accepted.

---

## 7. Verify the whole chain

This is the point of the exercise: one command that says which link is broken,
run from a machine that can reach both the robot and the groundstation.

```
reachyctl doctor \
  --robot reachy@192.0.2.10 \
  --url ws://192.0.2.10:8080/v1/session \
  --credential-file ~/.config/reachy/groundstation-credential \
  --intent declaration.json
```

> **⏳ PENDING HARDWARE VERIFICATION.** No expected output is recorded for a run
> with `--robot` against a robot. Nothing below is a transcript.

What *is* recorded is the same command without `--robot`, which is the shape you
should expect the groundstation half to take — see
[the groundstation runbook, step 7](groundstation.md#7-confirm-the-whole-chain-so-far).
The nine checks and what each one asks are generated from the registry into
[`docs/contracts/doctor-checks.md`](../contracts/doctor-checks.md).

`--intent` reads the same JSON document `config --declaration` does.

A check whose prerequisites were not supplied reports **skipped**, not failed.
Passing `--robot` is what makes the three daemon checks run; passing `--intent`
is what makes the configuration and identity checks run.

`--output json` gives one document per run, for a script:

```
reachyctl --output json doctor --robot reachy@192.0.2.10 ...
```

---

## The robot's own surfaces

Once the satellite is running, two things on the robot answer questions without
a shell:

| Where | What it is |
|---|---|
| `http://<robot>:8088/` | The satellite's settings page — every setting, which layer it came from, and a **Stop** |
| The robot's own dashboard | The daemon's application list; where you start the satellite again after a stop |

**Both are unauthenticated, and so is the rest of the robot.** The ESPHome API
announces `uses_password=false` and the daemon's dashboard is open too, so the
trust boundary is the network the robot is on. Put the robot on a network you
trust, the way you would a printer. `REACHY_SATELLITE_WEB_ENABLED=false` switches
the settings page off entirely.

[`docs/ops/satellite-deployment.md`](../ops/satellite-deployment.md) is the full
reference for the application: every setting, the three configuration layers and
which wins, what the antennas mean, and what `/status` says when the head returns
to neutral.

## Next

- [Add the robot to Home Assistant](home-assistant.md)
- [Update a running installation](../ops/deploy.md)
- [Diagnose a failure](../ops/troubleshooting.md)
