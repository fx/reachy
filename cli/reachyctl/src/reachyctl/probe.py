"""`probe`: a real session to the groundstation, with no robot involved.

The groundstation lands two phases before the robot application does, and
without something that speaks the session protocol its transport would be
exercised only by its own test suite for that whole time. `probe` is that
something — and unlike a compatibility shim it is worth keeping, because when
face tracking misbehaves the question is whether the groundstation is producing
bad results or the robot is applying good ones badly, and a probe fed a recorded
frame answers it in one command.

What makes the answer worth anything is that `probe` is a second **consumer** of
`reachy_session_client` and not a second implementation of the protocol. It
negotiates, sequences, stamps, supersedes and reconnects exactly as the robot
will, because it is the same code doing it. That is reachyctl REQ-057, and the
moment this module grows its own idea of what a session is, a green probe stops
being evidence about anything.

The run is bounded in three ways, because a diagnostic that hangs is not one.
Frames stop when the source runs out or the requested count is reached; results
stop when every frame sent has been answered by every agreed capability; and the
whole thing stops at `--timeout` regardless. Once the frames have run out, a
quiet period of one staleness window is enough to conclude that the rest are not
coming — which is the same window robot-link REQ-017 makes a consumer stop
acting on results after.
"""

from __future__ import annotations

import asyncio
import contextlib
import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from reachy_contracts import FACE_CAPABILITY, GESTURE_CAPABILITY, Capability
from reachy_session_client import (
    ConnectionFailedError,
    SessionClient,
    SessionClientError,
    open_websocket,
)
from reachyctl.errors import ConfigurationError, UnreachableError
from reachyctl.output import Report

if TYPE_CHECKING:
    from reachy_session_client import (
        Credential,
        FrameResult,
        SessionStats,
        TransportFactory,
    )
    from reachyctl.exits import ExitCode
    from reachyctl.frames import FrameSource
    from reachyctl.output import Reporter

__all__ = [
    "DEFAULT_CAPABILITIES",
    "FrameOutcome",
    "ProbeOutcome",
    "ProbePlan",
    "execute",
    "parse_capability",
    "report_for",
    "run_probe",
    "shortfall",
]

# What `probe` offers when nobody says otherwise: everything the contracts
# package currently declares a payload for, each at version one. Offering both
# is what makes the report say which of them the groundstation actually agreed
# to, which is half of what an operator ran the command to find out.
DEFAULT_CAPABILITIES: Final = (
    Capability(name=FACE_CAPABILITY, version=1),
    Capability(name=GESTURE_CAPABILITY, version=1),
)

_VERSION_SEPARATOR: Final = ":"


@dataclass(frozen=True, slots=True)
class ProbePlan:
    """What one probe run was asked to do.

    Attributes:
        url: Where the groundstation serves its session endpoint.
        capabilities: What to offer during negotiation.
        count: How many frames to send at most.
        interval: How long to wait between frames.
        timeout: The bound on the whole run.
        staleness: How long to keep waiting for results once the frames have
            run out.
    """

    url: str
    capabilities: tuple[Capability, ...]
    count: int
    interval: float
    timeout: float
    staleness: float


@dataclass(frozen=True, slots=True)
class FrameOutcome:
    """One capability's answer to one frame, as the report shows it.

    Attributes:
        sequence: The frame answered.
        capability: Which capability answered it.
        detections: How many things that capability found, legitimately zero.
        round_trip_ms: How long the frame took to go out and come back, on the
            clock that stamped it. `None` when the token was not this run's.
    """

    sequence: int
    capability: str
    detections: int
    round_trip_ms: float | None


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    """What one probe run found.

    Attributes:
        agreed: The capability names both sides settled on.
        frames: One entry per result applied, in arrival order.
        stats: What the session did, including what it dropped and superseded.
        complete: Whether every frame sent was answered by every agreed
            capability before the run stopped.
        complaint: Why it stopped short, when it did.
    """

    agreed: tuple[str, ...]
    frames: tuple[FrameOutcome, ...]
    stats: SessionStats
    complete: bool
    complaint: str = ""


