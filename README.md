# reachy

A Home Assistant voice satellite for the Reachy Mini, with the heavy computation
moved off the robot.

The robot listens, speaks, and looks at whoever is talking to it. **Home
Assistant runs the voice pipeline** — speech to text, intent, text to speech —
and the robot is its satellite: it detects the wake word locally, streams the
audio up, and plays the response back. Face detection is offloaded too, over
**one long-lived session** to a groundstation service on a machine with cores to
spare, because a robot with four of them running motion control, audio and a
wake-word model has none left for inference.

## What is here

| | |
|---|---|
| **`apps/ha-satellite`** | The robot application: an ESPHome voice satellite, a pure behaviour layer that turns pipeline events and detections into antenna and head motion, and a settings page |
| **`services/groundstation`** | The off-robot service: a session endpoint, a capability registry, and face detection. Ships as a multi-architecture container image, with an accelerated variant |
| **`cli/reachyctl`** | `probe`, `doctor`, `deploy`, `config`, `app`, `provision`, `bench` — one tool that says which link in the chain is broken |
| **`provisioning/ansible`** | A stock robot image to a configured one, idempotently, with a gate that proves it |
| **`packages/`** | The wire types, the session client, and the one definition of what a healthy installation is — each declared once and imported everywhere |
| **`bench/`** | The performance suite, its committed baseline, and the regression gate |

## Start here

**Setting one up:**

1. [The groundstation](docs/setup/groundstation.md) — deploy the service with compose
2. [The robot](docs/setup/robot.md) — provision it and install the satellite
3. [Home Assistant](docs/setup/home-assistant.md) — add the device.
   **Read its identity warning before you install or upgrade anything**; it is
   the one step in this repository that cannot be undone

**Running one:**

- [Updating a running installation](docs/ops/deploy.md)
- [Troubleshooting](docs/ops/troubleshooting.md), keyed to `reachyctl doctor`'s
  check identifiers

## ⚠️ The three motor switches need a forked `reachy-mini`, for now

The Home Assistant switches for the head, body and antenna motor groups turn
physical torque on and off, so the satellite will not announce one until it can
*prove* what the torque actually is. That takes a daemon call that correlates a
selective torque request with its completion and reads physical torque back for
every motor in the group individually. **No released `reachy-mini` provides it.**
The published methods send and return `None`, and the daemon's aggregate motor
mode is not per-group state.

So the robot must run the branch **`feat/correlated-motor-torque-readback`** from
the fork at **https://github.com/fx/reachy_mini** in its daemon's application
environment. That branch is reviewed, but it is neither released nor merged
upstream — no upstream pull request is open yet — and the pin in
[`pyproject.toml`](pyproject.toml) and
[`apps/ha-satellite/pyproject.toml`](apps/ha-satellite/pyproject.toml) therefore
still names the released range. **Move both pins to an official release once one
carries the API**, and delete this section with them.

Without that build the satellite **fails closed, and only there**: the daemon
boundary finds no confirmation method, every group stays unconfirmed, and the
application announces **no motor switch at all** rather than a switch whose state
it made up. Bounded, identifier-free diagnostics record the missing confirmation.
Nothing else changes. The groundstation URL text control, the live replacement
behind it, and the groundstation's `/stream.mjpg` camera feed are unaffected by
which `reachy-mini` the robot runs — they do not touch motors.

**Working on it:**

- [`AGENTS.md`](AGENTS.md) — the map of the repository and the invariants that
  hold across it. Read this before changing anything
- [`REVIEW.md`](REVIEW.md) — the review conventions every change is judged against
- [`docs/index.md`](docs/index.md) — ten specs and the change documents that
  sequence the work. The specs are the authority; a change document sequences, it
  does not redefine
- [`docs/contracts/`](docs/contracts/) — generated schemas and interface
  descriptions, kept honest by a drift gate

## Getting the toolchain

```
mise install     # the pinned python, uv, just, rust, duvet and gitleaks
just sync        # install the workspace exactly as uv.lock describes it
just models      # fetch the pinned model weights, which are never committed
just check       # lint, typecheck, test
```

`just --list` is the whole command surface. Continuous integration calls these
recipes rather than restating the commands, so every merge gate reproduces
locally with the command in its step.

## Three things worth knowing before you read the code

**The robot link is one session, not a request per detection.** Its predecessor
posted detections over per-request HTTP connections, and a cold handshake to the
robot measured 378 ms — so connection reuse was the difference between 22 ms and
1241 ms per post, and it was achieved by convention. The
[robot-link protocol](docs/specs/robot-link/) replaces that with a contract.

**Configuration cannot fail silently.** Its predecessor read environment
overrides in a function nothing ever called, so every value that looked tuned was
in fact a default — for months. Every component here refuses to start on a
variable under its own prefix that it does not recognise, and emits its fully
resolved configuration at startup with secrets shown as set or unset rather than
by value.

**Everything is testable without a robot.** No test may require a robot, a
camera or a microphone; anything that needs one is reached through an interface
and exercised with a fake. `pytest` runs with `--disable-socket`, so a unit test
that opens a socket errors rather than quietly depending on the machine it ran
on.

## Licence and provenance

The vendored ESPHome core under `apps/ha-satellite/src/reachy_mini_ha_satellite/esphome/`
is Apache-2.0, carries its licence and a `NOTICE` recording the upstream project,
the files derived from it and the commit they were taken at, and every file
records its own provenance in its header. The face model is YuNet, MIT, pinned by
digest and never committed. Weights are fetched by `just models` from the sources
`services/groundstation/src/reachy_groundstation/models/registry.py` records.
