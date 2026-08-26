# Updating a running installation

Getting a new version onto a working stack without breaking it. This assumes the
installation already exists — [setting one up](../setup/groundstation.md) is a
different page.

**The rule that makes this safe: every artifact in a release carries the same
version.** The container image, every wheel and the git tag all come from one
repository-wide version derived from conventional commits, so "which application
goes with which groundstation" is answerable from a tag rather than from a
compatibility matrix. Update the two halves to the same tag.

---

## Before you touch anything

### Record what is running

```
uv run --locked --all-packages reachyctl --output json doctor \
  --url ws://127.0.0.1:8080/v1/session \
  --credential-file ~/.config/reachy/groundstation-credential \
  > before.json
```

**Executed**, without a robot — the shape you get with `--robot` has more rows
passed and none skipped for the want of one:

```json
{
    "command": "doctor",
    "ok": true,
    "summary": "nothing failed, but not everything was checked (3 passed, 0 failed, 6 skipped)",
    "data": {
        "groundstation": "ws://127.0.0.1:8080/v1/session",
        "robot": null,
        "checks": 9,
        "passed": 3,
        "failed": 0,
        "skipped": 6,
        "first_failure": null,
        "round_trip_ms": 1.9161590025760233,
        "observer_failures": []
    },
    "rows": [
        {
            "check": "daemon.reachable",
            "status": "skipped",
            "detail": "no robot was given: pass --robot with user@host, or set REACHYCTL_ROBOT",
            "remediation": null,
            "command": null
        }
    ]
}
```

(One row shown; the real document carries all nine.)

A `doctor` run reads nothing and writes nothing but its output, so it can be run
from a script, from a provisioning play and from a laptop without any of them
sharing state. `round_trip_ms` is the one number worth trending, which is why it
is promoted out of the rows into the run's own fields.

### ⚠️ Never change the announced identity during an upgrade

