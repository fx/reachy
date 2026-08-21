# Setting up the groundstation

The groundstation is the off-robot service that does the heavy computation. It
runs anywhere Docker runs; the robot opens one long-lived session to it and
sends frames.

Every step below is a command and the output to expect. If a step's output does
not look like what is recorded here, stop there rather than continuing — the
next step will fail in a way that describes the wrong problem.

**Every transcript on this page was produced by running the command.** The
addresses are loopback or RFC 5737 documentation ranges; substitute your own.

- **You need:** a host with Docker and room for the image — the default variant
  is 437 MiB on x86-64 and 354 MiB on 64-bit ARM, uncompressed. The accelerated
  variant is 3.4 GiB, which is what a CUDA runtime plus cuDNN costs.
- **You get:** a service answering `/readyz` and accepting authenticated
  sessions, with a Prometheus scraping it.
- **Then go to:** [the robot](robot.md), which points the satellite at it.

---

## 1. Get the deployment files

The compose file, its overlay, the scrape configuration and the example
environment live together:

```
services/groundstation/deploy/
├─ compose.yaml
├─ compose.cuda.yaml
├─ prometheus.yml
└─ .env.example
```

Copy that directory to the host, or clone the repository there. Everything below
runs from inside it.

## 2. Choose an image, and set a credential

```
cp .env.example .env
```

`.env` is untracked and holds the values that belong to this deployment. Two
lines have to change before anything will start.

**`REACHY_GROUNDSTATION_CREDENTIAL`** is the shared secret a client presents to
open a session. It has no default and the service refuses to start without one —
a groundstation that authenticated nothing would be a worse failure than one that
will not start. Generate one:

```
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Keep the value: the robot and every `reachyctl` invocation present the same one.

**`GROUNDSTATION_IMAGE`** names the image to run. The published tag is
`ghcr.io/<owner>/<repository>/groundstation:<version>` — the example file carries
`OWNER` and `REPOSITORY` placeholders because this repository does not publish a
host or an account into a tracked file. To run the image built from a checkout
instead, build it first:

```
just image
```

and point the variable at the tag that produces:

```
GROUNDSTATION_IMAGE=reachy-groundstation:dev
```

> **On `latest`.** It moves. A deployment that has to answer "which
> groundstation is this?" sets a version tag, and every release publishes one.

Everything else in `.env` is at the service's own default, and the file doubles
as the list of what the service resolves. Four settings are deliberately
commented out — `REACHY_GROUNDSTATION_HOST`, `REACHY_GROUNDSTATION_PORT`,
`REACHY_GROUNDSTATION_MODELS_DIR` and `REACHY_GROUNDSTATION_INFERENCE_PROVIDERS`
— because compose's `env_file` **overrides the image's own `ENV`**, so a line
repeating what the image already sets replaces the image's answer with this
file's. Uncommenting the provider list is the one that does real damage: on the
accelerated variant it would replace `CUDAExecutionProvider,CPUExecutionProvider`
with the CPU provider alone, and the image would run on the CPU without saying
so. A test asserts that the commented set is exactly the set the Dockerfile's
`ENV` names.

## 3. Start it

```
docker compose up --detach
```

```
 Network reachy-groundstation_default Creating
 Volume reachy-groundstation_prometheus-data Creating
 Volume reachy-groundstation_prometheus-data Creating
 Network reachy-groundstation_default Creating
 Volume reachy-groundstation_prometheus-data Created
 Volume reachy-groundstation_prometheus-data Created
 Network reachy-groundstation_default Created
 Network reachy-groundstation_default Created
 Container reachy-groundstation-groundstation-1 Creating
 Container reachy-groundstation-groundstation-1 Created
 Container reachy-groundstation-prometheus-1 Creating
 Container reachy-groundstation-prometheus-1 Created
 Container reachy-groundstation-groundstation-1 Starting
 Container reachy-groundstation-groundstation-1 Started
 Container reachy-groundstation-groundstation-1 Waiting
 Container reachy-groundstation-groundstation-1 Healthy
 Container reachy-groundstation-prometheus-1 Starting
 Container reachy-groundstation-prometheus-1 Started
