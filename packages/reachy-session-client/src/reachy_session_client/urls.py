"""Rendering an address so that it can be written down.

reachyctl REQ-059 says a credential must not reach the output, the logs or the
error messages, and an address is one of the two things every layer has a
reason to quote. `validate_session_url` refuses the parts of a URL a secret
fits in, which is what makes the *configured* address safe — but that is a
property of the caller having validated, not of the value, and the function
that formats a URL into a connection failure is public API reachable without
it. A guarantee that holds only because the one caller happens to sanitise
first is not a guarantee, so the rendering is safe on its own terms here.

The two halves are deliberately different jobs. `validate_session_url` refuses
an address, because a session must not be opened on one that carries a
credential. This redacts one, because by the time a message is being written
the address is whatever it is and the choice is only what to say about it.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from reachy_session_client.credential import REDACTED

__all__ = ["redact_url"]


def redact_url(url: str) -> str:
    """Render an address with the parts a secret fits in left out.

    What a message about a connection needs is where the connection was to:
    the scheme, the host, the port and the path. The user information, the
    query and the fragment are not that, and each of them is somewhere a
    credential is routinely put — so they are dropped rather than starred out,
    because a placeholder still says how long the thing was and which of them
    carried it.

    An address that cannot be taken apart is replaced entirely. Echoing a
    string back because it failed to parse is how the value nobody could read
    ends up in the log line, and something unparseable is exactly the input
    least worth trusting.

    **What comes out has to go back in.** This is what an operator reads when a
    connection fails, and what `doctor` will build a remediation out of when it
    arrives in change 0008 — reachyctl REQ-055 requires every failing check to
    report one. An address that cannot be pasted back is worse than none at
    all, because it sends somebody to debug a host that was never involved.

    Args:
        url: The address, which may never have been validated.

    Returns:
        The address reduced to its scheme, authority and path, or a placeholder
        when it is not an address this can take apart. What is returned parses
        back to the same host and port.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
        # Read inside the guard: `port` parses lazily and raises on a value
        # that is not a number, so it is one of the ways this can fail.
        port = parts.port
    except ValueError:
        return REDACTED
    if not parts.scheme or not host:
        return REDACTED
    # `hostname` strips the brackets an IPv6 literal is written in, and they
    # are not decoration: they are what separates the address from the port.
    # Put back unbracketed, `[::1]:8080` becomes `::1:8080`, which does not
    # parse — and `[2001:db8::1]` with no port becomes an address whose host
    # reads as `2001`, which is worse, because it looks like something.
    # A colon cannot appear in any other kind of host, so its presence is
    # exactly the question being asked.
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority = f"{authority}:{port}"
    return f"{parts.scheme}://{authority}{parts.path}"
