"""The suite's command surface: run, sizes, compare, record, list.

Five verbs, and the split between the first two and `compare` is the whole
gating story.
`run` measures and writes a result document; `compare` reads that document and
the committed baseline and decides. They are separate commands because they run
in different places — a measurement happens where the hardware is, and a
comparison happens wherever the two files are — and because a run that also
decided would make a failing gate indistinguishable from a failing benchmark.

`sizes` is `run`'s narrow twin for REQ-073. An artifact's size is collected by
the change that produces it — the image workflow weighs the variant its matrix
entry built, the release workflow weighs the wheels — and neither of those runs
a benchmark. So `sizes` turns the JSON those recipes already emit into a result
document and gates it against the recorded sizes, without loading a model or
starting a service.

`record` exists so that adopting a new class of machine is a reviewable diff: it
prints the profile block to paste into the committed baseline and writes nothing
itself. That is REQ-071's scenario — accepting a change to the recorded numbers
is an explicit decision made in a review — expressed as a command that cannot
quietly update anything.

Argument parsing is `argparse`. The suite is a workspace member that is never
published and this is the one place a dependency would be added for
convenience's sake alone.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

from reachy_bench.baseline import Baseline, profile_document
from reachy_bench.benchmarks import SUITE
from reachy_bench.benchmarks.footprint import (
    FOOTPRINT,
    read_size_documents,
    size_measurements,
)
from reachy_bench.compare import compare, predecessor_lines
from reachy_bench.context import RunContext, collect_context
from reachy_bench.registry import (
    DEFAULT_FRAME,
    DEFAULT_ITERATIONS,
    DEFAULT_SAMPLE_SECONDS,
    DEFAULT_THREAD_COUNTS,
    DEFAULT_WARMUP,
    Options,
    plan,
    run_selected,
)
from reachy_bench.result import (
    BenchmarkResult,
    Measurement,
    RunResult,
    Status,
    Unit,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["main"]

# What the gate returns when a measurement is below the bar. Distinct from the
# 2 argparse uses for a usage mistake, so a script can tell "the benchmarks
# regressed" from "the command was wrong".
_REGRESSION_EXIT: Final = 1

_DEFAULT_RESULTS: Final = "bench-results.json"


def default_repository(start: Path | None = None) -> Path:
    """Find the repository root by looking for the task surface.

    Args:
        start: Where to start looking. The working directory by default.

    Returns:
        The first directory at or above `start` holding a `Justfile`, or
        `start` itself when there is none — a checkout without one is not this
        repository, and saying so at the first missing fixture is a better
        message than one from here.
    """
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "Justfile").is_file():
            return candidate
    return here


def _thread_counts(text: str) -> tuple[int, ...]:
    """Read a comma-separated thread sweep.

    Args:
        text: What was written on the command line, such as `1,2,4,6,8`.

    Returns:
        The thread counts, in the order given.

    Raises:
        argparse.ArgumentTypeError: If any of them is not a positive whole
            number.
    """
    counts: list[int] = []
    for part in text.split(","):
        stripped = part.strip()
        if not stripped.isdigit() or int(stripped) < 1:
            message = f"{part!r} is not a thread count"
            raise argparse.ArgumentTypeError(message)
        counts.append(int(stripped))
    if not counts:
        message = "a thread sweep names at least one thread count"
        raise argparse.ArgumentTypeError(message)
    return tuple(counts)


def _parser() -> argparse.ArgumentParser:
    """Build the command surface.

    Returns:
        The parser.
    """
    parser = argparse.ArgumentParser(
        prog="reachy_bench",
        description=(
            "Measure this stack, and judge a measurement against the baseline "
            "recorded in the repository."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser(
        "run",
        help="Take measurements and write a result document.",
        description=(
            "Naming a benchmark is what selects it. With no names, everything "
            "that needs no hardware runs and the rest are reported as excluded."
        ),
    )
    run.add_argument(
        "benchmark",
        nargs="*",
        help="Benchmarks to run. Empty means the default selection.",
    )
    run.add_argument("--output", type=Path, default=Path(_DEFAULT_RESULTS))
    run.add_argument("--repository", type=Path, default=None)
    run.add_argument("--models-dir", type=Path, default=None)
    run.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    run.add_argument("--warmup", type=int, default=DEFAULT_WARMUP)
    run.add_argument(
        "--threads",
        type=_thread_counts,
        default=DEFAULT_THREAD_COUNTS,
        help="The inference thread counts the detection sweep walks.",
    )
    run.add_argument("--frame", default=DEFAULT_FRAME)
    run.add_argument(
        "--artifact-size",
        type=Path,
        action="append",
        default=[],
        help=(
            "A JSON document from `just image-size` or `just wheel-size`, or a "
            "directory of them. Repeatable."
        ),
    )
    run.add_argument(
        "--network",
        default="",
        help="How the link behaved, for a run that crosses one.",
    )
    run.add_argument(
        "--profile",
        default="",
        help=(
            "A host-class label to record instead of the derived one. This is "
            "how a continuous integration job names its runner pool."
        ),
    )
    run.add_argument(
        "--observation",
        type=float,
        action="append",
        default=[],
        metavar="MS",
        help="A manually recorded photon-to-head interval, in milliseconds.",
    )
    run.add_argument(
        "--frame-rate",
        type=float,
        default=0.0,
        help=(
            "The frame rate the robot is already tracking at, as you know it "
            "to be. REQUIRED by `robot-load`, which refuses to report anything "
            "while this is left at its default of 0 — it reads the robot's "
            "processors and does not set the robot tracking, so a rate it "
            "defaulted would be a condition nothing established. Ignored by "
            "every other benchmark."
        ),
    )
    run.add_argument("--sample-seconds", type=float, default=DEFAULT_SAMPLE_SECONDS)

    sizes = commands.add_parser(
        "sizes",
        help="Judge artifact sizes against the committed baseline.",
        description=(
            "Reads the JSON `just image-size` and `just wheel-size` emit, "
            "writes a result document carrying nothing but those sizes, and "
            "compares it against the recorded ones. Nothing is built here: a "
            "size is collected from the change that produces the artifact."
        ),
    )
    sizes.add_argument(
        "--artifact-size",
        type=Path,
        action="append",
        default=[],
        required=True,
        help=(
            "A JSON document from `just image-size` or `just wheel-size`, or a "
            "directory of them. Repeatable."
        ),
    )
    sizes.add_argument("--output", type=Path, default=None)
    sizes.add_argument("--baseline", type=Path, default=None)
    sizes.add_argument("--repository", type=Path, default=None)

    compare_command = commands.add_parser(
        "compare",
        help="Judge a result document against the committed baseline.",
    )
    compare_command.add_argument("--results", type=Path, default=Path(_DEFAULT_RESULTS))
    compare_command.add_argument("--baseline", type=Path, default=None)
    compare_command.add_argument("--repository", type=Path, default=None)
    compare_command.add_argument(
        "--require-profile",
        action="store_true",
        help=(
            "Fail when nothing is recorded for this run's class of machine, "
            "rather than reporting it as unbaselined."
        ),
    )

    record = commands.add_parser(
        "record",
        help="Print the baseline profile block that would record a result.",
    )
    record.add_argument("--results", type=Path, default=Path(_DEFAULT_RESULTS))
    record.add_argument(
        "--description",
        default="",
        help="What the machine was, for whoever reviews the recorded numbers.",
    )

    commands.add_parser("list", help="List the benchmarks and what each needs.")
    return parser


def _write(path: Path, text: str) -> None:
    """Write a result document, making its directory if it is not there.

    The directory before the file: `--output` takes any path, and losing a
    whole suite's measurements to a missing parent would be an expensive way to
    learn about a typo.

    Args:
        path: Where to write.
        text: What to write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _options(arguments: argparse.Namespace) -> Options:
    """Read the run's arguments into the options the benchmarks are handed.

    Args:
        arguments: The parsed command line.

    Returns:
        The options.
    """
    repository = arguments.repository or default_repository()
    return Options(
        repository=repository,
        models_dir=arguments.models_dir or (repository / ".models"),
        iterations=arguments.iterations,
        warmup=arguments.warmup,
        thread_counts=tuple(arguments.threads),
        frame=arguments.frame,
        artifact_sizes=tuple(arguments.artifact_size),
        network=arguments.network,
        frame_rate=arguments.frame_rate,
        sample_seconds=arguments.sample_seconds,
        observations_ms=tuple(arguments.observation),
    )