def parse_capability(text: str) -> Capability:
    """Read a capability from `name` or `name:version`.

    Args:
        text: What the operator typed.

    Returns:
        The capability, at version one when no version was given.

    Raises:
        ConfigurationError: If it is not a capability the contract would accept.
            The check happens here so that a typo costs a message rather than a
            session that negotiates to nothing and then times out.
    """
    name, separator, version = text.partition(_VERSION_SEPARATOR)
    try:
        return Capability(
            name=name,
            version=int(version) if separator else 1,
        )
    except ValueError as error:
        message = (
            f"{text!r} is not a capability: expected a lowercase name, "
            f"optionally followed by {_VERSION_SEPARATOR} and a whole version "
            f"number ({error.__class__.__name__})"
        )
        raise ConfigurationError(message) from error


async def _produce(
    client: SessionClient,
    source: FrameSource,
    plan: ProbePlan,
) -> int:
    """Feed the session frames until the source or the count runs out.

    Args:
        client: The connected client.
        source: Where the frames come from.
        plan: What the run was asked to do.

    Returns:
        How many frames actually went out. Frames the client dropped because no
        session was up are not counted, so the results expected of the
        groundstation are the results it was actually given a chance to produce.
    """
    sent = 0
    async for payload in source.frames():
        if await client.submit_frame(payload) is not None:
            sent += 1
            if sent >= plan.count:
                break
        # The wait is between frames rather than after the last one: a
        # diagnostic that idled for one more interval before reporting would be
        # slower than the thing it is measuring.
        if plan.interval > 0:
            await asyncio.sleep(plan.interval)
    return sent


def _outcome(result: FrameResult) -> FrameOutcome:
    """Turn a result into the row the report shows.

    Args:
        result: What came back.

    Returns:
        The row.
    """
    return FrameOutcome(
        sequence=result.sequence,
        capability=result.capability,
        detections=result.detections,
        round_trip_ms=(
            None
            if result.round_trip_seconds is None
            else result.round_trip_seconds * 1000.0
        ),
    )


#:= docs/specs/reachyctl/index.md#req-057-the-probe-exercises-the-real-session-protocol
#:% The probe command MUST establish a session using the same protocol
#:% implementation the robot application uses.
async def run_probe(
    plan: ProbePlan,
    source: FrameSource,
    credential: Credential,
    reporter: Reporter,
    open_transport: TransportFactory = open_websocket,
) -> ProbeOutcome:
    """Open a session, feed it frames, and report what came back.

    Args:
        plan: What the run was asked to do.
        source: Where the frames come from.
        credential: What to present to the groundstation.
        reporter: Where the per-frame detail goes while the run is happening.
        open_transport: How to open the connection. Injected only so that the
            integration test can watch which connections were opened; the
            transport itself is always the real one.

    Returns:
        What the run found.

    Raises:
        SessionClientError: If the session could not be established or was
            broken by something retrying will not fix. The caller turns that
            into an exit status.
    """
    client = SessionClient(
        url=plan.url,
        credential=credential,
        capabilities=plan.capabilities,
        open_transport=open_transport,
        staleness_seconds=plan.staleness,
    )
    async with client:
        agreement = client.agreement
        agreed = (
            ()
            if agreement is None
            else tuple(named.name for named in agreement.capabilities)
        )
        reporter.detail(f"session established at {plan.url}")
        reporter.detail(f"agreed capabilities: {', '.join(agreed) or 'none'}")
        if not agreed:
            return ProbeOutcome(
                agreed=(),
                frames=(),
                stats=client.stats,
                complete=False,
                complaint=(
                    "the groundstation agreed to none of the capabilities "
                    "offered, so nothing would answer a frame"
                ),
            )
        return await _exchange(client, source, plan, reporter, agreed)


