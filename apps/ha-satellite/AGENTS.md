# apps/ha-satellite

The robot-side ESPHome voice satellite for Home Assistant. Distribution
`reachy-mini-ha-satellite`, import name `reachy_mini_ha_satellite`.

**Specs:** [ha-satellite](../../docs/specs/ha-satellite/),
[gaze-control](../../docs/specs/gaze-control/) and the proposed
[Home Assistant Configuration and Camera Feed](../../docs/specs/home-assistant-configuration-and-camera-feed/)
contract.
**Fills these in:**
[0011](../../docs/changes/0011-satellite-esphome-vendoring.md) (done),
[0012](../../docs/changes/0012-satellite-ports-and-adapters.md) (done),
[0013](../../docs/changes/0013-satellite-behaviour-and-ui.md) (done),
[0016](../../docs/changes/0016-audible-playback.md) (done, on the robot as well
as in the suite: announcements were played through the real speaker and judged
audible across a room, and that change's own Outcome section records the
levels), and
[0019](../../docs/changes/0019-predictive-gaze-and-coordinated-motion.md) (done,
including deterministic acceptance evidence and the scrubbed staged-rollout
outcome). The app is also one side of
[0020](../../docs/changes/0020-home-assistant-configuration-and-camera-feed.md)
(draft), which proposes live motor-group and groundstation entities plus the
robot half of the camera-feed contract. Body motion remains restart-bound, false
by default and a provisional opt-in because the rollout did not settle
calibration or its shipping default. The two implemented specs are registered in
`.duvet/config.toml`; repository traceability covers all nine implemented specs
and all 92 implemented requirements. The proposed spec remains unregistered
until 0020 completes REQ-093–098.

Read the root [`AGENTS.md`](../../AGENTS.md) first — it holds the invariants
that apply here.

## What is here

| Path | What it is |
|---|---|
| `src/reachy_mini_ha_satellite/ports.py` | `AudioPort`, `MotionPort`, `PerceptionPort` and the value types they speak in |
| `src/reachy_mini_ha_satellite/adapters/` | Everything that touches the robot: the daemon's shape, audio, motion and the two perception sources |
| `src/reachy_mini_ha_satellite/adapters/output_gain.py` | The boost, the soft knee and the limiter that make the robot audible. Pure arithmetic over arrays; its constants are the predecessor application's, tuned by ear against this speaker |
| `src/reachy_mini_ha_satellite/adapters/decode.py` | `av`, turning a resolved file into the mono float samples the daemon's push path takes, at the rate the daemon reports |
| `src/reachy_mini_ha_satellite/esphome/` | The vendored ESPHome protocol layer, with its `LICENSE`, `NOTICE` and per-file provenance |
| `src/reachy_mini_ha_satellite/esphome/seams.py` | The two audio interfaces cut into it. Not vendored; filled by `adapters/audio_reachy.py` |
| `src/reachy_mini_ha_satellite/assets/` | Wake-word models and sounds that ship in the wheel, with the registry recording each one's terms |
| `src/reachy_mini_ha_satellite/behaviour/` | The pure decision layer: pipeline expression, source-qualified face selection, predictive gaze estimation, bounded coordinated trajectories and head arbitration |
| `src/reachy_mini_ha_satellite/wake_word.py` | What actually *runs* the wake-word models over captured audio — the thresholds, the refractory window and the mute check, with only the model calls behind a seam |
| `src/reachy_mini_ha_satellite/config.py` | Settings, their three layers, and the one place a secret is declared to be one |
| `src/reachy_mini_ha_satellite/web/` | The settings interface REQ-049 requires |
| `src/reachy_mini_ha_satellite/main.py` | The composition root: ports to adapters, the loop, and the four services |
| `src/reachy_mini_ha_satellite/daemon_app.py` | The `reachy_mini_apps` entry point, and the ONLY module that imports the SDK |
| `tests/support/satellite_support.py` | The fake for every port, plus the fakes the adapters' own tests need |
| `tests/` | The carried upstream tests, with their own `LICENSE` and `NOTICE`, plus this repository's own |

Deployment is documented in
[`docs/ops/satellite-deployment.md`](../../docs/ops/satellite-deployment.md),
which leads with the identity-pinning warning because that is the one thing a
deployment can get irreversibly wrong.

