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
            ValueError: If the delays are not positive, if the multiplier would
                shrink the delay rather than grow it, or if the bound is below
                the first delay — which would mean the first attempt already
                exceeded the maximum.
        """
        if self.initial_seconds <= 0:
            message = f"the first delay must be positive, not {self.initial_seconds}"
            raise ValueError(message)
        if self.multiplier < 1:
            message = f"the multiplier must not shrink the delay: {self.multiplier}"
            raise ValueError(message)
        if self.maximum_seconds < self.initial_seconds:
            message = (
                f"the bound {self.maximum_seconds} is below the first delay "
                f"{self.initial_seconds}"
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
        grown = self.initial_seconds * self.multiplier ** (attempt - 1)
        return min(grown, self.maximum_seconds)


# Half a second, doubling, capped at thirty. Against a link whose idle
# round-trip is 100-170 ms, the first retry is already an order of magnitude
# longer than a healthy exchange, and the bound keeps a robot's recovery from an
# hour-long outage inside half a minute.
DEFAULT_BACKOFF: Final = Backoff()