If the upgrade involves a new `REACHY_SATELLITE_DEVICE_NAME` — because the
package name changed, or because somebody thought of a nicer name — **stop and
read [the identity warning](../setup/home-assistant.md#-the-one-thing-that-cannot-be-undone-the-announced-identity)**.
Home Assistant registers a new device, entity history detaches, and every
automation referencing the old identifiers silently stops matching.

An upgrade is precisely the situation the hazard exists in. A fresh install
cannot hit it.

---

## Updating the groundstation

### 1. Point at the new tag

In `.env` beside the compose file:

```
GROUNDSTATION_IMAGE=ghcr.io/<owner>/<repository>/groundstation:<new version>
```

A deployment left on `latest` gets a different service on the next pull with no
local change to explain it. A version tag is what makes the update a decision.

### 2. Pull and recreate

```
docker compose pull
docker compose up --detach
```

**Executed** — the second command, against a stack whose image tag had changed:

```
 Container reachy-groundstation-prometheus-1 Running
 Container reachy-groundstation-groundstation-1 Recreate
 Container reachy-groundstation-groundstation-1 Recreated
 Container reachy-groundstation-groundstation-1 Starting
 Container reachy-groundstation-groundstation-1 Started
 Container reachy-groundstation-groundstation-1 Waiting
 Container reachy-groundstation-groundstation-1 Healthy
```

Read the last two lines. `Waiting` then `Healthy` means the new container passed
the image's own health check before compose considered the update done, and
Prometheus — which was already `Running` and is not recreated — keeps scraping
through it. A recreate that ends at `Started` without a `Healthy` is one to
investigate before going further.

### 3. Read the resolved configuration again

```
docker compose logs groundstation --no-log-prefix | head -1
```

**Executed:**

```
{"credential": "<set>", "host": "0.0.0.0", "port": 8080, "queue_bound": 2, "capability_timeout_seconds": 5.0, "handshake_timeout_seconds": 10.0, "warm_up_timeout_seconds": 60.0, "max_message_bytes": 4194304, "log_level": "info", "log_format": "json", "service_name": "reachy-groundstation", "models_dir": "/opt/reachy/models", "inference_intra_op_threads": 4, "inference_inter_op_threads": 1, "inference_providers": "CPUExecutionProvider", "face_enabled": true, "face_score_threshold": 0.6, "face_nms_threshold": 0.3, "gesture_enabled": false, "gesture_score_threshold": 0.6, "gesture_sample_interval": 4, "event": "configuration.resolved", "level": "info", "timestamp": "2026-08-21T19:20:19.399845Z"}
```

That is every setting in force, including the ones left at their defaults, with
the credential shown as `<set>` rather than by value. Compare it against what
you had: a new release may have added a setting, and the default it chose is now
yours.

**A refusal to start naming a variable is the good outcome**, not a regression.
It means a setting was renamed or withdrawn and the service will not run on a
value nobody set —
[architecture REQ-009](../specs/architecture/index.md#req-009-configuration-is-validated-and-self-reporting).

### 4. Confirm it still answers a session

```
uv run --locked --all-packages reachyctl probe \
  --url ws://127.0.0.1:8080/v1/session \
  --frames services/groundstation/tests/fixtures/perception \
  --count 5 \
  --credential-file ~/.config/reachy/groundstation-credential
```

The expected shape is
[in the setup runbook](../setup/groundstation.md#6-drive-a-real-session-through-it).
`/readyz` says the service thinks it is ready; a probe says a frame went out and
a result came back over the protocol the robot actually speaks.

### 5. Confirm the camera feed still answers

Only if you configured one — Home Assistant's MJPEG IP Camera integration reads
`/stream.mjpg` on this service, so a groundstation that came back without it is a
camera that went unavailable with nothing else to explain it.

```
curl --silent --show-error --include --max-time 2 --output /dev/null http://127.0.0.1:8080/stream.mjpg
```

With the robot reconnected this answers `200` and a
`multipart/x-mixed-replace` content type. Immediately after a restart it answers
`503 no_eligible_session` instead, and briefly: the feed holds no frame from a
session that ended, so it stays unavailable until the robot's next frame arrives.
[The endpoint's four answers](../setup/groundstation.md#8-look-at-what-the-robot-is-sending)
say what any other status means.

### Rolling back

Point `GROUNDSTATION_IMAGE` at the previous tag and repeat. Nothing in the
service holds state that a downgrade would have to migrate: the bounded queue is
in memory and the model files are in the image.

---

## Updating the robot

### 1. Get the wheel, and check it is the one that was published

Every release publishes a `SHA256SUMS` file beside its wheels, written by the
release job over everything it publishes.

**Keep the published file name.** The release job writes `SHA256SUMS` with
`sha256sum ./*.whl`, so every line in it names a versioned wheel relative to the
directory you are checking from. Rename the download and
`sha256sum --check --ignore-missing` matches nothing and says
`no file was verified` — which is not the same as a failure and is easy to read
past. So fetch with `--remote-name`, never `--output`:

```
curl --location --remote-name \
  https://github.com/<owner>/<repository>/releases/download/v<version>/reachy_mini_ha_satellite-<version>-py3-none-any.whl
curl --location --remote-name \
  https://github.com/<owner>/<repository>/releases/download/v<version>/SHA256SUMS
sha256sum --check --ignore-missing SHA256SUMS
```

**Executed** — against a `SHA256SUMS` produced the way the release job produces
it, over a locally built wheel, with only that wheel in the directory:

```
./reachy_mini_ha_satellite-0.1.0-py3-none-any.whl: OK
```

One `OK` line per wheel present. Anything else means the file you have is not the
file that was published, and **it must not be installed**: the wheel goes into
the environment the daemon runs applications from, so it runs with the daemon's
access.

### 2. Deploy it

```
reachyctl deploy --robot reachy@192.0.2.20 \
  --wheel reachy_mini_ha_satellite-<version>-py3-none-any.whl --preview
reachyctl deploy --robot reachy@192.0.2.20 \
  --wheel reachy_mini_ha_satellite-<version>-py3-none-any.whl
```

> **⏳ PENDING HARDWARE VERIFICATION.** No expected output is recorded for either
> command against a robot. Nothing below is a transcript.

`deploy` installs and then **asks the robot which version is actually running**,
rather than trusting the install. That is not ceremony: the predecessor's most
expensive deployment failure was a package that installed perfectly into an
environment the running daemon was not using, which looks identical to success
unless something says out loud which version is there.

`--application` defaults to the name the wheel itself carries, read out of its
`.dist-info/METADATA` rather than off its file name. A file name is a claim;
the metadata is what pip records and what the daemon's interpreter will report
afterwards.

Or, reproducibly, through the playbook:

```
reachyctl provision --tags app_install --extra-vars @declaration.yml
```

with `reachy_app_wheel_url` and `reachy_app_wheel_checksum` set. The checksum is
**required** when the wheel is fetched by URL, for the reason in step 1.

### 3. Put a configuration change in force

The application inherits its environment from the daemon, so a change to the
managed drop-in takes effect when the **daemon** restarts — not when the
application does.

```
reachyctl config apply --robot reachy@192.0.2.20 --declaration declaration.json --preview
reachyctl config apply --robot reachy@192.0.2.20 --declaration declaration.json
```

> **⏳ PENDING HARDWARE VERIFICATION.** Nothing below is a transcript.

**`apply` owns the whole managed region.** A setting withdrawn from the
declaration is removed from the robot rather than left behind, and an edit made
to that file by hand is lost on the next apply — the file says so, in the file.
Other drop-ins in the same directory belong to whoever put them there and are
neither read nor written.

**A setting changed on the satellite's own settings page wins over a later
`config apply` of that same setting**, because the page's overrides file sits
above the environment. The page reports the layer each value came from for
exactly this reason: a value showing `override` is one `reachyctl` can no longer
change until the page is used to save it back to what the environment says.

### 4. Restart, and confirm

```
reachyctl app stop  --robot reachy@192.0.2.20
reachyctl app start --robot reachy@192.0.2.20
```

> **⏳ PENDING HARDWARE VERIFICATION.** Nothing below is a transcript.

Both confirm the robot reports the new state rather than returning as soon as
the command was accepted.

**Stopping is not restarting.** The daemon marks a cleanly-exited application
`done` and leaves it stopped; nothing relaunches it. The satellite's settings
page offers a **Stop** and says where to start it again, which is the robot
dashboard's application list — also a web interface, so no shell is involved
either way.

**`done` with a null error is not evidence of a clean exit.** The daemon reports
an application that refused to start, or raised, exactly as it reports one that
ran and finished — the exception does not reach its API. So `done` seconds after
a deployment, with no output, is a reading to investigate rather than a result.
See [How the daemon starts it](satellite-deployment.md#how-the-daemon-starts-it)
for what produces it and what to check.

---

## After: prove the chain again

**`--output` is a root option, so it goes before the command name**, exactly as
it does in the `before.json` run at the top of this page. After it, `reachyctl`
answers `No such option: --output`.

```
reachyctl --output json doctor \
  --robot reachy@192.0.2.20 \
  --url ws://127.0.0.1:8080/v1/session \
  --credential-file ~/.config/reachy/groundstation-credential \
  --intent declaration.json > after.json
```

> **⏳ PENDING HARDWARE VERIFICATION** for the `--robot` half. The groundstation
> half is recorded above and in
> [the setup runbook](../setup/groundstation.md#7-confirm-the-whole-chain-so-far).

Then diff it against `before.json`. Three things are worth looking at:

| Field | What a change means |
|---|---|
| `first_failure` | The first broken link. If it is non-null, start at [troubleshooting](troubleshooting.md) and look up that identifier |
| `round_trip_ms` | The link got slower. A large jump is usually a capability that has stopped answering rather than a network fault |
| `skipped` | Something that used to be checked no longer is — usually an argument that went missing from the command, not a regression in the robot |

**And open Home Assistant's device page.** `doctor` compares announced against
declared and deliberately holds no Home Assistant credentials, so whether Home
Assistant's own registry gained a second device is a manual check. It is the one
failure mode nothing here can see.

---

## Updating this repository's own checkout

For a contributor rather than an operator:

```
just sync        # install exactly what uv.lock describes
just models      # fetch the pinned weights; never committed
just check       # lint, typecheck, test
```

`just sync` uses `--locked`, not `--frozen`: it fails when the lockfile no longer
matches the manifests instead of running happily against a stale resolution.
Adding a dependency means committing the regenerated `uv.lock` in the same pull
request.

## See also

- [Troubleshooting](troubleshooting.md) — keyed to the `doctor` check identifiers
- [The managed daemon environment](managed-daemon-environment.md) — the drop-in's
  byte-level contract
- [The satellite reference](satellite-deployment.md) — every setting and which
  configuration layer wins