def _figure(measurement: Measurement) -> str:
    """Render one measurement's value for a person reading a log.

    A byte count is written out in full and followed by the mebibytes or
    kibibytes the producing recipe reported, because `4.59e+08 bytes` is a
    number nobody can compare with an image they have seen the size of.

    Args:
        measurement: The measurement.

    Returns:
        The value and its unit.
    """
    if measurement.unit is not Unit.BYTES:
        return f"{measurement.value:.3g} {measurement.unit.value}"
    friendly = measurement.detail.get("size_mib") or measurement.detail.get("size_kib")
    suffix = " MiB" if "size_mib" in measurement.detail else " KiB"
    return f"{measurement.value:,.0f} bytes" + (
        f" ({friendly}{suffix})" if friendly is not None else ""
    )


def _summarise(run: RunResult) -> str:
    """Render a run for somebody reading a log.

    Args:
        run: The run.

    Returns:
        The summary, derived from the same document a program reads — never
        assembled separately, so the two cannot disagree.
    """
    host = run.context.host
    lines = [
        f"host: {host.profile} ({host.cpu_model or 'unknown processor'}, "
        f"{host.cpu_count} cores, {host.memory_mib} MiB)",
        f"commit: {run.context.software.commit or 'unknown'}",
        "",
    ]
    for benchmark in run.benchmarks:
        lines.append(f"{benchmark.benchmark}: {benchmark.status.value}")
        if benchmark.reason:
            lines.append(f"    {benchmark.reason}")
        lines.extend(
            f"    {one.name} = {_figure(one)}"
            + (
                ""
                if one.distribution is None
                else f"  (p95 {one.distribution.p95_ms:.3g} ms, "
                f"n={one.distribution.samples})"
            )
            for one in benchmark.measurements
        )
        lines.extend(f"    note: {note}" for note in benchmark.notes)
        lines.append("")
    return "\n".join(lines)


