"""Resolving a registered model to the file already present in the artifact.

This is the run-time half, and what it does not do is the point:
groundstation REQ-023 says the service loads every model from a file already in
its deployed artifact and never fetches weights over the network. So this module
imports nothing that can open a socket. A model that is missing is a capability
that fails to warm up — which the registry contains, leaving the rest of the
service serving — and never a download.

It still verifies the digest, at warm-up rather than per frame. Verification at
build time is what stops unknown weights being baked in; verification here is
what catches the file having changed since, whether by a corrupted layer, a
mounted directory pointed somewhere unexpected, or an operator swapping the file
by hand. It costs one read of a few hundred kilobytes, once, before the service
reports itself ready.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reachy_groundstation.models.registry import Model

__all__ = [
    "ModelStore",
    "ModelStoreError",
    "digest_of",
]

# How much of a file to hash at a time. Large enough that the loop is not the
# cost, small enough that a model of any size is never held twice in memory.
_CHUNK_BYTES = 1 << 20


class ModelStoreError(RuntimeError):
    """A registered model is not where it should be, or is not what it should be."""


def digest_of(path: Path) -> str:
    """Hash a file's contents.

    Args:
        path: The file to read.

    Returns:
        The lowercase hexadecimal SHA-256 digest.
    """
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            hasher.update(chunk)
    return hasher.hexdigest()


class ModelStore:
    """The directory the artifact's model files live in."""

    def __init__(self, directory: Path | str) -> None:
        """Point a store at a directory.

        Args:
            directory: Where the build put the model files.
        """
        self._directory = Path(directory)

    @property
    def directory(self) -> Path:
        """Where this store reads from.

        Returns:
            The directory, as given.
        """
        return self._directory

    def path_for(self, model: Model) -> Path:
        """Say where a model's file would be, without looking.

        Args:
            model: The registered model.

        Returns:
            The path, whether or not anything is there.
        """
        return self._directory / model.filename

    #:= docs/specs/groundstation/index.md#req-024-model-provenance-is-recorded-and-verified
    #:% Every model file MUST be pinned by content hash, and the build MUST fail when a
    #:% fetched file's hash does not match the pinned value.
    def resolve(self, model: Model) -> Path:
        """Find a model's file and check it is the pinned one.

        Args:
            model: The registered model.

        Returns:
            The path to the verified file.

        Raises:
            ModelStoreError: If the file is absent, is not a file, or does not
                hash to the digest the registry pins. The message names the
                directory, because the likeliest cause is a service pointed at
                the wrong one.
        """
        path = self.path_for(model)
        if not path.is_file():
            message = (
                f"{model.name}: {path} is not a file. Models are put in place "
                f"when the artifact is built and are never fetched at run time; "
                f"check REACHY_GROUNDSTATION_MODELS_DIR."
            )
            raise ModelStoreError(message)
        actual = digest_of(path)
        if actual != model.sha256:
            message = (
                f"{model.name}: {path} hashes to {actual}, but the registry "
                f"pins {model.sha256}. These are not the weights this build was "
                f"reviewed with."
            )
            raise ModelStoreError(message)
        return path
