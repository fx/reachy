"""Model runtimes: where inference is bounded, warmed up, and kept off the loop.

A capability holds a `ModelRuntime` rather than an inference session of its own,
so the thread bounds groundstation REQ-027 requires are applied in one place and
a capability added later inherits them rather than restating them.
"""

from __future__ import annotations

from reachy_groundstation.runtime.onnx import (
    CPU_PROVIDER,
    ModelRuntime,
    RuntimeOptions,
    resolve_providers,
)

__all__ = [
    "CPU_PROVIDER",
    "ModelRuntime",
    "RuntimeOptions",
    "resolve_providers",
]