## Local rules

- **Do not edit the vendored directory casually.** Every file in
  `esphome/` except `seams.py` and `__init__.py` is derived from a third-party
  project, and its `NOTICE` enumerates the complete diff from upstream. An edit
  that is not in that list is either a bug fix that belongs upstream or a change
  that belongs outside the directory. Adding one means adding it to the NOTICE.
- **Vendored code is not reformatted, restyled or retyped, and it is not held to
  the diff-coverage threshold.** The shared ruff and mypy configuration names
  those nine files and stands down, and `just coverage-diff` excludes them: they
  are formatted the way upstream formats them, type-checked under a recorded
  relaxation, and covered by whatever tests upstream wrote. Each exception is
  deliberate, each names the files rather than the directory, and none of them
  extends to anything else in this package — `esphome/seams.py` sits among them
  and is subject to all three.
- **Nothing in the vendored directory may import anything Reachy-specific.** The
  dependency runs one way, and it is a build failure rather than a convention:
  ruff bans the imports, a grep catches the spellings ruff cannot express, and
  `just lint-boundary` proves both halves still fire by running them against a
  fixture that breaks them.
- **Licensing is the reason this package is a rewrite.** The predecessor carried
  no licence text despite declaring one. Any directory holding code derived from
  a third-party project carries that project's licence and a notice recording
  the upstream project, the derived files and the upstream commit — in the
  directory itself, readable without leaving it.
- **An asset that ships is an asset that is registered.** Adding a wake-word
  model or a sound means adding it to
  `src/reachy_mini_ha_satellite/assets/registry.py` with its source, licence and
  digest. `just check-assets` fails on an unregistered file, on a registered file
  that is missing, and on a digest that no longer matches; the unit test over the
  registry fails on a licence outside the allowlist. Exempting a file instead of
  registering it is not a way round either: the exemption list is a closed literal
  pinned by that same test, so adding to it means editing two files in one pull
  request. Widening the allowlist and extending the exemptions are both licensing
  decisions, made in review.
- **This package runs on the robot.** It installs into an application
  environment shared with, and managed by, the Reachy Mini daemon, so it cannot
  assume a virtual environment of its own, and it is built and tested for
  aarch64.
- **Hardware is reached through ports.** Audio, motion and perception are
  interfaces with Reachy adapters and test fakes, which is what lets the suite
  run with no robot attached. The Reachy Mini SDK is not a dependency of this
  package and must not become one outside an adapter: importing it pulls in
  system libraries a continuous integration runner does not have.
- **The SDK is an extra, and it is loaded by file path.** `local-detection` in
  `pyproject.toml` is the only place the distribution is named, and
  `adapters/perception_local.py` is the only module that reaches it — through
  `importlib`, inside the function that needs it, because `import
  reachy_mini.<anything>` executes an `import gi` three modules away. Everything
  else the daemon offers is reached through the protocols in `adapters/daemon.py`,
  which the application is handed an implementation of. Adding an ordinary import
  of the SDK anywhere in this package breaks the test suite on any machine
  without GStreamer, which is every runner.
- **Behaviour is pure, and it is enforced.** `behaviour/` takes the ports as
  arguments, performs no input or output, reads no clock and never sleeps — time
  is a parameter to every method that needs it. `just lint-behaviour-boundary`
  bans the imports that would break that (adapters, the vendored protocol layer,
  `main`, `web`, the SDK), greps for the dynamic imports and clock reads ruff
  cannot see, and proves both halves still fire by running them against a
  fixture that breaks them.
- **Predictive gaze is two-phase and owns one controller trajectory.** Behavior
  first prepares a source-qualified directive; `main.py` samples measured state
  and asks the motion adapter to calibrate outside behavior; behavior then
  finishes the controller tick. Re-reading one detection advances the existing
  trajectory without reapplying its image correction. Do not reintroduce a
  direct gaze gain, vertical trim, smoothing loop, deadzone loop or linear
  neutral return around it.
