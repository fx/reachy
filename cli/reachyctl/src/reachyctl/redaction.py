"""Removing secrets from anything on its way out.

reachyctl REQ-059 says a credential must not reach the output, the logs or the
error messages, and the interesting word is *error*. Nobody writes a line that
prints a credential; what happens is that a value ends up inside an exception
raised three libraries down, or inside a verbose dump of the configuration in
effect, and is then printed by a handler that was only trying to be helpful.

So redaction is not a rule applied where a secret is known to be. It is applied
at the one place everything leaves through — `Reporter` scrubs every string it
writes, on every path, including the exception path and the verbose path — and
this module is what it scrubs with.

The redactor holds the secret values themselves rather than their names, because
what has to be recognised in a stranger's error message is the value.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from reachy_session_client import REDACTED

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ["Redactor"]


class Redactor:
    """Replaces known secret values wherever they appear in a string."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        """Create a redactor that knows about some secrets.

        Args:
            secrets: The values to remove. Empty ones are ignored: an empty
                string appears between every pair of characters, so treating
                one as a secret would replace the whole of every message.
        """
        self._secrets: list[str] = []
        for secret in secrets:
            self.guard(secret)

    def guard(self, secret: str) -> None:
        """Add a value to remove from everything written from now on.

        Args:
            secret: The value. Empty values are ignored.
        """
        if secret and secret not in self._secrets:
            self._secrets.append(secret)
            # Longest first, so that a secret which contains another is
            # replaced whole rather than being left with a redacted fragment
            # embedded in the rest of it.
            self._secrets.sort(key=len, reverse=True)

    def scrub(self, text: str) -> str:
        """Remove every known secret from a string.

        Args:
            text: What was about to be written.

        Returns:
            The same string with each known secret replaced by a placeholder.
        """
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text
