"""The one client implementation of the robot link session protocol.

`reachyctl probe` and the robot's groundstation adapter both import
`SessionClient` from here. That is the whole purpose of the package: reachyctl
REQ-057 requires the probe to establish its session with the same protocol
implementation the robot uses, so that a groundstation which gets the protocol
wrong fails the probe in the way it would fail the robot. A second client
written for testing would pass its own tests and prove nothing.

The public surface is deliberately small:

- `SessionClient` — connect, submit frames, iterate results, close.
- `Credential` — the shared secret, in a type that will not print itself.
- `Backoff` — the growing, bounded reconnection delay.
- `FrameResult`, `SessionStats` — what came back, and what happened.
- The errors, classified by whether retrying helps.

Nothing here declares a wire type. Every message on the link is declared in
`reachy_contracts` and imported from there, and the TID253 lint rule holds this
package to that with no exemption.
"""

from __future__ import annotations

from reachy_session_client.backoff import DEFAULT_BACKOFF, Backoff
from reachy_session_client.clock import MonotonicStamps
from reachy_session_client.credential import REDACTED, Credential
from reachy_session_client.errors import (
    ConnectionFailedError,
    NotConnectedError,
    ProtocolError,
    SessionClientError,
    SessionRefusedError,
    describe_validation,
)
from reachy_session_client.framing import (
    FramingError,
    MessageKind,
    decode_control,
    encode_control,
    encode_frame,
)
from reachy_session_client.results import (
    FrameResult,
    SessionStats,
    count_detections,
    result_model_for,
)
from reachy_session_client.session import SessionClient, validate_session_url
from reachy_session_client.transport import (
    ClientTransport,
    TransportFactory,
    WebSocketTransport,
    open_websocket,
)

__all__ = [
    "DEFAULT_BACKOFF",
    "REDACTED",
    "Backoff",
    "ClientTransport",
    "ConnectionFailedError",
    "Credential",
    "FrameResult",
    "FramingError",
    "MessageKind",
    "MonotonicStamps",
    "NotConnectedError",
    "ProtocolError",
    "SessionClient",
    "SessionClientError",
    "SessionRefusedError",
    "SessionStats",
    "TransportFactory",
    "WebSocketTransport",
    "count_detections",
    "decode_control",
    "describe_validation",
    "encode_control",
    "encode_frame",
    "open_websocket",
    "result_model_for",
    "validate_session_url",
]
