# Troubleshooting

**Start here:**

```
reachyctl doctor \
  --robot reachy@192.0.2.20 \
  --url ws://192.0.2.10:8080/v1/session \
  --credential-file ~/.config/reachy/groundstation-credential \
  --intent declaration.json
```

`doctor` walks the chain from the machine you are on to a working robot, link by
link, and names **the first broken one**. This page is keyed to the same check
identifiers, so the tool's output leads directly to the section that helps.

Two rules about reading its output:

- **Skipped is not failed.** A check whose prerequisites were not supplied never
  ran. `doctor` with no `--robot` reports the three daemon checks as skipped and
  exits 0, with a summary line saying *nothing failed, but not everything was
  checked*. Collapsing the two would make the output worth ignoring.
- **Every check runs, whatever the ones before it did.** A failure does not stop
  the run and does not change another check's outcome, so an operator whose
  groundstation is down still learns whether the daemon is healthy. Do not read a
  failure as a reason the checks below it were not attempted — they were.
- **The three groundstation checks are the one exception, because they share one
  session.** If the session never opened there is nothing to negotiate and
  nothing to measure, so the two below it skip and say which check to read:
  `groundstation.round-trip` skipped with *no session was established, so there
  was nothing to measure* is not a second problem. Nothing else in the chain
  works that way — a `daemon.reachable` failure leaves `application.installed`
  running and reporting on its own terms.

## How this page stays true

**Every remediation quoted below is the registry's own string, word for word.**
The checks are declared once, in `packages/reachy-checks/src/reachy_checks/registry.py`,
and `reachyctl doctor` and the Ansible verification role both run those
declarations — reachyctl REQ-056. The remediation an operator sees is the one in
that registry, so a paraphrase here would be a second answer to "how do I fix
this?", free to drift from the one people actually read.

Two mechanisms hold it:

- [`docs/contracts/doctor-checks.md`](../contracts/doctor-checks.md) is
  **generated** from the registry by `just contracts`, and the contract-drift
  gate fails when the committed copy differs from what regenerating produces.
- `packages/reachy-checks/tests/test_checks_runbook.py` reads **this file** and
  requires its sections to be exactly the registered identifiers, in the
  registry's order, each quoting that check's remediation word for word. Rename a
  check or reword a remedy and the test suite goes red until this page is
  brought along.

---

## The checks

