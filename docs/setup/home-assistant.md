# Adding the robot to Home Assistant

The satellite presents the robot to Home Assistant as an ESPHome device: a voice
assistant with a microphone, a speaker, a wake word and a handful of entities.
Home Assistant discovers it over mDNS and speaks to it over the ESPHome native
API on port 6053.

**Read the next section before you install or upgrade anything.** It is the one
step on any page in this repository that cannot be undone.

---

## ⚠️ The one thing that cannot be undone: the announced identity

**Home Assistant keys an ESPHome device on the identity it announces. Change
that identity and Home Assistant does not update the device — it registers a
new one.**

When that happens:

- every entity acquires a suffixed identifier: `sensor.reachy_mini_1_wake_word`
  becomes `sensor.reachy_mini_1_wake_word_2`;
- the history detaches from the old entity and stays with a device nothing
  writes to any more;
- **every automation, script and dashboard card referencing the old identifiers
  silently stops matching.** Nothing errors. Nothing appears in a log. Things
  simply stop happening, and the first sign is usually somebody noticing weeks
  later that a chart is flat.

There is no repair worth the name. You can rename the new device and delete the
old one, but the history that was attached to the old one does not move with it.

### What decides it

`REACHY_SATELLITE_DEVICE_NAME`. **It has no default and the application refuses
to start without it**, and that refusal is deliberate: a default derived from
the package name would be correct on a fresh installation and silently
destructive on an upgrade — and an upgrade from an application with a different
package name is exactly the case this repository exists to serve. Being asked
for the value is how the hazard becomes visible *before* it has happened.

Home Assistant keys the device on the **hardware address** as well. A satellite
moved to new network hardware announces a new device even under the old name, so
pin that too when the robot's network interface has changed:

```
REACHY_SATELLITE_DEVICE_NAME=reachy-mini-1
REACHY_SATELLITE_MAC_ADDRESS=02:00:5e:10:00:00
```

Left unset, the hardware address is read from the interface at startup and
reported in the boot log and on the settings page — so the value to pin is always
visible before you need it.

### Upgrading an existing installation

Set the name to **whatever the previous application announced**. Two places tell
you what that was:

- Home Assistant's device page shows it;
- it is the prefix of every entity identifier belonging to the device — an entity
  called `sensor.reachy_mini_1_wake_word` was announced by a device called
  `reachy-mini-1`.

Do not guess, and do not choose a nicer name. If you want a nicer name, change
`REACHY_SATELLITE_FRIENDLY_NAME` instead: that is the display name, Home
Assistant renames the device rather than replacing it, and it is safe to change
whenever you like.

### A new robot

Choose a name now and never change it.

```
REACHY_SATELLITE_DEVICE_NAME=reachy-mini-1
```

### Only the announced name is dangerous

| Setting | Changing it |
|---|---|
| `REACHY_SATELLITE_DEVICE_NAME` | **Registers a new device. History detaches.** |
| `REACHY_SATELLITE_MAC_ADDRESS` | **The same.** |
| `REACHY_SATELLITE_FRIENDLY_NAME` | Renames the device. Safe. |
| Everything else | Safe. |

---

## ⚠️ Known gap: `doctor` watches a different variable

`reachyctl doctor`'s `home-assistant.identity` check compares the declared
identity against **`REACHY_HOME_ASSISTANT_IDENTITY`**, read out of the daemon's
effective environment. The satellite announces **`REACHY_SATELLITE_DEVICE_NAME`**.
They are two different variables, and nothing today makes them agree.

So the check can report `passed` while the satellite is announcing something
else entirely — on the one hazard this whole page is about. Until that is
reconciled (it is a backlog item in [`docs/tasks.md`](../tasks.md)):

