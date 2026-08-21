# 0004: Groundstation session and pipeline

## Summary

Implement the groundstation's transport, session layer, capability registry,
pipeline and observability — everything except the capabilities themselves and
the container packaging.

**Spec:** [Groundstation](../specs/groundstation/)
**Status:** complete
**Depends On:** 0003

## Motivation

This is the load-bearing change of phase 1. It establishes the session
behaviour the whole system depends on, and it is where the predecessor's
structural problem gets fixed: results travelled over per-request connections to
a listener on the robot, and connection reuse was a property of how the two
programs happened to be written rather than of the design.

It also has to be built before the capabilities, because a capability that is
written first will shape the registry around itself.

## Requirements

### Testing Requirements

This change MUST satisfy the project's standing testing rules (see
[Testing conventions](../specs/architecture/index.md#testing-conventions)). CI
enforces these as merge gates:

- Tests run with `pytest`, with async strict mode enabled.
- Unit tests MUST perform no input or output.
- Integration tests MUST exercise the real transport in-process rather than
  mocking it.
- Contract tests MUST run the golden fixtures from `reachy-contracts`.
- Coverage MUST be gated on the diff rather than on the whole tree.
- Type checking MUST run in strict mode for new modules.
- A lint or type suppression MUST carry the rule identifier and a justification.

Skipping or weakening any of these rules to land the PR MUST be treated as a bug
in the PR, not in the rule.

Backpressure and reconnection MUST be tested by inducing them, not by asserting
that the configuration exists. A test that sets a queue bound and never fills it
proves nothing about what happens when it fills.

### Functional requirements

The [groundstation spec](../specs/groundstation/) owns the service's observable
behaviour and the [robot link spec](../specs/robot-link/) owns the protocol.
Their scenarios are this change's acceptance criteria. What implementing them
requires of this change:

- A capability is an interface plus a registration. The session layer routes by
  capability name and holds no knowledge of any specific capability — the lint
  rule forbidding session-layer imports of capability modules is what keeps
  [REQ-022](../specs/groundstation/index.md#req-022-capabilities-register-without-transport-changes)
  true rather than merely intended.
- Negotiation is performed once per session and is never cached across
  reconnections, because a groundstation that restarted with a different
  capability set is an ordinary case.
- Frames are decoded exactly once per frame and the decoded result is shared by
  every capability, not decoded per capability.
- The queue is bounded per session and drops oldest, with the drop counted as a
  metric rather than logged per occurrence.
- Configuration handling implements
  [architecture REQ-009](../specs/architecture/index.md#req-009-configuration-is-validated-and-self-reporting):
  an unrecognised `REACHY_GROUNDSTATION_*` variable fails startup. This is the
  direct remedy for the predecessor bug where environment overrides were read by
  a function nothing called.
- Readiness reports not-ready until warm-up completes; liveness reports the
  process is alive. They are separate endpoints with separate meanings.
- A capability registers as unhealthy on initialisation failure and the service
  keeps serving the rest.

## Design

### Approach

Structure follows the spec's layout: `api/`, `session/`, `capabilities/`,
`pipeline/`, `runtime/`, `obs/`, `config.py`. This change delivers all of them
except `capabilities/` — which gets its interface and registry here, and its
first implementation in 0005 — and `runtime/`, which is only meaningful with a
model to load.

A test capability that returns a fixed result is shipped in the test suite, so
the registry, routing and pipeline are exercised end to end without any model.
That is what makes this change independently verifiable.

### Decisions

- **Decision**: A trivial in-test capability, not a stubbed real one.
  - **Why**: The session layer needs a second capability to prove it is not
    coupled to the first. Two real capabilities do not exist yet, so the test
    supplies the second.
  - **Alternatives considered**: Deferring the routing tests to 0005, which
    would mean the registry's central guarantee is unverified in the change that
    introduces it.
- **Decision**: Decode once, share the frame.
  - **Why**: Decode measured 2 ms against a 39 ms face pass. Negligible once,
    and it scales with capability count in a service explicitly designed to grow
    capabilities.
  - **Alternatives considered**: Per-capability decode, simpler and quadratic in
    exactly the dimension this service expands along.
- **Decision**: Drops are a counter, not a log line.
  - **Why**: Under sustained overload, per-occurrence logging produces its own
    load at the moment the service is least able to absorb it.
- **Decision**: Negotiation is not resumed across reconnections.
  - **Why**: A reconnection is frequently caused by a restart, and a restart is
    the most likely moment for the capability set to have changed. Caching would
    make the one case that matters the one case that breaks.

### Non-Goals

- No perception, no models, no inference — 0005.
- No container image or compose file — 0006.
- No client implementation; `probe` is 0007 and the robot adapter is 0012.
- No horizontal scaling or multi-robot support.

## Tasks

- [x] Implement configuration and observability
  - [x] Settings with prefix validation, unknown-variable rejection, and a boot
        dump that redacts every value marked secret
  - [x] A single place that marks which settings are secret, so redaction cannot
        be applied in the log and forgotten at the endpoint
  - [x] Structured logging carrying session identifier and sequence number
  - [x] Metrics registry and per-stage timing instrumentation
  - [x] Tracing spans across pipeline stages
- [x] Implement the session layer
  - [x] WebSocket endpoint and connection lifecycle
  - [x] Credential verification and rejection path
  - [x] Capability negotiation against the registry
  - [x] Routing by capability name, with the import-direction lint rule
- [x] Implement the pipeline
  - [x] Bounded per-session queue with drop-oldest and a drop counter
  - [x] Single decode per frame, shared across capabilities
  - [x] Result assembly keyed to sequence number, including the empty result
  - [x] Induced-backpressure and induced-reconnection integration tests
- [x] Implement the health and configuration surface
  - [x] Liveness and readiness endpoints with distinct semantics
  - [x] Capability health reporting, including the degraded case
  - [x] Metrics endpoint
  - [x] Effective-configuration endpoint

## Open Questions

- [x] Whether a session may renegotiate capabilities in place, without
      reconnecting. Useful if a capability recovers from a failed
      initialisation; more protocol surface. Current lean: no, reconnection
      covers it.
      **Resolved: no.** A session negotiates once and holds nothing that
      outlives its connection. In-place renegotiation would add a message type
      and a mid-session state change to every one of the protocol's three
      clients, in exchange for a case — a capability recovering after a failed
      initialisation — that this build cannot even produce: a failed capability
      stays unhealthy until the process restarts, and a restart drops the
      session anyway. Reconnection already covers it, and reconnection is
      exercised end to end.
- [x] What the queue bound should default to. It trades latency under burst
      against dropped frames, and the right value depends on the capability mix.
      Current lean: small, and measured in 0014.
      **Resolved: two, and 0014 measures it.** Small because the value of a
      frame is its recency: on a link measured at 100–170 ms idle, a frame
      queued behind two others has already been overtaken by the camera. Two
      rather than one so that a single slow pass does not discard the frame
      behind it, and rather than four so that the backlog cannot outlive its own
      usefulness. It is `REACHY_GROUNDSTATION_QUEUE_BOUND`, so the measurement
      in 0014 changes a number rather than the code.

## Completion Notes

- The capability boundary is enforced by `just lint-capability-boundary`, which
  is a dependency of `just lint` and therefore of `just check` and CI. It is
  ruff's `flake8-tidy-imports` `banned-api`, the same rule the vendored ESPHome
  boundary uses, but configured on the invocation rather than in
  `pyproject.toml`: `banned-api` is a single global list already spent on that
  boundary, whose negated `per-file-ignores` entry switches TID251 off
  everywhere outside the vendored directory — so an entry added there would be
  dead in the three packages it needs to guard, and would also ban
  `reachy_contracts`, which is the one import the session layer exists to carry.
  The recipe proves the rule fires against a committed fixture, proves it is
  scoped by running the same fixture under a `capabilities/` path, then runs it
  over the tree, then greps for the dynamic imports TID251 cannot see.
- The capability interface and registry live in `capabilities/`, and the types
  the transport, session and pipeline need live in `ports.py` beside them. That
  split is what makes the boundary expressible: a rule forbidding those three
  packages from importing `reachy_groundstation.capabilities` is only
  enforceable if what they legitimately need is somewhere else.
- The session identifier and the sequence number reach the metrics as
  OpenMetrics **exemplars** rather than as labels, which is how
  [REQ-028](../specs/groundstation/index.md#req-028-work-is-attributable-end-to-end)
  is satisfied without a series per frame — as labels they would be unbounded
  cardinality, and the series would outnumber the frames. `/metrics` negotiates
  the exposition format, because exemplars travel only in the newer one.
- Framing is `session/framing.py` and is built from `json` and `struct` rather
  than from a model of its own. The contracts package deliberately leaves
  framing to the transport, and a pydantic model here would be a second wire
  type outside the package that owns them — which is what TID253 exists to
  prevent. A control message embeds the contract type's own canonical bytes
  verbatim, so the golden fixtures still pin what goes out.
- No production capability ships here, and that is the design rather than an
  omission: the registry, the routing and the pipeline are exercised end to end
  by two unrelated test capabilities, which is what proves the registry is not
  shaped around whichever capability arrives first. Perception is 0005.
- Nothing in this change was verified against physical Reachy Mini hardware. The
  transport, the session and the pipeline are exercised in-process against a
  real uvicorn server and a real WebSocket client; the robot-side half arrives
  in 0012 and the end-to-end run against a robot is a separate exercise.

## References

- Spec: [Groundstation](../specs/groundstation/), [Robot Link](../specs/robot-link/)
- Related changes: [0003-contracts-package](./0003-contracts-package.md),
  [0005-perception-capability](./0005-perception-capability.md),
  [0006-groundstation-images](./0006-groundstation-images.md)