def _run(arguments: argparse.Namespace, context: RunContext) -> int:
    """Take the measurements and write the result document.

    Args:
        arguments: The parsed command line.
        context: What to record the run as having happened on.

    Returns:
        The exit status: non-zero when a selected benchmark failed, because a
        benchmark that could not measure is a broken run rather than a slow
        one.
    """
    options = _options(arguments)
    try:
        selections = plan(SUITE, arguments.benchmark)
    except ValueError as error:
        sys.stderr.write(f"{error}\n")
        return _REGRESSION_EXIT
    result = run_selected(selections, options, context)
    _write(arguments.output, result.as_json())
    sys.stdout.write(_summarise(result))
    sys.stdout.write(f"results written to {arguments.output}\n")
    failed = [one.benchmark for one in result.benchmarks if one.status is Status.FAILED]
    if failed:
        sys.stderr.write(
            f"{len(failed)} benchmark(s) could not measure: {', '.join(failed)}\n",
        )
        return _REGRESSION_EXIT
    return 0


def _sizes(arguments: argparse.Namespace, context: RunContext) -> int:
    """Judge the artifact sizes a producing workflow measured.

    Args:
        arguments: The parsed command line.
        context: What to record the measurement as having happened on.

    Returns:
        The exit status: zero when nothing grew beyond its tolerance.
    """
    options = Options(
        repository=arguments.repository or default_repository(),
        artifact_sizes=tuple(arguments.artifact_size),
    )
    try:
        documents = read_size_documents(options.artifact_sizes)
        measurements = size_measurements(documents)
        baseline = Baseline.load(_baseline_path(arguments))
    except (OSError, ValueError) as error:
        sys.stderr.write(f"{error}\n")
        return _REGRESSION_EXIT
    run = RunResult(
        context=context,
        benchmarks=(
            BenchmarkResult(
                benchmark=FOOTPRINT.name,
                status=Status.MEASURED,
                configuration={"size_documents": len(documents)},
                measurements=measurements,
            ),
        ),
    )
    if arguments.output is not None:
        _write(arguments.output, run.as_json())
    sys.stdout.write(_summarise(run))
    outcome = compare(run, baseline)
    sys.stdout.write(outcome.report())
    sys.stdout.write("\n")
    return 0 if outcome.ok else _REGRESSION_EXIT


