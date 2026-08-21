"""The result document: what a run measured, in a shape a program can read.

Benchmarks REQ-067 asks that a comparison tool be able to read two runs without
screen-scraping, so a run's whole output is one JSON document and the
human-readable summary is derived from it rather than the other way round.

Three shapes, nested: a `Measurement` is one number with the distribution behind
it, a `BenchmarkResult` is one benchmark's measurements plus the configuration
they were produced under, and a `RunResult` is every benchmark plus the context
the whole run happened in. Reading a result file therefore answers "how fast"
and "under what conditions" together, which is what makes REQ-068's scenario —
telling a genuine improvement from a changed measurement condition — answerable
from the file alone.

**A benchmark that did not run still appears.** REQ-072 requires the hardware
benchmarks be excluded from the default selection *and* reported as excluded,
which is a different thing from being absent: a suite that simply omitted them
would look identical to a suite that had lost them. So `Status` has three
values, and every selected benchmark contributes exactly one result whichever
of them it earns.

**Every quantity here is one where less is better** — milliseconds, bytes,
cores, mebibytes of resident memory. The comparison in `reachy_bench.compare`
relies on that and says so; a measurement where more is better would need a
direction beside it, and there is not one today.

Nothing in this module performs input or output. Writing the document to a file
is the caller's business, which is what keeps its tests free of both.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Self

from reachy_bench.context import HostContext, RunContext, SoftwareContext
from reachy_bench.stats import Distribution, finite

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping

__all__ = [
    "SCHEMA_VERSION",
    "BenchmarkResult",
    "Detail",
    "Measurement",
    "RunResult",
    "Status",
    "Unit",
]

# The document's shape, so a reader that meets a later one can say so rather
# than misreading it. It changes when a field is removed or repurposed, not when
# one is added.
SCHEMA_VERSION: Final = 1

# What a measurement may carry beside its number: the thread count it ran at,
# the model it ran, whether a stage had anything behind it. Scalars only,
# because this is data a gate and a reviewer read rather than a nested record.
type Detail = str | int | float | bool


class Status(StrEnum):
    """How a selected benchmark ended.

    Attributes:
        MEASURED: It ran and produced measurements.
        EXCLUDED: It was not run, deliberately — the default selection leaves
            out anything needing a physical robot. This is REQ-072's "without
            reporting a skip as a failure", and it is why an excluded benchmark
            appears in the document rather than being missing from it.
        FAILED: It was selected, it ran, and it could not produce a measurement.
    """

    MEASURED = "measured"
    EXCLUDED = "excluded"
    FAILED = "failed"


class Unit(StrEnum):
    """What a measurement is counted in.

    Attributes:
        MILLISECONDS: A duration. Every one of them carries a distribution.
        BYTES: An artifact's size on disk, as the producing recipe reports it.
        MEBIBYTES: Resident memory.
        CORES: Processor time per unit of wall time, so 1.52 of four cores is
            the predecessor's recorded figure spelled the same way.
    """

    MILLISECONDS = "ms"
    BYTES = "bytes"
    MEBIBYTES = "MiB"
    CORES = "cores"


@dataclass(frozen=True, slots=True, kw_only=True)
class Measurement:
    """One number, with what is known about how it was arrived at.

    Attributes:
        name: What is being measured, dotted and stable across runs. This is
            the key a baseline is written against, so renaming one is a
            deliberate change to the baseline in the same pull request.
        unit: What the number is counted in.
        value: The figure a comparison reads. For a timing this is the median,
            never the mean — the mean is in the distribution beside it.
        distribution: Every statistic behind the value, for a timing. `None`
            for a size or a memory figure, which are read once rather than
            sampled.
        detail: Scalars that qualify the number without being comparable to
            anything — the thread count, the model, the frame size.
    """

    name: str
    unit: Unit
    value: float
    distribution: Distribution | None = None
    detail: Mapping[str, Detail] = field(default_factory=dict)

    @classmethod
    def timing(
        cls,
        name: str,
        distribution: Distribution,
        **detail: Detail,
    ) -> Self:
        """Build a timing whose headline figure is its median.

        Deriving the value here rather than at each call site is what keeps
        "the gate compares medians" true of every timing in the suite instead of
        true of the ones somebody remembered.

        Args:
            name: What is being measured.
            distribution: The observations.
            detail: Scalars that qualify the number.

        Returns:
            The measurement.
        """
        return cls(
            name=name,
            unit=Unit.MILLISECONDS,
            value=distribution.median_ms,
            distribution=distribution,
            detail=dict(detail),
        )

    def as_document(self) -> dict[str, Any]:
        """Render the measurement for the result document.

        Returns:
            A JSON-serialisable mapping.
        """
        document: dict[str, Any] = {
            "name": self.name,
            "unit": self.unit.value,
            "value": round(self.value, 3),
        }
        if self.distribution is not None:
            document["distribution"] = self.distribution.as_document()
        if self.detail:
            document["detail"] = dict(self.detail)
        return document

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> Self:
        """Read a measurement back out of a result document.

        Args:
            document: One entry from a document's `measurements` array.

        Returns:
            The measurement.

        Raises:
            ValueError: If the entry is not one — a missing name, a unit this
                build does not know. A comparison that guessed past a malformed
                result would report on numbers it had invented.
        """
        try:
            spread = document.get("distribution")
            return cls(
                name=str(document["name"]),
                unit=Unit(document["unit"]),
                value=finite(document["value"]),
                distribution=(
                    None
                    if spread is None
                    else Distribution(
                        samples=int(spread["samples"]),
                        min_ms=finite(spread["min_ms"]),
                        median_ms=finite(spread["median_ms"]),
                        p95_ms=finite(spread["p95_ms"]),
                        max_ms=finite(spread["max_ms"]),
                        mean_ms=finite(spread["mean_ms"]),
                        stdev_ms=finite(spread["stdev_ms"]),
                    )
                ),
                detail=dict(document.get("detail", {})),
            )
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            # Every way a half-written document can fail, reported as the one
            # thing every caller documents. A truncated distribution block and a
            # `unit` this build has never heard of are the same event to a
            # comparison: a result it cannot read, which must not become a
            # traceback out of a command.
            message = f"not a measurement: {document!r}"
            raise ValueError(message) from error


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchmarkResult:
    """One benchmark's contribution to a run.

    Attributes:
        benchmark: The benchmark's name, as the suite selects it by.
        status: Whether it measured, was excluded, or failed.
        configuration: What it was configured with — the half of REQ-068 that
            differs between two benchmarks in the same run, so it lives here
            rather than on the run.
        measurements: What it measured. Empty unless the status is `MEASURED`.
        notes: Things a reader needs in order not to misread the numbers. A
            stage with no model behind it says so here rather than reporting a
            figure that would flatter the build.
        reason: Why it was excluded or why it failed. Empty otherwise.
    """

    benchmark: str
    status: Status
    configuration: Mapping[str, Detail] = field(default_factory=dict)
    measurements: tuple[Measurement, ...] = ()
    notes: tuple[str, ...] = ()
    reason: str = ""

    @classmethod
    def excluded(cls, benchmark: str, reason: str) -> Self:
        """Report a benchmark that was deliberately not run.

        Args:
            benchmark: Its name.
            reason: Why it was left out, in terms an operator can act on.

        Returns:
            The result.
        """
        return cls(benchmark=benchmark, status=Status.EXCLUDED, reason=reason)

    @classmethod
    def failed(cls, benchmark: str, reason: str) -> Self:
        """Report a benchmark that was selected and could not measure.

        Args:
            benchmark: Its name.
            reason: What went wrong.

        Returns:
            The result.
        """
        return cls(benchmark=benchmark, status=Status.FAILED, reason=reason)

    def as_document(self) -> dict[str, Any]:
        """Render the benchmark's result for the result document.

        Returns:
            A JSON-serialisable mapping.
        """
        return {
            "benchmark": self.benchmark,
            "status": self.status.value,
            "configuration": dict(self.configuration),
            "measurements": [one.as_document() for one in self.measurements],
            "notes": list(self.notes),
            "reason": self.reason,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> Self:
        """Read one benchmark's result back out of a result document.

        Args:
            document: One entry from a document's `benchmarks` array.

        Returns:
            The result.

        Raises:
            ValueError: If the entry is not one.
        """
        try:
            return cls(
                benchmark=str(document["benchmark"]),
                status=Status(document["status"]),
                configuration=dict(document.get("configuration", {})),
                measurements=tuple(
                    Measurement.from_document(one)
                    for one in document.get("measurements", ())
                ),
                notes=tuple(str(note) for note in document.get("notes", ())),
                reason=str(document.get("reason", "")),
            )
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            # Inside the guard, so a malformed measurement or a `configuration`
            # that is not a mapping reaches a caller as the ValueError every
            # reader of a result document is documented to raise.
            message = f"not a benchmark result: {document!r}"
            raise ValueError(message) from error


#:= docs/specs/benchmarks/index.md#req-067-results-are-structured-and-machine-readable
#:% Every benchmark run MUST emit its results in a structured format that another
#:% program can consume without parsing human-facing output.
@dataclass(frozen=True, slots=True, kw_only=True)
class RunResult:
    """Everything one invocation of the suite produced.

    Attributes:
        context: The machine, the versions and the moment.
        benchmarks: One entry per selected benchmark, whatever became of it.
    """

    context: RunContext
    benchmarks: tuple[BenchmarkResult, ...]

    def measurements(self) -> Iterator[tuple[BenchmarkResult, Measurement]]:
        """Walk every measurement in the run, with the benchmark that took it.

        Yields:
            Each measurement, paired with its benchmark's result so a consumer
            can read the configuration it was taken under.
        """
        for benchmark in self.benchmarks:
            for measurement in benchmark.measurements:
                yield benchmark, measurement

    def by_name(self) -> dict[str, Measurement]:
        """Index every measurement by its name.

        Returns:
            Measurement name to measurement.

        Raises:
            ValueError: If two benchmarks measured the same name. A comparison
                against a baseline is keyed on the name, so a duplicate would
                silently gate on whichever of the two came last.
        """
        indexed: dict[str, Measurement] = {}
        for _benchmark, measurement in self.measurements():
            if measurement.name in indexed:
                message = f"two benchmarks both measured {measurement.name!r}"
                raise ValueError(message)
            indexed[measurement.name] = measurement
        return indexed

    def statuses(self) -> dict[str, Status]:
        """Report what became of each selected benchmark.

        Returns:
            Benchmark name to status.
        """
        return {one.benchmark: one.status for one in self.benchmarks}

    def as_document(self) -> dict[str, Any]:
        """Render the whole run.

        Returns:
            A JSON-serialisable mapping.
        """
        return {
            "schema": SCHEMA_VERSION,
            "context": self.context.as_document(),
            "benchmarks": [one.as_document() for one in self.benchmarks],
        }

    def as_json(self) -> str:
        """Render the whole run as JSON text.

        Returns:
            The document, indented and with a trailing newline, so that two
            runs of the suite produce files a reviewer can diff line by line.
        """
        return json.dumps(self.as_document(), indent=2, sort_keys=False) + "\n"

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> Self:
        """Read a run back out of its document.

        Args:
            document: A parsed result document.

        Returns:
            The run.

        Raises:
            ValueError: If the document is not one this build can read — a
                missing section, or a schema from a later version. Reading past
                either would compare numbers whose meaning had changed.
        """
        schema = document.get("schema")
        if schema != SCHEMA_VERSION:
            message = (
                f"result document schema {schema!r} is not the {SCHEMA_VERSION} "
                f"this build reads"
            )
            raise ValueError(message)
        try:
            context = document["context"]
            host = context["host"]
            software = context["software"]
            benchmarks: Iterable[Mapping[str, Any]] = document["benchmarks"]
            # Inside the guard, not after it. A host block missing a field, or
            # a context that is a string rather than a mapping, is the same
            # event as a missing section — a document this build cannot read —
            # and both must reach a caller as the ValueError it handles.
            recorded = RunContext(
                host=HostContext(
                    profile=str(host["profile"]),
                    system=str(host["system"]),
                    release=str(host["release"]),
                    machine=str(host["machine"]),
                    cpu_model=str(host["cpu_model"]),
                    cpu_count=int(host["cpu_count"]),
                    memory_mib=int(host["memory_mib"]),
                ),
                software=SoftwareContext(
                    python=str(software["python"]),
                    commit=str(software["commit"]),
                    versions=dict(software["versions"]),
                ),
                started_at=str(context["started_at"]),
                network=str(context.get("network", "")),
            )
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            message = "a result document carries a context and a benchmark list"
            raise ValueError(message) from error
        return cls(
            context=recorded,
            benchmarks=tuple(BenchmarkResult.from_document(one) for one in benchmarks),
        )

    @classmethod
    def from_json(cls, text: str) -> Self:
        """Read a run back out of JSON text.

        Args:
            text: The document as it was written.

        Returns:
            The run.

        Raises:
            ValueError: If the text is not a result document.
        """
        try:
            document = json.loads(text)
        except json.JSONDecodeError as error:
            message = f"a result document is JSON: {error}"
            raise ValueError(message) from error
        if not isinstance(document, dict):
            message = "a result document is a JSON object"
            raise ValueError(message)
        return cls.from_document(document)
