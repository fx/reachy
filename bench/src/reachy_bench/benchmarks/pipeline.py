"""`pipeline`: every stage of answering a frame, timed on its own.

Benchmarks REQ-070 asks that the suite report each stage individually **in
addition to** an end-to-end figure, and the scenario is the one that matters: an
end-to-end number that has worsened should name the stage responsible without a
second instrumented run. So this reports four numbers where a lesser suite would
report one.

**Why not read the service's own per-stage timings.** The groundstation already
instruments every stage — `groundstation_stage_seconds` and
`groundstation_capability_seconds`, groundstation REQ-029 — and those are
Prometheus histograms with fixed buckets. A histogram answers "how many passes
fell between 25 and 50 ms"; REQ-069 asks for a median and a 95th percentile, and
deriving those from eleven buckets would be an interpolation reported as a
measurement. So the stages are timed here, against the same callables the
pipeline invokes, and the service's histograms stay what they are: an operator's
view of a running deployment.

**The end-to-end figure drives the real `FramePipeline`.** It is measured
separately from the stages rather than derived from them, so the two are two
measurements of overlapping work and not an identity: the difference between
them is the composition's own cost — the span bookkeeping, the metric
observations, the delivery callback — plus whatever the event loop does
differently when it has one thing to wait on rather than several. It can fall
either side of the sum, and the result carries both numbers so a reader can see
which.

**Gesture is a stage with nothing behind it, and this says so rather than timing
it.** No gesture model clears this repository's licence bar, which is change
0005's recorded decision, so the capability answers every frame with an empty
payload in microseconds. Reporting that beside the predecessor's 5 ms would
claim a three-order improvement that is really an absent model, so no gesture
timing is emitted at all and the result carries a note saying why.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final

from reachy_bench.registry import BenchmarkSpec, Options
from reachy_bench.result import BenchmarkResult, Measurement, Status
from reachy_bench.timing import measure, measure_async

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from reachy_bench.result import Detail
    from reachy_bench.stats import Distribution

__all__ = ["PIPELINE", "Stages", "build"]

NAME: Final = "pipeline"

# The note that keeps an absent model from reading as a fast one.
_GESTURE_NOTE: Final = (
    "no gesture timing is reported: this build wires no gesture model, which is "
    "the perception spec's recorded decision, so the capability answers every "
    "frame with an empty payload. Timing that and putting it beside the "
    "predecessor's 5 ms would report an absent model as an improvement"
)


class Stages(dict[str, "Distribution"]):
    """The per-stage distributions, keyed by the stage's measurement suffix.

    A named type rather than a bare mapping so the seam below has something to
    say in its signature about what a stage measurement is: `decode`,
    `capability.face`, `emit` and `end_to_end`. The first three are timed
    against the same callables the pipeline invokes and the fourth against the
    pipeline itself, so they are four measurements of overlapping work rather
    than a decomposition of one.
    """


# How the stages are measured. An argument so the result assembly around it is
# exercised without opening a model.
type StageMeasure = Callable[[Options], tuple[Stages, Mapping[str, Detail]]]


def _measure_stages(  # pragma: no cover
    options: Options,
) -> tuple[Stages, Mapping[str, Detail]]:
    """Time each stage of answering one frame, and the whole of answering it.

    Not unit-tested, and excluded from coverage rather than mocked: it opens the
    pinned model, warms it and drives the real pipeline, so a unit test of it
    would be a unit test of the groundstation. What exercises it is `just
    bench`, which the benchmark workflow runs on every pull request.

    Args:
        options: What the run was configured with.

    Returns:
        The distributions by stage, and the configuration they were taken
        under.
    """
    from reachy_contracts import FACE_CAPABILITY, FrameHeader, ResultEnvelope
    from reachy_groundstation.capabilities.perception.face import FaceCapability
    from reachy_groundstation.config import load_settings
    from reachy_groundstation.obs import build_observability
    from reachy_groundstation.pipeline.decode import decode_jpeg
    from reachy_groundstation.pipeline.queue import QueuedFrame
    from reachy_groundstation.pipeline.runner import FramePipeline
    from reachy_groundstation.ports import AgreedCapability, DecodedFrame
    from reachy_groundstation.session.framing import MessageKind, encode_control

    payload = (options.fixtures / options.frame).read_bytes()
    # Through the service's own loader rather than by building the model here:
    # it is a pure function of the mapping it is handed, and it is the one path
    # the service itself uses. The credential is not a secret — the model
    # requires one and nothing in this benchmark opens a session. See the root
    # AGENTS.md on what may enter a tracked file.
    settings = load_settings(
        {
            "REACHY_GROUNDSTATION_CREDENTIAL": "benchmark-placeholder",
            "REACHY_GROUNDSTATION_MODELS_DIR": str(options.models_dir),
        },
    )
    image = decode_jpeg(payload)
    header = FrameHeader(sequence=0, captured_at="0.0")
    decoded = DecodedFrame(header=header, image=image)
    obs = build_observability(settings)
    face = FaceCapability(settings)

    async def _run() -> Stages:
        await face.warm_up()
        stages = Stages()
        try:
            stages["decode"] = measure(
                lambda: decode_jpeg(payload),
                iterations=options.iterations,
                warmup=options.warmup,
            )
            stages["capability.face"] = await measure_async(
                lambda: face.process(decoded),
                iterations=options.iterations,
                warmup=options.warmup,
            )
            answer = await face.process(decoded)
            stages["emit"] = measure(
                lambda: encode_control(
                    MessageKind.RESULT,
                    ResultEnvelope.for_frame(header, FACE_CAPABILITY, answer),
                ),
                iterations=options.iterations,
                warmup=options.warmup,
            )

            async def _deliver(kind: MessageKind, message: object) -> None:
                """Take delivery of a finished message and do nothing with it.

                The session layer owns the transport, and what the transport
                costs is `session`'s question rather than this one.

                Args:
                    kind: Which kind of message it is.
                    message: The message.
                """
                del kind, message

            pipeline = FramePipeline(
                capabilities=(AgreedCapability(name=FACE_CAPABILITY, capability=face),),
                deliver=_deliver,
                settings=settings,
                obs=obs,
                session_id="benchmark",
            )
            stages["end_to_end"] = await measure_async(
                lambda: pipeline.process(
                    QueuedFrame(header=header, payload=payload, received_at=0.0),
                ),
                iterations=options.iterations,
                warmup=options.warmup,
            )
        finally:
            await face.aclose()
        return stages

    stages = asyncio.run(_run())
    configuration: Mapping[str, Detail] = {
        "model": "face_detection_yunet",
        "frame": options.frame,
        "frame_size": f"{int(image.shape[1])}x{int(image.shape[0])}",
        "capabilities": FACE_CAPABILITY,
        "intra_op_threads": settings.inference_intra_op_threads,
        "inter_op_threads": settings.inference_inter_op_threads,
        "iterations": options.iterations,
        "warmup": options.warmup,
    }
    return stages, configuration


#:= docs/specs/benchmarks/index.md#req-070-stages-are-measured-separately
#:% The suite MUST report the duration of each pipeline stage individually in
#:% addition to any end-to-end measurement.
def build(
    options: Options,
    *,
    measure_stages: StageMeasure = _measure_stages,
) -> BenchmarkResult:
    """Report every stage of the pipeline, and the whole of it.

    Args:
        options: What the run was configured with.
        measure_stages: How to time the stages.

    Returns:
        The benchmark's result.
    """
    stages, configuration = measure_stages(options)
    measurements = tuple(
        Measurement.timing(f"{NAME}.{stage}", distribution, stage=stage)
        for stage, distribution in stages.items()
    )
    notes = [_GESTURE_NOTE]
    end_to_end = stages.get("end_to_end")
    parts = sum(
        distribution.median_ms
        for stage, distribution in stages.items()
        if stage != "end_to_end"
    )
    if end_to_end is not None and parts:
        notes.append(
            f"the stages sum to {parts:.2f} ms and the pipeline answers a frame "
            f"in {end_to_end.median_ms:.2f} ms. The two are measured separately "
            f"— each stage on its own, and the whole through the real "
            f"FramePipeline — so the difference is the composition's own cost "
            f"(spans, metric observations, the delivery callback) plus whatever "
            f"the event loop does differently when it has one thing to wait on "
            f"rather than several. It is worth reading, and it is not a fifth "
            f"stage",
        )
    return BenchmarkResult(
        benchmark=NAME,
        status=Status.MEASURED,
        configuration=dict(configuration),
        measurements=measurements,
        notes=tuple(notes),
    )


PIPELINE: Final = BenchmarkSpec(
    name=NAME,
    summary="Decode, detection and result emission, per stage and end to end.",
    requires_hardware=False,
    run=build,
)