**Set both, to the same string.** `REACHY_SATELLITE_DEVICE_NAME` in a drop-in of
your own, `REACHY_HOME_ASSISTANT_IDENTITY` in the managed declaration — see
[the known-gap box in the robot runbook](robot.md#-known-gap-this-declaration-does-not-configure-the-satellite)
for exactly where each one goes. With both set to the same value the check means
what it says.

And whatever `doctor` reports, **the authority on what Home Assistant sees is
Home Assistant's own device page.** Look at it after any upgrade.

---

## 1. Check what the satellite is announcing

Before Home Assistant is involved at all. The satellite reports its own resolved
configuration on its settings port:

```
curl --silent --show-error http://192.0.2.20:8088/config
```

> **⏳ PENDING HARDWARE VERIFICATION.** No expected output is recorded for this
> step, because the satellite has never been run on a robot from this
> repository. Nothing below is a transcript.

The document names every setting in force, which layer each value came from, and
reports the groundstation credential as set or unset rather than by value. The
announced name and the hardware address are both in it.

The same values are on the settings page at `http://192.0.2.20:8088/` and in the
boot log:

```
reachyctl app logs --robot reachy@192.0.2.20
```

> **⏳ PENDING HARDWARE VERIFICATION.** Nothing below is a transcript.

## 2. Let Home Assistant discover it

With `REACHY_SATELLITE_ADVERTISE=true` — the default — the satellite advertises
itself over mDNS and Home Assistant offers it under **Settings → Devices &
Services** as a discovered ESPHome device. Accept the discovery.

**mDNS does not cross a router.** The robot and Home Assistant have to be on the
same layer-2 network for discovery to work. If they are not, add the device by
hand instead: **Add Integration → ESPHome**, then the robot's address and port
`6053`.

> **⏳ PENDING HARDWARE VERIFICATION.** The discovery flow has never been
> exercised against a Home Assistant instance from this repository.

The satellite announces `uses_password=false`, so Home Assistant will not ask for
one. That is the same trust model as the rest of the robot — see
[the trust boundary](#the-trust-boundary-is-the-network) below.

## 3. Confirm the device is the one you expected

On the device page, check:

- **the device name is the string you set**, character for character;
- **the entity identifiers carry that prefix and no `_2` suffix.** A `_2` means
  Home Assistant registered a second device and the first one still holds the
  history. Stop and fix the announced identity before doing anything else.

## 4. Assign it a voice assistant pipeline

The satellite is a voice satellite: Home Assistant runs the pipeline and the
robot is its microphone, its speaker and its face. Assign a pipeline on the
device page under **Voice assistant**.

The shipped wake word is `okay_nabu`, with `stop` alongside it — the smallest set
that works, being a default wake word plus the stop word the protocol needs to
interrupt a response or silence a ringing timer. Home Assistant can activate a
wake word the robot does not ship; the vendored protocol layer downloads it on
demand.

`REACHY_SATELLITE_ACTIVE_WAKE_WORD` selects which shipped wake word listens.

### What a working answer sounds like, and how to check it

The robot amplifies what it plays: Home Assistant's text-to-speech arrives
quiet, and the robot's one hardware volume control is already at its maximum, so
the loudness comes from `REACHY_SATELLITE_SPEAKER_BOOST_PERCENT` — 500 by
default, and adjustable between 100 and 800. Every utterance logs what that did:

```
reachyctl app logs --robot reachy@192.0.2.20
```

```
speech: peak in -16.1dBFS out -2.3dBFS limited 0.2% gain 5.00x
speech: peak in -11.6dBFS out -0.9dBFS limited 0.3% gain 3.79x
music: level is now 50%
```

**Read the line rather than guessing at the sound.** "Still too quiet" and "it
sounds distorted" are different faults with different fixes, and the three
numbers tell them apart:

- **`out` near 0 dBFS and `limited` under about 1%** is the healthy case — the
  robot is running as loud as it can without squashing anything.
- **`out` well below −6 dBFS** means it is quiet because the source was quiet and
  the boost has run out of room. Raise
  `REACHY_SATELLITE_SPEAKER_BOOST_PERCENT`.
- **`limited` in the tens of percent** means the boost is set higher than this
  material wants and peaks are being compressed. Lower it.
- **`gain` below what the boost asks for** — the `3.79x` above, against a
  configured 5.00× — is not a fault. Each source is capped at the gain that
  brings *it* to full scale, so that a chime mastered loud is not given the
  multiplier a quiet voice needs.

Home Assistant's own volume control works on top of that, and the robot ducks
music while it speaks. Both show up in the same log.

> **Verified on hardware.** Announcements at the default were judged audible at
> a normal speaking distance, and the numbers above are transcribed from that
> session — with the `assist_satellite` entity identifier and the
> text-to-speech URLs removed, because the first embeds the robot's hardware
> address and the second the Home Assistant host. The levels are untouched.
> [Change 0016](../changes/0016-audible-playback.md) records the rest.

### Both speaker controls are on the device page

You do not have to reach for the environment to change either number. The device
page carries both, in the **Configuration** group beside **Mic Volume**:

- **Speaker Volume** — 0 to 100%, the same level the media-player card sets.
  There is one level underneath the two, so moving either moves the other and
  they never show different numbers. Muting takes the level to 0, so this reads
  0 too; a value you then set *here* is remembered and restored when you unmute
  rather than applied straight away. Setting the volume from the **media-player
  card** while muted is the one case that behaves differently — that level is
  applied and this slider follows it, and the robot stays marked muted while
  being audible. That last part is upstream behaviour rather than something
  these controls do; [change
  0017](../changes/0017-speaker-controls-in-home-assistant.md#known-limitations)
  records it.
- **Speaker Boost** — 100 to 800%, the multiplier the section above is about.

Both take effect without a restart and both survive one, but they are kept in
two different places, and which one a value lands in is worth knowing before you
go looking for it:

- **Speaker Volume** is written to `preferences.json` in the state directory —
  the same store the media-player volume has always used. That is why the two
  controls cannot disagree: they are not being kept in step, they are the one
  level.
- **Speaker Boost** is written to the overrides file, `settings.json` in the
  same state directory, which is the same file the robot's own settings page
  writes. Because it is one file and not two, a boost set from Home Assistant is
  the number the settings page shows afterwards.

The boost reaches both outputs from the next pushed chunk onwards, so you can
move the slider while the robot is talking and hear the result.

`REACHY_SATELLITE_SPEAKER_BOOST_PERCENT` remains how the **starting** boost is
set — it is what the robot boots at before anything has overridden it, and it is
still the right place to set a fleet-wide default. Once the boost has been moved
from either surface, the overrides file is what wins. The variable says nothing
about Speaker Volume, which has no environment setting of its own.

```
<!-- transcript to be captured on hardware before merge -->
```

## 5. Watch the robot, not the screen

The point of a robot satellite is that you can tell what it is doing from across
a room. The antennas say it, and they differ in the **kind** of motion rather
than in its size, because that is what a person reads first:

| Pipeline state | The antennas | The head |
|---|---|---|
| Idle, somebody about | still, at rest | following a face |
| Idle, alone for a while | a slow symmetric sway | neutral |
| **Listening** | both raised, and **still** | following a face, or slightly raised |
| **Processing** | **counter-rotating** — one rises as the other falls | lowered, drifting |
| **Responding** | both **bobbing together**, twice as fast | following a face, or nodding |
| Error | a fast opposed shake, for about a second and a half | a slight roll |
| Muted | folded down, and held | neutral |
| Disconnected | drooped, and held | lowered |

**A head that returns to neutral is a signal, not a failure to move.** When
detections stop arriving within the staleness window the head goes back to
looking straight ahead rather than holding its last pose — holding would look
like successfully tracking somebody who has left the room. `/status` on the
settings port says which of four situations it is: `tracking`, `nobody`,
`stale`, or `unknown`.

**A mute outlives a disconnection.** A muted robot that loses Home Assistant and
gets it back is still muted, even though the reconnection announces an idle
pipeline. Only being unmuted unmutes it.

> **⏳ PENDING HARDWARE VERIFICATION.** Whether the three required motions read
> as distinct across a room, and whether the head tracks smoothly at the tuned
> deadzone and smoothing, has never been observed. The behaviour layer that
> decides them is pure and fully covered; what has not been checked is how it
> looks.

## The trust boundary is the network

Anything that can reach the robot can open the settings page, read the resolved
configuration, change a setting and stop the application. The ESPHome API
announces no password, and the daemon's own dashboard is open too. **Put the
robot on a network you trust, the way you would a printer.**

What the settings interface does close is the exposure that needs no peer on
that network: a state-changing request a browser reports as coming from another
site is refused, so a page an operator happens to visit cannot stop the robot or
replace its credential. Every response is `no-store`.
`REACHY_SATELLITE_WEB_ENABLED=false` switches the interface off entirely.

## When it goes wrong

| Symptom | Where to look |
|---|---|
| Home Assistant never finds it | `REACHY_SATELLITE_ADVERTISE`, and whether a router sits between the two — [troubleshooting](../ops/troubleshooting.md#home-assistant-never-discovers-the-robot) |
| Found, but as a **new** device | The announced identity changed — [troubleshooting](../ops/troubleshooting.md#home-assistantidentity) |
| Found, but the head never tracks | `/status`, and the groundstation link — [troubleshooting](../ops/troubleshooting.md#groundstationsession) |
| A setting on the page did nothing | It is marked *needs a restart* — [troubleshooting](../ops/troubleshooting.md#a-setting-changed-on-the-page-did-nothing) |

## Next

- [Update a running installation](../ops/deploy.md)
- [Diagnose a failure](../ops/troubleshooting.md)
- [The satellite reference](../ops/satellite-deployment.md) — every setting, the
  three configuration layers, and which one wins
