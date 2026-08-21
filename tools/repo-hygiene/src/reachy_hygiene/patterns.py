"""The generic environment-leak patterns and the exclusions applied to them.

Every rule here describes a *shape* — a private address range, an internal
hostname suffix, an address form — and never a name. A denylist holding the real
hostnames and accounts this repository keeps out would itself publish them in
the repository whose purpose is to exclude them, which is why the requirement
these rules implement is written in terms of generic patterns.

Two exclusions keep the rules usable:

* `is_documentation_value` suppresses a match that is a documentation-reserved
  value — the RFC 5737 IPv4 ranges, the RFC 3849 IPv6 prefix, the `example.com`
  family, and the loopback addresses. A specification or a runbook has to be
  able to show an address without failing the gate.
* `ALLOW_MARKER` suppresses a single line that carries it. It exists for the
  case a shape matches something that is not an address at all — a Python
  attribute named `local`, say — and it is deliberately per-line, so a reviewer
  reading the diff sees the exemption on the line it applies to.

`EXEMPT_PATHS` is narrower still and has exactly one member: the corpus that
pins this module's behaviour is the one tracked file that must contain
leak-shaped strings without carrying a marker, because the tests feed it to the
scanner and assert that it is caught.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

__all__ = [
    "ALLOW_MARKER",
    "EXEMPT_PATHS",
    "RULES",
    "Rule",
    "is_documentation_value",
]


@dataclass(frozen=True, slots=True)
class Rule:
    """One leak shape and the expression that recognises it.

    Attributes:
        name: Stable identifier reported with every finding.
        summary: What the shape is, for the failure message.
        pattern: The expression matched against a single line of text.
    """

    name: str
    summary: str
    pattern: re.Pattern[str]


# A dotted-quad octet. Spelled out rather than `\d{1,3}` so that a version
# string such as "10.400.0.1" is not mistaken for an address.
_OCTET: Final = r"(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])"

# The boundary asserted on both ends of an address. `.` and `-` are excluded as
# well as word characters, so "1.10.0.3" — a four-part version number — does not
# read as an RFC 1918 address starting at its second component.
_ADDRESS_START: Final = r"(?<![\w.-])"
_ADDRESS_END: Final = r"(?![\w.-])"

# A hostname label: alphanumeric at both ends, hyphens allowed inside.
_LABEL: Final = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"

_PRIVATE_IPV4: Final = re.compile(
    _ADDRESS_START
    + r"(?:"
    # RFC 1918, the 10/8 block.
    + rf"10(?:\.{_OCTET}){{3}}"
    # RFC 1918, the 172.16/12 block.
    + rf"|172\.(?:1[6-9]|2[0-9]|3[01])(?:\.{_OCTET}){{2}}"
    # RFC 1918, the 192.168/16 block.
    + rf"|192\.168(?:\.{_OCTET}){{2}}"
    # RFC 6598, the 100.64/10 shared address space a private overlay
    # network hands out. Not an RFC 1918 range, and just as much a leak.
    + rf"|100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])(?:\.{_OCTET}){{2}}"
    + r")"
    + _ADDRESS_END
)

# RFC 4193 unique local addresses — the fc00 half of the address space.
# Requiring at least one colon-separated group after the first hextet is what
# keeps a bare hexadecimal string such as a commit identifier from matching,
# and allowing that group to be empty is what admits a compressed address: a
# bare prefix is as much of a leak as a full address. The next line holds one of
# each, so it carries the inline marker: leak-scan:allow  fd00:: / fd00:1:2::3
_PRIVATE_IPV6: Final = re.compile(
    r"(?<![\w:])[fF][cCdD][0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{0,4}){1,7}(?![\w])"
)

# Any number of labels before the suffix. A single-label pattern misses every
# fully qualified name, because the boundary rejects a match starting after a
# dot and so finds nothing anywhere in one — and a fully qualified name is most
# of what is worth catching. leak-scan:allow  example: db.prod.internal
_INTERNAL_HOSTNAME: Final = re.compile(
    rf"(?<![\w.-]){_LABEL}(?:\.{_LABEL})*\.(?:local|internal|lan|home\.arpa)(?![\w-])",
    re.IGNORECASE,
)

# The shape of a private overlay-network name. The suffix belongs to a public
# service, which makes it citable here; what it prefixes is somebody's network.
_OVERLAY_HOSTNAME: Final = re.compile(
    rf"(?<![\w.-]){_LABEL}(?:\.{_LABEL})*\.ts\.net(?![\w-])",
    re.IGNORECASE,
)

_EMAIL_ADDRESS: Final = re.compile(
    r"(?<![\w.%+-])[A-Za-z0-9._%+-]+@"
    rf"{_LABEL}(?:\.{_LABEL})*"
    r"\.[A-Za-z]{2,}(?![\w-])"
)

RULES: Final[tuple[Rule, ...]] = (
    Rule(
        name="private-ipv4",
        summary="an address inside a private or shared IPv4 range",
        pattern=_PRIVATE_IPV4,
    ),
    Rule(
        name="private-ipv6",
        summary="a unique local IPv6 address",
        pattern=_PRIVATE_IPV6,
    ),
    Rule(
        name="internal-hostname",
        summary="a hostname under an internal-only suffix",
        pattern=_INTERNAL_HOSTNAME,
    ),
    Rule(
        name="overlay-hostname",
        summary="a hostname under a private overlay network",
        pattern=_OVERLAY_HOSTNAME,
    ),
    Rule(
        name="email-address",
        summary="an email address",
        pattern=_EMAIL_ADDRESS,
    ),
)

# Values a specification, a runbook or an example is entitled to contain. The
# reserved names are the documentation ones only: `.invalid`, `.test` and
# `.localhost` are special-use rather than documentation names, and a change
# that genuinely needs one carries the inline marker instead.
_DOCUMENTATION_VALUES: Final[tuple[re.Pattern[str], ...]] = (
    # RFC 5737 IPv4 ranges reserved for documentation.
    re.compile(r"^192\.0\.2\.[0-9]{1,3}$"),
    re.compile(r"^198\.51\.100\.[0-9]{1,3}$"),
    re.compile(r"^203\.0\.113\.[0-9]{1,3}$"),
    # RFC 3849 IPv6 prefix reserved for documentation.
    re.compile(r"^2001:0?[dD][bB]8:"),
    # RFC 2606 documentation domains, as a hostname or as an email domain.
    re.compile(r"(?:^|[@.])example\.(?:com|org|net)$", re.IGNORECASE),
    re.compile(r"(?:^|[@.])[A-Za-z0-9-]+\.example$", re.IGNORECASE),
    # Loopback and wildcard addresses, which name no environment at all.
    re.compile(r"^(?:localhost|127\.0\.0\.1|0\.0\.0\.0|::1)$", re.IGNORECASE),
)

# A line carrying this marker is not scanned. Per-line and never per-directory,
# so the exemption is visible in the diff a reviewer is already reading.
ALLOW_MARKER: Final = "leak-scan:allow"

# The single path exempt from the scan, by exact name rather than by directory.
# `corpus.py` exists to be caught: the tests feed it to the scanner and assert
# it fails, so it cannot carry markers, and it cannot fail the repository's own
# gate either. A directory rule here would quietly exempt every file added
# beside it later, which is the opposite of what this exemption is for.
EXEMPT_PATHS: Final[frozenset[str]] = frozenset(
    {"tools/repo-hygiene/src/reachy_hygiene/corpus.py"}
)


def is_documentation_value(text: str) -> bool:
    """Report whether a matched value is documentation-reserved.

    Args:
        text: The exact substring a rule matched.

    Returns:
        `True` when the value is reserved for documentation and therefore
        allowed in a tracked file, `False` otherwise.
    """
    return any(pattern.search(text) for pattern in _DOCUMENTATION_VALUES)
