"""`robot-load`: how much of the robot's CPU tracking actually costs.

The predecessor ran detection on the robot and took 1.52 of four cores at 10
frames per second — after the detection was offloaded. That figure is what the
whole offloading argument is made of, and it is about a four-core aarch64 device
that is also running motion control and audio, so anything measured anywhere
else is not this measurement.

**How it is measured.** The robot publishes cumulative processor time in
`/proc/stat`; two samples an interval apart give the busy time in that interval,
and dividing by the interval gives cores. Sampling twice and subtracting is the
only honest way to read it — the file's first line is time since boot, so a
single sample reports the machine's whole history rather than what tracking
costs now.

**What talks to the robot is an argument.** This benchmark is handed something
that runs a command there and hands back its output, which is what
`reachyctl bench` supplies from the SSH access it already has. The parsing and
the arithmetic are ordinary pure functions with ordinary tests, so what is not
tested here is the transport, which reachyctl tests already own.

It needs a robot, so it is not in the default selection: a run that does not
name it reports it as excluded and nothing here executes. Selected with no way
to reach a robot, it fails saying so rather than reporting the machine it
happens to be running on — which would be a groundstation's CPU labelled as a
robot's.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Final

from reachy_bench.registry import BenchmarkSpec, Options
from reachy_bench.result import BenchmarkResult, Measurement, Status, Unit

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

__all__ = [
    "ROBOT_LOAD",
    "CpuSample",
    "build",
    "busy_fraction",
    "measure_load",
    "parse_proc_stat",
]

NAME: Final = "robot-load"

# What `reachyctl bench` is told to read. A fixed command with no arguments
# taken from anywhere: this benchmark reads a file and never composes one.
_STAT_COMMAND: Final = ("cat", "/proc/stat")
_CPU_COUNT_COMMAND: Final = ("nproc",)

# The fields of `/proc/stat`'s aggregate line that are not work. `idle` is the
# processor doing nothing and `iowait` is it waiting on a device, and counting
# either as busy would report a robot that is asleep as a robot at capacity.
# They are the fourth and fifth fields, so a line with fewer than five is not
# one this can read.
_FIRST_IDLE_FIELD: Final = 3
_IDLE_FIELDS: Final = 2

# `user` through `steal`, and deliberately not the two after them. The kernel
# counts `guest` inside `user` and `guest_nice` inside `nice`, so a total that
# summed every field would count a virtualised robot's guest time twice and
# report a *lower* busy fraction than the real one — which is the direction that
# hides load rather than inventing it.
_COUNTED_FIELDS: Final = 8


class CpuSample(tuple[int, int]):
    """One reading of the robot's cumulative processor time.

    A pair of jiffy counts — total and idle — rather than a dataclass, because
    the whole of what this benchmark does with a sample is subtract it from
    another one.
    """

    @property
    def total(self) -> int:
        """Cumulative processor time across every state.

        Returns:
            The jiffy count.
        """
        return self[0]

    @property
    def idle(self) -> int:
        """Cumulative processor time spent idle or waiting on a device.

        Returns:
            The jiffy count.
        """
        return self[1]


def parse_proc_stat(text: str) -> CpuSample:
    """Read the aggregate processor line out of a `/proc/stat` dump.

    Args:
        text: The file's contents.

    Returns:
        The cumulative total and idle jiffy counts.

    Raises:
        ValueError: If the dump carries no aggregate `cpu` line, or its fields
            are not numbers. A sample that half-parsed would produce a load
            figure nobody could account for.
    """
    for line in text.splitlines():
        fields = line.split()
        if not fields or fields[0] != "cpu":
            continue
        try:
            values = [int(field) for field in fields[1:]]
        except ValueError as error:
            message = f"the aggregate cpu line is not numeric: {line!r}"
            raise ValueError(message) from error
        if len(values) < _FIRST_IDLE_FIELD + _IDLE_FIELDS:
            message = f"the aggregate cpu line is too short: {line!r}"
            raise ValueError(message)
        idle = sum(values[_FIRST_IDLE_FIELD : _FIRST_IDLE_FIELD + _IDLE_FIELDS])
        return CpuSample((sum(values[:_COUNTED_FIELDS]), idle))
    message = "no aggregate cpu line in the /proc/stat dump"
    raise ValueError(message)


def busy_fraction(first: CpuSample, second: CpuSample) -> float:
    """Work out what fraction of the machine was working between two samples.

    Jiffies cancel: busy jiffies over total jiffies is the fraction of the whole
    machine that was doing work, whatever the kernel's tick rate is and however
    long the interval was. Reading the tick rate would be a second thing to get
    wrong for no gain, and the wall time between the samples never enters it.

    Args:
        first: The earlier sample.
        second: The later sample.

    Returns:
        The fraction busy, between 0.0 and 1.0.

    Raises:
        ValueError: If no processor time elapsed between the samples, which
            means they are one sample read twice — and dividing by that would
            report zero load on a busy robot.
    """
    elapsed = second.total - first.total
    if elapsed <= 0:
        message = (
            "no processor time elapsed between the two samples, so they are "
            "the same sample read twice"
        )
        raise ValueError(message)
    busy = (second.total - second.idle) - (first.total - first.idle)
    return busy / elapsed


def measure_load(
    run: Callable[[Sequence[str]], str],
    seconds: float,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[float, int]:
    """Sample the robot's processor time twice and report the cores busy.

    The unit is the one the recorded baseline is written in — "1.52 of 4 cores"
    — so the fraction busy is multiplied back up by the core count the robot
    itself reports.

    Args:
        run: How to run a command on the robot and read its output.
        seconds: How long to sample for.
        sleep: How to wait between the samples. An argument so a test drives
            the arithmetic without spending the interval.

    Returns:
        Cores busy, and how many cores the robot has.

    Raises:
        ValueError: If either sample is unreadable, if the interval is not
            positive, or if the robot reports a core count that is not one.
    """
    if seconds <= 0.0:
        message = f"two samples are taken an interval apart, not {seconds}s"
        raise ValueError(message)
    reported = run(_CPU_COUNT_COMMAND).strip()
    if not reported.isdigit() or int(reported) < 1:
        message = f"the robot reported {reported!r} processors"
        raise ValueError(message)
    cores = int(reported)
    first = parse_proc_stat(run(_STAT_COMMAND))
    sleep(seconds)
    second = parse_proc_stat(run(_STAT_COMMAND))
    return busy_fraction(first, second) * cores, cores


def build(
    options: Options,
    sleep: Callable[[float], None] = time.sleep,
) -> BenchmarkResult:
    """Measure what the robot's processors are doing while it tracks.

    Args:
        options: What the run was configured with.
        sleep: How to wait out the sampling interval.

    Returns:
        The benchmark's result: the cores busy, or a failure saying why not.
    """
    if options.robot is None:
        return BenchmarkResult.failed(
            NAME,
            "no robot: this benchmark reads /proc/stat on the robot, and "
            "measuring the machine it happens to be running on instead would "
            "report a groundstation's processors as a robot's. Run it through "
            "`reachyctl bench --robot user@host`",
        )
    cores, available = measure_load(options.robot, options.sample_seconds, sleep)
    return BenchmarkResult(
        benchmark=NAME,
        status=Status.MEASURED,
        configuration={
            "frame_rate": options.frame_rate,
            "sample_seconds": options.sample_seconds,
            "cores_available": available,
            "network": options.network,
        },
        measurements=(
            Measurement(
                name=f"{NAME}.cpu_cores",
                unit=Unit.CORES,
                value=cores,
                detail={
                    "cores_available": available,
                    "frame_rate": options.frame_rate,
                },
            ),
        ),
        notes=(
            "the whole machine's processors, not the application's: the robot "
            "runs motion control and audio alongside, and what the recorded "
            "1.52-of-4 figure describes is the device rather than one process. "
            "The frame rate is configuration and is recorded above, because the "
            "figure means nothing without it",
        ),
    )


ROBOT_LOAD: Final = BenchmarkSpec(
    name=NAME,
    summary="The robot's processor load while it tracks at a given frame rate.",
    requires_hardware=True,
    run=build,
)
