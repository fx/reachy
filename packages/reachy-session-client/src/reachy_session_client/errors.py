"""What can go wrong on a session, classified by what a caller can do about it.

The hierarchy is shaped by one question: does waiting help? A transport that
dropped and a groundstation that is not listening are both answered by the
bounded retry in `SessionClient`; a credential the groundstation refuses and a
message it sent that this client cannot parse are not, and no delay makes either
of them true later. So `SessionRefusedError` and `ProtocolError` travel out of
the retry loop to the caller, and everything else is retried.

Nothing raised from this package embeds a credential. The one message that
carries one is the offer, and the one thing that reads a credential out of a
failure is a `pydantic.ValidationError`, which renders the value it rejected
into its own text. `describe_validation` is what the offer path reports instead:
which field failed and how, and never with what.
"""

from __future__ import annotations

__all__ = [
    "ConnectionFailedError",
    "NotConnectedError",
    "ProtocolError",
    "SessionClientError",
    "SessionRefusedError",
    "describe_validation",
]


class SessionClientError(Exception):
    """Anything this package raises."""


class ConnectionFailedError(SessionClientError):
    """The transport could not be opened, or it dropped.

    Retrying is the answer: this is the groundstation restarting, a name that
    does not resolve yet, or a network that came back.
    """


class NotConnectedError(SessionClientError):
    """Something was asked of a session that has not been established."""


class ProtocolError(SessionClientError):
    """The other side sent something this protocol does not describe.

    Not retried. A groundstation answering an offer with a result is not a
    groundstation a delay will fix, and looping against one would turn a bug
    into a quiet reconnection storm.
    """


class SessionRefusedError(SessionClientError):
    """The groundstation closed the session instead of agreeing to it.

    Not retried either, and that is the deliberate reading of robot-link
    REQ-018: its scenarios are a groundstation that restarted and a name that
    does not resolve, both of which come back on their own. A credential the
    other side does not accept comes back when an operator changes something,
    so retrying it forever would hide the one failure that needs a person.

    Attributes:
        reason: The close reason the groundstation named.
        detail: What it said about it, which is never a credential.
    """

    def __init__(self, reason: str, detail: str) -> None:
        """Record the refusal.

        Args:
            reason: The close reason the groundstation named.
            detail: What it said about it.
        """
        super().__init__(f"the groundstation refused the session ({reason}): {detail}")
        self.reason = reason
        self.detail = detail


def describe_validation(error: ValueError) -> str:
    """Say which fields of a message failed to validate, and never with what.

    A `pydantic.ValidationError` renders the offending input value into its own
    text, which is helpful everywhere except on a message carrying a credential.
    This reports the location and the kind of each fault and discards the
    values, the same way the groundstation's `validation_summary` does for the
    same reason.

    Args:
        error: What validation raised.

    Returns:
        A short, value-free description of what was wrong.
    """
    faults = getattr(error, "errors", None)
    if faults is None:
        return type(error).__name__
    return "; ".join(
        f"{'.'.join(str(part) for part in fault['loc']) or '(message)'}: "
        f"{fault['type']}"
        for fault in faults()
    )
