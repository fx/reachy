"""Every model this service can load, with where it came from and its terms.

This is the licence gate, and it is a gate rather than a note. A model listed
here with terms outside `ALLOWED_LICENCES` fails an ordinary unit test over this
module, so a model whose licence would reach everyone who deploys the published
image breaks the build instead of resting on somebody remembering to check —
which is what perception REQ-032 asks for and REQ-033 asks be answerable without
network access or archaeology.

**No weights are committed.** Each record pins the bytes by digest and says
exactly where to fetch them; `fetch.py` beside this file does the fetching at
build time and refuses anything whose digest does not match, and `store.py`
resolves a record to the file already in the artifact at run time. The service
never reaches the network for a model.

It is Python rather than TOML for the same reason the satellite's asset registry
is: a registry that is already a Python literal lets the licence check be an
import-only unit test, and this repository requires that a unit test perform no
input or output. The shape deliberately matches
`reachy_mini_ha_satellite.assets.registry`, so the two can be unified later if
that is ever worth the dependency between them. It does not import it, and
nothing here depends on that package.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "ALLOWED_LICENCES",
    "FACE_DETECTION_YUNET",
    "MODELS",
    "Model",
    "ModelKind",
    "licence_problems",
    "model_by_name",
]

# What a SHA-256 digest looks like written down: sixty-four lowercase hexadecimal
# characters. The case matters as much as the length — `hashlib` renders lowercase
# hex, so an upper-cased digest of the right file never matches and the build
# fails reporting that upstream is serving different bytes, which is both wrong
# and the most expensive way to learn about a typo.
_DIGEST: Final = re.compile(r"[0-9a-f]{64}")


class ModelKind(StrEnum):
    """What a model does, which decides which capability loads it."""

    FACE_DETECTOR = "face-detector"
    """Locates faces in a frame."""

    HAND_DETECTOR = "hand-detector"
    """Locates hands in a frame, for the classifier stage to crop to."""

    GESTURE_CLASSIFIER = "gesture-classifier"
    """Names the hand signal in a cropped hand region."""


#:= docs/specs/perception/index.md#req-032-detection-models-are-permissively-licensed
#:% Every model shipped in the published artifact MUST be redistributable under a
#:% licence that places no obligation on the licensing of the code that runs it.
#
# Licences a model may ship under. Every one of them permits redistribution
# inside a public container image, with attribution and without a copyleft
# obligation reaching the service that runs the model or anyone who deploys it.
# AGPL-3.0 and GPL-3.0 are absent on purpose and are why this repository swapped
# its face model: see the perception spec's decision records. Adding to this set
# is a licensing decision, not a formality — make it in review, not in passing.
ALLOWED_LICENCES: Final[frozenset[str]] = frozenset(
    {
        "Apache-2.0",
        "BSD-3-Clause",
        "CC-BY-4.0",
        "CC0-1.0",
        "MIT",
    },
)


#:= docs/specs/perception/index.md#req-033-model-licence-and-provenance-are-recorded-beside-the-model
#:% Each model MUST have a record naming its upstream source, its licence, and the
#:% retrieval location, stored alongside the pinned hash required by
#:% [groundstation REQ-024](../groundstation/index.md#req-024-model-provenance-is-recorded-and-verified).
@dataclass(frozen=True, kw_only=True)
class Model:
    """One model file, and everything needed to defend shipping it."""

    name: str
    """How this repository refers to it. Stable across upstream renames."""

    filename: str
    """What the file is called inside the model directory."""

    kind: ModelKind
    """What it does."""

    licence: str
    """SPDX identifier. Checked against `ALLOWED_LICENCES` by a unit test."""

    licence_url: str
    """Where those terms are stated, at the pinned revision."""

    attribution: str
    """The credit the licence requires be carried with the file."""

    upstream: str
    """The project the weights originate from, whoever redistributes them."""

    source: str
    """The exact URL the build fetches, pinned to an immutable revision."""

    sha256: str
    """Digest of the bytes, so a substitution upstream fails the build."""

    size_bytes: int
    """How large the file is, so a truncated fetch is named as truncated."""


_YUNET_REVISION: Final = "2b8e922362946a0db67e861bae0f77826980effd"
_YUNET_REPO: Final = (
    "https://huggingface.co/pollen-robotics/face_detection_yunet_2026may"
)

# The detector the Reachy Mini SDK itself uses, which is what makes the parity
# test in perception REQ-036 a comparison against something already maintained
# and already running on the target hardware — and what removes the accuracy
# discontinuity between detecting on the robot and detecting off it. An
# unmodified redistribution of the OpenCV Zoo model, dynamic-input-shape
# variant; the weights are identical to the fixed-shape 2023mar variant.
FACE_DETECTION_YUNET: Final = Model(
    name="face_detection_yunet",
    filename="face_detection_yunet_2026may.onnx",
    kind=ModelKind.FACE_DETECTOR,
    licence="MIT",
    licence_url=f"{_YUNET_REPO}/blob/{_YUNET_REVISION}/README.md",
    attribution=(
        "YuNet face detection by Wei Wu, Hanyang Peng and Shiqi Yu, "
        "distributed under the MIT licence through the OpenCV Zoo "
        "(https://github.com/opencv/opencv_zoo/tree/main/models/"
        "face_detection_yunet)"
    ),
    upstream="https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet",
    source=(
        f"{_YUNET_REPO}/resolve/{_YUNET_REVISION}/face_detection_yunet_2026may.onnx"
    ),
    sha256="ebafce4e3c118d6554634be5c27ab333b4c047a9a8c3faf1d7cf93101c22f0f0",
    size_bytes=229738,
)

# Everything the build fetches and the artifact carries.
#
# There is no gesture model here, and its absence is the decision the perception
# spec records rather than an omission. The predecessor's classifier reported
# hand signals at 0.9 confidence in an empty room and its weights never had a
# provenance check, so shipping it would make a known defect the out-of-box
# behaviour and would put weights of unknown terms inside a public image. The
# gesture capability is therefore built with no model wired, switched off by
# default, and measured by the negatives evaluation the test suite runs — which
# is what lets a candidate be compared rather than argued about when one is
# proposed.
MODELS: Final[tuple[Model, ...]] = (FACE_DETECTION_YUNET,)


def licence_problems(models: Sequence[Model] = MODELS) -> tuple[str, ...]:
    """List every reason a registry's models may not ship, or find none.

    This is the licence gate's judgement, separated from the registry it judges
    so that a test can run it over both — over the real registry, where it must
    find nothing, and over a registry holding a copyleft model, where it must
    find it. A rule nobody has watched fail is a rule that does not exist.

    Args:
        models: The registry to judge. Defaults to the real one.

    Returns:
        One message per problem, in the order the models are listed, and empty
        when every model may ship.
    """
    problems: list[str] = []
    seen_names: set[str] = set()
    seen_files: set[str] = set()
    for model in models:
        if model.licence not in ALLOWED_LICENCES:
            problems.append(
                f"{model.name}: licence {model.licence!r} is not in the "
                f"allowlist {sorted(ALLOWED_LICENCES)} — shipping it would put "
                f"its terms on this service and on everyone who deploys it",
            )
        if not model.attribution:
            problems.append(f"{model.name}: no attribution recorded")
        if not model.licence_url:
            problems.append(f"{model.name}: no licence location recorded")
        if not model.source:
            problems.append(f"{model.name}: no retrieval location recorded")
        if not _DIGEST.fullmatch(model.sha256):
            problems.append(
                f"{model.name}: {model.sha256!r} is not a SHA-256 digest, so "
                f"nothing pins the bytes",
            )
        if model.name in seen_names:
            problems.append(f"{model.name}: registered more than once")
        if model.filename in seen_files:
            problems.append(f"{model.name}: {model.filename} is registered twice")
        seen_names.add(model.name)
        seen_files.add(model.filename)
    return tuple(problems)


def model_by_name(name: str) -> Model:
    """Look a model up by the name this repository refers to it as.

    Args:
        name: The model's `name`.

    Returns:
        The record.

    Raises:
        KeyError: If nothing is registered under that name.
    """
    for model in MODELS:
        if model.name == name:
            return model
    message = (
        f"no model named {name!r} is registered; known: {[m.name for m in MODELS]}"
    )
    raise KeyError(message)
