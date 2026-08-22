# 0018: Satellite runtime stability

## Summary

Make the HA satellite own its startup, connection, listener, audio-pump and
shutdown lifecycles explicitly, so transient failures cannot silently leave the
robot asleep, disconnected, deaf or half-closed.

**Spec:** [ha-satellite](../specs/ha-satellite/)
**Status:** in-progress
**Depends On:** 0013

## Approval

The operator requested this follow-up with `/dev` after reporting that the
processes or connections must not go stale or die randomly. They separately
selected **“Wake fully on start”**, explicitly approving motor enablement, the
SDK-controlled wake motion and the SDK wake sound during application startup.

## Motivation

The application works on the robot, but several independent runtime paths still
have one-shot ownership:

- startup begins normal services without performing the SDK's controlled wake;
- overlapping authenticated Home Assistant connections share one active slot,
  so losing the newest connection can discard an older healthy survivor;
- the ESPHome listener has no owner that restores a listener which stops
  unexpectedly;
- a single microphone read, conditioning or forwarding exception can end the
  long-lived audio work;
- accepted protocol transports and asynchronous cleanup can outlive shutdown.

Each defect is intermittent in production and deterministic at its lifecycle
boundary. This change makes those boundaries observable and testable without a
robot, network socket, filesystem access or wall-clock delay.

## Requirements

### Testing Requirements

This change MUST satisfy the project's standing testing rules (see
[Testing conventions](../specs/architecture/index.md#testing-conventions)). CI
enforces those rules as merge gates. This change additionally sequences the
focused RED regressions and hardware-free lifecycle verification below.

### Behaviour

The existing HA satellite specification owns the acceptance criteria. This
change is linked specifically to
[REQ-044](../specs/ha-satellite/index.md#req-044-wake-word-detection-runs-on-the-robot),
[REQ-046](../specs/ha-satellite/index.md#req-046-voice-pipeline-state-is-expressed-through-movement),
[REQ-048](../specs/ha-satellite/index.md#req-048-the-head-returns-to-neutral-when-tracking-data-goes-stale)
and
[REQ-050](../specs/ha-satellite/index.md#req-050-shutdown-is-graceful-and-leaves-the-robot-safe).
This document sequences the stability work and does not redefine those
requirements.

## Design

### Decisions

- **Perform the SDK's full controlled wake before composing normal services.**
  Startup offloads `enable_motors()` and then `wake_up()` in that order. It checks
  for stop before motor enable and between each completed blocking SDK call and
  the next boundary; a call already running on a worker thread finishes, but no
  later wake, composition or service start begins. A wake failure aborts
  composition, so the application never advertises a normal runtime while the
  approved wake sequence is incomplete.
- **Treat authenticated Home Assistant protocols as a surviving set.** The
  newest authenticated protocol is active. Losing a non-active protocol changes
  nothing; losing the active protocol promotes a survivor; only losing the final
  protocol performs the shared disconnect transition.
- **Supervise listener ownership.** A healthy listener is left alone. A listener
  demonstrably stopped without an intentional close is rebound. Bind failures
  retry on a bounded, capped backoff schedule; an intentional close disables the
  supervisor before releasing the listener.
- **Make audio failure isolation per chunk.** Capture/read, conditioning and
  protocol-forwarding failures are logged and the pump continues. A forwarding
  failure does not withhold that same conditioned chunk from local wake-word
  detection.
- **Close what the service accepted.** Shutdown closes every accepted protocol
  transport and clears shared active-connection state before returning.
- **Bound asynchronous cleanup independently.** A cleanup that does not return
  cannot prevent later cleanup. Shutdown records the timeout, gives cancellation
  one event-loop turn, then force-finalizes and observes a child that still will
  not stop. It snapshots pre-existing loop tasks and owns only tasks the cleanup
  spawns, finalizing nested shield descendants to a fixed point without touching
  unrelated runner work. Repeated owner cancellation is deferred until that
  finalization and later cleanup attempts have finished, then re-raised.
- **Log lifecycle transitions, not environment identity.** Startup, promotion,
  listener retry/rebind, isolated audio failure and cleanup timeout logs carry no
  host, address, account, credential or other installation identifier. Failure
  logs name only their static stage, aggregate count or numeric retry delay;
  exception text and tracebacks are not lifecycle-log payloads.

### Non-goals

- No speaker volume, software boost, limiter or playback-gain change.
- No wake-model tuning or microphone-gain change.
- No groundstation protocol or `reachyctl deploy` redesign.
- No vendoring or patching of the Reachy Mini SDK's stale `ready` or
  `last_alive` status fields.
- No serial retry policy and no unrelated health-registry expansion.
- No live robot interaction and no live identifier or credential in tracked
  files.

## Testing

The regressions are deterministic unit tests built from existing repository
fakes and vendored-wiring test patterns. They open no sockets, touch no
filesystem and do not sleep or wait on wall time.

The focused suite covers:

1. motor enablement followed by controlled wake before normal composition, with
   wake failure and stop requests at achievable SDK-call boundaries preventing
   normal startup;
2. active Home Assistant handoff across overlapping authenticated protocols,
   including out-of-order authentication, non-active and final loss;
3. microphone capture recovery after one transient failure;
4. per-chunk conditioning and forwarding isolation, with local wake detection
   still receiving a chunk whose forwarding failed;
5. listener health, stopped-listener rebind, capped bind retry and intentional
   close;
6. explicit closure of every accepted protocol transport and clearing of active
   connection state;
7. bounded non-returning asynchronous cleanup followed by later cleanup,
   including a child that suppresses every cancellation, nested tasks created by
   repeated `asyncio.shield` calls, and repeated owner cancellation during
   finalization while the process-level runner still returns; and
8. listener retry and per-chunk failures whose exception text contains fake
   installation identifiers, proving lifecycle logs retain only static stage,
   count and retry-delay data.

The mandatory RED commit contains only tests and minimal test support. The
focused command must fail for the intended missing lifecycle behaviour before
any production implementation is added.

## Verification

The RED phase is verified only against the hardware-free focused tests. The
implementation phase will rerun those tests, the HA satellite member tests and
the repository merge gates. Robot verification, if later requested, must be a
separate scrubbed session; none is required or performed by this change's RED
phase.

## Tasks

- [ ] Stabilize the HA satellite runtime lifecycle in one follow-up PR
  - [x] Add deterministic failing regressions for all seven lifecycle defects
        before changing production code
  - [x] Perform the approved controlled wake before normal service composition
  - [x] Preserve and promote surviving authenticated Home Assistant protocols
  - [x] Supervise and rebind an unexpectedly stopped listener with capped retry
  - [x] Keep capture, conditioning, forwarding and local wake detection alive
        across transient per-chunk failures
  - [x] Close accepted protocol transports and clear active connection state
  - [x] Bound each asynchronous cleanup so later cleanup still runs
  - [x] Add identifier-free lifecycle logs for the new transitions
  - [x] Run the focused, member and repository-local automated verification
        required for the implementation phase
  - [ ] Perform scrubbed live robot verification of wake and runtime recovery
  - [ ] Complete the required review channels
  - [ ] Pass the pull request's continuous-integration checks

## References

- Spec: [HA Satellite](../specs/ha-satellite/)
- Dependency: [0013-satellite-behaviour-and-ui](./0013-satellite-behaviour-and-ui.md)
