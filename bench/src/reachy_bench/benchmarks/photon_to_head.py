"""`photon-to-head`: physical movement to commanded head movement, by hand.

This is the measurement that matters to a person in the room and the hardest to
automate: it spans a physical stimulus, capture, transport, inference, transport
again and a motor command. The predecessor's figure is 150-250 ms end to end.

**There is no automated stimulus, and that is a stated non-goal rather than an
omission.** How to stimulate it repeatably is an open question in the benchmarks
spec: a person moving is not reproducible, a screen showing a moving face is but
measures something slightly different, and building either is disproportionate
to the project as it stands. So the measurement stays manual, and what this
benchmark owns is the part a program can own — validating the observations,
reporting them as a distribution, and recording the context they were taken in
so that a figure from six months ago is still interpretable.

**It fails rather than inventing anything.** Selected with no observations, it
reports a failure saying exactly what it needs. That is deliberate: the
alternative — reporting the link's measurable round trip and calling it
photon-to-head — would put a number against the one figure that is supposed to
catch the case where every stage improved and the experience did not.

It is not in the default selection, so a run that does not name it reports it as
excluded and nothing here executes.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Final

from reachy_bench.registry import BenchmarkSpec, Options
from reachy_bench.result import BenchmarkResult, Measurement, Status
from reachy_bench.stats import Distribution

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["PHOTON_TO_HEAD", "build", "observation_problems"]

NAME: Final = "photon-to-head"

_MILLISECONDS: Final = 1000.0

# What the benchmark says when it has nothing to report on. It names the flag
# and the method, because the person reading it is standing next to a robot.
_NEEDS_OBSERVATIONS: Final = (
    "no observations were given. There is no automated photon-to-head stimulus "
    "— that is an open question in the benchmarks spec and this change's stated "
    "non-goal — so the measurement is taken by hand: record the robot and the "
    "stimulus together at a known frame rate, count the frames between the "
    "stimulus moving and the head moving, and pass each interval in "
    "milliseconds with --observation. Nothing is reported without them, because "
    "a number this benchmark invented would be worse than no number"
)


def observation_problems(observations: Sequence[float]) -> tuple[str, ...]:
    """List every reason a set of observations cannot be reported.

    Separated from the reporting so a test can watch it reject: an observation
    that is negative, infinite or absurd is a transcription mistake, and one
    that reached the distribution would move a median nobody could explain.

    Args:
        observations: The intervals, in milliseconds.

    Returns:
        One message per problem, empty when they are all usable.
    """
    problems: list[str] = []
    if not observations:
        problems.append(_NEEDS_OBSERVATIONS)
        return tuple(problems)
    problems.extend(
        f"observation {position} is {value}, which is not a duration in milliseconds"
        for position, value in enumerate(observations, start=1)
        # Finite as well as positive. A transcription that produced `nan`
        # compares false against every bound, so a check written only as
        # `value <= 0` would let it through and then quietly poison a median.
        if not math.isfinite(value) or value <= 0.0
    )
    return tuple(problems)


def build(options: Options) -> BenchmarkResult:
    """Report the manually recorded stimulus-to-motion intervals.

    Args:
        options: What the run was configured with, carrying the observations.

    Returns:
        The benchmark's result: a measured distribution, or a failure naming
        what it needs.
    """
    problems = observation_problems(options.observations_ms)
    if problems:
        return BenchmarkResult.failed(NAME, " ".join(problems))
    distribution = Distribution.of_seconds(
        [value / _MILLISECONDS for value in options.observations_ms],
    )
    return BenchmarkResult(
        benchmark=NAME,
        status=Status.MEASURED,
        configuration={
            "method": "manual: frames counted between stimulus and head motion",
            "observations": len(options.observations_ms),
            "network": options.network,
        },
        measurements=(Measurement.timing(f"{NAME}.stimulus_to_motion", distribution),),
        notes=(
            "recorded by hand. The benchmarks spec leaves how to stimulate this "
            "repeatably open, so two runs are only comparable when the same "
            "method produced both — which is why the method is in the "
            "configuration above rather than assumed",
        ),
    )


PHOTON_TO_HEAD: Final = BenchmarkSpec(
    name=NAME,
    summary="Stimulus to head movement, end to end. Needs a robot and a person.",
    requires_hardware=True,
    run=build,
)
