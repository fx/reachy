"""Shared wire types and golden fixtures for the Reachy Mini stack.

Every name a consumer needs is re-exported here, so a component imports from
`reachy_contracts` and never reaches into a submodule. That is the whole
mechanism behind "types are declared once": the groundstation, the robot app and
`reachyctl probe` all name the same class object, and a field renamed in one
place is a parse failure everywhere rather than a divergence nobody notices.

The wire contract these types express is owned by the robot-link spec, and the
`golden/` directory beside this module pins their byte-level form.
"""

from reachy_contracts.fixtures import (
    FIXTURES,
    Fixture,
    fixture_bytes,
    fixture_for,
    golden_file_names,
    load_fixture,
    round_trip,
)
from reachy_contracts.session import (
    Capability,
    CapabilityVersion,
    CloseReason,
    ErrorCode,
    FrameHeader,
    ResultEnvelope,
    SequenceNumber,
    SessionAgreement,
    SessionClose,
    SessionError,
    SessionOffer,
    negotiate,
)
from reachy_contracts.settings import (
    ROBOT_SETTINGS,
    Setting,
    SettingError,
    SettingKind,
    UnknownSettingError,
    setting_for,
    setting_names,
    validate_setting,
    validate_settings,
)
from reachy_contracts.values import (
    CAPABILITY_PAYLOADS,
    FACE_CAPABILITY,
    GESTURE_CAPABILITY,
    CapabilityName,
    CaptureTimestamp,
    Confidence,
    FaceDetection,
    FaceDetections,
    GestureDetection,
    GestureDetections,
    GestureLabel,
    NormalisedCoordinate,
    NormalisedPoint,
    WireModel,
)
from reachy_contracts.version import VERSION, SemanticVersion, __version__

__all__ = [
    "CAPABILITY_PAYLOADS",
    "FACE_CAPABILITY",
    "FIXTURES",
    "GESTURE_CAPABILITY",
    "ROBOT_SETTINGS",
    "VERSION",
    "Capability",
    "CapabilityName",
    "CapabilityVersion",
    "CaptureTimestamp",
    "CloseReason",
    "Confidence",
    "ErrorCode",
    "FaceDetection",
    "FaceDetections",
    "Fixture",
    "FrameHeader",
    "GestureDetection",
    "GestureDetections",
    "GestureLabel",
    "NormalisedCoordinate",
    "NormalisedPoint",
    "ResultEnvelope",
    "SemanticVersion",
    "SequenceNumber",
    "SessionAgreement",
    "SessionClose",
    "SessionError",
    "SessionOffer",
    "Setting",
    "SettingError",
    "SettingKind",
    "UnknownSettingError",
    "WireModel",
    "__version__",
    "fixture_bytes",
    "fixture_for",
    "golden_file_names",
    "load_fixture",
    "negotiate",
    "round_trip",
    "setting_for",
    "setting_names",
    "validate_setting",
    "validate_settings",
]
