# apps/ha-satellite

The robot-side ESPHome voice satellite for Home Assistant. Distribution
`reachy-mini-ha-satellite`, import name `reachy_mini_ha_satellite`.

**Spec:** [ha-satellite](../../docs/specs/ha-satellite/).
**Fills this in:**
[0011](../../docs/changes/0011-satellite-esphome-vendoring.md) (done),
[0012](../../docs/changes/0012-satellite-ports-and-adapters.md) and
[0013](../../docs/changes/0013-satellite-behaviour-and-ui.md).

Read the root [`AGENTS.md`](../../AGENTS.md) first — it holds the invariants
that apply here.

## What is here

| Path | What it is |
|---|---|
| `src/reachy_mini_ha_satellite/esphome/` | The vendored ESPHome protocol layer, with its `LICENSE`, `NOTICE` and per-file provenance |
| `src/reachy_mini_ha_satellite/esphome/seams.py` | The two audio interfaces cut into it. Not vendored, and unimplemented until 0012 |
| `src/reachy_mini_ha_satellite/assets/` | Wake-word models and sounds that ship in the wheel, with the registry recording each one's terms |
| `tests/` | The carried upstream tests, with their own `LICENSE` and `NOTICE`, plus this repository's own |

Everything the spec's structure diagram shows and this table does not — `main.py`,
`ports.py`, `adapters/`, `behaviour/`, `web/`, `config.py` — belongs to 0012 and
0013. Do not add it ahead of the change that owns it.

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
- **Behaviour is pure.** The behaviour layer takes the ports as arguments and
  performs no input or output itself.
