"""Verifying model files against the digests the groundstation registry pins.

The registry and the hashing are the groundstation's, and this file proves that
the check uses them rather than a second copy: the passing case is built by
hashing bytes and registering a model that pins that digest, so a check that
had its own idea of what to hash would not agree with it.

`pyfakefs` performs no real input or output, so these are unit tests and carry
no marker — see `REVIEW.md`.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Final

import pytest
from pyfakefs.fake_filesystem import FakeFilesystem

import reachy_groundstation.models as registry
from reachy_checks import REGISTRY_MISSING, GroundstationModelFiles
from reachy_groundstation.models import MODELS, Model, ModelKind

DIRECTORY: Final = "/opt/reachy/models"
CONTENT: Final = b"not real weights, but real bytes to hash"
FILENAME: Final = "example_detector.onnx"


def _registered(digest: str) -> tuple[Model, ...]:
    """Build a one-model registry pinning a digest.

    The record is the groundstation's own `Model`, imported here rather than
    imitated, so a field added to it fails this test rather than being quietly
    unrepresented.

    Args:
        digest: What the model's bytes are supposed to hash to.

    Returns:
        A registry holding one model.
    """
    return (
        Model(
            name="example_detector",
            filename=FILENAME,
            kind=ModelKind.FACE_DETECTOR,
            licence="MIT",
            licence_url="https://example.invalid/licence",
            attribution="An example, drawn for this test",
            upstream="https://example.invalid/upstream",
            source="https://example.invalid/source",
            sha256=digest,
            size_bytes=len(CONTENT),
        ),
    )


def test_a_present_and_matching_file_is_verified(
    fs: FakeFilesystem,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The passing case, built by hashing the bytes the check will read."""
    fs.create_file(f"{DIRECTORY}/{FILENAME}", contents=CONTENT)
    monkeypatch.setattr(
        registry,
        "MODELS",
        _registered(hashlib.sha256(CONTENT).hexdigest()),
    )

    report = GroundstationModelFiles(DIRECTORY).inspect()

    assert report.problems == ()
    assert report.verified == ("example_detector",)
    assert report.directory == DIRECTORY


