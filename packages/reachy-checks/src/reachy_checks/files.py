"""Verifying the model files against the digests that pin them.

Neither the registry of models nor the hashing is reimplemented here. Both live
in `reachy_groundstation.models`, which is where they belong: the digests pin
the weights a build was reviewed with, and a second copy of them in a
diagnostic would be a second opinion about which bytes are the right ones. The
two would drift, and the drift would surface as a service warming up against
weights nobody checked while a green `doctor` run said the files were fine.

The import is inside the function, and that is the one thing about this module
worth arguing over. The groundstation pulls OpenCV, onnxruntime and an ASGI
stack; a CLI and an Ansible control machine have no use for any of it, and
making this package depend on all of it to read a directory of hashes would put
tens of megabytes into every install. So it is an optional extra, the import is
attempted where it is used, and a machine that does not have it gets a check
that says so rather than an `ImportError` out of the middle of a run.

That is not a hidden dependency: the model files exist inside the
groundstation's deployed artifact, so the machine that has files to check is
the machine that has the service installed.
"""

from __future__ import annotations

from pathlib import Path

from reachy_checks.ports import ModelFileReport

__all__ = ["REGISTRY_MISSING", "GroundstationModelFiles"]

# What the check reports when the groundstation is not installed. Phrased as a
# problem rather than a skip: a caller who pointed `doctor` at a model
# directory asked for the files to be verified, and answering "there was
# nothing to verify them against" is a finding, not silence.
REGISTRY_MISSING = (
    "the pinned model registry is not importable here, so nothing says which "
    "files should be present or what they should hash to; the models extra "
    "installs it (pip install 'reachy-checks[models]')"
)


class GroundstationModelFiles:
    """A directory of model files, judged against the groundstation's registry."""

    def __init__(self, directory: Path | str) -> None:
        """Point the check at a directory.

        Args:
            directory: Where the build put the model files.
        """
        self._directory = Path(directory)

    @property
    def directory(self) -> Path:
        """Where this reads from.

        Returns:
            The directory, as given.
        """
        return self._directory

    def inspect(self) -> ModelFileReport:
        """Verify every registered model against the file on disk.

        Returns:
            What is present and unaltered, and one line per problem. A model
            that is absent, unreadable or hashes to something else is a
            problem; every model is examined, so one bad file does not hide
            the state of the others.
        """
        try:
            # Imported here rather than at module level. See the module
            # documentation: the groundstation is an optional extra, and
            # importing it at the top would pull its whole dependency tree into
            # every install of this package.
            from reachy_groundstation.models import (
                MODELS,
                ModelStore,
                ModelStoreError,
            )
        except ImportError:
            return ModelFileReport(
                directory=str(self._directory),
                problems=(REGISTRY_MISSING,),
            )
        store = ModelStore(self._directory)
        verified: list[str] = []
        problems: list[str] = []
        for model in MODELS:
            try:
                store.resolve(model)
            except (ModelStoreError, OSError) as error:
                problems.append(str(error))
                continue
            verified.append(model.name)
        return ModelFileReport(
            directory=str(self._directory),
            verified=tuple(verified),
            problems=tuple(problems),
        )
