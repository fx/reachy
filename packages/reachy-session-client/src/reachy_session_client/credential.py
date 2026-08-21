"""The shared secret, held in something that will not print itself.

reachyctl REQ-059 forbids a credential reaching output, logs or error messages,
and the paths it actually escapes on are not the ones anybody writes
deliberately: an object rendered into a log line, a `repr` in a traceback frame,
a dataclass that grew a `__repr__` for free. A plain `str` loses on all three.

`Credential` is therefore the type the credential travels in from the moment it
is read until the moment the offer is built, and `reveal` is the only way back
out. The call sites that reveal are countable — there is one — which is what
makes "does this leak?" a question with an answer.

The contracts package holds the same value as a `pydantic.SecretStr` once it is
inside a `SessionOffer`. This type exists because everything before that point
is this package's own, and importing pydantic to hold a string would trip the
TID253 ban for a shape that is not a wire type.
"""

from __future__ import annotations

from typing import Final, final

__all__ = ["REDACTED", "Credential"]

# What a credential looks like anywhere it is rendered.
REDACTED: Final = "<redacted>"


@final
class Credential:
    """A secret that renders as a placeholder wherever it is rendered."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        """Hold a credential.

        Args:
            value: The secret, which must not be empty. An empty credential is
                not a credential, and rejecting it here means "nothing was
                configured" fails where it was read rather than at an
                authentication check that has to decide what an empty string
                means.

        Raises:
            ValueError: If the value is empty.
        """
        if not value:
            message = "a credential must not be empty"
            raise ValueError(message)
        self._value = value

    def reveal(self) -> str:
        """Hand over the secret itself.

        Returns:
            The credential, for the one caller that has to present it.
        """
        return self._value

    def __repr__(self) -> str:
        """Render for a traceback or a debugger.

        Returns:
            A placeholder, never the value.
        """
        return f"Credential({REDACTED})"

    def __str__(self) -> str:
        """Render for a log line or a message.

        Returns:
            A placeholder, never the value.
        """
        return REDACTED