def test_a_missing_file_is_a_problem_naming_the_directory(
    fs: FakeFilesystem,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The likeliest cause is a service pointed at the wrong directory."""
    fs.create_dir(DIRECTORY)
    monkeypatch.setattr(
        registry,
        "MODELS",
        _registered(hashlib.sha256(CONTENT).hexdigest()),
    )

    report = GroundstationModelFiles(DIRECTORY).inspect()

    assert report.verified == ()
    assert len(report.problems) == 1
    assert DIRECTORY in report.problems[0]


def test_a_file_that_hashes_to_something_else_is_a_problem(
    fs: FakeFilesystem,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """These are not the weights the build was reviewed with."""
    fs.create_file(f"{DIRECTORY}/{FILENAME}", contents=b"different bytes entirely")
    monkeypatch.setattr(
        registry,
        "MODELS",
        _registered(hashlib.sha256(CONTENT).hexdigest()),
    )

    report = GroundstationModelFiles(DIRECTORY).inspect()

    assert report.verified == ()
    assert "hashes to" in report.problems[0]


def test_one_bad_file_does_not_hide_the_state_of_the_others(
    fs: FakeFilesystem,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every model is examined, so a report is about the directory, not its first entry."""
    good = _registered(hashlib.sha256(CONTENT).hexdigest())
    bad = _registered("0" * 64)
    fs.create_file(f"{DIRECTORY}/{FILENAME}", contents=CONTENT)
    monkeypatch.setattr(registry, "MODELS", (*bad, *good))

    report = GroundstationModelFiles(DIRECTORY).inspect()

    assert len(report.problems) == 1
    assert report.verified == ("example_detector",)


def test_the_real_registry_is_what_is_verified_against(fs: FakeFilesystem) -> None:
    """Nothing patched: the check reads the models the groundstation actually pins."""
    fs.create_dir(DIRECTORY)

    report = GroundstationModelFiles(DIRECTORY).inspect()

    assert len(report.problems) == len(MODELS)
    for model in MODELS:
        assert any(model.name in problem for problem in report.problems)


class _RefusingFinder:
    """A meta-path finder that raises when asked for one module.

    The import machinery is driven for real rather than a loader being injected
    into the class under test, because what is being checked is how a *real*
    import failure is classified — and a seam would only ever raise whatever a
    test told it to.
    """

    def __init__(self, target: str, error: BaseException) -> None:
        """Describe which import fails and how.

        Args:
            target: The module to refuse.
            error: What to raise for it.
        """
        self._target = target
        self._error = error

    def find_spec(
        self,
        fullname: str,
        path: object = None,
        target: object = None,
    ) -> None:
        """Refuse the target module and defer on everything else.

        Args:
            fullname: The module being imported.
            path: The parent package's search path, unused.
            target: The module being reloaded, unused.

        Returns:
            `None` for anything but the target, which lets the finders after
            this one answer.

        Raises:
            BaseException: Whatever this finder was told to raise, when the
                target is asked for.
        """
        del path, target
        if fullname == self._target:
            raise self._error
        return


def _importing_raises(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> None:
    """Make importing the model registry fail with a given error.

    Args:
        monkeypatch: Used to install the finder and to forget the already
            imported module, both undone when the test ends.
        error: What the import should raise.
    """
    monkeypatch.delitem(sys.modules, "reachy_groundstation.models", raising=False)
    monkeypatch.setattr(
        sys,
        "meta_path",
        [_RefusingFinder("reachy_groundstation.models", error), *sys.meta_path],
    )


def test_a_machine_without_the_groundstation_reports_an_absent_prerequisite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registry is an optional extra, and its absence is neither a crash nor a fault.

    It goes in `unavailable` and NOT in `problems`, which is what makes the
    check skip rather than fail. A control machine carrying the checks but not
    the service has nothing to verify against; calling that broken would fail
    every provisioning verification run on a machine that was never meant to
    carry the groundstation package.
    """
    # Binding the name to None is what makes an import of it raise ImportError,
    # which is the same thing a machine that never installed the extra does.
    monkeypatch.setitem(sys.modules, "reachy_groundstation.models", None)

    report = GroundstationModelFiles(DIRECTORY).inspect()

    assert report.unavailable == REGISTRY_MISSING
    assert report.problems == ()
    assert report.verified == ()


def test_a_broken_transitive_dependency_is_a_fault_and_not_an_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trap in the type: `ModuleNotFoundError` covers both facts.

    A groundstation that is installed and whose own dependency is missing must
    not be reported as a groundstation that was never installed. Reporting the
    wrong link is the worst outcome available to a tool whose job is naming the
    failing one.
    """
    _importing_raises(
        monkeypatch,
        ModuleNotFoundError("No module named 'numpy'", name="numpy"),
    )

    report = GroundstationModelFiles(DIRECTORY).inspect()

    assert report.unavailable == ""
    assert len(report.problems) == 1
    assert "installed here but could not be imported" in report.problems[0]
    # The cause, so the operator sees what actually broke.
    assert "numpy" in report.problems[0]


def test_a_registry_that_refuses_to_load_is_a_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An `ImportError` that is not a missing module is a broken installation."""
    _importing_raises(
        monkeypatch,
        ImportError("cannot import name 'MODELS' from partially initialized module"),
    )

    report = GroundstationModelFiles(DIRECTORY).inspect()

    assert report.unavailable == ""
    assert "partially initialized module" in report.problems[0]


def test_a_missing_parent_package_is_still_an_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing of the groundstation installed reports as `reachy_groundstation`."""
    _importing_raises(
        monkeypatch,
        ModuleNotFoundError(
            "No module named 'reachy_groundstation'",
            name="reachy_groundstation",
        ),
    )

    report = GroundstationModelFiles(DIRECTORY).inspect()

    assert report.unavailable == REGISTRY_MISSING
    assert report.problems == ()


def test_an_import_failure_with_no_name_is_treated_as_a_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing to judge by, so it reports something rather than staying quiet."""
    _importing_raises(monkeypatch, ModuleNotFoundError("no name on this one"))

    report = GroundstationModelFiles(DIRECTORY).inspect()

    assert report.unavailable == ""
    assert report.problems


def test_a_machine_with_the_registry_reports_nothing_unavailable(
    fs: FakeFilesystem,
) -> None:
    """The other side of the line: the registry was consulted, so the fault is real."""
    fs.create_dir(DIRECTORY)

    report = GroundstationModelFiles(DIRECTORY).inspect()

    assert report.unavailable == ""
    assert report.problems


def test_the_directory_is_kept_as_it_was_given() -> None:
    """A consumer that passed a path gets a path back, not a re-rooted one."""
    assert GroundstationModelFiles(Path(DIRECTORY)).directory == Path(DIRECTORY)
