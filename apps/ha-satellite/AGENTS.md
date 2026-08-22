# apps/ha-satellite

The robot-side ESPHome voice satellite for Home Assistant. Distribution
`reachy-mini-ha-satellite`, import name `reachy_mini_ha_satellite`.

**Spec:** [ha-satellite](../../docs/specs/ha-satellite/).
**Fills this in:**
[0011](../../docs/changes/0011-satellite-esphome-vendoring.md) (done),
[0012](../../docs/changes/0012-satellite-ports-and-adapters.md) (done),
[0013](../../docs/changes/0013-satellite-behaviour-and-ui.md) (done) and
[0016](../../docs/changes/0016-audible-playback.md) (in progress — its code has
landed and its listening test has not). `docs/specs/ha-satellite/index.md` is
registered in `.duvet/config.toml`, so all eleven of its requirements are
traced.

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
| `src/reachy_mini_ha_satellite/behaviour/` | The pure decision layer: a pipeline state machine, a face tracker, and what each state looks like |
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
