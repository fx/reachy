"""Bounded per-session stages: queue, decode once, answer, emit.

Like `session/`, this package names no capability and may not import
`reachy_groundstation.capabilities`. It is handed the capabilities a session
agreed on and runs each of them over the same decoded frame.
"""

from __future__ import annotations

from reachy_groundstation.pipeline.decode import DecodeError, decode_jpeg
from reachy_groundstation.pipeline.queue import (
    FrameQueue,
    QueueClosedError,
    QueuedFrame,
)
from reachy_groundstation.pipeline.runner import Deliver, FramePipeline

__all__ = [
    "DecodeError",
    "Deliver",
    "FramePipeline",
    "FrameQueue",
    "QueueClosedError",
    "QueuedFrame",
    "decode_jpeg",
]
