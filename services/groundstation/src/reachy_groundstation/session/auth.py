"""Credential verification, and nothing else.

The comparison is constant-time. The credential is a shared secret rather than a
hash, so a naive `==` leaks its prefix through timing to anyone who can open a
session — which, by construction, is everybody.

The function takes plain strings rather than `SecretStr`. That keeps pydantic out
of this module, and it keeps the unwrapping visible at the call site, where a
reader can see that the secret is being read deliberately and once.

Both sides are encoded before they are compared. `hmac.compare_digest` accepts
`str` only when both arguments are ASCII throughout and raises `TypeError`
otherwise, so a credential with a non-ASCII character in it would take the
session handler down rather than failing authentication.
"""

from __future__ import annotations

import hmac

__all__ = ["credential_is_valid"]


#:= docs/specs/robot-link/index.md#req-019-sessions-are-authenticated
#:% The groundstation MUST reject a session whose client does not present a valid
#:% credential.
def credential_is_valid(presented: str, expected: str) -> bool:
    """Decide whether a client presented the configured credential.

    Args:
        presented: What the client sent.
        expected: What this service is configured with.

    Returns:
        True only if the two are the same, compared without leaking where they
        first differ.
    """
    return bool(expected) and hmac.compare_digest(
        presented.encode("utf-8"),
        expected.encode("utf-8"),
    )
