# services/groundstation

The off-robot service that hosts heavy computation as pluggable capabilities.
Distribution `reachy-groundstation`, import name `reachy_groundstation`.

**Specs:** [groundstation](../../docs/specs/groundstation/), with
[perception](../../docs/specs/perception/) for the first capability and the
proposed
[Home Assistant Configuration and Camera Feed](../../docs/specs/home-assistant-configuration-and-camera-feed/)
contract for its operator video surface.
**Filled in by:** [0004](../../docs/changes/0004-groundstation-session.md) —
transport, session layer, capability registry, pipeline and observability —
[0005](../../docs/changes/0005-perception-capability.md), which added the model
runtime, the pinned model store and the perception capabilities, and
[0006](../../docs/changes/0006-groundstation-images.md), which added the
container image, its verification and the standalone deployment. The proposed
[0020](../../docs/changes/0020-home-assistant-configuration-and-camera-feed.md)
(draft) adds one global latest-only MJPEG value for the sole authenticated
session after explicit JPEG-format validation and successful decode; it does not
change the robot-link wire contract.

Read the root [`AGENTS.md`](../../AGENTS.md) first — it holds the invariants
that apply here.

## Layout

```
src/reachy_groundstation/
├─ api/            # the session endpoint and the operator surface
├─ session/        # authentication, framing, negotiation, routing
├─ pipeline/       # the bounded queue, the single decode, result assembly
├─ capabilities/   # the interface, the registry, and the capabilities
│  └─ perception/  # face detection, gesture recognition, coordinates
├─ models/         # the pinned registry, the build-time fetch, the run-time store
├─ runtime/        # model-runtime sessions: providers, thread bounds, warm-up
├─ obs/            # structured logging, metrics, tracing
├─ ports.py        # the seam: the decoded frame and the two interfaces
├─ config.py       # settings, read once by a function the entry point calls
└─ service.py      # the composition root

Dockerfile         # the image: one definition, two variants, two architectures
Dockerfile.dockerignore
deploy/
├─ compose.yaml    # the service and a Prometheus that actually scrapes it
├─ prometheus.yml  # the scrape configuration
└─ .env.example    # every variable compose reads, checked against config.py
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
- **No test may need a camera or a GPU.** The perception integration tests run
  real inference on the CPU against committed fixture images — mocking the
  runtime there would test the mock — and they read the weights `just models`
  put in place, so each declares `@pytest.mark.filesystem`. The transport
  integration tests do open a socket, in-process, and each one declares
  `@pytest.mark.enable_socket`.
- **Weights are never committed.** `models/registry.py` pins each model by
  digest and records its licence, attribution, upstream project and retrieval
  URL. `models/fetch.py` retrieves and verifies at build time and is the only
  module here that can reach a network; `models/store.py` is what the running
  service uses and cannot. Adding a model means adding a registry entry, and the
  licence allowlist is a unit test rather than a review step.
- **Disabled is not unhealthy.** A capability switched off by configuration
  raises `CapabilityDisabledError` from its factory and the registry records
  `CapabilityState.DISABLED`. An operator has to be able to tell a setting from
  a fault.
- **The image is verified by being run, never by being read.** `just image`
  builds it from the repository root; `just image-verify` starts it on a Docker
  network with no route off the host, proves the model source really is
  unreachable from inside, asserts the runtime stage carries no toolchain and
  does not run as root, and drives a real session with a committed fixture frame
  through it from a sibling container. A build that succeeds and produces a
  service that cannot start is a passing build and a broken release.
- **A setting added here is a setting added to `deploy/.env.example`.** The two
  are compared by `tests/test_groundstation_deployment.py`, which also checks
  that every documented value is the default the model carries, that everything
  compose interpolates is documented, and that the scrape configuration targets
  the port the example sets. Deployment documentation that can rot is
  deployment documentation that has.
