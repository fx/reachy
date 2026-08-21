"""The model registry, the licence gate, the build-time fetch and the store.

Two of these are gates rather than tests of convenience, and both are written so
that the failing case is exercised rather than assumed.

The licence gate is a unit test over a Python literal, which is what lets it run
with no input or output at all. It checks the real registry and then checks a
registry holding a copyleft model, because a check that has only ever been seen
to pass is a check nobody knows fires.

The hash gate is groundstation REQ-024, and it is checked by handing the fetcher
bytes that are not the pinned ones and watching the build refuse them — including
that nothing partial is left where a later stage could mistake it for a verified
model.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from reachy_groundstation.models import (
    ALLOWED_LICENCES,
    FACE_DETECTION_YUNET,
    MODELS,
    ModelKind,
    ModelStore,
    ModelStoreError,
    digest_of,
    licence_problems,
    model_by_name,
)
from reachy_groundstation.models.fetch import (
    ModelFetchError,
    fetch,
    main,
)

if TYPE_CHECKING:
    from pathlib import Path

    from reachy_groundstation.models import Model

# A model with terms that would reach everyone who deploys the image. It exists
# only to be rejected: the predecessor's face model was Ultralytics-derived and
# AGPL-3.0, which is the licence this whole registry was built to keep out.
_COPYLEFT = replace(FACE_DETECTION_YUNET, name="copyleft_face", licence="AGPL-3.0")


def _payload(size: int = 32, fill: bytes = b"\x00") -> bytes:
    """Build some bytes to stand in for a model file.

    Args:
        size: How many bytes.
        fill: What to repeat.

    Returns:
        The bytes.
    """
    return fill * size


def _pinned(payload: bytes, source: str | None = None) -> Model:
    """Build a registry entry that pins exactly these bytes.

    Args:
        payload: The bytes the entry should describe.
        source: A retrieval location to use instead of the face model's.

    Returns:
        The record.
    """
    model = replace(
        FACE_DETECTION_YUNET,
        name="fixture_model",
        filename="fixture_model.onnx",
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )
    return model if source is None else replace(model, source=source)


#:= docs/specs/perception/index.md#req-032-detection-models-are-permissively-licensed
#:% Every model shipped in the published artifact MUST be redistributable under a
#:% licence that places no obligation on the licensing of the code that runs it.
def test_every_shipped_model_is_permissively_licensed() -> None:
    """The registry as it stands may ship, with no problems at all."""
    assert licence_problems() == ()
    assert {model.licence for model in MODELS} <= ALLOWED_LICENCES


def test_the_licence_gate_rejects_a_copyleft_model() -> None:
    """A gate nobody has watched fail is a gate that does not exist.

    This is the scenario the perception spec states: a candidate whose weights
    derive from a copyleft codebase is rejected regardless of its accuracy or
    speed — and the same weights under acceptable terms are not.
    """
    problems = licence_problems([_COPYLEFT])
    assert len(problems) == 1
    assert "AGPL-3.0" in problems[0]
    assert "copyleft_face" in problems[0]


@pytest.mark.parametrize(
    ("broken", "expected"),
    [
        (
            replace(FACE_DETECTION_YUNET, attribution=""),
            "no attribution recorded",
        ),
        (
            replace(FACE_DETECTION_YUNET, licence_url=""),
            "no licence location recorded",
        ),
        (
            replace(FACE_DETECTION_YUNET, source=""),
            "no retrieval location recorded",
        ),
        (
            replace(FACE_DETECTION_YUNET, sha256="abc123"),
            "is not a SHA-256 digest",
        ),
    ],
)
def test_the_licence_gate_rejects_an_unanswerable_record(
    broken: Model,
    expected: str,
) -> None:
    """A licence audit has to be answerable from the repository alone.

    Args:
        broken: A record missing one of the things an auditor would ask for.
        expected: What the reported problem should say.
    """
    problems = licence_problems([broken])
    assert any(expected in problem for problem in problems)


def test_the_licence_gate_rejects_a_model_registered_twice() -> None:
    """Two entries for one file are two answers to one licence question."""
    problems = licence_problems([FACE_DETECTION_YUNET, FACE_DETECTION_YUNET])
    assert any("registered more than once" in problem for problem in problems)
    assert any("registered twice" in problem for problem in problems)


#:= docs/specs/perception/index.md#req-033-model-licence-and-provenance-are-recorded-beside-the-model
#:% Each model MUST have a record naming its upstream source, its licence, and the
#:% retrieval location, stored alongside the pinned hash required by
#:% [groundstation REQ-024](../groundstation/index.md#req-024-model-provenance-is-recorded-and-verified).
def test_the_face_model_is_answerable_without_leaving_the_repository() -> None:
    """What it is and under what terms it ships, from the registry alone."""
    assert FACE_DETECTION_YUNET.licence == "MIT"
    assert FACE_DETECTION_YUNET.kind is ModelKind.FACE_DETECTOR
    assert "opencv_zoo" in FACE_DETECTION_YUNET.upstream
    assert FACE_DETECTION_YUNET.source.startswith("https://")
    # The retrieval location names an immutable revision rather than a branch,
    # so "fetch it again" and "fetch what was reviewed" are the same act.
    assert "2b8e922362946a0db67e861bae0f77826980effd" in FACE_DETECTION_YUNET.source


def test_no_gesture_model_is_registered() -> None:
    """The absence is the perception spec's decision, not an oversight.

    A gesture model appearing here without the negatives evaluation having been
    run against it is exactly the mistake the predecessor made, so the registry
    states the absence and this test pins it. Registering one means changing this
    test, in the pull request that reports what the evaluation measured.
    """
    assert [model.kind for model in MODELS] == [ModelKind.FACE_DETECTOR]


def test_a_model_is_found_by_name() -> None:
    """The registry is a lookup, not a list to be indexed into."""
    assert model_by_name("face_detection_yunet") is FACE_DETECTION_YUNET


def test_an_unregistered_name_says_what_is_registered() -> None:
    """The message is the whole value of the failure."""
    with pytest.raises(KeyError, match="face_detection_yunet"):
        model_by_name("gesture_classifier")


#:= docs/specs/groundstation/index.md#req-024-model-provenance-is-recorded-and-verified
#:% Every model file MUST be pinned by content hash, and the build MUST fail when a
#:% fetched file's hash does not match the pinned value.
@pytest.mark.filesystem
def test_the_build_fails_when_upstream_serves_different_bytes(tmp_path: Path) -> None:
    """The scenario the requirement states, with the substitution performed.

    Args:
        tmp_path: Where the fetch would have written.
    """
    model = _pinned(_payload())
    substituted = _payload(fill=b"\xff")

    with pytest.raises(ModelFetchError, match="hashes to"):
        fetch(model, tmp_path, lambda _url, _limit: substituted)

    assert not (tmp_path / model.filename).exists()
    # Nothing partial is left behind either: a later build stage that found one
    # would have no way to tell it from a verified model.
    assert list(tmp_path.iterdir()) == []


@pytest.mark.filesystem
def test_the_build_reports_a_truncated_download_as_truncated(tmp_path: Path) -> None:
    """A short read and a substitution are different faults and read differently.

    Args:
        tmp_path: Where the fetch would have written.
    """
    model = _pinned(_payload())
    with pytest.raises(ModelFetchError, match="returned 8 bytes"):
        fetch(model, tmp_path, lambda _url, _limit: _payload(size=8))


@pytest.mark.filesystem
def test_the_fetch_is_bounded_by_the_size_the_registry_pins(tmp_path: Path) -> None:
    """A mirror serving an endless body must not be held in memory in full.

    The bound is one byte past the pin, which is the smallest bound that still
    lets the size check report "too long" rather than "wrong digest".

    Args:
        tmp_path: Where the fetch would have written.
    """
    payload = _payload()
    model = _pinned(payload)
    asked: list[int] = []

    def _endless(url: str, limit: int) -> bytes:
        """Serve as much as the caller is willing to hold, and no less.

        Args:
            url: What was asked for, unused.
            limit: How much of it was asked for.

        Returns:
            Exactly that much, which is already more than the registry pins.
        """
        del url
        asked.append(limit)
        return b"\x00" * limit

    with pytest.raises(ModelFetchError, match="returned 33 bytes"):
        fetch(model, tmp_path, _endless)
    assert asked == [len(payload) + 1]


@pytest.mark.filesystem
def test_a_verified_fetch_writes_the_file(tmp_path: Path) -> None:
    """The ordinary path, so the failing ones above are failures of something.

    Args:
        tmp_path: Where to write.
    """
    payload = _payload()
    model = _pinned(payload)
    path = fetch(model, tmp_path, lambda _url, _limit: payload)
    assert path.read_bytes() == payload
    assert digest_of(path) == model.sha256


@pytest.mark.filesystem
def test_a_model_already_in_place_is_not_fetched_again(tmp_path: Path) -> None:
    """A rebuilt layer costs nothing, and neither does a second `just models`.

    Args:
        tmp_path: Where to write.
    """
    payload = _payload()
    model = _pinned(payload)
    fetch(model, tmp_path, lambda _url, _limit: payload)

    def _refuse(url: str, limit: int) -> bytes:
        """Fail the test if anything is fetched.

        Args:
            url: What was asked for.
            limit: How much of it was asked for, unused.

        Returns:
            Never.

        Raises:
            AssertionError: Always.
        """
        del limit
        message = f"fetched {url} for a file already verified"
        raise AssertionError(message)

    assert fetch(model, tmp_path, _refuse).read_bytes() == payload


@pytest.mark.filesystem
def test_a_file_in_place_with_the_wrong_bytes_is_fetched_again(
    tmp_path: Path,
) -> None:
    """Already in place is not already right, and the digest is what decides.

    Args:
        tmp_path: Where to write.
    """
    payload = _payload()
    model = _pinned(payload)
    (tmp_path / model.filename).write_bytes(_payload(fill=b"\xff"))
    assert fetch(model, tmp_path, lambda _url, _limit: payload).read_bytes() == payload


@pytest.mark.filesystem
def test_fetching_refuses_a_source_that_is_not_https(tmp_path: Path) -> None:
    """A digest checked over plain HTTP is a digest whoever is in the way chose.

    Args:
        tmp_path: Where the fetch would have written.
    """
    model = _pinned(_payload(), "http://example.invalid/model.onnx")
    # The real fetcher, reached through the default, because what is under test
    # is that it refuses the scheme before it opens anything — the suite runs
    # with sockets disabled, so a fetcher that got as far as connecting would
    # fail for a different reason and prove nothing.
    with pytest.raises(ModelFetchError, match="refusing to fetch"):
        fetch(model, tmp_path)


@pytest.mark.filesystem
def test_a_fetcher_that_raises_is_reported_as_a_fetch_failure(
    tmp_path: Path,
) -> None:
    """A network fault names the model and the URL rather than unwinding raw.

    Args:
        tmp_path: Where the fetch would have written.
    """
    model = _pinned(_payload())

    def _explode(url: str, limit: int) -> bytes:
        """Fail the way a network does.

        Args:
            url: What was asked for, unused.
            limit: How much of it was asked for, unused.

        Returns:
            Never.

        Raises:
            OSError: Always.
        """
        del url, limit
        message = "name resolution failed"
        raise OSError(message)

    with pytest.raises(ModelFetchError, match="could not retrieve"):
        fetch(model, tmp_path, _explode)


@pytest.mark.filesystem
def test_the_fetch_entry_point_reports_a_mismatch_and_exits_non_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """What a build actually sees when upstream changes underneath it.

    Args:
        tmp_path: Where the fetch would have written.
        monkeypatch: Used to substitute the retrieval, so no socket is opened.
        capsys: Used to read what the failure said.
    """
    # The right number of bytes, and the wrong ones: the size check would
    # otherwise fire first and this test would never reach the digest.
    monkeypatch.setattr(
        "reachy_groundstation.models.fetch._https_get",
        lambda _url, _limit: b"\x00" * FACE_DETECTION_YUNET.size_bytes,
    )
    assert main([str(tmp_path)]) == 1
    assert "hashes to" in capsys.readouterr().err


@pytest.mark.filesystem
def test_the_fetch_entry_point_reports_what_it_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The successful run says which files it stands behind.

    Args:
        tmp_path: Where to write.
        monkeypatch: Used to substitute the retrieval and the registry.
        capsys: Used to read what it reported.
    """
    payload = _payload()
    model = _pinned(payload)
    monkeypatch.setattr("reachy_groundstation.models.fetch.MODELS", (model,))
    monkeypatch.setattr(
        "reachy_groundstation.models.fetch._https_get",
        lambda _url, _limit: payload,
    )
    assert main([str(tmp_path)]) == 0
    assert model.filename in capsys.readouterr().out


