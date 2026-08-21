"""Minting a frame's capture token, and reading one back on the way home.

The robot link spec's Clocks section is the whole of this module's reasoning. A
capture timestamp is read from a **monotonic** clock on the capturing side and
is an **opaque token** to everybody else: the groundstation copies it from the
frame onto the result and never inspects it, because it has no clock the value
means anything against.

The only party that interprets a token is the one that minted it. That is this
module. It stamps a frame from a monotonic source and, when the same token comes
back on a result, subtracts it from the same source — so a result's age is a
single-clock measurement despite having crossed two machines, and it survives a
robot whose wall clock jumps when the network sets it at boot.

`age_of` therefore refuses to interpret a token it did not mint. A token in
another format is one from another client or another format version, and
answering with a plausible number for it would be the cross-clock comparison the
protocol is built to avoid.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Final

from reachy_contracts import CaptureTimestamp

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["MonotonicStamps"]

# Microseconds. Enough resolution that two frames captured in the same
# millisecond are distinguishable, and short enough that the rendered token
# stays well inside the 64 characters `CaptureTimestamp` allows for the decades
# of uptime a monotonic clock can report.
_PRECISION: Final = 6


class MonotonicStamps:
    """Mints capture tokens from one monotonic clock and reads them back."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        """Choose the clock frames are stamped from.

        Args:
            clock: A monotonic source. Injected so that a test can advance time
                without waiting for it, which is what keeps the staleness and
                round-trip behaviour testable without sleeping.
        """
        self._clock = clock

    def now(self) -> float:
        """Read the clock.

        Returns:
            The current value of the monotonic source.
        """
        return self._clock()

    def stamp(self) -> CaptureTimestamp:
        """Mint a token for a frame being captured now.

        Successive calls never go backwards, because the source is monotonic —
        which is what lets a consumer of the results order them by capture as
        well as by sequence number.

        Returns:
            The token to put on the frame's header.
        """
        return CaptureTimestamp(f"{self._clock():.{_PRECISION}f}")

    #:= docs/specs/robot-link/index.md#req-016-results-return-the-capture-timestamp-unaltered
    #:% Every result MUST carry the capture timestamp of the frame it derives from,
    #:% byte-for-byte as the capturing side supplied it, so that the capturing side can
    #:% compute the result's age against the same clock that produced it.
    def age_of(self, token: CaptureTimestamp, now: float) -> float | None:
        """Say how long ago the frame carrying this token was captured.

        The reference reading is passed in rather than taken here, so that one
        result is aged against one instant however many times it is asked.

        Args:
            token: The token as it came back on a result, byte for byte as it
                was sent.
            now: This client's clock, read once by the caller.

        Returns:
            The seconds elapsed on this client's own clock, or `None` when the
            token is not one this module minted — in which case there is no
            clock it can be compared against and no number worth returning.
        """
        try:
            captured_at = float(token.root)
        except ValueError:
            return None
        return now - captured_at