async def _exchange(
    client: SessionClient,
    source: FrameSource,
    plan: ProbePlan,
    reporter: Reporter,
    agreed: tuple[str, ...],
) -> ProbeOutcome:
    """Send the frames and collect the answers, within the run's bounds.

    Args:
        client: The connected client.
        source: Where the frames come from.
        plan: What the run was asked to do.
        reporter: Where the per-frame detail goes.
        agreed: The capability names both sides settled on.

    Returns:
        What the run found.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + plan.timeout
    collected: list[FrameOutcome] = []
    expected = plan.count * len(agreed)
    producer = asyncio.create_task(_produce(client, source, plan), name="frames")
    results = client.results()
    # The pending `anext`, held across iterations rather than started and
    # abandoned inside each one. Cancelling an `anext` closes the asynchronous
    # generator it was taken from, so a wait this loop gives up on has to be
    # the *loop's* last, and a wait it merely stops waiting on has to survive
    # to be awaited again.
    pending: asyncio.Task[FrameResult] | None = None
    try:
        while True:
            if producer.done():
                # Now that the frames have run out, what the groundstation owes
                # is one answer per capability per frame that actually went out
                # — which is fewer than was asked for when the source was
                # shorter than the count, and when the link dropped frames.
                expected = producer.result() * len(agreed)
            if len(collected) >= expected:
                break
            if pending is None:
                pending = asyncio.ensure_future(anext(results))
            # What is left of the run's overall bound, narrowed to one
            # staleness window once the frames have stopped: results that have
            # not arrived by then are not coming. Clamped at zero rather than
            # guarded with a branch — a zero timeout asks "is it ready now?",
            # which is exactly the question at that point.
            budget = deadline - loop.time()
            if producer.done():
                budget = min(budget, plan.staleness)
            # The producer is waited on beside the result, so that it finishing
            # *during* a wait shortens that wait instead of being noticed only
            # after it. Without this, a run whose last result never arrives
            # sits out the whole `--timeout` — thirty seconds by default —
            # having entered the wait with the frames still flowing and a full
            # budget. One staleness window is the answer this loop is supposed
            # to give, and a diagnostic that takes thirty seconds to give it is
            # the thing the run's bounds exist to prevent.
            waiters: list[asyncio.Task[Any]] = [pending]
            if not producer.done():
                waiters.append(producer)
            settled, _still_waiting = await asyncio.wait(
                waiters,
                timeout=max(0.0, budget),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not settled:
                # Nothing completed, so the wait ran out its budget: one
                # staleness window if the frames have stopped, what was left of
                # the run's overall bound if they have not.
                break
            if pending not in settled:
                # The producer finished and the result has not arrived yet. Go
                # round rather than keep waiting on the budget chosen before it
                # did: `expected` narrows to what actually went out, and the
                # budget narrows to one staleness window. The pending `anext`
                # is carried over untouched, because cancelling it would close
                # the generator it came from.
                continue
            try:
                result = pending.result()
            except StopAsyncIteration:  # pragma: no cover
                # `results()` returns only once the client has been closed, and
                # `run_probe` closes it after this loop rather than during it,
                # so nothing here can reach this today. It is handled anyway
                # because the alternative is this loop raising
                # `StopAsyncIteration` out of a command, and because the second
                # consumer of this client — the robot adapter in change 0012 —
                # closes on a different schedule than `probe` does.
                break
            finally:
                pending = None
            collected.append(_outcome(result))
            reporter.detail(
                f"frame {result.sequence} answered by {result.capability}: "
                f"{result.detections} detection(s)",
            )
    finally:
        if pending is not None:
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending
        producer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await producer
        await results.aclose()

    complete = len(collected) >= expected and expected > 0
    return ProbeOutcome(
        agreed=agreed,
        frames=tuple(collected),
        stats=client.stats,
        complete=complete,
        complaint="" if complete else shortfall(len(collected), expected),
    )


def shortfall(applied: int, expected: int) -> str:
    """Say what was missing when a run stopped short.

    Args:
        applied: How many results arrived.
        expected: How many were owed.

    Returns:
        One line naming the shortfall.
    """
    if expected == 0:
        return "no frame reached the groundstation"
    return f"{applied} of {expected} expected results arrived before the run stopped"


def _timings(frames: tuple[FrameOutcome, ...]) -> dict[str, object]:
    """Summarise the round trips that were measurable.

    Args:
        frames: The results collected.

    Returns:
        The fastest, the median and the slowest in milliseconds, or empty
        placeholders when nothing carried a measurable timing.
    """
    measured = [
        frame.round_trip_ms for frame in frames if frame.round_trip_ms is not None
    ]
    if not measured:
        return {
            "round_trip_ms_fastest": None,
            "round_trip_ms_median": None,
            "round_trip_ms_slowest": None,
        }
    return {
        "round_trip_ms_fastest": min(measured),
        "round_trip_ms_median": statistics.median(measured),
        "round_trip_ms_slowest": max(measured),
    }


#:= docs/specs/reachyctl/index.md#req-058-output-is-machine-readable-on-request
#:% Every command that reports results MUST offer a structured output format
#:% suitable for consumption by another program.
def report_for(outcome: ProbeOutcome, plan: ProbePlan, description: str) -> Report:
    """Shape what the run found into the thing every rendering is built from.

    The command builds one report and never learns which format was asked for,
    which is what keeps the structured output and the human one from carrying
    different fields.

    Args:
        outcome: What the run found.
        plan: What it was asked to do.
        description: Where the frames came from.

    Returns:
        The report to emit.
    """
    stats = outcome.stats
    data: dict[str, object] = {
        "url": plan.url,
        "source": description,
        "offered": tuple(named.name for named in plan.capabilities),
        "agreed": outcome.agreed,
        "frames_submitted": stats.frames_submitted,
        "frames_dropped": stats.frames_dropped,
        "results_applied": stats.results_applied,
        "results_superseded": stats.results_superseded,
        "results_ignored": stats.results_ignored,
        "errors_received": stats.errors_received,
        "reconnections": stats.reconnections,
    }
    data.update(_timings(outcome.frames))
    return Report(
        command="probe",
        ok=outcome.complete,
        summary=outcome.complaint
        or f"{len(outcome.frames)} result(s) over one session",
        data=data,
        columns=("sequence", "capability", "detections", "round_trip_ms"),
        rows=tuple(
            {
                "sequence": frame.sequence,
                "capability": frame.capability,
                "detections": frame.detections,
                "round_trip_ms": frame.round_trip_ms,
            }
            for frame in outcome.frames
        ),
    )


def execute(
    plan: ProbePlan,
    source: FrameSource,
    credential: Credential,
    reporter: Reporter,
    open_transport: TransportFactory = open_websocket,
) -> ExitCode:
    """Run a probe and report it, turning every failure into an exit status.

    Args:
        plan: What the run was asked to do.
        source: Where the frames come from.
        credential: What to present to the groundstation.
        reporter: Where everything is written.
        open_transport: How to open the connection.

    Returns:
        The exit status.

    Raises:
        UnreachableError: If the session could not be established or was
            broken. The message is whatever the client said, which by
            construction never quotes a credential — the one message that
            carries one holds it in a type that will not render it, and the
            client's own tests pin that. It is scrubbed again on the way out
            regardless.
    """
    reporter.detail(f"probing {plan.url} with {source.description}")
    try:
        outcome = asyncio.run(
            run_probe(plan, source, credential, reporter, open_transport),
        )
    except ConnectionFailedError as error:
        raise UnreachableError(str(error)) from error
    except SessionClientError as error:
        raise UnreachableError(f"{type(error).__name__}: {error}") from error
    finally:
        source.close()
    return reporter.emit(report_for(outcome, plan, source.description))
