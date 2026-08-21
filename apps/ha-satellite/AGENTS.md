# apps/ha-satellite

The robot-side ESPHome voice satellite for Home Assistant. Distribution
`reachy-mini-ha-satellite`, import name `reachy_mini_ha_satellite`.

**Spec:** [ha-satellite](../../docs/specs/ha-satellite/).
**Fills this in:**
[0011](../../docs/changes/0011-satellite-esphome-vendoring.md),
[0012](../../docs/changes/0012-satellite-ports-and-adapters.md) and
[0013](../../docs/changes/0013-satellite-behaviour-and-ui.md).

Read the root [`AGENTS.md`](../../AGENTS.md) first — it holds the invariants
that apply here.

## Local rules

- **This is a scaffold.** It has a `pyproject.toml`, this file and an empty
  package. Do not add implementation ahead of the change that owns it.
- **Licensing is the reason this package is a rewrite.** The predecessor carried
  no licence text despite declaring one. Any directory holding code derived from
  a third-party project carries that project's licence and a notice recording
  the upstream project, the derived files and the upstream commit — in the
  directory itself, readable without leaving it.
- **This package runs on the robot.** It installs into an application
  environment shared with, and managed by, the Reachy Mini daemon, so it cannot
  assume a virtual environment of its own, and it is built and tested for
  aarch64.
- **Hardware is reached through ports.** Audio, motion and perception are
  interfaces with Reachy adapters and test fakes, which is what lets the suite
  run with no robot attached.
- **Behaviour is pure.** The behaviour layer takes the ports as arguments and
  performs no input or output itself.
