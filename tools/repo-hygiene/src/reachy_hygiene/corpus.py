"""The conformance corpus that pins what the leak scanner does and does not.

`MUST_BE_CAUGHT` holds one line per shape the scan exists to reject, and
`MUST_BE_ALLOWED` holds the legitimate content it must never reject. The test
suite asserts both directions over every entry, so tightening a pattern to catch
one more thing cannot silently start failing documentation, and loosening one to
stop a false positive cannot silently stop catching a leak.

Every value here is synthetic. The addresses are drawn from the private ranges
the scanner recognises — which is the point, since they have to be caught — and
the names use suffixes that are reserved never to resolve. Nothing in this file
belongs to anyone's environment.

This module is the single path listed in `patterns.EXEMPT_PATHS`. It is the one
tracked file that must hold leak-shaped strings without the inline marker,
because the tests feed these strings to the scanner and assert that it flags
them; a marker would make that assertion vacuous.
"""

from __future__ import annotations

from typing import Final

__all__ = ["MUST_BE_ALLOWED", "MUST_BE_CAUGHT"]

MUST_BE_CAUGHT: Final[tuple[str, ...]] = (
    # RFC 1918 10.0.0.0/8.
    "ROBOT_HOST=10.42.0.7",
    # RFC 1918 172.16.0.0/12.
    "gateway: 172.20.1.1",
    # RFC 1918 192.168.0.0/16.
    "  upstream = 192.168.1.50",
    # RFC 6598 shared address space, as handed out by a private overlay.
    "peer_address: 100.100.7.42",
    # RFC 4193 unique local IPv6, written out and compressed to a bare prefix.
    "listen = [fd00:1234:5678::1]:8443",
    "prefix = fd00::",
    # Internal-only hostname suffixes, with one label before them and with
    # several: a fully qualified internal name is the common case, not the
    # exception, and a single-label pattern misses all of them.
    "ROBOT_HOST=robot.local",
    "ROBOT_HOST=robot.lab.local",
    "backup_target: storage.internal",
    "backup_target: db.prod.internal",
    "printer = printer.lan",
    "resolver: gateway.home.arpa",
    # A private overlay network name, likewise at more than one depth.
    "endpoint = https://robot.tailnet-example.ts.net:8443/link",
    "endpoint = https://robot.lab.tailnet-example.ts.net:8443/link",
    # An email address, in a domain reserved never to resolve.
    "maintainer = someone@reachy.invalid",
)

MUST_BE_ALLOWED: Final[tuple[str, ...]] = (
    # RFC 5737 IPv4 ranges reserved for documentation.
    "ROBOT_HOST=192.0.2.10",
    "secondary: 198.51.100.42",
    "tertiary = 203.0.113.7",
    # RFC 3849 IPv6 prefix reserved for documentation.
    "listen = [2001:db8::1]:8443",
    # Loopback and wildcard addresses, which name no environment.
    "bind = 0.0.0.0:8443",
    "probe = http://127.0.0.1:8443/healthz",
    "ipv6_probe = http://[::1]:8443/healthz",
    "host = localhost",
    # RFC 2606 documentation domains.
    "contact = ops@example.com",
    "docs = https://example.org/reachy",
    "mirror = https://downloads.example.net/reachy",
    # A four-part version number, which starts with something that reads like
    # the second half of an RFC 1918 address.
    "onnxruntime == 1.10.0.3",
    # A hexadecimal identifier that begins the way a unique local address does
    # but carries no colon.
    "digest = fd00abcdef0123456789",
    # A public registry reference and a public repository name.
    "image = ghcr.io/example/reachy-groundstation:0.1.0",
    "action = actions/checkout@v4",
)