| Check | What it asks |
|---|---|
| [`daemon.reachable`](#daemonreachable) | The robot's daemon is reachable and answering |
| [`application.installed`](#applicationinstalled) | The satellite is installed on the robot, at a known version |
| [`application.running`](#applicationrunning) | The satellite is running on the robot |
| [`groundstation.session`](#groundstationsession) | A session opens to the groundstation |
| [`groundstation.capabilities`](#groundstationcapabilities) | The session agrees on at least one capability |
| [`groundstation.round-trip`](#groundstationround-trip) | A frame goes out and comes back, and how long that took |
| [`models.files`](#modelsfiles) | Every pinned model file is present and unaltered |
| [`configuration.effective`](#configurationeffective) | The configuration in force matches what was declared |
| [`home-assistant.identity`](#home-assistantidentity) | The satellite announces the Home Assistant identity declared |

---

### daemon.reachable

**What it means.** Nothing on the robot can be asked anything. The three
robot-side checks still run — they are independent — so expect
`application.installed`, `application.running` and, if a declaration was given,
`configuration.effective` and `home-assistant.identity` to fail alongside it,
each naming its own transport failure. **Fix this one first**: they are all the
same fault reported four more times.

**Where to look.** The robot is off, the address is wrong, the account or the key
is wrong, or the daemon unit is not running. `doctor` reaches the robot over SSH
with host-key verification on — there is no option that turns it off — so a
changed host key looks like a connection failure. Point `--known-hosts` at a file
that has the robot in it.

> **⏳ PENDING HARDWARE VERIFICATION.** No failing transcript is recorded for
> this check: reaching a real robot's daemon has never been attempted from this
> repository. What is recorded is what the check reports when no robot is given
> at all, in [the setup runbook](../setup/groundstation.md#7-confirm-the-whole-chain-so-far).

**Remediation, as `doctor` prints it:**

> Nothing here starts a daemon it cannot reach. Confirm the robot is powered on
> and answering at the address configured, and that its daemon is running.

---

### application.installed

**What it means.** The daemon answered and the satellite is not in the
environment it runs applications from, or is at a version nothing expected.

**Where to look.** The check reports the version **when it passes**, not only
when it fails, and that is deliberate: the predecessor's most expensive
deployment failure was a package that installed perfectly into an environment the
running daemon was not using — which looks identical to success unless something
says out loud which version is there. If the version reported is not the one you
installed, you installed into a different environment.

`deploy` reads the wheel's own `.dist-info/METADATA` for the distribution name
rather than parsing its file name, so "what was installed" and "what is running"
are answers to the same question.

> **⏳ PENDING HARDWARE VERIFICATION.** No failing transcript. Against the
> container target the check passes, reporting `the application is installed at
> version 1.2.3` — see [the robot runbook](../setup/robot.md#5-what-a-successful-verification-looks-like).

**Remediation, as `doctor` prints it:**

> Install the satellite into the robot's application environment. Deployment
> installs it and then verifies the version actually running, rather than
> trusting the install.

```
reachyctl deploy
```

---

### application.running

**What it means.** It is installed and it is not running.

**Where to look.** First at whether it *exited*. The daemon marks a
cleanly-exited application `done` and leaves it stopped; nothing relaunches it.
So this check failing right after somebody pressed **Stop** on the settings page
is the settings page working, not a fault.

If it exits at startup, the reason is almost always configuration:
`REACHY_SATELLITE_DEVICE_NAME` is unset, or the detection source needs something
it was not given — `remote` needs an address *and* a credential, `local` needs a
path to model weights that are deliberately not shipped in the wheel. Read the
journal:

```
reachyctl app logs --robot reachy@192.0.2.20
```

> **⏳ PENDING HARDWARE VERIFICATION.** No failing transcript, and no `app logs`
> transcript at all.

**Remediation, as `doctor` prints it:**

> Start the application on the robot.

```
reachyctl app start
```

---

### groundstation.session

**What it means.** No session. This is the most common failure and it has three
distinguishable causes, all of which the detail line separates for you.

**Executed — the service is not there:**

```
groundstation.session	failed	no session at ws://127.0.0.1:8099/v1/session: ConnectionFailedError: could not open a session at ws://127.0.0.1:8099/v1/session: [Errno 111] Connect call failed ('127.0.0.1', 8099)	The groundstation refused the session or never answered. Confirm the service is running, that the configured endpoint reaches it, and that the credential presented is the one it expects; its readiness endpoint reports whether it finished warming up.	-
```

`ConnectionFailedError` with `Errno 111` is nothing listening. Check the service
is up and that `GROUNDSTATION_PUBLISH` publishes it on an interface the robot can
reach — the default is loopback only, which no robot on another host can get to.

**Executed — the credential is wrong:**

```
groundstation.session	failed	no session at ws://127.0.0.1:8080/v1/session: SessionRefusedError: the groundstation refused the session (unauthenticated): the credential presented is not the configured one	The groundstation refused the session or never answered. Confirm the service is running, that the configured endpoint reaches it, and that the credential presented is the one it expects; its readiness endpoint reports whether it finished warming up.	-
```

`SessionRefusedError` with `unauthenticated` means the service answered and said
no. The credential the robot presents and the one the service is configured with
are different strings.

**The third cause is a service still warming up.** Ask it directly rather than
guessing:

```
curl --silent --show-error http://127.0.0.1:8080/readyz
```

`"ready": false` means a capability has not finished loading its model. Wait, or
raise `REACHY_GROUNDSTATION_WARM_UP_TIMEOUT_SECONDS` if the host is slow.

**Remediation, as `doctor` prints it:**

> The groundstation refused the session or never answered. Confirm the service is
> running, that the configured endpoint reaches it, and that the credential
> presented is the one it expects; its readiness endpoint reports whether it
> finished warming up.

---

### groundstation.capabilities

**What it means.** A session opened and the two sides have nothing in common, so
no frame would ever be answered. The link looks healthy and does nothing.

**Executed** — a client offering only `gesture` against a service with gestures
disabled, which is this build's default:

```
groundstation.capabilities	failed	the groundstation agreed to none of the capabilities offered (gesture), so nothing would answer a frame	The session opened and the two sides have no capability in common, so nothing would answer a frame. Compare the versions installed on each side, and check the groundstation's capability health for one that failed to warm up.	-
```

**Where to look.** Negotiation is an **exact match on the name and the version
together**, so a capability the other side offers at a different version drops
silently out of the agreed set rather than failing the session. Ask the service
what it is offering:

```
curl --silent --show-error http://127.0.0.1:8080/capabilities | python3 -m json.tool
```

```json
{
    "ready": true,
    "offered": [
        "face"
    ],
    "capabilities": [
        {
            "name": "face",
            "version": 1,
            "state": "ready",
            "detail": ""
        },
        {
            "name": "gesture",
            "version": null,
            "state": "disabled",
            "detail": ""
        }
    ]
}
```

**`disabled` is not `failed`.** A capability switched off by configuration
declines to be built and is offered to nobody; the health surface keeps that
distinct from one that broke, so an operator can tell "I turned that off" from
"that stopped working". **Gestures ship disabled with no model wired** — no hand
classifier clears this repository's licence and provenance bar — so a gesture
capability that will not negotiate is the design rather than a fault.

A capability in state `failed` carries the reason in `detail`.

**Remediation, as `doctor` prints it:**

> The session opened and the two sides have no capability in common, so nothing
> would answer a frame. Compare the versions installed on each side, and check
> the groundstation's capability health for one that failed to warm up.

---

### groundstation.round-trip

**What it means.** A session is up, a capability was agreed, a frame went out and
no result came back inside the budget. `--timeout` is one budget for the whole
exchange — opening the session, sending the frame, waiting — rather than per
step.

**Where to look.** At the groundstation's logs and its capability health, not at
the network. A link this quiet is usually a capability that accepted the frame
and produced nothing.

The service's own metrics answer whether frames are arriving at all:

```
curl --silent --show-error http://127.0.0.1:8080/metrics | grep -E '^groundstation_[a-z_]+_total'
```

**Executed**, after five frames through a probe and one session refused for a
wrong credential:

```
groundstation_sessions_total{outcome="going_away"} 1.0
groundstation_sessions_total{outcome="unauthenticated"} 1.0
groundstation_frames_received_total 5.0
groundstation_frames_dropped_total 0.0
groundstation_results_emitted_total{capability="face"} 5.0
groundstation_errors_total{code="unauthenticated"} 1.0
```

`frames_received` climbing with `results_emitted` flat is a capability that has
stopped answering. `frames_dropped` climbing means frames are arriving faster
than they are being answered and the oldest are being discarded at the queue
bound — which is deliberate, because a result is useful only while the frame it
describes is recent.

When the round trip is merely *slow* rather than absent, the number is in
`doctor`'s output as `round_trip_ms` — promoted out of the rows precisely so a
consumer can trend it. Compare against
[the recorded baseline](../specs/benchmarks/) rather than against a memory.

**Remediation, as `doctor` prints it:**

> A session is up and no result came back to time. Read the groundstation's logs
> and capability health: a link this quiet is usually a capability that accepted
> the frame and produced nothing, rather than a network fault.

---

### models.files

**What it means.** A model file the registry pins is missing, or is not the bytes
it should be.

**Executed** — pointed at an empty directory:

```
models.files	failed	1 model file problem(s) in /tmp/empty-models: face_detection_yunet: /tmp/empty-models/face_detection_yunet_2026may.onnx is not a file. Models are put in place when the artifact is built and are never fetched at run time; check REACHY_GROUNDSTATION_MODELS_DIR.	A model file is missing or is not the bytes the registry pins. Fetch them again into the directory the groundstation reads; the fetcher refuses anything whose digest does not match, so a run that succeeds leaves the reviewed weights in place.	python -m reachy_groundstation.models.fetch "$REACHY_GROUNDSTATION_MODELS_DIR"
```

**Where to look.** Weights are **never committed** and are **never fetched at run
time**: they are put in place while the artifact is built, which is what lets the
service start on a host with no outbound internet access. So this failing on a
container means the image is wrong, and this failing on a checkout means
`just models` has not been run.

The registry that pins each file by digest, with its licence, its attribution and
its retrieval URL, is
`services/groundstation/src/reachy_groundstation/models/registry.py`. The fetcher
refuses anything whose digest disagrees and deletes it rather than leaving it
where a later stage could find it — so a run that succeeds leaves the reviewed
weights in place.

**Remediation, as `doctor` prints it:**

> A model file is missing or is not the bytes the registry pins. Fetch them again
> into the directory the groundstation reads; the fetcher refuses anything whose
> digest does not match, so a run that succeeds leaves the reviewed weights in
> place.

```
python -m reachy_groundstation.models.fetch "$REACHY_GROUNDSTATION_MODELS_DIR"
```

Locally that is `just models`, which writes to `.models/` and is gitignored.

---

### configuration.effective

**What it means.** The robot is not running the configuration that was declared.

**Where to look.** At the difference between what is in the file and what is in
force. Those are two different questions and the check compares them rather than
assuming they agree:

- the **file** is the managed drop-in, and `reachyctl config get` reads it;
- what is **in force** is
  `systemctl show reachy-mini-daemon.service --property=Environment --value`,
  which is the whole environment the unit ended up with, whichever drop-in put it
  there.

A setting that is in the file and not in force is the silently-inert
configuration this entire stack is written against — the predecessor's
configuration reader was never called, so every value that looked tuned was in
fact a default, for months. Only the comparison finds it. Usually it means the
daemon has not been restarted since the file changed: the application inherits
its environment from the daemon, so putting a change in force restarts the
daemon.

**The check reports which keys differ and never what they hold.** A setting is
exactly where a credential ends up.

> **⏳ PENDING HARDWARE VERIFICATION.** No failing transcript. Against the
> container target the check passes, reporting `all 5 declared setting(s) are in
> force`.

**Remediation, as `doctor` prints it:**

> The robot is not running the configuration that was declared. Apply the
> declaration; preview it first to see what changes.

```
reachyctl config apply
```

---

### home-assistant.identity

**What it means.** The satellite announces an identity other than the declared
one. **This is the most consequential failure on this page**: Home Assistant
keys an ESPHome device on the identity it announces, so a second identity is a
second device, every entity gains a suffixed identifier, and the history stays
attached to the first — while every automation and dashboard card referencing the
old identifiers silently stops matching. Nothing errors.

**Where to look.** [The identity
warning](../setup/home-assistant.md#-the-one-thing-that-cannot-be-undone-the-announced-identity)
first, then Home Assistant's own device list. **Whether Home Assistant already
holds a stale device is a manual check** — `doctor` deliberately holds no Home
Assistant credentials, because holding them would widen this tool's blast radius
for one comparison and give every output path a second secret to scrub against.

> **⚠️ And read [the known gap](../setup/home-assistant.md#-known-gap-doctor-watches-a-different-variable):**
> this check reads `REACHY_HOME_ASSISTANT_IDENTITY` out of the daemon
> environment, while the satellite announces `REACHY_SATELLITE_DEVICE_NAME`.
> Until those are reconciled, a `passed` here is only meaningful if you have set
> both to the same string.

> **⏳ PENDING HARDWARE VERIFICATION.** No failing transcript. Against the
> container target the check passes, reporting `the satellite announces 'Reachy
> Mini Example', as declared` — from `REACHY_HOME_ASSISTANT_IDENTITY`, per the
> gap above.

**Remediation, as `doctor` prints it:**

> The satellite announces an identity other than the declared one, so Home
> Assistant sees a second device and the entity history stays attached to the
> first. Apply the declared configuration to restore it. Whether Home Assistant's
> own device registry already holds a stale entry is a manual check, in its
> device list.

```
reachyctl config apply
```

---

## Failures `doctor` does not have a check for

### The service refuses to start naming a variable

**Executed:**

```
docker run --rm \
  --env REACHY_GROUNDSTATION_CREDENTIAL=example-credential \
  --env REACHY_GROUNDSTATION_LOGLEVEL=info \
  reachy-groundstation:dev
```

```
unrecognised configuration variable(s): REACHY_GROUNDSTATION_LOGLEVEL. Every REACHY_GROUNDSTATION_* variable must name a known setting; the known ones are REACHY_GROUNDSTATION_CAPABILITY_TIMEOUT_SECONDS, REACHY_GROUNDSTATION_CREDENTIAL, REACHY_GROUNDSTATION_FACE_ENABLED, REACHY_GROUNDSTATION_FACE_NMS_THRESHOLD, REACHY_GROUNDSTATION_FACE_SCORE_THRESHOLD, REACHY_GROUNDSTATION_GESTURE_ENABLED, REACHY_GROUNDSTATION_GESTURE_SAMPLE_INTERVAL, REACHY_GROUNDSTATION_GESTURE_SCORE_THRESHOLD, REACHY_GROUNDSTATION_HANDSHAKE_TIMEOUT_SECONDS, REACHY_GROUNDSTATION_HOST, REACHY_GROUNDSTATION_INFERENCE_INTER_OP_THREADS, REACHY_GROUNDSTATION_INFERENCE_INTRA_OP_THREADS, REACHY_GROUNDSTATION_INFERENCE_PROVIDERS, REACHY_GROUNDSTATION_LOG_FORMAT, REACHY_GROUNDSTATION_LOG_LEVEL, REACHY_GROUNDSTATION_MAX_MESSAGE_BYTES, REACHY_GROUNDSTATION_MODELS_DIR, REACHY_GROUNDSTATION_PORT, REACHY_GROUNDSTATION_QUEUE_BOUND, REACHY_GROUNDSTATION_SERVICE_NAME, REACHY_GROUNDSTATION_WARM_UP_TIMEOUT_SECONDS.
```

**This is the requirement working, not a regression.** `LOGLEVEL` is a typo for
`LOG_LEVEL`, and a service that started on the default would leave the operator
believing they had set it. The message lists every name the service does
recognise, so the correction is in the failure.

The satellite makes one distinction the groundstation does not: a name under
`REACHY_SATELLITE_` that `reachy_contracts.ROBOT_SETTINGS` declares but the
application does not consume is **accepted and reported**, not refused —
otherwise `reachyctl config apply` with the documented vocabulary would produce
a robot that will not start. A name in neither set is still fatal.

### The service refuses to start with no credential

**Executed:**

```
docker run --rm reachy-groundstation:dev
```

```
configuration is not usable: REACHY_GROUNDSTATION_CREDENTIAL: Field required
```

There is no default, deliberately: a groundstation that authenticated nothing
because nobody configured it is a worse failure than one that will not start.

### Home Assistant never discovers the robot

Check `REACHY_SATELLITE_ADVERTISE` is on, and that the robot and Home Assistant
are on the same layer-2 network — **mDNS does not cross a router**. The boot log
records the interface, the address and the port it advertised. Failing that, add
the device by hand: **Add Integration → ESPHome**, the robot's address, port
`6053`.

### The head never tracks a face

`/status` on the satellite's settings port says which of four situations it is:

| `/status` | What it means |
|---|---|
| `tracking` | It is following somebody |
| `nobody` | A live detector looked and there was no face |
| `stale` | Results were arriving and stopped; the head returned to neutral |
| `unknown` | Nothing has ever produced a result — the session is not up, or face tracking is off |

`unknown` sends you to [`groundstation.session`](#groundstationsession). `stale`
means the link was working, which is a different investigation:
[`groundstation.round-trip`](#groundstationround-trip).

**A head that returns to neutral is a signal.** Holding the last pose would look
like successfully tracking somebody who has left the room.

### A setting changed on the page did nothing

The page marks each setting *applies at once* or *needs a restart*, and the ones
that need a restart genuinely do: they are read while something is being built —
a socket bound, a session opened, a detector loaded, an identity announced.
`REACHY_SATELLITE_FACE_TRACKING_ENABLED` is the common one; switching it on means
building a detector, which happens once at startup.

Press **Stop**, then start the application again from the robot dashboard's
application list. **Stopping is not restarting** — the daemon leaves a
cleanly-exited application stopped and nothing relaunches it.

### A setting reverts after `reachyctl config apply`

It does not revert; it never took effect. Configuration has three layers and the
settings page's overrides file sits **above** the daemon environment:

| Layer | Where | Written by |
|---|---|---|
| Defaults | The wheel | — |
| Environment | The daemon's managed drop-in | `reachyctl config apply`, the Ansible `daemon_env` role |
| Overrides | `<state_dir>/settings.json` | The settings page |

A value showing `override` on the page is one `reachyctl` can no longer change,
until the page is used to save it back to what the environment says — which
removes the override rather than pinning a duplicate of it.

### An edit to the managed drop-in disappeared

That is the ownership rule, and the file says so in its own header. **The whole
file is owned**: `reachyctl config apply` and the Ansible role both rewrite it
whole, so a setting removed from the declaration is removed from the robot rather
than left behind, and a hand edit is lost on the next apply.

Put your own settings in a **different drop-in** in the same directory. Other
drop-ins belong to whoever put them there and neither implementation reads or
writes them. The format's exact bytes are
[a written contract](managed-daemon-environment.md).

### `reachyctl` refuses to read the managed region

A file that exists and is empty, or whose markers are missing, unpaired or out of
order, is reported as **unreadable** rather than treated as empty — because a
region read as ours is a region the next apply rewrites. This format never writes
an empty file: withdrawing every setting still writes the header, `[Service]` and
both markers, so a blank file is one something else blanked.

The reader also re-renders what it parsed and refuses the file unless the result
is what it was given, byte for byte. Every rule on
[the format page](managed-daemon-environment.md) is therefore load-bearing for a
reader as well as a writer.

---

## Still stuck

| Question | Where |
|---|---|
| What does this check actually do? | [`docs/contracts/doctor-checks.md`](../contracts/doctor-checks.md), generated from the registry |
| What does this setting mean? | [The satellite reference](satellite-deployment.md), or the groundstation's `.env.example` |
| Why is it built this way? | The [specs](../index.md), under each one's Decision Records |
| What changed in this release? | The change document listed against it in [`docs/index.md`](../index.md) |
