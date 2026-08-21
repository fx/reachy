"""The pinned model store: what may ship, where it came from, and its terms.

Three modules, split by when they run.

`registry.py` is a tracked Python literal listing every model, its licence, its
provenance and the digest that pins its bytes. Weights are never committed.

`fetch.py` runs at build time, on a machine with a network, and refuses any file
whose digest is not the pinned one.

`store.py` runs inside the service, on a host that may have no network at all,
and resolves a registered model to the file the build already put in place.
"""

from __future__ import annotations

from reachy_groundstation.models.registry import (
    ALLOWED_LICENCES,
    FACE_DETECTION_YUNET,
    MODELS,
    Model,
    ModelKind,
    licence_problems,
    model_by_name,
)
from reachy_groundstation.models.store import ModelStore, ModelStoreError, digest_of

__all__ = [
    "ALLOWED_LICENCES",
    "FACE_DETECTION_YUNET",
    "MODELS",
    "Model",
    "ModelKind",
    "ModelStore",
    "ModelStoreError",
    "digest_of",
    "licence_problems",
    "model_by_name",
]
