# Deploying the HA satellite

The robot-side Home Assistant voice satellite ships as a wheel on GitHub
Releases. Installing it into the robot's shared application environment is the
whole of registering it: the wheel declares a `reachy_mini_apps` entry point, and
the Reachy Mini daemon enumerates that group when it starts.

There is deliberately no Hugging Face Space. The daemon can install applications
from one, but it discovers them through a standard Python entry point either way
— see the [architecture spec](../specs/architecture/index.md#versioning-and-distribution).

Discovering an application and starting one are different mechanisms, and the
second is not what the entry point's spelling suggests:
[How the daemon starts it](#how-the-daemon-starts-it).

---

## ⚠️ Before you install: pin the announced identity

**`REACHY_SATELLITE_DEVICE_NAME` has no default, and the application refuses to
start without it.** That refusal is deliberate and it is the single most
important thing on this page.

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

**Four settings are readable on the page and not writable there**, because they
decide whether the page can be reached at all. An override sits *above* the
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
| `REACHY_SATELLITE_DEVICE_NAME` | **none — required** | The identity announced to Home Assistant. See the warning above. |
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
| `REACHY_SATELLITE_FACE_TRACKING_ENABLED` | `true` | Whether the head follows a face at all. |
| `REACHY_SATELLITE_DETECTION_SOURCE` | `remote` | `remote`, `local`, or `remote_with_local_fallback`. |
| `REACHY_SATELLITE_GROUNDSTATION_URL` | none | Where the groundstation serves its session endpoint. `ws://` or `wss://`, with no user information, query or fragment. Required unless the source is `local`. |
| `REACHY_SATELLITE_GROUNDSTATION_CREDENTIAL` | none | **Secret.** The shared secret presented to open a session. Required whenever a session is opened. |
| `REACHY_SATELLITE_FRAME_INTERVAL_SECONDS` | `0.1` | How often a frame goes up to the groundstation. |
| `REACHY_SATELLITE_STALENESS_SECONDS` | `2.0` | How long a detection stays worth acting on. Past it the head returns to neutral. |
| `REACHY_SATELLITE_LOCAL_MODEL_PATH` | none | The face-detection weights the robot's own detector loads. Required unless the source is `remote`. |
| `REACHY_SATELLITE_LOCAL_SCORE_THRESHOLD` | `0.6` | Confidence a locally-detected face must reach. |
| `REACHY_SATELLITE_LOCAL_NMS_THRESHOLD` | `0.3` | Overlap at which the lower-scoring local box is suppressed. |
| `REACHY_SATELLITE_LOCAL_DETECTION_INTERVAL_SECONDS` | `0.2` | How often the local detector looks. |
| `REACHY_SATELLITE_CAMERA_HORIZONTAL_FOV_DEGREES` | `87.0` | How much of the scene the camera sees across. |
| `REACHY_SATELLITE_CAMERA_VERTICAL_FOV_DEGREES` | `67.0` | The same, vertically. |
| `REACHY_SATELLITE_BEHAVIOUR_TICK_SECONDS` | `0.05` | How often the behaviour layer is asked what to do. |
| `REACHY_SATELLITE_GAZE_DEADZONE` | `0.02` | How far a face must move before the head follows. |
| `REACHY_SATELLITE_GAZE_SMOOTHING` | `0.35` | How much of the way to a new target one tick moves. |
| `REACHY_SATELLITE_IDLE_SECONDS` | `6.0` | How long without a face before the idle behaviour starts. |
| `REACHY_SATELLITE_LOG_LEVEL` | `info` | `debug`, `info`, `warning` or `error`. |

**One setting is secret**: `REACHY_SATELLITE_GROUNDSTATION_CREDENTIAL`. It is
reported as set or unset everywhere it is reported, and its value appears in no
log line, no page and no error message.

### Detection source

`remote` is the default, and the reason is measured rather than aesthetic: with
detection offloaded to a groundstation the predecessor's robot sat at 1.52 of its
four cores; with detection local it saturated. The robot is running motion
control, audio and a wake-word model at the same time.

The weights the local detector needs are **not shipped in the wheel** — they are
somebody else's model under somebody else's terms — so `local` and
`remote_with_local_fallback` both need `REACHY_SATELLITE_LOCAL_MODEL_PATH`
pointing at a file on the robot. `remote` and `remote_with_local_fallback` both
need an address *and* a credential. The application refuses to start if the
selection needs something it has not been given, rather than starting and never
tracking anything.

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

It reads **every** operator-facing setting, and writes every one but the four the
page's own existence depends on — see above for why those are set in the
environment. The credential is the
exception to *reading*: it shows as set or unset, its field is always blank, and
submitting it blank leaves it alone — so the page is usable for rotating a
credential and useless for learning one. A separate control unsets it.

| Path | What it is |
|---|---|
| `/` | The settings form and the resolved configuration |
| `/config` | The resolved configuration as JSON, secrets redacted, with which settings are secret, which apply at once and which are read-only |
| `/status` | What the robot is doing: pipeline state, why the head is where it is |
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

## Stopping

The daemon's stop signal is what the application listens for. On receiving it,
it stops commanding movement, releases the daemon's media interface, and exits —
leaving the daemon free to return the robot to its default position. Run directly
rather than under the daemon, `SIGINT` and `SIGTERM` do the same.

---

## Troubleshooting

**It refuses to start and talks about the device name.** That is the warning at
the top of this page. Read it; do not invent a name.

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

**It starts but Home Assistant never finds it.** Check `REACHY_SATELLITE_ADVERTISE`
is on and that the robot and Home Assistant are on the same layer-2 network —
mDNS does not cross a router. The boot log records the interface, the address and
the port it advertised.

**Home Assistant found it, but as a new device.** The announced identity changed.
Set `REACHY_SATELLITE_DEVICE_NAME` — and `REACHY_SATELLITE_MAC_ADDRESS` — back to
what the previous installation announced, restart, and remove the device Home
Assistant created in the meantime.

**It starts but never tracks a face.** `/status` says why. `unknown` means
nothing has ever produced a detection: the groundstation session is not up, or
face tracking is switched off. `stale` means results were arriving and stopped.

**Switching face tracking on from the settings page did nothing.** It is marked
*needs a restart*, and it is one of the settings that genuinely does: switching
it on means building a detector — opening a session, or loading a model — which
happens once at startup. Stop the application and start it again.

**A setting on the page does not take effect.** The page marks it *needs a
restart*. Press **Stop**, then start the application again from the robot
dashboard — the daemon does not relaunch it on its own.

**The robot went quiet after pressing Stop.** That is what Stop does. Start it
again from the robot dashboard's application list.
