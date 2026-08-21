"""Vendored ESPHome voice-satellite protocol layer.

Derived from the Home Assistant project's Linux voice assistant, Apache-2.0.
`LICENSE` and `NOTICE` in this directory record the upstream project, the files
derived from it and the commit they were taken at; every module carries the same
provenance in its own header.

Nothing in this directory may import anything Reachy-specific. The dependency
runs one way — the robot side depends on the vendored protocol, never the
reverse — and a lint rule enforces it rather than leaving it to convention. The
two audio seams are `seams.py`, which is the one file here that is not vendored.
"""

#:= docs/specs/architecture/index.md#req-007-vendored-third-party-code-is-attributed-in-place
#:% Any directory containing code derived from a third-party project MUST carry that
#:% project's licence text and a notice recording the upstream project, the files
#:% derived from it, and the upstream commit they were taken at.
