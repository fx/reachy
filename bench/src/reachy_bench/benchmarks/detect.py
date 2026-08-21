"""`detect`: the face pass, across a sweep of inference thread counts.

The predecessor measured 93 ms at one thread, 51 ms at four and 55 ms at six, so
four was the knee — **on that hardware**. The knee moves with the host, and a
suite that measured only the configured value would never reveal that it had
moved, which is why this reproduces the curve rather than asserting a number.
The groundstation's own default of four threads is written down as "measured
somewhere, not guessed"; this is what re-measures it.

Every thread count gets its own runtime, because the thread bound is a session
option set when the model is opened and cannot be changed afterwards. Each is
warmed before it is timed, so what the distribution describes is the steady
state rather than the first inference's arena allocation.

The measurement is `detect_faces`, which is the whole pass the service runs —
pad, blob, infer, decode, suppress — rather than the inference call alone. The
same function the parity gate drives, for the same reason: what is measured
should be what the service does.

The real work is one function, `_measure_threads`, and it is excluded from
coverage rather than mocked: it opens a model file and runs inference, so a unit
test of it would be a unit test of ONNX Runtime. It is exercised by `just bench`,
which the benchmark workflow runs on every pull request. Everything around it —
the sweep, the knee, the result assembly — takes it as an argument and is tested
against a fake.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from reachy_bench.registry import BenchmarkSpec, Options
from reachy_bench.result import BenchmarkResult, Measurement, Status
from reachy_bench.stats import Distribution
from reachy_bench.timing import measure

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

__all__ = ["DETECT", "build", "knee_of", "sweep"]

NAME: Final = "detect"

# The thresholds the service defaults to, so the sweep measures the work a
# deployment actually does rather than a sensitivity nobody runs.
_SCORE_THRESHOLD: Final = 0.6
_NMS_THRESHOLD: Final = 0.3

# What one thread count's pass produced: how long it took, and how many faces it
# found. The count is carried so a sweep that quietly stopped detecting anything
# — which would be very fast indeed — is visible in the result.
type ThreadOutcome = tuple[Distribution, int]

# How a thread count is measured. An argument so that everything around the real
# inference is exercised without it.
type ThreadMeasure = Callable[[Options, int], ThreadOutcome]


def sweep(
    options: Options,
    measure_threads: ThreadMeasure,
) -> tuple[Measurement, ...]:
    """Walk the thread counts and time the face pass at each.

    Args:
        options: What the run was configured with.
        measure_threads: How to measure one thread count.

    Returns:
        One measurement per thread count, in the order the sweep walked them.
    """
    measurements: list[Measurement] = []
    for threads in options.thread_counts:
        distribution, faces = measure_threads(options, threads)
        measurements.append(
            Measurement.timing(
                f"{NAME}.face.threads.{threads}",
                distribution,
                threads=threads,
                faces=faces,
            ),
        )
    return tuple(measurements)


def knee_of(measurements: Sequence[Measurement]) -> Measurement | None:
    """Find the thread count that was fastest.

    Args:
        measurements: The sweep's measurements.

    Returns:
        The fastest, or `None` when the sweep is empty. Reported as a note
        rather than as a measurement of its own: a knee that moves is
        information about the host, not a regression, and gating on it would
        fail a run for landing on a differently-shaped machine.
    """
    if not measurements:
        return None
    return min(measurements, key=lambda one: one.value)


def _measure_threads(  # pragma: no cover
    options: Options,
    threads: int,
) -> ThreadOutcome:
    """Time the face pass at one thread count.

    Not unit-tested, and excluded from coverage rather than mocked: it opens the
    pinned model and runs real inference, so a unit test of it would be a unit
    test of ONNX Runtime. What exercises it is `just bench`, which the benchmark
    workflow runs on every pull request.

    Args:
        options: What the run was configured with.
        threads: How many threads the runtime may spread one operator across.

    Returns:
        The distribution and the number of faces the pass found.
    """
    from reachy_groundstation.capabilities.perception.face import detect_faces
    from reachy_groundstation.models import FACE_DETECTION_YUNET, ModelStore
    from reachy_groundstation.pipeline.decode import decode_jpeg
    from reachy_groundstation.runtime import CPU_PROVIDER, ModelRuntime, RuntimeOptions

    path = ModelStore(str(options.models_dir)).resolve(FACE_DETECTION_YUNET)
    image = decode_jpeg((options.fixtures / options.frame).read_bytes())
    runtime = ModelRuntime(
        path,
        RuntimeOptions(
            intra_op_threads=threads,
            inter_op_threads=1,
            providers=(CPU_PROVIDER,),
        ),
        f"{FACE_DETECTION_YUNET.name}-{threads}",
    )
    try:
        found = detect_faces(runtime, image, _SCORE_THRESHOLD, _NMS_THRESHOLD)
        distribution = measure(
            lambda: detect_faces(runtime, image, _SCORE_THRESHOLD, _NMS_THRESHOLD),
            iterations=options.iterations,
            warmup=options.warmup,
        )
    finally:
        runtime.close()
    return distribution, len(found)


def _frame_shape(options: Options) -> str:  # pragma: no cover
    """Describe the frame the sweep ran over.

    Excluded from coverage for the same reason as `_measure_threads`: it reads
    the committed fixture off disk, and the file it reads is the one that
    function measures over.

    Args:
        options: What the run was configured with.

    Returns:
        The frame's dimensions as `WxH`, or an empty string when it cannot be
        read — which `_measure_threads` will report properly a moment later.
    """
    from reachy_groundstation.pipeline.decode import decode_jpeg

    try:
        image = decode_jpeg((options.fixtures / options.frame).read_bytes())
    except (OSError, ValueError):
        return ""
    return f"{int(image.shape[1])}x{int(image.shape[0])}"


def build(
    options: Options,
    *,
    measure_threads: ThreadMeasure = _measure_threads,
    describe_frame: Callable[[Options], str] = _frame_shape,
) -> BenchmarkResult:
    """Measure the face pass across the configured thread counts.

    Args:
        options: What the run was configured with.
        measure_threads: How to measure one thread count.
        describe_frame: How to describe the frame being measured over.

    Returns:
        The benchmark's result.
    """
    measurements = sweep(options, measure_threads)
    knee = knee_of(measurements)
    notes: list[str] = []
    if knee is not None:
        notes.append(
            f"the curve's knee on this host is {knee.detail['threads']} "
            f"thread(s) at {knee.value:.1f} ms; the predecessor's was four, on "
            f"different hardware, and the knee is reported rather than gated "
            f"because it is a property of the machine",
        )
        if not knee.detail["faces"]:
            notes.append(
                "no face was detected in the frame, so the pass being timed is "
                "a detector finding nothing rather than the pass a session pays "
                "for",
            )
    return BenchmarkResult(
        benchmark=NAME,
        status=Status.MEASURED,
        configuration={
            "model": "face_detection_yunet",
            "frame": options.frame,
            "frame_size": describe_frame(options),
            "score_threshold": _SCORE_THRESHOLD,
            "nms_threshold": _NMS_THRESHOLD,
            "inter_op_threads": 1,
            "providers": "CPUExecutionProvider",
            "iterations": options.iterations,
            "warmup": options.warmup,
        },
        measurements=measurements,
        notes=tuple(notes),
    )


DETECT: Final = BenchmarkSpec(
    name=NAME,
    summary="The face pass, across a sweep of inference thread counts.",
    requires_hardware=False,
    run=build,
)
