"""The perception capabilities: faces now, gestures when a model clears the bar.

Importing this package is what registers both capabilities, because each of them
decorates its factory with `capabilities.register` in its own module. Nothing
under `api/`, `session/` or `pipeline/` learns that either exists.

The two are independent in every way that matters. Each is built by its own
factory, each reads its own settings, and each declines to be built at all when
those settings switch it off — which is perception REQ-038 stated as a mechanism
rather than an intention: switching one off leaves the other building, warming
up, being offered and answering frames exactly as before, and switching both off
leaves a service that negotiates an empty capability set and serves sessions that
receive no results, which is not an error.
"""

from __future__ import annotations

from reachy_groundstation.capabilities.perception.face import (
    FACE_VERSION,
    FaceCapability,
    build_face_capability,
    detect_faces,
)
from reachy_groundstation.capabilities.perception.gesture import (
    GESTURE_VERSION,
    GestureCapability,
    GestureClassifier,
    HandDetector,
    HandRegion,
    build_gesture_capability,
)

__all__ = [
    "FACE_VERSION",
    "GESTURE_VERSION",
    "FaceCapability",
    "GestureCapability",
    "GestureClassifier",
    "HandDetector",
    "HandRegion",
    "build_face_capability",
    "build_gesture_capability",
    "detect_faces",
]