def _baseline_path(arguments: argparse.Namespace) -> Path:
    """Decide where the committed baseline is.

    Args:
        arguments: The parsed command line.

    Returns:
        The path.
    """
    if arguments.baseline is not None:
        path: Path = arguments.baseline
        return path
    repository = arguments.repository or default_repository()
    return repository / "bench" / "baseline.json"


def _compare(arguments: argparse.Namespace) -> int:
    """Judge a result document against the committed baseline.

    Args:
        arguments: The parsed command line.

    Returns:
        The exit status: zero when nothing regressed.
    """
    try:
        run = RunResult.from_json(
            arguments.results.read_text(encoding="utf-8"),
        )
        baseline = Baseline.load(_baseline_path(arguments))
    except (OSError, ValueError) as error:
        sys.stderr.write(f"{error}\n")
        return _REGRESSION_EXIT
    outcome = compare(run, baseline, require_profile=arguments.require_profile)
    sys.stdout.write(outcome.report())
    sys.stdout.write("\n")
    beside = predecessor_lines(run, baseline)
    if beside:
        sys.stdout.write("\nbeside the predecessor's hand-measured figures:\n")
        sys.stdout.write("\n".join(beside))
        sys.stdout.write("\n")
    return 0 if outcome.ok else _REGRESSION_EXIT


def _record(arguments: argparse.Namespace) -> int:
    """Print the baseline profile block that would record a result.

    Args:
        arguments: The parsed command line.

    Returns:
        The exit status.
    """
    try:
        run = RunResult.from_json(arguments.results.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        sys.stderr.write(f"{error}\n")
        return _REGRESSION_EXIT
    host = run.context.host
    description = arguments.description or (
        f"{host.cpu_model or 'unknown processor'}, {host.cpu_count} logical "
        f"processors, {host.memory_mib} MiB, {host.system} {host.release}"
    )
    sys.stdout.write(
        json.dumps(profile_document(run, description=description), indent=2),
    )
    sys.stdout.write("\n")
    sys.stderr.write(
        "\nPaste this into the `profiles` object of bench/baseline.json. "
        "Nothing was written: changing the recorded numbers is a pull request, "
        "so that accepting one is a decision somebody made in a review.\n",
    )
    return 0


def _list() -> int:
    """List the benchmarks and say what each of them needs.

    Returns:
        The exit status.
    """
    for spec in SUITE:
        needs = "needs a robot" if spec.requires_hardware else "no hardware"
        sys.stdout.write(f"{spec.name:16} [{needs:13}] {spec.summary}\n")
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    context: RunContext | None = None,
) -> int:
    """Run the suite's command surface.

    Args:
        argv: The command line, without the program name. The process's own
            when omitted.
        context: What to record a run as having happened on. Collected from
            this machine when omitted; an argument so that a test of this
            command surface does not have to run `git` and read `/proc` to
            exercise it.

    Returns:
        The exit status.
    """
    arguments = _parser().parse_args(argv)
    if arguments.command in {"run", "sizes"}:
        collected = context or collect_context(
            profile=getattr(arguments, "profile", ""),
            network=getattr(arguments, "network", ""),
        )
        if arguments.command == "run":
            return _run(arguments, collected)
        return _sizes(arguments, collected)
    if arguments.command == "compare":
        return _compare(arguments)
    if arguments.command == "record":
        return _record(arguments)
    return _list()
