"""Saying what went wrong without repeating what it said.

Three surfaces of this service publish the fact that something failed to
somebody who is not an operator: a `SessionError` sent to a client, a
`SessionClose` reason, and the `detail` on the capability health endpoint. All
three are reachable by anything that can reach the service, and all three would
otherwise carry the text of an exception raised by code this module does not
control — a model loader naming the path it could not open, a validator quoting
the value it rejected.

So they carry a classification and the logs carry the text. An operator reads
the log, which is theirs; a client learns that a capability failed, which is all
it can act on. This is the same reasoning that makes the configuration endpoint
report a secret as set rather than by value.
"""

from __future__ import annotations

__all__ = ["describe_fault", "validation_summary"]


def describe_fault(error: BaseException) -> str:
    """Name a failure by its kind.

    Args:
        error: What was raised.

    Returns:
        The exception's type name, and nothing it said.
    """
    return type(error).__name__


def validation_summary(error: ValueError) -> str:
    """Say which fields of a message failed to validate, and never with what.

    A `pydantic.ValidationError` renders the offending input value into its own
    text. That is helpful everywhere except on a message that carries a
    credential, so this reports the location and the kind of each fault and
    discards the values.

    Args:
        error: What validation raised.

    Returns:
        A short, value-free description of what was wrong.
    """
    faults = getattr(error, "errors", None)
    if faults is None:
        return describe_fault(error)
    return "; ".join(
        f"{'.'.join(str(part) for part in fault['loc']) or '(message)'}: "
        f"{fault['type']}"
        for fault in faults()
    )
