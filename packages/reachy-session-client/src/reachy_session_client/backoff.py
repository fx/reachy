"""How long to wait before the next reconnection attempt.

Robot-link REQ-018 asks for two things and they pull in opposite directions: the
delay grows, so a groundstation that is down does not get hammered, and it stops
growing at a bound, so a groundstation that has been down for an afternoon is
still noticed within seconds of coming back. A delay that doubled without limit
would satisfy the first and fail the second — after an hour of outage the robot
would sit idle for another hour with the service already up.

There is no jitter. Jitter exists to stop many clients retrying in lockstep, and
the population here is one robot and one operator's `probe`. What it would cost
is a delay sequence nobody can predict, which is exactly what a test of "the
delay stops growing at its bound" has to assert against.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

__all__ = ["DEFAULT_BACKOFF", "Backoff"]


@dataclass(frozen=True, slots=True)
class Backoff:
    """A growing, bounded delay between successive failed attempts.

    Attributes:
        initial_seconds: What the first retry waits.
        multiplier: What each subsequent wait is multiplied by.
        maximum_seconds: The bound the delay stops growing at.
    """

    initial_seconds: float = 0.5
    multiplier: float = 2.0
    maximum_seconds: float = 30.0

    def __post_init__(self) -> None:
        """Reject a policy that would not retry, or would not grow.

        Raises:
            ValueError: If any value is not a finite number, if the delays are
                not positive, if the multiplier would not grow the delay, or if
                the bound leaves it no room to grow into — a bound below the
                first delay would mean the first attempt already exceeded the
                maximum, and a bound equal to it means every attempt waits the
                same.
        """
        # Checked before anything else, because a non-finite value passes the
        # comparisons below rather than failing them: every comparison against
        # `nan` is false, so `nan` satisfies each rule here and then raises out
        # of `_steps` on the first retry — inside the reconnection loop, which
        # is the one place an exception ends the session for good. An infinite
        # multiplier is worse because it does not raise at all: it makes
        # `_steps` zero and yields the first delay forever, which is the
        # constant delay the next two checks exist to refuse.
        for name, value in (
            ("first delay", self.initial_seconds),
            ("multiplier", self.multiplier),
            ("bound", self.maximum_seconds),
        ):
            if not math.isfinite(value):
                message = f"the {name} must be a finite number, not {value}"
                raise ValueError(message)
        if self.initial_seconds <= 0:
            message = f"the first delay must be positive, not {self.initial_seconds}"
            raise ValueError(message)
        # Both of the checks below reject the same thing — a policy whose delay
        # never increases — approached from its two sides, and REQ-018 asks for
        # a delay that grows as well as one that is bounded. A multiplier of
        # exactly one is the obvious way to write a constant delay; a bound
        # equal to the first delay is the non-obvious one, because the growth
        # is clamped away on the very first attempt. Neither is reachable from
        # this tool's own surface today, but `Backoff` is public API and the
        # robot's groundstation adapter constructs one of its own, so a policy
        # that cannot satisfy the requirement is refused where it is built
        # rather than discovered as a robot that never retries any faster.
        if self.multiplier <= 1:
            message = f"the multiplier must grow the delay: {self.multiplier}"
            raise ValueError(message)
        if self.maximum_seconds <= self.initial_seconds:
            message = (
                f"the bound {self.maximum_seconds} leaves the first delay "
                f"{self.initial_seconds} no room to grow"
            )
            raise ValueError(message)

    def delay(self, attempt: int) -> float:
        """Say how long to wait before an attempt.

        Args:
            attempt: Which attempt this is, counting from one.

        Returns:
            The seconds to wait, never more than `maximum_seconds`.

        Raises:
            ValueError: If the attempt is not a positive number.
        """
        if attempt < 1:
            message = f"attempts are counted from one, not {attempt}"
            raise ValueError(message)
        # Whether the bound has been reached is decided in log space, and the
        # power is computed only on the branch where the answer is no. That is
        # a correctness fix rather than an elegance one, twice over.
        #
        # Float exponentiation *raises* `OverflowError` rather than saturating
        # at infinity, and the reconnection loop never bounds its attempt count
        # — REQ-018 asks for a bounded delay, not for attempts that stop — so
        # with the default policy attempt 1025 would compute `2.0 ** 1024` and
        # end the session for good, eight hours into precisely the outage the
        # requirement's second scenario describes. Comparing first means the
        # power is never evaluated once the delay is already at its bound.
        #
        # The obvious alternative, clamping the exponent at the number of steps
        # it takes to reach the bound, is what this replaced: it computes that
        # count from `maximum / initial`, and that ratio is itself infinite for
        # a policy as ordinary-looking as `Backoff(5e-324, 2.0, 1e308)` — every
        # value finite, every rule above satisfied, and `ceil(inf)` raising out
        # of the first retry. Working from the difference of two logarithms
        # rather than the logarithm of a quotient has no such range to fall out
        # of, so the whole class goes rather than one more value being refused.
        #
        # The comparison is `int >= float` and deliberately not
        # `int * float >= float`. Python compares the two exactly, without
        # converting the integer, so an attempt count too large to *be* a float
        # — `10 ** 1000`, which is a perfectly ordinary `int` — answers the
        # question instead of raising `OverflowError` while being converted.
        # Multiplying first would put that conversion back.
        steps_to_bound = (
            math.log(self.maximum_seconds) - math.log(self.initial_seconds)
        ) / math.log(self.multiplier)
        if attempt - 1 >= steps_to_bound:
            return self.maximum_seconds
        # Below the bound, so both the exponent and the power are in range.
        # `min` keeps the guarantee exact anyway: the comparison above is float
        # arithmetic, and a value landing within an ulp of the bound must not
        # step over it.
        return min(
            self.initial_seconds * self.multiplier ** (attempt - 1),
            self.maximum_seconds,
        )


# Half a second, doubling, capped at thirty. Against a link whose idle
# round-trip is 100-170 ms, the first retry is already an order of magnitude
# longer than a healthy exchange, and the bound keeps a robot's recovery from an
# hour-long outage inside half a minute.
DEFAULT_BACKOFF: Final = Backoff()
