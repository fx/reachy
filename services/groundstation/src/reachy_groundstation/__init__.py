"""The off-robot service that hosts heavy computation as pluggable capabilities.

The shape, from the edge inwards:

- `api/` — the WebSocket session endpoint and the operator surface: liveness,
  readiness, capability health, metrics and the resolved configuration.
- `session/` — authentication, framing, capability negotiation and routing by
  name. Knows no capability.
- `pipeline/` — the bounded per-session queue, the single decode, and result
  assembly. Knows no capability either.
- `capabilities/` — the interface, the registry, and the capabilities
  themselves. The first one arrives in change 0005.
- `obs/` — structured logging, metrics and tracing.
- `ports.py` — the seam between the two halves: the decoded frame, the
  capability interface, and the registry interface.
- `service.py` — the composition root, and the only module outside
  `capabilities/` that imports it.

`just lint-capability-boundary` enforces that direction rather than trusting it:
nothing under `api/`, `session/` or `pipeline/` may import
`reachy_groundstation.capabilities`, which is groundstation REQ-022 expressed as
something a build can fail on.

Nothing here reads the environment except `config.load_settings`, and nothing
reaches a model, a camera or a network at import time — see
`docs/specs/groundstation/` for what this package is required to do.
"""
