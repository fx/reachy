"""Authentication, capability negotiation, framing and routing.

Nothing in this package names a capability, and nothing in it may import
`reachy_groundstation.capabilities` — `just lint-capability-boundary` fails the
build if that changes. Routing is by name against a `CapabilityRegistryPort`
handed in from outside, which is what makes adding a capability a change to the
capability's own package and nowhere else.
"""

from __future__ import annotations

from reachy_groundstation.session.auth import credential_is_valid
from reachy_groundstation.session.framing import (
    FramingError,
    MessageKind,
    decode_control,
    decode_frame,
    encode_control,
    encode_frame,
)
from reachy_groundstation.session.runner import (
    SessionOutcome,
    SessionRunner,
    new_session_id,
)
from reachy_groundstation.session.transport import (
    SessionTransport,
    TransportClosedError,
)

__all__ = [
    "FramingError",
    "MessageKind",
    "SessionOutcome",
    "SessionRunner",
    "SessionTransport",
    "TransportClosedError",
    "credential_is_valid",
    "decode_control",
    "decode_frame",
    "encode_control",
    "encode_frame",
    "new_session_id",
]