- **Calibrated gaze commands absolute canonical world poses.**
  `adapters/motion_reachy.py` retains bounded measured-pose history whose retention
  age derives from configured staleness and whose capacity is sized for that window
  at the minimum supported cadence, brackets the SDK's non-moving image query,
  rebases capture against query, and constructs zero-roll,
  zero-translation head commands. The controller seeds world yaw/elevation from
  measured pose before its first hardware sample. With body enabled it also waits
  for measured joint zero before head and body travel in one `set_target` call;
  world gaze remains body yaw plus head-on-body yaw. Later body feedback observes
  commanded state and never overwrites commanded position, velocity or
  acceleration.
- **Gaze acquisition disables daemon automatic body yaw.** This happens before
  the first gaze head command even in head-only mode. Tracking disabled at
  startup performs no acquisition, automatic-yaw toggle or motion-feedback read.
  Terminal release is always called, restores automatic yaw exactly once only
  when acquired, and blocks every racing or later gaze, head and antenna call.
  Body motion is restart-bound, false by default and remains a provisional opt-in.
- **Values crossing the motion boundary belong to `ports.py`.** Qualified gaze
  directives, measured motion and coordinated samples must remain resolvable
  runtime types there; adapters and behavior import them rather than redeclaring
  aliases or leaving protocol annotations behind `TYPE_CHECKING`.
- **Controller fault and lifecycle are independent.** Stable fault categories
  derive `safe_hold`; they are never encoded as tracking modes. One validated
  `ControllerConfig` instance is shared by behavior and the production motion
  adapter, every atomic q/v/a sample is checked before promotion or hardware,
  and a failed daemon call cannot promote its candidate. Recovery counts only
  configured consecutive independent timestamps, observation identities,
  candidates or command calls; replayed evidence does not advance it.
- **Controller diagnostics are bounded and identifier-free.** The pure fixed-size
  ring retains only its fixed scalar/enum/boolean/null schema. `/status` adds the
  mode/fault/safe-hold summary; `GET /diagnostics/controller` reads the bounded
  events and same-origin `POST /diagnostics/controller/reset` clears only that
  ring, without changing controller, motion, settings or perception state.
- **Four released gaze settings are compatibility inputs only.**
  `gaze_deadzone`, `gaze_smoothing`, `camera_horizontal_fov_degrees` and
  `camera_vertical_fov_degrees` remain accepted and validated so upgrades keep
  starting, but predictive control reads none of them. They stay outside
  `LIVE_SETTINGS`, are read-only and marked `legacy compatibility; ignored` on
  the settings surface, and an ordinary save drops stale override copies.
- **The announced Home Assistant identity has no default.** `device_name` is
  required and the application refuses to start without it. Home Assistant keys
  a device on what it announces, so a derived default would be correct on a
  fresh install and silently destructive on an upgrade. Do not add one.
- **The overrides layer cannot supply a setting the settings page depends on.**
  An override sits above the environment, so it can only be undone by writing
  another one — and a page that had written one of these wrongly is the page
  nobody can open to undo it. `config.BOOTSTRAP_SETTINGS` is the list —
  `state_dir`, which names the directory the overrides file lives in, and the
  three `web_*` settings, which decide whether the interface is served and
  where. The page renders them read-only and the form refuses to write one
  however it was submitted. A setting the interface's own reachability turns on
  belongs in that set.
- **The settings interface is unauthenticated, and the network is the trust
  boundary.** So is the ESPHome API this application serves
  (`uses_password=false`) and so is the daemon's own dashboard. What `web/` does
  close is the cross-site half: a state-changing request a browser reports as
  coming from another site is refused, so a page an operator visits cannot stop
  the robot. Do not add a state-changing route without that check — `web/app.py`
  has it on every one — and do not invent an authentication scheme here: it is a
  question for the spec, recorded as an escalation in change 0013.
- **A secret is declared secret in exactly one place.** `SECRET_SETTINGS` is
  derived from the field types in `config.py`, and the boot log, the settings
  page and `/config` all read it through `resolved_configuration`. Redact first,
  render second: a value transformed before a redactor sees it leaks in a form
  no search finds.
- **The SDK is imported by one file and one file only.** `daemon_app.py` is the
  `reachy_mini_apps` entry point, so it has to subclass the daemon's application
  base class; nothing else in the package may name the SDK. `main.py` is handed
  an `adapters.daemon.RobotHandle` and never learns where it came from.
