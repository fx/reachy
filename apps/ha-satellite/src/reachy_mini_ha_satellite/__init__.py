"""The robot-side ESPHome voice satellite for Home Assistant.

`esphome/` holds the vendored ESPHome protocol layer, with its own licence,
notice and per-file provenance; `assets/` holds the wake-word models and sounds
that ship in the wheel, with the registry that records each one's terms. The
ports and adapters that fill the two audio seams arrive in change 0012, and the
behaviour layer and settings interface in 0013. See `docs/specs/ha-satellite/`
for what this package is required to do.
"""
