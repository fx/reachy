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

**Absent and broken are different answers, and telling them apart is the whole
job of the classification below.** A registry that is not installed is a
prerequisite that is not there, and the check skips. A registry that is
installed and raises on import is a fault, and the check fails naming what
raised. Collapsing the second into the first is the more expensive mistake of
the two: it is silent, and it points the operator at the wrong link.
"""

from __future__ import annotations

from pathlib import Path

from reachy_checks.ports import ModelFileReport

__all__ = ["REGISTRY_MISSING", "GroundstationModelFiles"]

# Why the check could not run, when the groundstation package is not installed.
# It goes in `unavailable` rather than in `problems`, and the check is skipped
# rather than failed: an absent optional dependency is a prerequisite that is
# not there, not a fault in the files. A machine carrying the checks but not
# the service — a control machine running the provisioning verification, say —
# is not in an error state, and a diagnosis that told it otherwise would be
# noise on every run.
REGISTRY_MISSING = (
    "the pinned model registry is not installed here, so nothing says which "
    "files should be present or what they should hash to; the models extra "
    "installs it (pip install 'reachy-checks[models]')"
)

# The module the check needs. Named once, because deciding whether an import
# failure is this module's absence means comparing against it.
_REGISTRY_MODULE = "reachy_groundstation.models"


def _is_absent(error: ModuleNotFoundError) -> bool:
    """Say whether an import failure means the registry itself is not installed.

    `ModuleNotFoundError` is raised both when the module asked for is missing
    and when something *it* imports is missing, and the two are opposite facts
    about the machine. Catching the type alone would report a groundstation
    whose transitive dependency is broken as a groundstation that was never
    installed, and the operator would go looking somewhere else entirely — the
    worst outcome available to a tool whose job is naming the failing link.

    So the name is what decides. An error naming the registry, or one of the
    packages it sits inside, is the module genuinely not being here; an error
    naming anything else is raised from inside a registry that is present.

    Args:
        error: What the import raised.

    Returns:
        True when the registry is absent rather than broken.
    """
    name = error.name
    if name is None:
        # No name to judge by, so this cannot be shown to be absence. Treated
        # as a fault, which is the direction that reports something rather than
        # the direction that stays quiet.
        return False
    return name == _REGISTRY_MODULE or _REGISTRY_MODULE.startswith(f"{name}.")


def _broken(error: ImportError) -> str:
    """Say that the registry is installed and would not load.

    Args:
        error: What the import raised.

    Returns:
        One line naming the underlying failure, because what actually broke is
        the only part an operator can act on.
    """
    return (
        f"the pinned model registry is installed here but could not be "
        f"imported, so the model files were not checked against anything: "
        f"{type(error).__name__}: {error}"
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
            the state of the others. A machine with no registry to judge
            against reports that in `unavailable` and finds no problems,
            because it looked at nothing — but a machine whose registry is
            *there and broken* reports a problem, because that is a fault and
            calling it an absent prerequisite would send the operator to look
            somewhere else entirely.

            An exception that is not an `ImportError` at all — a module that
            raises `RuntimeError` while executing, say — is deliberately not
            caught here. The runner turns anything a probe raises into a failed
            result naming it, which is the outcome this would have produced,
            and catching more here would widen exactly the net this method
            narrowed.
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
        except ModuleNotFoundError as error:
            if not _is_absent(error):
                # The package is installed and something *it* imports is not.
                # That is a broken installation, not a missing one.
                return ModelFileReport(
                    directory=str(self._directory),
                    problems=(_broken(error),),
                )
            return ModelFileReport(
                directory=str(self._directory),
                unavailable=REGISTRY_MISSING,
            )
        except ImportError as error:
            # The module was found and refused to load — a circular import, a
            # name that has gone from it, a partially installed distribution.
            return ModelFileReport(
                directory=str(self._directory),
                problems=(_broken(error),),
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
