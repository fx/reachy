"""`footprint`: how much memory the service holds and how large the artifacts are.

Two quantities that are not latency and are gated for two different reasons.

**Resident memory** is what the predecessor's 205 MB figure is about, and the
note beside it — "training framework never imported" — is the whole point: the
number is small because of what is *absent* from the process, and a dependency
that quietly pulls a training stack back in shows up here before it shows up in
an image. It is measured by starting the service as a real subprocess and
reading its resident set once it reports itself ready, because measuring it from
inside this process would measure the benchmark harness too.

**Artifact size** is REQ-073, and the mechanism it uses already exists. Change
0006 and change 0009 both emit one line of JSON with a `size_bytes` field —
`just image-size` and `just wheel-size` — and the release and image workflows
already record those into build artifacts. So this benchmark *consumes* those
documents rather than building anything: sizes are collected from the change
that produces each artifact, which is what the change document asks for and what
stops a size gate quietly measuring a differently-built image.

That is also why a size is compared against a flat `artifacts` set rather than
against a host profile. An image is the same number of bytes whichever machine
weighed it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final

from reachy_bench.registry import BenchmarkSpec, Options
from reachy_bench.result import BenchmarkResult, Measurement, Status, Unit

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

__all__ = [
    "FOOTPRINT",
    "build",
    "read_size_documents",
    "size_measurement",
    "size_measurements",
]

NAME: Final = "footprint"

_KIBIBYTE: Final = 1024


def size_measurement(document: Mapping[str, Any]) -> Measurement:
    """Turn one size document into a measurement.

    The two producing recipes emit deliberately similar shapes — `image-size`
    names an `image` and a `variant`, `wheel-size` names an `artifact` — and
    `size_bytes` is the field a gate compares in both. This reads that one
    field and derives a stable name from the rest, so a wheel whose version
    changed is still the same measurement.

    Args:
        document: A parsed line of JSON from `just image-size` or
            `just wheel-size`.

    Returns:
        The measurement, named `footprint.image.<variant>.<platform>` or
        `footprint.wheel.<artifact>`.

    Raises:
        ValueError: If the document is neither shape, or carries no size. A
            size gate that skipped a document it could not read would report
            nothing and pass.
    """
    try:
        size = int(document["size_bytes"])
    except (KeyError, TypeError, ValueError) as error:
        message = f"no size_bytes in {document!r}"
        raise ValueError(message) from error
    if "variant" in document:
        platform = str(document.get("platform", "unknown")).replace("/", "-")
        return Measurement(
            name=f"{NAME}.image.{document['variant']}.{platform}",
            unit=Unit.BYTES,
            value=float(size),
            detail={
                "image": str(document.get("image", "")),
                "platform": str(document.get("platform", "")),
                "size_mib": round(size / _KIBIBYTE / _KIBIBYTE, 1),
            },
        )
    if "artifact" in document:
        return Measurement(
            name=f"{NAME}.wheel.{document['artifact']}",
            unit=Unit.BYTES,
            value=float(size),
            detail={
                "wheel": str(document.get("wheel", "")),
                "version": str(document.get("version", "")),
                "size_kib": round(size / _KIBIBYTE, 1),
            },
        )
    message = (
        f"a size document names an image variant or a wheel artifact: {document!r}"
    )
    raise ValueError(message)


def size_measurements(
    documents: Sequence[Mapping[str, Any]],
) -> tuple[Measurement, ...]:
    """Turn a set of size documents into measurements.

    Args:
        documents: The parsed documents, in the order they were given.

    Returns:
        One measurement per document.

    Raises:
        ValueError: If any document is not a size document, or if two of them
            describe the same artifact. Two entries for one name would gate on
            whichever came last.
    """
    measurements = [size_measurement(document) for document in documents]
    seen = {measurement.name for measurement in measurements}
    if len(seen) != len(measurements):
        message = "two size documents describe the same artifact"
        raise ValueError(message)
    return tuple(measurements)


def read_size_documents(  # pragma: no cover
    paths: Sequence[Path],
) -> list[Mapping[str, Any]]:
    """Read the size documents a run was pointed at.

    Excluded from coverage: it reads the JSON the producing workflows publish,
    and the parsing it feeds — `size_measurement` — is unit-tested against the
    same shapes.

    A path that is a directory is walked for `*.json`, because that is the
    shape the image and release workflows publish their artifacts in.

    Args:
        paths: Files or directories.

    Returns:
        The parsed documents.

    Raises:
        ValueError: If one of them is not a JSON object.
    """
    documents: list[Mapping[str, Any]] = []
    for path in paths:
        files = sorted(path.glob("*.json")) if path.is_dir() else [path]
        for file in files:
            parsed = json.loads(file.read_text(encoding="utf-8"))
            if not isinstance(parsed, dict):
                message = f"{file} is not a size document"
                raise ValueError(message)
            documents.append(parsed)
    return documents


def _measure_memory(options: Options) -> tuple[int, str]:  # pragma: no cover
    """Start the service and read how much memory it holds once it is ready.

    One line, and it is here rather than inlined so that the import of the
    subprocess machinery happens only when a footprint is actually measured.
    Excluded from coverage: what it delegates to starts a real service.

    Args:
        options: What the run was configured with.

    Returns:
        The resident set in mebibytes, and a description of what was measured.

    Raises:
        RuntimeError: If the service did not become ready, or if this platform
            does not publish a resident set the way Linux does. Both are said
            out loud rather than reported as zero.
    """
    from reachy_bench.memory import resident_memory_of_service

    return resident_memory_of_service(options)


def build(
    options: Options,
    *,
    read_documents: Callable[[Sequence[Path]], list[Mapping[str, Any]]] = (
        read_size_documents
    ),
    measure_memory: Callable[[Options], tuple[int, str]] = _measure_memory,
) -> BenchmarkResult:
    """Measure resident memory and record the sizes of the built artifacts.

    Args:
        options: What the run was configured with.
        read_documents: How to read the size documents.
        measure_memory: How to measure the service's resident set.

    Returns:
        The benchmark's result.
    """
    resident, described = measure_memory(options)
    measurements = [
        Measurement(
            name=f"{NAME}.resident_memory",
            unit=Unit.MEBIBYTES,
            value=float(resident),
            detail={"process": described},
        ),
    ]
    measurements.extend(size_measurements(read_documents(options.artifact_sizes)))
    notes = [
        "the predecessor's 205 MB figure was its robot application's, and the "
        "note beside it — that no training framework was ever imported — is "
        "what the figure is about. What is measured here is the groundstation "
        "service, which is where this architecture puts the model runtime, so "
        "the two are recorded beside each other rather than compared",
    ]
    if not options.artifact_sizes:
        notes.append(
            "no artifact sizes were given, so none are reported: sizes are "
            "collected from the change that produces each artifact — "
            "`just image-size` in the image workflow, `just wheel-size` in the "
            "release workflow — and passed in with --artifact-size",
        )
    return BenchmarkResult(
        benchmark=NAME,
        status=Status.MEASURED,
        configuration={
            "models_dir": str(options.models_dir),
            "size_documents": len(options.artifact_sizes),
        },
        measurements=tuple(measurements),
        notes=tuple(notes),
    )


#:= docs/specs/benchmarks/index.md#req-073-artifact-size-is-measured-as-a-tracked-quantity
#:% The suite MUST record the size of each published artifact and treat growth
#:% beyond a stated tolerance as a regression.
FOOTPRINT: Final = BenchmarkSpec(
    name=NAME,
    summary="Resident memory, and the size of every published artifact.",
    requires_hardware=False,
    run=build,
)
