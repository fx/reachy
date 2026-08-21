# services/groundstation

The off-robot service that hosts heavy computation as pluggable capabilities.
Distribution `reachy-groundstation`, import name `reachy_groundstation`.

**Spec:** [groundstation](../../docs/specs/groundstation/), with
[perception](../../docs/specs/perception/) for the first capability.
**Filled in by:** [0004](../../docs/changes/0004-groundstation-session.md) —
transport, session layer, capability registry, pipeline and observability. The
first capability arrives in
[0005](../../docs/changes/0005-perception-capability.md) and the container image
in [0006](../../docs/changes/0006-groundstation-images.md).

Read the root [`AGENTS.md`](../../AGENTS.md) first — it holds the invariants
that apply here.

## Layout

```
src/reachy_groundstation/
├─ api/            # the session endpoint and the operator surface
├─ session/        # authentication, framing, negotiation, routing
├─ pipeline/       # the bounded queue, the single decode, result assembly
├─ capabilities/   # the interface, the registry, and the capabilities
├─ obs/            # structured logging, metrics, tracing
├─ ports.py        # the seam: the decoded frame and the two interfaces
├─ config.py       # settings, read once by a function the entry point calls
└─ service.py      # the composition root
```

## Local rules

- **The capability boundary is enforced, not documented.** Nothing under `api/`,
  `session/` or `pipeline/` may import `reachy_groundstation.capabilities` —
  they hold a `CapabilityRegistryPort` handed to them by `service.py`, which is
  the only module outside `capabilities/` that names it.
  `just lint-capability-boundary` proves that rule fires against a committed
  fixture and then runs it over the tree; it is part of `just lint`.
- **A capability is an interface plus a registration.** Implement
  `ports.CapabilityPort` — `CapabilityBase` gives you the two lifecycle hooks
  defaulted — and decorate a factory with `capabilities.register`. That, plus
  importing your module from `capabilities/__init__.py`, is the whole of adding
  one. Do not touch the transport, the session layer or another capability.
- **Wire types come from `reachy-contracts`.** Never redefine one locally. The
  framing in `session/framing.py` is built from `json` and `struct` for exactly
  that reason: it is transport, and a model here would be a second wire type
  outside the package that owns them.
- **The capture timestamp is opaque.** Copy it from the frame onto the result and
  never read it. `ResultEnvelope.for_frame` is what does that; use it. Where the
  service needs its own notion of recency it uses arrival order and its own
  monotonic clock, both purely local.
- **Configuration fails loud.** An unrecognised `REACHY_GROUNDSTATION_*` variable
  stops startup and names itself. `config.load_settings` is the only reader, and
  it is a pure function of the mapping it is given.
- **Mark a secret in one place.** A setting declared as a `SecretStr` is in
  `SECRET_SETTINGS` automatically, and both self-reporting surfaces — the boot
  log and `/config` — render through `resolved_configuration`. Do not redact
  anywhere else.
- **Drops are counted, never logged.** Frames are dropped when the service is
  already saturated, and per-occurrence logging would add load at the worst
  moment.
- **No test may need a camera or a GPU.** Model runtimes are reached through an
  interface and exercised with a fake. The integration tests do open a socket,
  in-process, and each one declares `@pytest.mark.enable_socket`.