#:= docs/specs/groundstation/index.md#req-023-model-files-are-present-in-the-image
#:% The service MUST load every model from a file already present in its deployed
#:% artifact, and MUST NOT fetch model weights over the network at run time.
@pytest.mark.filesystem
def test_the_store_resolves_a_file_the_build_put_in_place(tmp_path: Path) -> None:
    """The run-time half reads; it never reaches for anything.

    Args:
        tmp_path: The artifact's model directory.
    """
    payload = _payload()
    model = _pinned(payload)
    (tmp_path / model.filename).write_bytes(payload)
    assert ModelStore(tmp_path).resolve(model) == tmp_path / model.filename


@pytest.mark.filesystem
def test_the_store_says_where_it_looked_when_the_model_is_absent(
    tmp_path: Path,
) -> None:
    """The likeliest cause is a service pointed at the wrong directory.

    Args:
        tmp_path: An empty model directory.
    """
    with pytest.raises(ModelStoreError, match="MODELS_DIR"):
        ModelStore(tmp_path).resolve(_pinned(_payload()))


@pytest.mark.filesystem
def test_the_store_refuses_a_file_that_changed_since_the_build(
    tmp_path: Path,
) -> None:
    """A corrupted layer or a hand-swapped file is caught before readiness.

    Args:
        tmp_path: The artifact's model directory.
    """
    model = _pinned(_payload())
    (tmp_path / model.filename).write_bytes(_payload(fill=b"\xff"))
    with pytest.raises(ModelStoreError, match="not the weights"):
        ModelStore(tmp_path).resolve(model)


@pytest.mark.filesystem
def test_the_store_names_a_model_without_looking_for_it(tmp_path: Path) -> None:
    """Where a model would be is a question that does not need it to exist.

    Args:
        tmp_path: A model directory that stays empty.
    """
    store = ModelStore(tmp_path)
    assert store.directory == tmp_path
    assert store.path_for(FACE_DETECTION_YUNET).name == FACE_DETECTION_YUNET.filename
