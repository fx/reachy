# Deploying the HA satellite

The robot-side Home Assistant voice satellite ships as a wheel on GitHub
Releases. Installing it into the robot's shared application environment is the
whole of registering it: the wheel declares a `reachy_mini_apps` entry point, and
the Reachy Mini daemon enumerates that group when it starts.

**There are two ways to get it there, and they end in the same place.** The
daemon has an application-installation path of its own — it downloads a
published application source and installs it into the robot's shared
application environment — so on a robot you have no shell on, and want no shell
on, that path is the whole installation. The other way is to fetch the wheel and
install it yourself, which needs a shell and is what an operator iterating on
the application wants. Either way the daemon finds the application through the
same entry point, so this is one artifact reached two ways rather than two
packaging stories: [Installing](#installing).

> **An earlier version of this page said there was deliberately no published
> application source, and there now is.** That decision made a shell a
> prerequisite for installing at all, which is the thing
> [stock-robot-installation REQ-104](../specs/stock-robot-installation/index.md#req-104-the-application-installs-through-the-daemon-s-own-path)
> reverses. Two design notes still record the old decision — the
> [architecture](../specs/architecture/index.md#versioning-and-distribution) and
> [HA Satellite](../specs/ha-satellite/index.md#packaging-and-deployment) specs
> both say a wheel is sufficient and no source is published. They are wrong and
> they are not this document's to correct: a spec edit is its own proposal, and
> reconciling those two notes is one somebody still has to make.

Discovering an application and starting one are different mechanisms, and the
second is not what the entry point's spelling suggests:
[How the daemon starts it](#how-the-daemon-starts-it).

---

## ⚠️ Before you install: pin the announced identity

**Nothing derives `REACHY_SATELLITE_DEVICE_NAME`, and nothing is announced to
Home Assistant until you set it.** That embargo is deliberate and it is the single
most important thing on this page.

Home Assistant keys an ESPHome device on the identity it announces. If that
identity changes, Home Assistant does not update the existing device — it
registers a **new** one:

- every entity acquires a suffixed identifier (`..._2`);
- history detaches from the old entity and stays with a device nothing writes to
  any more;
- every automation, script and dashboard card referencing the old identifiers
  silently stops matching anything. Nothing errors. Things just stop happening.

A default derived from the package name would be correct on a fresh installation
and silently destructive on an upgrade — and an upgrade from an application with
a different package name is exactly what this is. So there is no default, and
being asked for the value is how the hazard becomes visible before it has
happened rather than after.

### Starting without one is safe, and it is how a stock robot is configured

The application used to **refuse to start** without the identity. It no longer
does, and the reason is that the refusal was the wrong shape rather than the
wrong idea: on a robot reached only through the surfaces its shipped image
exposes, the settings page is the only place the value can be typed, and a
refusal to start meant the settings page was never served. There was no route to
a first configuration that did not involve writing a systemd drop-in over `ssh`.

What replaces it keeps the whole of the hazard closed. With no identity:

- the application **starts**, and its settings page and `/status` answer;
- **nothing announcing is built at all** — no ESPHome listener, no mDNS record,
  no entities. Home Assistant discovers no device, so there is nothing for the
  eventual correct identity to collide with, split from or orphan;
- the settings page says so, in as many words, and takes the value;
- the same is true after any number of restarts. An application left
  unconfigured announces nothing indefinitely.

An identity the model *rejects* — longer than 64 characters — is still refused at
startup, and refused on the settings page with the constraint stated. "Not
supplied" and "not acceptable" are different answers and only the first one is
safe to start on.

The identity is read while the announcement is being built, so it takes effect at
the next start: set it, press **Stop** on the settings page, and start the
application again from the robot's own dashboard. No shell, and no reinstall.

Because it is read at startup, the configured identity and the announced one are
two different facts and the page keeps them apart. It says *Announced to Home
Assistant as* whatever this process actually announces — never what the
configuration now says — and names the pending value beside it when the two
disagree. So a robot that has just been renamed is not described as renamed, and
one whose identity has been cleared is not described as announcing nothing.

### Upgrading an existing installation

Set it to whatever the previous application announced. Home Assistant shows it on
the device page, and it is the prefix of every entity identifier belonging to the
device — an entity called `sensor.reachy_mini_1_wake_word` was announced by a
device called `reachy-mini-1`.

Pin the hardware address as well if the robot's network interface has changed.
Home Assistant keys the device on that too, so a satellite moved to new network
hardware announces a new device even under the old name:

```
REACHY_SATELLITE_DEVICE_NAME=reachy-mini-1
REACHY_SATELLITE_MAC_ADDRESS=02:00:5e:10:00:00
```

Left unset, the hardware address is read from the network interface at startup
and reported in the boot log and on the settings page, so the value to pin is
always visible.

### A new robot

Choose a name now and never change it:

```
REACHY_SATELLITE_DEVICE_NAME=reachy-mini-1
```

Only the *announced* name is dangerous. `REACHY_SATELLITE_FRIENDLY_NAME` is the
display name and is safe to change whenever you like — Home Assistant renames the
device rather than replacing it.

---

## Installing

Two routes, one outcome.

| | Route A — the robot's own surfaces | Route B — a wheel |
|---|---|---|
| Needs a shell on the robot | No | Yes |
| Needs the application source published | Yes | No |
| Where the commands run | Your machine, over the robot's HTTP API | On the robot |
| Use it for | A stock robot, and any robot you would rather not open a shell on | Iterating on the application, and any build that is not a release |

Route B is the older one and is unchanged. Route A is what makes a stock robot
installable at all, and it is the one to read first.

---

## Route A: install from the robot's own surfaces

> **⏳ PENDING HARDWARE VERIFICATION.** No transcript is recorded for any step in
> this route. The requests, their bodies and the daemon's behaviour are read out
> of the released daemon's own source — `reachy-mini` 1.9.0, which is what
> ReachyMiniOS v0.2.3 runs — and **not** from having run them against a robot.
> Nothing in this repository has a Reachy Mini attached, and the Space itself
> has not been published, so this route has never been executed end to end.
> [`docs/tasks.md`](../tasks.md) carries it.

Nothing here opens a shell on the robot, copies a file onto it, or changes a
file its image ships. Every request goes to the daemon's own HTTP API, which
answers on port **8000**, and every one of them can equally be made from the
daemon's dashboard where the dashboard offers it.

### A1. Know the Space

The application source is published as a Hugging Face Space named
`<owner>/reachy-mini-ha-satellite`. It is three files and no code — a project
manifest, the Space's card, and the static page the Space serves — and the
manifest names one released wheel, so installing it installs that wheel. It is
committed at [`apps/ha-satellite/app-source/`](../../apps/ha-satellite/app-source/)
and published from there, so what a robot installs is reviewable here —
[Publishing the application source](#publishing-the-application-source) is how it
gets there.

**The Space's name is not cosmetic.** The daemon stores an installed
application's metadata under the Space's repository name and reads it back under
the application's entry-point name, so a Space called anything other than
`reachy-mini-ha-satellite` installs and then cannot find what it recorded about
itself. `just publish-app-source` refuses to publish to any other name.

You do not need to be in the manufacturer's curated application list. The
dashboard's *browse* list is limited to that list, but the install endpoints
below take any Space id.

### A2. Give the robot a token, if the Space is private

A **public** Space needs no token and no account: skip this step.

A **private** Space is installed through an endpoint that requires a token
stored on the robot first, and answers `401 No HuggingFace token found` without
one. The dashboard has a Hugging Face login that stores it; the same thing over
the API is:

```
curl --request POST http://<robot>:8000/api/hf-auth/save-token \
  --header 'content-type: application/json' \
  --data '{"token": "<a Hugging Face token with read access>"}'
```

The token is stored on the robot. Treat it the way you would treat any other
credential the robot holds — see the trust model under
[the settings page](#-it-is-unauthenticated-and-so-is-the-rest-of-the-robot).

### A3. Ask the daemon to install it

For a **public** Space:

```
curl --request POST http://<robot>:8000/api/apps/install \
  --header 'content-type: application/json' \
  --data '{
    "name": "reachy-mini-ha-satellite",
    "source_kind": "hf_space",
    "url": "https://huggingface.co/spaces/<owner>/reachy-mini-ha-satellite"
  }'
```

For a **private** one, with the token from A2 already stored:

```
curl --request POST http://<robot>:8000/api/apps/install-private-space \
  --header 'content-type: application/json' \
  --data '{"space_id": "<owner>/reachy-mini-ha-satellite"}'
```

Both answer with a job identifier — `{"job_id": "..."}` — and do the work in the
background.

**What the daemon then does**, because it is worth knowing before reading an
install log: it downloads the Space, warns if the directory it got has neither a
`pyproject.toml` nor a `setup.py` in its root, and runs `uv pip install` over it
(falling back to `pip` when `uv` is absent) against the interpreter of the
robot's **shared application environment** — a sibling of the daemon's own, which
it creates and pre-populates with `reachy-mini` the first time an application is
installed. Nothing is installed into the daemon's own environment, and no file
the image ships is touched.

### A4. Watch the job

```
curl http://<robot>:8000/api/apps/job-status/<job_id>
```

The install downloads a wheel and resolves its dependencies from a package
index, so on a robot's connection it is minutes rather than seconds.

### A5. Start it

From the dashboard's application list, or:

```
curl --request POST http://<robot>:8000/api/apps/start-app/reachy-mini-ha-satellite
```

The application is listed under its entry-point name, which is the name above,
whatever the Space was called.

**The daemon does not report why an application stopped**, and that is the single
most misleading thing about this route as well as the other one — see
[How the daemon starts it](#how-the-daemon-starts-it) before concluding anything
from `state: done`.

### A6. Name the robot, in a browser

The application starts **with no identity and announces nothing**. Open its
settings page at `http://<robot>:8088/`; it heads itself *This robot is not
configured yet* and says so in as many words.

Set `REACHY_SATELLITE_DEVICE_NAME` there — read
[the warning at the top of this page](#-before-you-install-pin-the-announced-identity)
first, because this is the one value that cannot be changed later without cost —
then press **Stop** on that page and start the application again from the
dashboard. The identity is read while the announcement is being built, so it
takes effect at the next start. No shell, and no reinstall.

### A7. Point it at a groundstation, if you have one

On the same page, set the groundstation address and its credential. Both are
needed; either one missing means no session is opened, which the page reports as
*unconfigured* rather than failed. Supplying them is adopted **without a
restart** — see
[Replacing the groundstation while the satellite runs](#replacing-the-groundstation-while-the-satellite-runs),
which is the same transition.

Without one, the robot runs on its own detector where
`REACHY_SATELLITE_LOCAL_MODEL_PATH` names weights, and sees nothing until a
groundstation arrives where it does not. Both states are on the page.

### Upgrading an installation made this way

Publish the newer source, then install it again exactly as in A3 — the source
names the wheel by version, so a newer source is a newer wheel. The daemon's own
update path (its dashboard's update control, `POST /api/apps/update/<app_name>`)
reinstalls the Space in place and is the shorter route where it works.

The announced identity and everything else the settings page wrote live in the
robot's state directory, outside the installation, so they survive either.

### ⚠️ What the daemon's Uninstall leaves behind

Removing the application from the dashboard uninstalls the distribution named by
the entry point — `reachy-mini-ha-satellite`, the wheel — which is what makes the
application disappear from the list, because the entry point goes with it. The
metadata-only distribution the Space installs alongside it,
`reachy-mini-ha-satellite-app-source`, is not named by any entry point and stays
in the shared application environment. It carries no code and no configuration
and does nothing; installing again replaces it. It is recorded here because
"uninstalled" and "no trace left" are not the same sentence, and the difference
is the kind of thing that costs somebody an hour later.

---

## Route B: install a wheel

Every command below runs on the robot. The addresses are from the RFC 5737
documentation ranges; substitute your own.

### 1. Fetch the wheel

```
curl --location --remote-name \
  https://github.com/<owner>/<repository>/releases/download/v<version>/reachy_mini_ha_satellite-<version>-py3-none-any.whl
```

**`--remote-name`, not `--output`.** Keeping the published file name is what
makes the digest check in the next step work at all — see the note there.

Every artifact in a release carries the same version, so the wheel that goes with
a given groundstation image is the one under the same tag.

### 2. Check you have the artifact the release built

The release publishes a `SHA256SUMS` file beside the wheel. Fetch it and check
the digest before installing: the wheel goes into the environment the daemon runs
applications from, so it runs with the daemon's access.

```
curl --location --remote-name \
  https://github.com/<owner>/<repository>/releases/download/v<version>/SHA256SUMS
sha256sum --check --ignore-missing SHA256SUMS
```

```
./reachy_mini_ha_satellite-0.1.0-py3-none-any.whl: OK
```

One `OK` line per wheel present. Anything else means the file you have is not the
file that was published, and it should not be installed.

⚠️ **`no file was verified` is the failure to watch for.** The release job writes
that file with `sha256sum ./*.whl`, so every line in it names a versioned wheel
relative to the directory the check runs in. A wheel renamed on download matches
no line, and `--ignore-missing` then reports that message rather than a
mismatch — which reads like success if you are not looking. That is why the fetch
above uses `--remote-name`.

Every wheel in the release is covered, not only this one.

### 3. Install it into the daemon's application environment

```
/opt/reachy/venv/bin/pip install --upgrade \
  ./reachy_mini_ha_satellite-<version>-py3-none-any.whl
```

The environment is shared with the daemon and with any other application, so the
wheel's dependencies coexist with theirs. Nothing here creates a virtual
environment of its own.

### 4. Restart the daemon

```
sudo systemctl restart reachy-mini-daemon
```

The application appears in the daemon's list of installed applications. Nothing
else registers it.

---

## Publishing the application source

This is a maintainer's step, not an operator's, and it happens once per release
rather than once per robot. Route A above installs whatever it finds published;
this is what puts it there.

**It is not done by continuous integration**, and that is deliberate rather than
pending: publishing needs a Hugging Face account, and this repository holds no
token for one. The account and the one-time authentication are a person's.

### Release first, then publish

The source names a **release asset** — the wheel, by version, under its tag — so
a Space published before the release it names points at a file that is not
there, and the robot finds out several minutes into an install. `just wheels`
and the release workflow produce the wheel; publish after the release carrying
it exists.

> ⛔ **This repository has no releases at all today, so the Space cannot be
> published yet.** There are no tags, `gh release list` is empty, and the
> release workflow's publishing job has been skipped on every run it has ever
> made, because it is gated on a tag that has never existed. The cause is
> release automation rather than anything on this page — a merged release pull
> request that was never tagged, which makes release-please abort before
> opening another — and it is recorded with its evidence in
> [`docs/tasks.md`](../tasks.md). **Nothing below is wrong; it simply cannot
> complete until that is fixed**, and the third refusal in the next section is
> exactly what trying produces.

```
export HF_TOKEN=<a Hugging Face token with write access>
export REACHY_APP_SPACE_ID=<owner>/reachy-mini-ha-satellite
just publish-app-source --dry-run
just publish-app-source
```

`--dry-run` runs every check, including asking the release for the wheel, and
stops before creating or writing to the Space. Neither the token nor the target
is committed: both name an account, and this repository is public.

### What it refuses, and what that looks like

Every refusal happens before the Space is created or written to, and all but
one before anything at all is contacted: no token; a Space named something the
daemon will not find its own metadata under; a source whose version has drifted
from this checkout, which is what a half-applied release bump looks like; a
modified, deleted or untracked file under the source, because what is published
has to be what somebody reviewed; and a wheel from a repository other than the
one this checkout releases from, because a version is not an identity and
another repository's release under the same tag would install somebody else's
code on every robot that took the Space.

The remaining one asks the release asset with `HEAD`, follows GitHub's redirect
for it as a `HEAD` as well, and reads no body: a dry run cannot download the
wheel it is asking about. That is why every one of them is covered by tests in a
workspace with no account.

Three of them, run here with nothing exported, so each command carries what it
needs:

```
$ just publish-app-source --dry-run
publish-app-source: no Hugging Face token: set HF_TOKEN or HUGGING_FACE_HUB_TOKEN to a token with write access to the Space. Nothing is contacted without one
```

```
$ HF_TOKEN=<placeholder> REACHY_APP_SPACE_ID=<owner>/ha-satellite just publish-app-source --dry-run
publish-app-source: REACHY_APP_SPACE_ID names the Space 'ha-satellite', and it has to be 'reachy-mini-ha-satellite': the daemon saves an installed application's metadata under the Space's name and reads it back under the entry-point name, so any other name installs and then cannot find what it recorded
```

```
$ HF_TOKEN=<placeholder> REACHY_APP_SPACE_ID=<owner>/reachy-mini-ha-satellite just publish-app-source --dry-run
publish-app-source: https://github.com/<owner>/<repository>/releases/download/v<version>/reachy_mini_ha_satellite-<version>-py3-none-any.whl answered 404: the release this source names does not carry that wheel yet. Publish the Space after the release, not before
```

Those are transcripts, with the owner, the repository and the version replaced
by their placeholders, the token that was passed replaced by the word
`<placeholder>` — it never was one, and no request carrying it was made — and
`just`'s own echo of the command it runs cut. **The third one is the current
state of this repository**: no release has ever been cut, so the wheel the
committed source names does not exist and publishing is correctly refused until
one does. Why there is no release is a fault in release automation rather than
in anything here, and it is [`docs/tasks.md`](../tasks.md)'s to fix.

> **⏳ PENDING HARDWARE VERIFICATION.** A successful publish has never been run,
> and strictly what is missing is an account rather than hardware: no output for
> the command's success path is recorded, and nothing below the refusals above
> is a transcript. The Space itself does not exist yet, which is why every step
> of Route A is marked the same way.

### What a publish does

It creates the Space if it is not there — a static Space, which is what a
Reachy Mini application source is — and then makes its contents the files git
tracks under that directory, deleting anything else on the Space that this
checkout does not have. A file withdrawn here is withdrawn there, which is what
lets this page say that what an operator installs is what is reviewable in this
repository.

One remote file survives that, and it is the Hub's own: `upload_folder` never
deletes a `.gitattributes`, whatever it is told to delete, and the Hub writes
one into every repository it creates. It reaches no robot — the daemon's
downloader ignores `.gitattributes` explicitly — so the installed source is
still exactly what is tracked here.

Both halves of that are deliberate. A modified, deleted or untracked file under
the directory is refused outright, because bytes nobody has reviewed are exactly
what this route promises not to ship. An **ignored** file is not refused — a
`build/` or an `.egg-info/` is what installing the source locally leaves behind,
which is a thing this page asks a maintainer to do — and what keeps it off the
Space is the allow-list rather than a refusal: the client reads no `.gitignore`,
so without one it would publish the lot.

---

## How the daemon starts it

Worth knowing before reading a log that says nothing, because the mechanism is
not the one the entry point's spelling suggests.

The entry point is written `<module>:<class>`, but **the daemon does not import
that module and instantiate that class in its own process.** It reads the
*module* half — everything left of the colon — and starts the application as a
separate process, equivalent to:

```
<the application environment's python> -u -m reachy_mini_ha_satellite.daemon_app
```

Three consequences follow, and each has cost time already.

**The application runs as its own process, with the daemon's environment.** So
`REACHY_SATELLITE_*` variables have to be visible to the daemon — see
[Configuring](#configuring) — and anything the application writes is captured
from that process rather than written to the daemon's own log.

**Startup performs the SDK's full controlled wake before opening normal
services.** It enables the motors, then asks the SDK to run its wake motion and
sound. A failure in either step aborts startup rather than leaving a satellite
advertised while its robot is still asleep. The movement and sound are therefore
expected effects of starting the application, not evidence that Home Assistant
has begun a voice pipeline.

Startup then reads physical torque back for each of the three motor groups —
head, body, antennas — and announces a Home Assistant switch **only for a group
that answered**. A group that did not is left with its command gate closed and no
entity, which is why the switches are the one part of this application that
depends on which `reachy-mini` the daemon environment holds: the released
versions send torque commands fire-and-forget and return `None`, so nothing
answers and no switch appears. See
[the README](../../README.md#-the-three-motor-switches-need-a-forked-reachy-mini-for-now)
for the build that does answer and for when this note stops being true.

**⚠️ The daemon does not report why an application stopped.** It reports
`state: done` with a **null error** whether the application ran and finished, or
refused to start and exited non-zero, or raised. An exception the application
raised does not reach the daemon's API at all. So through the daemon,
*successfully finished* and *failed at startup* are the same reading, and the
absence of an error is not evidence that nothing went wrong. This is the single
most misleading thing about diagnosing the satellite from the dashboard.

**The module named by the entry point has to be runnable on its own.** A module
that is only importable exits 0 immediately under that command, having printed
nothing — not even the boot configuration dump this application always writes.
Combined with the point above, that is indistinguishable from a clean run, which
is exactly why it took a robot and a hand-patched file to find.

`just wheel-verify` runs the entry point's module the way the daemon runs it and
refuses a wheel where it exits 0 having done nothing, so a release cannot carry
that defect silently.

---

## Configuring

Configuration is read from the daemon's environment, with one layer above it that
the application's own settings page writes. In precedence order, lowest first:

| Layer | Where it lives | Written by | Survives |
|---|---|---|---|
| Defaults | The wheel | — | Everything |
| Environment | The daemon's managed drop-in | `reachyctl config apply`, or the Ansible `daemon_env` role | A reinstall of the wheel |
| Overrides | `<state_dir>/settings.json` | The settings page | A reinstall of the wheel, but not a re-image |

The settings page shows which layer each value came from. Saving a value back to
what the environment says removes the override rather than pinning a duplicate of
it.

**The two upper layers are written by different tools, and neither touches the
other's file.** The environment layer is the managed drop-in described in
[the managed daemon environment](managed-daemon-environment.md), which is owned
in full: `reachyctl config apply` rewrites it whole, so anything written there by
hand — or by this application — is discarded on the next apply. The settings page
therefore writes its own file instead, and the separation is what lets both keep
working. It also means a setting changed on the page **wins over a later
`reachyctl config apply` of that same setting**, because the override sits above
the environment. The page reports the layer for exactly this reason: a value
showing `override` is one `reachyctl` can no longer change, until the page is
used to save it back to what the environment says.

**Four bootstrap settings are readable on the page and not writable there**,
because they decide whether the page can be reached at all. This is separate
from the four retired gaze compatibility inputs, which are also read-only but
because predictive gaze ignores them. An override sits *above* the
environment, so an override can only be undone by writing another one — and a
page that had written one of these wrongly would be the page you could no longer
open to undo it. Set them in the environment:

| Setting | Why the page cannot write it |
|---|---|
| `REACHY_SATELLITE_STATE_DIR` | It names the directory the overrides file lives in, so an override would be a file saying where to look for itself. Startup would keep reading the old location while the page wrote to the new one, and the first ordinary save after that would drop the credential. |
| `REACHY_SATELLITE_WEB_ENABLED` | Saving `false` leaves no interface to change it back with. |
| `REACHY_SATELLITE_WEB_HOST` | A host the robot has not got binds nowhere reachable. |
| `REACHY_SATELLITE_WEB_PORT` | The same, on a port nothing is listening on. |

Moving the state directory means moving the files in it, and moving the interface
means knowing where it went — neither is something a web form could finish
anyway.

**A variable under `REACHY_SATELLITE_` that names nothing is fatal at startup**,
and the refusal names the variable. That is architecture
[REQ-009](../specs/architecture/index.md#req-009-configuration-is-validated-and-self-reporting)
and it exists because the predecessor's configuration reader was never called, so
every value that looked tuned was in fact a default — for months, silently.

"Names nothing" and "names no setting of this application" are not the same
thing, and the difference matters on a real robot. `reachyctl config apply`
writes the whole documented vocabulary — `reachy_contracts.settings`
(`ROBOT_SETTINGS`) — and some of those names fall under this application's prefix
without being settings it reads. Those are **accepted and reported, not
refused**: a correctly-configured robot has to start. The startup log and the
settings page both name them, saying that setting them has no effect here, so a
variable that appears to do nothing has a written answer rather than being a
mystery. A name in neither set is a typo, and a typo is still fatal.

The fully resolved configuration is emitted at startup, and served at
`/config` on the settings port. Secrets appear on both as `<set>` or `<unset>`,
never by value.

### Every setting

| Variable | Default | What it is |
|---|---|---|
| `REACHY_SATELLITE_DEVICE_NAME` | **none — nothing is announced until it is set** | The identity announced to Home Assistant. **Needs a restart.** See the warning above. |
| `REACHY_SATELLITE_FRIENDLY_NAME` | the announced identity | The display name. Safe to change. |
| `REACHY_SATELLITE_MAC_ADDRESS` | read from the interface | The hardware address announced. Home Assistant keys the device on it too. |
| `REACHY_SATELLITE_NETWORK_INTERFACE` | the default route's | Which interface the address and the mDNS record are taken from. |
| `REACHY_SATELLITE_API_HOST` | `0.0.0.0` | Where the ESPHome native API binds. |
| `REACHY_SATELLITE_API_PORT` | `6053` | The port Home Assistant looks for. |
| `REACHY_SATELLITE_ADVERTISE` | `true` | Whether to advertise over mDNS, which is how Home Assistant discovers the robot. |
| `REACHY_SATELLITE_WEB_ENABLED` | `true` | Whether the settings page is served. **Environment only.** |
| `REACHY_SATELLITE_WEB_HOST` | `0.0.0.0` | Where the settings page binds. **Environment only.** |
| `REACHY_SATELLITE_WEB_PORT` | `8088` | The settings page's port. **Environment only.** |
| `REACHY_SATELLITE_STATE_DIR` | `~/.local/state/reachy-mini-ha-satellite` | Preferences, downloaded media, and the overrides the settings page writes. **Environment only** — see below. |
| `REACHY_SATELLITE_ACTIVE_WAKE_WORD` | `okay_nabu` | Which shipped wake word listens. Home Assistant can add more at run time. |
| `REACHY_SATELLITE_SAMPLES_PER_CHUNK` | `160` | Samples per channel in one capture chunk. |
| `REACHY_SATELLITE_FACE_TRACKING_ENABLED` | `true` | Whether predictive gaze follows a face at all. **Needs a restart.** |
| `REACHY_SATELLITE_BODY_MOTION_ENABLED` | `false` | Whether predictive gaze coordinates body yaw with its world head target. Provisional, explicit opt-in, and **needs a restart**. |
| `REACHY_SATELLITE_DETECTION_SOURCE` | `remote` | `remote`, `local`, or `remote_with_local_fallback`. |
| `REACHY_SATELLITE_GROUNDSTATION_URL` | none | Where the groundstation serves its session endpoint. `ws://` or `wss://`, with no user information, query or fragment, and **at most 255 characters** — see below. Unset means no session is opened, which is *unconfigured* rather than failed. **Applies at once**: it is changeable from the settings page and from Home Assistant without a restart. |
| `REACHY_SATELLITE_GROUNDSTATION_CREDENTIAL` | none | **Secret.** The shared secret presented to open a session. One half without the other opens nothing, so an address with no credential is unconfigured too. **Applies at once**: supplying, clearing or rotating it re-opens the session, so it is the one secret that needs no restart. |
| `REACHY_SATELLITE_FRAME_INTERVAL_SECONDS` | `0.1` | How often a frame goes up to the groundstation. |
| `REACHY_SATELLITE_STALENESS_SECONDS` | `2.0` | How long a detection stays worth acting on. Past it the head returns to neutral. |
| `REACHY_SATELLITE_LOCAL_MODEL_PATH` | none | The face-detection weights the robot's own detector loads. Required unless the source is `remote`. |
| `REACHY_SATELLITE_LOCAL_SCORE_THRESHOLD` | `0.6` | Confidence a locally-detected face must reach. |
| `REACHY_SATELLITE_LOCAL_NMS_THRESHOLD` | `0.3` | Overlap at which the lower-scoring local box is suppressed. |
| `REACHY_SATELLITE_LOCAL_DETECTION_INTERVAL_SECONDS` | `0.2` | How often the local detector looks. |
| `REACHY_SATELLITE_CAMERA_HORIZONTAL_FOV_DEGREES` | `87.0` | Legacy compatibility; accepted and validated but ignored by daemon-calibrated predictive gaze. |
| `REACHY_SATELLITE_CAMERA_VERTICAL_FOV_DEGREES` | `67.0` | Legacy compatibility; accepted and validated but ignored. |
| `REACHY_SATELLITE_BEHAVIOUR_TICK_SECONDS` | `0.05` | How often the predictive trajectory advances. |
| `REACHY_SATELLITE_GAZE_DEADZONE` | `0.02` | Legacy compatibility; accepted and validated but ignored by predictive control. |
| `REACHY_SATELLITE_GAZE_SMOOTHING` | `0.35` | Legacy compatibility; accepted and validated but ignored. |
| `REACHY_SATELLITE_IDLE_SECONDS` | `6.0` | How long without a face before the idle behaviour starts. |
| `REACHY_SATELLITE_LOG_LEVEL` | `info` | `debug`, `info`, `warning` or `error`. |

**One setting is secret**: `REACHY_SATELLITE_GROUNDSTATION_CREDENTIAL`. It is
reported as set or unset everywhere it is reported, and its value appears in no
log line, no page and no error message.

### The groundstation address is capped at 255 characters

Home Assistant reports the address as a text entity, and an ESPHome text state
carries at most 255 characters. One bound therefore applies everywhere the
address is accepted: the settings model, the settings page's field, the Home
Assistant control's declared maximum, and both submission paths. It is declared
once, in `reachy_contracts`, so `reachyctl config` and the provisioning
declaration validate against the same number.

Nothing is truncated. A shortened address is a different groundstation, so an
address that does not fit is refused rather than trimmed.

**Upgrading from a release that accepted 512 characters.** The previous settings
model allowed twice this length. If an existing installation has one between 256
and 512 characters — in the daemon's environment or in the overrides file the
settings page writes — the upgraded application **refuses to start** and says
which of the two layers holds it and what to do about it. The refusal never
repeats the address, because the address is reported by value on every surface
and one that cannot be represented must not reach the boot log.

Two remedies, one per layer:

- **Environment.** Set `REACHY_SATELLITE_GROUNDSTATION_URL` to an address of 255
  characters or fewer, or unset it, and start the application again. On a robot
  provisioned by `reachyctl`, change it there — see the
  [robot runbook](../setup/robot.md).
- **Persisted override.** The settings page wrote it. Remove the
  `groundstation_url` entry from `<state_dir>/settings.json`, or replace it with
  a shorter address, and start the application again.

*Pending hardware verification: the transcript of a robot refusing to start on a
legacy over-long address has not been recorded, because nothing in this
repository has a Reachy Mini attached. The behaviour is covered by
`apps/ha-satellite/tests/test_satellite_config.py` at 255, 256 and 512
characters and from both layers.*

### Replacing the groundstation while the satellite runs

Both surfaces — the settings page and Home Assistant's `groundstation_url` text
control — hand the whole submission to one owner, which performs the change in a
fixed order:

1. resolve and validate the candidate, refusing an over-long or unusable address
   before anything is built or written;
2. cancel and await any restoration attempt still running;
3. build the replacement source;
4. retire and close the preceding one;
5. start the replacement;
6. **atomically replace the durable settings file** — this is the commit point;
7. publish the new address as the effective read-back.

**The durable write is last on purpose.** Persisting first and adopting second
would let a change the running application rejected become the file the next
start reads, so a robot would recover into the configuration that broke it.

**On a running satellite, every change to the settings file is serialized
against that replacement**, including changes that have nothing to do with the
address. The replacement is the only change that pauses partway — it has a
session to close and another to open between reading the file and rewriting it —
so a control that wrote the file on its own during that pause would have its
setting discarded when the replacement finished. The speaker-boost control is
the one this matters for today: setting the address and the boost together, from
a scene or from two automations, leaves both in the file rather than whichever
finished last.

**Any failure before step 6 leaves the file untouched**, and the preceding
address stays durable, effective and what both surfaces report. The owner then
rebuilds the preceding source from the configuration it retained. If that
rebuild fails it retries a bounded number of times with an increasing, capped
delay, closing each partly built source before the next attempt, and stops. What
is true after it stops:

- the effective and durable address is still the preceding one;
- remote health is **unavailable** — there is no session client, rather than a
  broken one;
- local detection remains available wherever
  `REACHY_SATELLITE_DETECTION_SOURCE` selects a fallback;
- no unbounded recovery work and no second, overlapping client is left behind.

**Recovering from that state without a restart:** submit the address again from
either surface. Submitting the address that is already in effect writes nothing
— there is nothing to write — but where there is no source behind it, it begins
one fresh bounded restoration, which is the natural thing to do once the
groundstation is back. A submission that arrives while a restoration is pending
cancels and awaits it first, and shutdown does the same, so a source built too
late is closed rather than installed.

**A source that will not close is not treated as retired.** If closing the
preceding source raises, nothing knows whether the session behind it is gone, so
the owner refuses to build or install another one rather than risk two clients.
The transition is refused, the preceding address stays durable and effective,
remote health reads unavailable and local detection keeps working. The next
submission tries the close once more; when it succeeds, that submission proceeds
normally.

**The address is changeable only while the application is running.** The
settings interface can also be served with nothing behind it, and in that mode
it refuses an address change rather than persisting one that nothing has opened
a session at — the next start would read a value no running application ever
accepted. Every other setting still saves. Set
`REACHY_SATELLITE_GROUNDSTATION_URL` in the drop-in instead, or change it from
the running satellite.

**A submission that would leave no groundstation source at all is refused,
unless unconfiguring the groundstation is what it asked for.** Whether a session
exists is decided by `REACHY_SATELLITE_FACE_TRACKING_ENABLED` and
`REACHY_SATELLITE_DETECTION_SOURCE`, both of which take effect at the next start.
Changing one of them on its own is stored and badged "needs a restart", and the
running source is untouched. Changing one of them **together with the address**
would retire the running source into nothing without having been asked to, so it
is refused with a message saying to submit them without the address. Nothing is
written and the running source keeps answering.

**Clearing the address, or clearing the credential, is the exception**, because
there the empty result is the request: the running source is retired, nothing
replaces it, and the remote detector goes back to *unconfigured*. Both halves
count — a session needs an address and a credential — so removing either one
retires the source rather than leaving it answering under a value the operator
has just taken away. **Rotating a credential** is the same transition for the
same reason: the address is unchanged and the groundstation is still configured,
but a robot that went on authenticating with the secret you had just replaced
would be the revoked-credential case one step along, so the session is re-opened
with the new value rather than at the next start.

This is compensation rather than a transaction: a filesystem and a network
cannot be committed together. What holds after every outcome is that the durable
value, the one eligible remote source and both read-backs describe the same
address.

*Pending hardware verification: replacing a live groundstation against a real
robot and a real Home Assistant has not been run, so no transcript is recorded
here. The ordering, every failure point and the bounded restoration are covered
by `apps/ha-satellite/tests/test_satellite_groundstation_url.py`.*

Predictive gaze consumes each source-qualified detection once, calibrates its
image point against measured capture and query poses without moving, and then
advances one jerk-limited world-yaw/elevation trajectory at the behavior cadence.
Measured-pose history derives its retention age from the configured staleness
window and sizes its capacity for that window at the minimum supported behavior
tick, so any supported fresh capture can be rebased without extrapolation. The first hardware sample is seeded from measured world
head pose; body-enabled motion additionally waits for valid measured body yaw.
It owns the head through active tracking, loss hold and neutral return; antennas
continue expressing pipeline state, and the current pipeline head pose receives
one handoff only after the return settles. The daemon's automatic body yaw is
disabled before either head-only or coordinated gaze takes ownership and restored
once during terminal shutdown. With face tracking disabled at startup, none of
those acquisition, feedback or automatic-yaw calls are made.

The four legacy field-of-view, deadzone and smoothing variables remain valid only
so existing environments keep starting. The startup report and settings page mark
them `legacy compatibility; ignored`; the form renders them read-only, and an
ordinary save removes stale copies from the overrides file. They do not enter the
predictive controller. Body motion is separately restart-bound, disabled by
default and remains a provisional opt-in because the completed rollout did not
settle calibration or a shipping-default decision.

### Detection source

`remote` is the default, and the reason is measured rather than aesthetic: with
detection offloaded to a groundstation the predecessor's robot sat at 1.52 of its
four cores; with detection local it saturated. The robot is running motion
control, audio and a wake-word model at the same time.

The weights the local detector needs are **not shipped in the wheel** — they are
somebody else's model under somebody else's terms — so `local` and
`remote_with_local_fallback` both need `REACHY_SATELLITE_LOCAL_MODEL_PATH`
pointing at a file on the robot, and the application refuses to start without it
rather than starting and never tracking anything.

**A missing groundstation is different, and it is not a refusal.** `remote` and
`remote_with_local_fallback` both use an address *and* a credential, and either
one unsupplied means no session is opened — which is *unconfigured* rather than
failed, in the same sense `reachyctl doctor` distinguishes a skipped check from a
failed one. Nothing connects, nothing retries, and `/status` reports
`remote: unconfigured`.

**Until one is supplied, the robot runs on its own detector** — including under
`remote`, which is the default. That selection means "the groundstation
answers", and while there is no groundstation the honest reading is the robot's
own camera rather than nothing, so the satellite composes the fallback instead
and the `remote` choice reasserts itself the moment a session exists. It needs
`REACHY_SATELLITE_LOCAL_MODEL_PATH` pointing at weights on the robot; with none
set there is nothing local to run and the robot sees nothing until a
groundstation arrives, which the settings page and `/status` both say. A process
that started unconfigured keeps the fallback for its lifetime, which does strictly
more than `remote` asked for: the groundstation while the session is up, the
robot while it is not.

Supplying both from the settings page is adopted **without a restart**, by the
same transition that replaces an address on a running robot — so a first
groundstation and a replacement are one code path rather than two. Clearing the
address retires the running source the same way.

The other three states `/status` can report are `available` (a source is
connected or connecting), `disabled` (face tracking is off, or the robot's own
detector is the selection, so there is deliberately no remote source) and
`unavailable` (a groundstation is configured and there is no source, which is the
one that means something went wrong).

**The address carries no credential.** `ws://someone:secret@host/v1/session` is
refused, as are a query and a fragment. The address is not a secret setting, so
it is printed in the boot log and shown on the settings page — and no redactor
can remove a credential it was never given. The credential has a setting of its
own, and that one is never printed.

---

## The settings page

`http://<robot>:8088/`, and the daemon's dashboard links to it.

### ⚠️ It is unauthenticated, and so is the rest of the robot

Anything that can reach the robot can open this page, read the resolved
configuration, change a setting and stop the application. That is the same trust
model the rest of the robot already has — the ESPHome API this application serves
announces `uses_password=false`, and the daemon's own dashboard is likewise open
— and **the trust boundary is the network the robot is on**. Put the robot on a
network you trust, the way you would a printer.

What the interface does close is the exposure that needs no peer on that network
at all: any page a browser visits can submit a form to any address that browser
can reach. A request a browser reports as coming from another site is refused, so
a web page cannot stop the robot or replace its credential because somebody with
a laptop on the same network happened to open it. Setting
`REACHY_SATELLITE_WEB_ENABLED=false` switches the interface off entirely, leaving
the environment as the only way to configure the application.

**It is also the first-time configuration surface**, which is why it is served
before an identity exists. On a robot with nothing configured it heads itself
*This robot is not configured yet*, says that nothing is announced to Home
Assistant until `device_name` is set, and takes the value; and when the
groundstation is unsupplied it says that the remote detector is unconfigured
rather than failed. An identity supplied here needs the application stopped and
started again from the robot's dashboard before it is announced, and the page
says that too.

It reads **every** operator-facing setting, and writes every one but the four the
page's own existence depends on — see above for why those are set in the
environment. The credential is the
exception to *reading*: it shows as set or unset, its field is always blank, and
submitting it blank leaves it alone — so the page is usable for rotating a
credential and useless for learning one. A separate control unsets it.

| Path | What it is |
|---|---|
| `/` | The settings form and the resolved configuration |
| `/config` | The resolved configuration as JSON, secrets redacted, with which settings are secret, which apply at once, which bootstrap values are read-only and which compatibility inputs are ignored |
| `/status` | What the robot is doing: pipeline and gaze state, controller mode, fault and derived safe hold, whether the configured identity is `resolved` or `unresolved`, whether this process is `announcing` and `announced_as` which identity, the remote detector's state, and the motion-gating mode in force with the bounded reason for it |
| `/diagnostics/controller` | `GET` — bounded scalar controller events with no image, credential or installation identity |
| `/diagnostics/controller/reset` | `POST` — same-origin diagnostics-only reset; it does not move the robot or change controller state |
| `/stop` | `POST` — stops the application so a restart-required change can take effect |
| `/livez` | Whether the interface is up |

Settings marked *applies at once* are swapped into the running application.
Everything else is read while something is being built — a socket bound, a
session opened, a detector loaded, an identity announced — so it takes effect at
the next start, and the page says so per setting and offers a **Stop**.

**Stopping is not restarting.** The daemon marks a cleanly-exited application
`done` and leaves it stopped; nothing relaunches it. So after pressing Stop,
start the satellite again from the robot dashboard's application list. That is a
web interface too, so no remote shell is involved either way.

**What survives what.** Overrides written from this page live in
`<state_dir>/settings.json`, outside the wheel, so reinstalling the application
keeps them. They do not survive re-imaging the robot. Anything that has to
survive that belongs in the daemon's environment, managed by `reachyctl` — see
the [reachyctl spec](../specs/reachyctl/index.md).

The file holds whatever secrets were typed into the page, in plain text and with
owner-only permissions. That is the same trust level as the environment the
daemon starts the application with: anything that can read the file can already
read that environment.

---

## What the robot does, and how to read it

The robot's antennas say what the voice pipeline is doing, and they say it in a
way that is legible from across a room:

| State | The antennas | The head |
|---|---|---|
| Idle, somebody about | still, at rest | following a face |
| Idle, alone for a while | a slow symmetric sway | neutral |
| **Listening** | both raised, and **still** | following a face, or slightly raised |
| **Processing** | **counter-rotating** — one rises as the other falls | lowered, drifting |
| **Responding** | both **bobbing together**, twice as fast | following a face, or nodding |
| Error | a fast opposed shake, for about a second and a half | a slight roll |
| Muted | folded down, and held | neutral |
| Disconnected | drooped, and held | lowered |

Still, opposed, together — the three the spec requires be distinguishable differ
in the *kind* of motion rather than in its size, because that is what a person
reads first.

**A head that returns to neutral is a signal, not a failure to move.** When face
detections stop arriving within the staleness window, the head goes back to
looking straight ahead rather than holding its last pose. Holding would look like
successfully tracking somebody who has left the room; neutral is an honest
statement that something upstream stopped. `/status` says which of the four
situations it is: `tracking`, `nobody` (a live detector reported an empty frame),
`stale` (results stopped arriving), or `unknown` (nothing has produced a result
yet).

---

## Predictive gaze canary and rollback

This is the reusable rollback contract for private head-only and
coordinated-body canaries. It declares the decision thresholds before motion
begins and remains the procedure for later candidate evaluation.

The current staged rollout ran to completion. Its scrubbed aggregate evidence is
recorded in the [change 0019 outcome](../changes/0019-predictive-gaze-and-coordinated-motion.md#outcome).
No transcript is supplied here; raw evidence and installation-specific details
remain private.

### Retain the rollback target first

Before installing a candidate, complete this preflight transaction:

1. retain the last released satellite wheel and its published checksum in private
   storage, and verify the retained wheel against that checksum;
2. copy the exact managed daemon environment layer, including all comments,
   ordering and whitespace, and record its original SHA256;
3. copy the exact application overrides layer at `<state_dir>/settings.json` and
   record its original SHA256; if the file is absent, record `ABSENT` rather than
   creating a layer that was not running;
4. inspect the application override JSON key `body_motion_enabled`; when present,
   record the override layer as the effective body-setting winner;
5. only when that override key is absent, inspect the managed environment variable
   `REACHY_SATELLITE_BODY_MOTION_ENABLED`; record the managed layer as winner when
   present, otherwise record the retained wheel default as winner;
6. seal a private manifest that pairs each exact configuration-layer backup with
   its original SHA256, then confirm these checks completed before any candidate
   install.

The backup names and contents must carry no installation identifier into this
repository. A version label, parsed key/value export or remembered defaults are
not a backup: rollback uses the retained bytes, including non-setting bytes, and
the exact precedence that was running.

The `/config` field `body_motion_enabled` must resolve to JSON boolean `false`
before the head-only canary is allowed to own motion. It may become `true` only
for the separately approved body canary after all head-only evidence passes, and
it must be restored to `false` when that canary or any rollback ends. Body motion
remains restart-bound and false by default regardless of a successful canary.

### Abort thresholds

Abort the current canary immediately, issue no further candidate motion and run
the rollback below on any of these observations:

- any non-finite command or measurement, malformed/non-canonical pose, atomic
  world/head/body identity failure, derivative-envelope breach or configured
  workspace rejection;
- any non-`none` controller fault or `safe_hold: true` in `/status`, including
  timing, pose, calibration, derivative, workspace, body-feedback or command
  acceptance failure;
- `/livez` stops returning its successful response, the application leaves its
  running state, or `/status` cannot be read;
- head-only step evidence misses the gaze-control envelope: normalized error
  above `0.025` three seconds after a supported 35-degree step, angular
  overshoot above 2 degrees, or 5-degree-per-second horizontal or vertical
  tracking exceeding 1.5 degrees mean lag, 2 degrees maximum lag, or 1.5 degrees
  lag half a second after stopping;
- perception remains `unknown` or `stale` for more than one configured staleness
  window while the canary target is intentionally visible, or a fresh result
  cannot transition back to tracking;
- the groundstation session fails to remain healthy, Home Assistant loses the
  existing device or its entities, or one complete wake/listen/process/respond
  exchange fails;
- during the body canary, body feedback is missing or divergent long enough to
  fault, a coordinated command fails exact world-gaze equals body plus
  head-on-body identity, any body derivative exceeds its envelope, or body motion
  occurs while the restart-bound setting is false.

A reset of `/diagnostics/controller` is never remediation for one of these
conditions. It clears evidence only; it neither clears a controller fault nor
changes motion, settings, perception or pipeline state.

### Staged execution

1. After every preflight checksum and precedence record above is sealed, stop the
   released application and install the verified candidate with body motion
   staged false; no candidate wheel is installed before that preflight completes.
2. Restart the candidate with motion ownership inhibited, require the resolved
   `/config` field `body_motion_enabled` to be JSON boolean `false`, then allow
   the head-only deterministic-envelope canary while observing `/livez`,
   `/status`, bounded controller diagnostics, perception, the groundstation
   session and the existing Home Assistant device.
3. If every head-only threshold passes, stop the application. When the override
   layer wins, privately set the application override JSON key
   `body_motion_enabled` with the string-valued entry
   `"body_motion_enabled": "true"`.
4. When the managed layer or retained default wins, privately set the managed
   environment variable `REACHY_SATELLITE_BODY_MOTION_ENABLED` with the native
   environment assignment `REACHY_SATELLITE_BODY_MOTION_ENABLED=true`; this
   makes the managed layer the explicit canary winner instead of modifying wheel
   defaults.
5. Restart with motion inhibited, require the `/config` field
   `body_motion_enabled` to resolve to JSON boolean `true`, and only then run the
   separately gated coordinated-body canary against the same abort rules plus the
   body-specific rules above.
6. Stop after the evidence interval. When the override layer was the canary
   winner, restore the application override JSON key `body_motion_enabled` with
   the string-valued entry `"body_motion_enabled": "false"`.
7. When the managed layer was the canary winner, restore the managed environment
   variable `REACHY_SATELLITE_BODY_MOTION_ENABLED` with the native environment
   assignment `REACHY_SATELLITE_BODY_MOTION_ENABLED=false`.
8. Restart with motion inhibited, require the `/config` field
   `body_motion_enabled` to resolve to JSON boolean `false`, and only then verify
   the head-only steady state. Do not infer a shipping-default decision from a
   canary; changing the default requires its own approved proposal.

### Rollback

On any abort, or when the private canary is intentionally ended without approval
to retain the candidate, use one ordered transaction:

1. preserve every byte-for-byte configuration backup and the sealed precedence
   manifest unchanged as the audit and recovery source; never edit retained
   backup bytes to encode a safer target;
2. stop and release the candidate application before any configuration or
   artifact write, then confirm it no longer owns motion or media;
3. restore the managed daemon environment layer with all non-body bytes exactly
   as retained, including comments, ordering, whitespace and unrelated values;
4. restore the application overrides layer with all non-body bytes exactly as
   retained; restore absence as absence rather than creating an override file;
5. inspect the restored application override JSON key `body_motion_enabled`; when
   present, name the override as the highest-precedence effective body-setting
   layer;
6. only when that override key is absent, inspect the restored managed environment
   variable `REACHY_SATELLITE_BODY_MOTION_ENABLED`; name the managed layer as the
   highest-precedence effective body-setting layer when present, otherwise name
   the retained artifact default;
7. when the override wins, force only the application override JSON key
   `body_motion_enabled` with the string-valued entry
   `"body_motion_enabled": "false"`, and leave every other override byte exactly
   as restored;
8. when the managed layer wins, force only the managed environment variable
   `REACHY_SATELLITE_BODY_MOTION_ENABLED` with the native environment assignment
   `REACHY_SATELLITE_BODY_MOTION_ENABLED=false`, and leave every other managed-
   environment byte exactly as restored; when the retained default wins at
   `false`, leave both mutable layers byte-identical rather than manufacturing an
   override;
9. record and verify the original SHA256 and safety-modified SHA256 for each layer
   in the private manifest; an unchanged layer has equal checksums and an absent
   layer remains `ABSENT`, so every divergence is explicit per layer;
10. install the retained checksum-verified released wheel only after the candidate
    is stopped and every configuration-layer checksum and body precedence action
    is complete;
11. restart the retained application with motion ownership inhibited so its
    configuration and health endpoints can be checked without a motion command;
12. verify the resolved `/config` field `body_motion_enabled` is JSON boolean
    `false` while motion ownership remains inhibited;
13. only after that resolved check may you permit any motion restart or canary
    check; verify `/livez`, perception, the groundstation session, Home
    Assistant's existing device and entities, and one complete voice exchange
    before calling rollback complete.

Status verification is retained-artifact-version-aware: always require the
legacy status keys `running`, `pipeline`, `gaze`, `tracking` and `idle`, with the
application running and its legacy health state coherent. The absence of
`controller` is valid for an older retained artifact that predates that schema;
only when `controller` is present — or retained-artifact metadata says that
schema is supported — require controller fault `none` and safe hold `false`.
Never reject an otherwise healthy retained release merely because it lacks a
field introduced by the candidate being rolled back.

The rollback is incomplete if only the wheel or only the effective configuration
was restored, if any layer lacks its original and safety-modified checksum, if
the winning-layer body-false divergence was not accounted for, or if the
candidate retained ownership during a write. The resolved body value plus the
application, liveness, status, perception, groundstation and Home Assistant
checks above must pass before the robot is called recovered.

---

## Stopping

The daemon's stop signal is what the application listens for. On receiving it,
it stops commanding movement, releases the daemon's media interface, and exits —
leaving the daemon free to return the robot to its default position. Run directly
rather than under the daemon, `SIGINT` and `SIGTERM` do the same.

---

## Troubleshooting

**It starts, and the settings page says the robot is not configured yet.** That
is the warning at the top of this page, and it is the intended state of a robot
nobody has named. Read it; do not invent a name. Set
`REACHY_SATELLITE_DEVICE_NAME` on the settings page, stop the application there
and start it again from the robot's dashboard.

**It refuses to start and talks about the device name.** Then the value it was
given is one the identity contract does not accept — longer than 64 characters.
That is a different answer from "not supplied", which starts.

**The dashboard says it finished, successfully, seconds after starting, and it
printed nothing at all.** `done` with no error is not evidence of a clean run:
the daemon reports a startup failure exactly the same way, and swallows the
exception — see [How the daemon starts it](#how-the-daemon-starts-it). Two
different things produce this reading and they need different fixes.

- **The application refused to start.** Every refusal on this page exits
  non-zero and writes its reason, so run it outside the daemon to see that
  reason — `python -m reachy_mini_ha_satellite` from the application
  environment, with the same variables set. That is what this way in exists
  for.
- **The installed wheel's entry module has no execution path.** Then it prints
  nothing however it is run, and running it directly reproduces that in one
  line. Check the installed wheel is one `just wheel-verify` passed; a wheel
  built before that check existed can carry the defect.

**It refuses to start naming a variable.** The variable is misspelled, or it
belongs to a setting that no longer exists. The message lists every variable the
application does recognise.

**It starts but Home Assistant never finds it.** `/status` answers this before
anything else does. `announcing: false` means nothing announcing was ever built,
and `identity` says whether that is because nobody has named the robot — the
state at the top of this page — or because the name was supplied to a process
that had already started, in which case stop it and start it again. With
`announcing: true`, check `REACHY_SATELLITE_ADVERTISE` is on and that the robot
and Home Assistant are on the same layer-2 network — mDNS does not cross a
router. The boot log records the interface, the address and the port it
advertised.

**Home Assistant found it, but as a new device.** The announced identity changed.
Set `REACHY_SATELLITE_DEVICE_NAME` — and `REACHY_SATELLITE_MAC_ADDRESS` — back to
what the previous installation announced, restart, and remove the device Home
Assistant created in the meantime.

**It tracks a face but never moves.** `/status` says which of the two motion
paths it is on. `motion_gating` reporting `{"mode": "confirmed", "reason":
"daemon_confirmation_available"}` beside `controller.fault: "command"` and
`safe_hold: true` is a robot whose daemon *can* confirm torque and whose groups
were not confirmed — read `motors` for the group that failed, and
`/diagnostics/controller` for what it did. `{"mode": "ungated", "reason":
"daemon_confirmation_absent"}` is a stock robot, which commands motion directly
and announces no motor switch; if that robot is still not moving, the cause is
somewhere other than the gate.

**It starts but never tracks a face.** `/status` says why. `unknown` means
nothing has ever produced a detection: the groundstation session is not up, or
face tracking is switched off. `stale` means results were arriving and stopped.

**Switching face tracking on from the settings page did nothing.** It is marked
*needs a restart*, and it is one of the settings that genuinely does: switching
it on means building a detector — opening a session, or loading a model — which
happens once at startup. Stop the application and start it again.

**A gaze deadzone, smoothing or camera FOV value does not take effect.** Those
four names are migration-only inputs. The page marks each `legacy compatibility;
ignored`; predictive gaze uses the daemon's calibrated image query and its own
controller configuration instead. Save any ordinary setting to remove a stale
copy from the overrides file.

**Body motion stayed off after enabling it.**
`REACHY_SATELLITE_BODY_MOTION_ENABLED` is restart-bound. Stop the application and
start it again from the robot dashboard. It remains false by default; enabling it
is an explicit provisional opt-in, not a live tuning change.

**A setting on the page does not take effect.** The page marks it *needs a
restart*. Press **Stop**, then start the application again from the robot
dashboard — the daemon does not relaunch it on its own.

**The robot went quiet after pressing Stop.** That is what Stop does. Start it
again from the robot dashboard's application list.