```

That is a verbatim capture, repeated lines and all: with no terminal attached
compose emits each progress transition as its own line instead of redrawing one
line per object, and some transitions arrive twice. Run interactively you get one
line per object, updating in place. Nothing is wrong with a run that prints
either.

`Waiting` then `Healthy` is the point: Prometheus is held back until the
groundstation's own health check passes, so the first two minutes of a cold
start are not a stream of connection-refused lines that look like a fault.

```
docker compose ps
```

```
NAME                                   IMAGE                                                                                            COMMAND                  SERVICE         CREATED          STATUS                   PORTS
reachy-groundstation-groundstation-1   reachy-groundstation:dev                                                                         "/opt/reachy/venv/bi…"   groundstation   11 seconds ago   Up 8 seconds (healthy)   127.0.0.1:8080->8080/tcp
reachy-groundstation-prometheus-1      prom/prometheus:v3.7.3@sha256:49214755b6153f90a597adcbff0252cc61069f8ab69ce8411285cd4a560e8038   "/bin/prometheus --c…"   prometheus      26 minutes ago   Up 26 minutes            127.0.0.1:9090->9090/tcp
```

Wide, and pasted whole rather than trimmed. `(healthy)` on the groundstation row
is the column to read; the Prometheus row has no health state because the image
declares no check.

## 4. Read the startup log

```
docker compose logs groundstation --no-log-prefix
```

```
{"credential": "<set>", "host": "0.0.0.0", "port": 8080, "queue_bound": 2, "capability_timeout_seconds": 5.0, "handshake_timeout_seconds": 10.0, "warm_up_timeout_seconds": 60.0, "max_message_bytes": 4194304, "log_level": "info", "log_format": "json", "service_name": "reachy-groundstation", "models_dir": "/opt/reachy/models", "inference_intra_op_threads": 4, "inference_inter_op_threads": 1, "inference_providers": "CPUExecutionProvider", "face_enabled": true, "face_score_threshold": 0.6, "face_nms_threshold": 0.3, "gesture_enabled": false, "gesture_score_threshold": 0.6, "gesture_sample_interval": 4, "event": "configuration.resolved", "level": "info", "timestamp": "2026-08-21T19:20:19.399845Z"}
{"capability": "gesture", "event": "capability.disabled", "level": "info", "timestamp": "2026-08-21T19:20:19.401222Z"}
{"host": "0.0.0.0", "port": 8080, "event": "service.starting", "level": "info", "timestamp": "2026-08-21T19:20:19.401519Z"}
{"model": "face_detection_yunet", "providers": ["CPUExecutionProvider"], "intra_op_threads": 4, "inter_op_threads": 1, "event": "runtime.loaded", "level": "info", "timestamp": "2026-08-21T19:20:19.433322Z"}
{"capability": "face", "event": "capability.ready", "level": "info", "timestamp": "2026-08-21T19:20:19.439867Z"}
```

Five lines, and each one is worth reading:

- **`configuration.resolved`** is every setting in force, including the ones left
  at their defaults — architecture
  [REQ-009](../specs/architecture/index.md#req-009-configuration-is-validated-and-self-reporting).
  `"credential": "<set>"` is how a secret appears: its presence is visible and
  its value is not.
- **`capability.disabled` for `gesture`** is not a fault. No gesture model clears
  this repository's licence bar, so the capability ships switched off with no
  model wired. Disabled and broken are different states and the health surface
  distinguishes them.
- **`runtime.loaded`** names the providers actually in use. On the accelerated
  image this is where you find out whether the CUDA provider loaded, or whether
  the container never asked the host for a device.
- **`capability.ready` for `face`** is the one that matters: the model is loaded
  and the capability will be offered.

**If it refuses to start naming a variable**, that variable is misspelled. The
service rejects any `REACHY_GROUNDSTATION_*` name it does not recognise rather
than running on the default — see
[troubleshooting](../ops/troubleshooting.md#the-service-refuses-to-start-naming-a-variable).

## 5. Ask it whether it is ready

The service emits one line; `python3 -m json.tool` is what makes it readable, and
every JSON transcript below goes through it.

```
curl --silent --show-error http://127.0.0.1:8080/readyz | python3 -m json.tool
```

```json
{
    "ready": true,
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

`ready` is true when every enabled capability finished warming up. A disabled one
does not hold readiness back, and has no version because nothing was built.

Two more endpoints answer the questions that come next:

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

`offered` is what a session will actually be able to negotiate. If it is empty,
nothing will answer a frame however healthy the service looks.

```
curl --silent --show-error http://127.0.0.1:8080/config | python3 -m json.tool
```

```json
{
    "credential": "<set>",
    "host": "0.0.0.0",
    "port": 8080,
    "queue_bound": 2,
    "capability_timeout_seconds": 5.0,
    "handshake_timeout_seconds": 10.0,
    "warm_up_timeout_seconds": 60.0,
    "max_message_bytes": 4194304,
    "log_level": "info",
    "log_format": "json",
    "service_name": "reachy-groundstation",
    "models_dir": "/opt/reachy/models",
    "inference_intra_op_threads": 4,
    "inference_inter_op_threads": 1,
    "inference_providers": "CPUExecutionProvider",
    "face_enabled": true,
    "face_score_threshold": 0.6,
    "face_nms_threshold": 0.3,
    "gesture_enabled": false,
    "gesture_score_threshold": 0.6,
    "gesture_sample_interval": 4
}
```

The same document the startup log emitted. This is the answer to "what is this
service actually running on?" and it is worth checking against what you thought
you set, because a variable that names nothing under the service's prefix would
have prevented startup, while one that names nothing at all is simply ignored by
everybody.

## 6. Drive a real session through it

`/readyz` says the service thinks it is ready. A probe says a frame went out and
a result came back, over the same protocol client the robot uses — so a
groundstation that gets the protocol wrong fails here the way it would fail the
robot.

Put the credential in a file. There is deliberately no option that takes the
credential itself, because an argument is visible in the process list to every
user on the machine — and for the same reason, do not type the value on a
command line either, where it lands in the shell history. Read it instead:

```
install --directory --mode 700 ~/.config/reachy
read -r -s -p 'Groundstation credential: ' credential
printf '\n'
(umask 077; printf '%s' "$credential" > ~/.config/reachy/groundstation-credential)
unset credential
```

Then, from a checkout of this repository:

```
just sync
uv run --locked --all-packages reachyctl probe \
  --url ws://127.0.0.1:8080/v1/session \
  --frames services/groundstation/tests/fixtures/perception \
  --count 5 \
  --credential-file ~/.config/reachy/groundstation-credential
```

```
sequence	capability	detections	round_trip_ms
0	face	2	5.1
1	face	1	2.9
2	face	1	2.4
3	face	1	2.7
4	face	0	3.1
url	ws://127.0.0.1:8080/v1/session
source	12 recorded frames from services/groundstation/tests/fixtures/perception
offered	face,gesture
agreed	face
frames_submitted	5
frames_dropped	0
results_applied	5
results_superseded	0
results_ignored	0
errors_received	0
reconnections	0
round_trip_ms_fastest	2.4
round_trip_ms_median	2.9
round_trip_ms_slowest	5.1
probe	ok	5 result(s) over one session
```

The timings are this machine's over a loopback interface and are not a
prediction about yours; what matters is the shape. `agreed face` means
negotiation settled on something, `frames_dropped 0` means nothing was discarded
at the queue bound, and one row per frame means every frame was answered.

`frame 4` answering with `0` detections is correct: the fixture corpus contains
a frame with no face in it.

> The output is tab-separated because it was captured without a terminal
> attached. Run interactively, the same run is a table.

## 7. Confirm the whole chain, so far

`doctor` is the command that says which link is broken. Run without a robot it
checks the groundstation half and reports the rest as **skipped** rather than
failed — a check that did not run is not a check that found something wrong:

```
uv run --locked --all-packages reachyctl doctor \
  --url ws://127.0.0.1:8080/v1/session \
  --credential-file ~/.config/reachy/groundstation-credential \
  --models-dir .models
```

```
check	status	detail	remediation	command
daemon.reachable	skipped	no robot was given: pass --robot with user@host, or set REACHYCTL_ROBOT	-	-
application.installed	skipped	no robot was given: pass --robot with user@host, or set REACHYCTL_ROBOT	-	-
application.running	skipped	no robot was given: pass --robot with user@host, or set REACHYCTL_ROBOT	-	-
groundstation.session	passed	a session was established at ws://127.0.0.1:8080/v1/session	-	-
groundstation.capabilities	passed	negotiated face	-	-
groundstation.round-trip	passed	one frame went out and came back in 1.5 ms	-	-
models.files	passed	1 model file(s) present and matching their pinned digests: face_detection_yunet	-	-
configuration.effective	skipped	no robot was given: pass --robot with user@host, or set REACHYCTL_ROBOT; nothing declares what this robot is supposed to be: pass --intent with a declaration	-	-
home-assistant.identity	skipped	no robot was given: pass --robot with user@host, or set REACHYCTL_ROBOT; nothing declares what this robot is supposed to be: pass --intent with a declaration	-	-
groundstation	ws://127.0.0.1:8080/v1/session
robot	-
checks	9
passed	4
failed	0
skipped	5
first_failure	-
round_trip_ms	-
observer_failures	-
doctor	ok	nothing failed, but not everything was checked (4 passed, 0 failed, 5 skipped)
```

The exit status is 0, and the summary line says exactly what that means:
**nothing failed, but not everything was checked**. `--models-dir` is the
directory `just models` wrote into; leave it off and that check is skipped too.

## 8. Decide what the network may reach

By default the service is published on the loopback interface only:

```
GROUNDSTATION_PUBLISH=127.0.0.1:8080
```

A robot on another host cannot reach that, so a real deployment changes it — and
should do so deliberately, because **the session endpoint is authenticated and
the endpoints beside it are not**. `/livez`, `/readyz`, `/capabilities`,
`/config` and `/metrics` answer anybody who can reach the port. `/config` reports
secrets as `<set>` rather than by value, so what is exposed is the shape of the
deployment rather than its credential — but that is still more than a public
network should see.

Publish it on the interface the robot is on, not on all of them:

```
GROUNDSTATION_PUBLISH=198.51.100.10:8080
```

## 9. The accelerated variant, if the host has an NVIDIA GPU

Point `GROUNDSTATION_IMAGE` at the tag ending in `-cuda` **and** bring in the
overlay:

```
docker compose -f compose.yaml -f compose.cuda.yaml up --detach
```

> **⏳ PENDING HARDWARE VERIFICATION — no GPU host.** No expected output is
> recorded for this step. The accelerated image is built and verified as far as
> "every library its CUDA provider declares is present, bar the driver the
> container runtime injects", and it has never been run on a machine with a GPU.
> Nothing below is a transcript.

The overlay is what asks the host for a device. Without it the accelerated image
runs perfectly on the CPU and says so only in the `runtime.loaded` log line — a
safe fallback and a confusing one, which is why the accelerated deployment is a
second file rather than a tag substitution. The accelerated tag is published for
x86 only.

Whether the accelerated variant is faster than the CPU path is
[the benchmark suite's](../specs/benchmarks/) question, and the measurements that
made CPU the default say it may well not be for this workload.

## 10. Stopping, and starting again

```
docker compose down
```

removes the containers and the network but keeps the Prometheus volume. Add
`--volumes` to discard the collected metrics as well.

---

## What you have now

| Endpoint | What it answers |
|---|---|
| `http://<host>:8080/livez` | The process is up |
| `http://<host>:8080/readyz` | Every enabled capability finished warming up |
| `http://<host>:8080/capabilities` | What a session would be able to negotiate |
| `http://<host>:8080/config` | Every setting in force, secrets redacted |
| `http://<host>:8080/metrics` | Prometheus exposition, with exemplars |
| `ws://<host>:8080/v1/session` | The session endpoint the robot opens |
| `http://<host>:9090/` | The Prometheus scraping all of it |

`/metrics` carries the session identifier and frame sequence number as
OpenMetrics **exemplars** rather than as labels, so a slow frame is attributable
end to end without one time series per frame. Exemplars travel only in the newer
exposition format, which `/metrics` negotiates:

```
curl --silent --show-error http://127.0.0.1:8080/metrics | head -14
```

```
# HELP groundstation_sessions_total Sessions that reached a terminal state, by how they ended.
# TYPE groundstation_sessions_total counter
groundstation_sessions_total{outcome="going_away"} 1.0
groundstation_sessions_total{outcome="unauthenticated"} 1.0
# HELP groundstation_sessions_created Sessions that reached a terminal state, by how they ended.
# TYPE groundstation_sessions_created gauge
groundstation_sessions_created{outcome="going_away"} 1.7873420225198042e+09
groundstation_sessions_created{outcome="unauthenticated"} 1.7873420228297086e+09
# HELP groundstation_sessions_active Sessions currently established.
# TYPE groundstation_sessions_active gauge
groundstation_sessions_active 0.0
# HELP groundstation_frames_received_total Frames accepted from a client.
# TYPE groundstation_frames_received_total counter
groundstation_frames_received_total 5.0
```

The `unauthenticated` row above is real: it is the session refused during
[troubleshooting](../ops/troubleshooting.md#groundstationsession), on the same
service.

## Next

- [Provision the robot and deploy the satellite](robot.md)
- [Add the robot to Home Assistant](home-assistant.md) — read the identity
  warning there before you deploy anything
- [Update a running installation](../ops/deploy.md)
- [Diagnose a failure](../ops/troubleshooting.md)
