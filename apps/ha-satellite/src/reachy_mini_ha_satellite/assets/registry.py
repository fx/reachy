"""Every asset that ships in this wheel, with where it came from and its terms.

This is the licence gate. An asset that is not listed here fails
`just check-assets`, and an asset listed with terms outside `ALLOWED_LICENCES`
fails the unit test over this registry — so an asset with unacceptable terms
breaks the build rather than shipping in a wheel to somebody else's robot.

It is Python rather than TOML on purpose. This repository requires that a unit
test perform no input or output, and a registry that is already a Python literal
lets the licence check be an ordinary, import-only test. The shape deliberately
matches the model registry that change 0005 gives the groundstation, so the two
can be unified later if that is ever worth the dependency; today it is not, and
this change does not depend on 0005.

`verify.py` beside this file is the other half: it walks the assets directory and
checks that what is on disk is exactly what is recorded here, unmodified.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

# The commit in the upstream repository every asset below was taken at. Same
# commit as the vendored code in `../esphome/`, and for the same reason: an
# asset and the code that loads it should be auditable as one snapshot.
UPSTREAM_COMMIT = "d1f5761f7591495794734e79c98f7199100153c0"
UPSTREAM_URL = "https://github.com/OHF-Voice/linux-voice-assistant"


class AssetKind(StrEnum):
    """What an asset is, which decides who loads it."""

    WAKE_WORD_MODEL = "wake-word-model"
    """A wake-word model, or the JSON configuration that describes one."""

    SOUND = "sound"
    """An audio file the satellite plays to signal a state change."""


#: Licences an asset may ship under. Everything here permits redistribution in a
#: binary wheel from a public repository, with attribution and without a
#: copyleft obligation reaching the rest of this repository. Adding to this set
#: is a licensing decision, not a formality: make it in review, not in passing.
ALLOWED_LICENCES: frozenset[str] = frozenset(
    {
        "Apache-2.0",
        "BSD-3-Clause",
        "CC-BY-4.0",
        "CC0-1.0",
        "MIT",
    }
)


@dataclass(frozen=True, kw_only=True)
class Asset:
    """One shipped file, and everything needed to defend shipping it."""

    path: str
    """Location relative to this directory, with forward slashes."""

    kind: AssetKind
    """What the file is."""

    licence: str
    """SPDX identifier. Checked against `ALLOWED_LICENCES`."""

    licence_url: str
    """Where those terms are stated."""

    attribution: str
    """The credit the licence requires be carried with the file."""

    source: str
    """Where the file was taken from, precisely enough to fetch it again."""

    sha256: str
    """Digest of the file as committed, so a silent substitution is visible."""


def _upstream(path: str) -> str:
    """Describe a file taken from the vendored upstream at `UPSTREAM_COMMIT`."""
    return f"{UPSTREAM_URL}/blob/{UPSTREAM_COMMIT}/{path}"


_MICRO_WAKE_WORD_ATTRIBUTION = (
    "microWakeWord models by Kevin Ahrendt (https://www.kevinahrendt.com/), "
    "distributed in the Apache-2.0 Linux Voice Assistant repository; "
    "model project https://github.com/kahrendt/microWakeWord"
)

_VOICE_PE_SOUNDS_ATTRIBUTION = (
    "Home Assistant Voice Preview Edition Sounds (c) 2024 by Clayton Charles "
    "Tapp (https://www.cctaudio.com/), licensed under CC BY 4.0; see "
    "sounds/LICENSE.md"
)

_CC_BY_4_0 = "https://creativecommons.org/licenses/by/4.0/"
_APACHE_2_0 = f"{UPSTREAM_URL}/blob/{UPSTREAM_COMMIT}/LICENSE.md"


def _wake_word(path: str, sha256: str) -> Asset:
    return Asset(
        path=f"wakewords/{path}",
        kind=AssetKind.WAKE_WORD_MODEL,
        licence="Apache-2.0",
        licence_url=_APACHE_2_0,
        attribution=_MICRO_WAKE_WORD_ATTRIBUTION,
        source=_upstream(f"wakewords/{path}"),
        sha256=sha256,
    )


def _sound(path: str, sha256: str) -> Asset:
    return Asset(
        path=f"sounds/{path}",
        kind=AssetKind.SOUND,
        licence="CC-BY-4.0",
        licence_url=_CC_BY_4_0,
        attribution=_VOICE_PE_SOUNDS_ATTRIBUTION,
        source=_upstream(f"sounds/{path}"),
        sha256=sha256,
    )


#: The shipped set. Wake words are the smallest set that makes the application
#: usable: the default wake word and the stop word the protocol requires. Home
#: Assistant can add more at run time — the vendored protocol downloads an
#: external wake word on demand — and anything downloaded is not shipped, so it
#: is not this registry's business.
ASSETS: tuple[Asset, ...] = (
    _wake_word(
        "okay_nabu.json",
        "f8026e2ce93a0855ca23483036fd2d74ee924a2588d9ea6a6c6c7478fbf4be57",
    ),
    _wake_word(
        "okay_nabu.tflite",
        "d89128c4d16a72de429119fb2254ce46649553c2a24f5dd840175c80d7b9d094",
    ),
    _wake_word(
        "stop.json",
        "bd13aeb1b83852649dc4fb6135cb160ff68716d14612b06f6a405342c57447aa",
    ),
    _wake_word(
        "stop.tflite",
        "020ef80d522cb09169a866f3aeeb58f2ad4045461937e78e8c806df29ff61eea",
    ),
    _sound(
        "button_double_press.flac",
        "2cda3d78ccce5b522fb671092a9e07d29b224b6e861fb6c5fae2d555b6a77021",
    ),
    _sound(
        "button_long_press.flac",
        "7eb8baa6de8ed087bdea04fa23d989e5c4459120338f98929d4e8a20207f1f50",
    ),
    _sound(
        "button_triple_press.flac",
        "9ccb4a79a00509dc84e901565e42a510f02deef8082dd17da13a43d493014728",
    ),
    _sound(
        "mute_switch_off.flac",
        "b0ce950b316ad35ece4ffcba7a7978b1bb701f7f527d69e2d273e06a751642cc",
    ),
    _sound(
        "mute_switch_on.flac",
        "536f3daa7e41ae0789f9309b42a980eb6cccb36a16f1b92d79112129579b9fde",
    ),
    _sound(
        "processing.wav",
        "bc5c914bfa860a77fa9d88ac2d96601adfede578cf146637ec98b5688911a951",
    ),
    _sound(
        "start_listening_button.flac",
        "a33ed44b29da0de31cbc8d503643a1a0c5f9f22df739562880ee7c5cf81da9c4",
    ),
    _sound(
        "timer_finished.flac",
        "e9ddba38c4a993fd5a218ec7d04d7a2824b995a9dbb6cb0d8599d7a0ac6a56ee",
    ),
    _sound(
        "wake_word_triggered.flac",
        "5c26cad33931670f2b62db5d2ed35c562b162e819ade38e646680065ad1b055f",
    ),
)

#: Every file under this directory that is not a shipped asset, named exactly.
#:
#: Four of them are this package's own code and documentation. The other two are
#: licence text, and not optional: CC BY 4.0 requires its notice travel with the
#: sounds, and Apache-2.0 requires a copy of the licence reach everyone the models
#: reach, which is everyone who installs the wheel.
#:
#: This is a list and deliberately not a rule. A rule — "anything named LICENSE",
#: "anything outside the asset directories" — can be satisfied by a file chosen to
#: satisfy it, so exempting an awkward asset would be cheaper than answering the
#: licence question about it, and it would still ship. A list can only be extended
#: by editing it, and a unit test pins it to its own copy so that edit cannot
#: happen quietly: adding an entry means changing both, in one pull request, where
#: a reviewer decides whether the file really is not an asset.
UNREGISTERED: frozenset[str] = frozenset(
    {
        "NOTICE.md",
        "__init__.py",
        "registry.py",
        "sounds/LICENSE.md",
        "verify.py",
        "wakewords/LICENSE",
    }
)


def assets_dir() -> Path:
    """Absolute path of the directory the registry's paths are relative to."""
    return Path(__file__).parent
