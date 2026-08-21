"""The golden fixture corpus and the loader every consumer reads it through.

A fixture is a file of bytes, committed beside this module in `golden/`. It is
deliberately not a factory function: a factory shared between the side that
produces a message and the side that consumes it can be wrong in the same way
for both, and agreeing with itself is exactly the drift the corpus exists to
catch. A file agrees with nobody. It is a third party to both sides.

The loader lives here rather than in each consumer's test suite for the same
reason. Three components exercise these bytes — the robot app, the groundstation
and `reachyctl probe` — and three loaders would be three chances to normalise,
re-indent or re-order something on the way in, at which point the corpus stops
pinning the wire format and starts pinning whatever each loader does to it.

Reading a file is input, so the tests that drive this module are not unit tests
and do not pretend to be: they carry the `filesystem` marker declared in the
root `pyproject.toml`.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from reachy_contracts.session import (
    FrameHeader,
    ResultEnvelope,
    SessionAgreement,
    SessionClose,
    SessionError,
    SessionOffer,
)
from reachy_contracts.values import FaceDetections, GestureDetections, WireModel

if TYPE_CHECKING:
    from collections.abc import Mapping
    from importlib.resources.abc import Traversable

__all__ = [
    "FIXTURES",
    "Fixture",
    "fixture_bytes",
    "fixture_for",
    "golden_file_names",
    "load_fixture",
    "round_trip",
]

_PACKAGE: Final = "reachy_contracts"
_GOLDEN: Final = "golden"


@dataclass(frozen=True, slots=True)
class Fixture:
    """One golden fixture and the message type it pins.

    Attributes:
        name: The file's name within the `golden/` directory.
        model: The message type these bytes are the wire form of.
        summary: One line describing what this fixture pins.
    """

    name: str
    model: type[WireModel]
    summary: str


#:= docs/specs/robot-link/index.md#req-020-the-wire-format-is-pinned-by-shared-fixtures
#:% Every message type MUST have a golden fixture in the shared contracts package,
#:% and both the producing and the consuming implementation MUST be verified against
#:% that same fixture.
FIXTURES: Final[tuple[Fixture, ...]] = (
    Fixture(
        "session-offer.json",
        SessionOffer,
        "a client presenting its credential and offering two capabilities",
    ),
    Fixture(
        "session-agreement.json",
        SessionAgreement,
        "the agreed set, one capability short of what was offered",
    ),
    Fixture(
        "frame-header.json",
        FrameHeader,
        "a frame's sequence number and its opaque capture token",
    ),
    Fixture(
        "face-result.json",
        ResultEnvelope[FaceDetections],
        "two faces answering one frame, at normalised coordinates",
    ),
    Fixture(
        "empty-face-result.json",
        ResultEnvelope[FaceDetections],
        "a successful result for a frame that contained no face",
    ),
    Fixture(
        "gesture-result.json",
        ResultEnvelope[GestureDetections],
        "one recognised hand signal answering one frame",
    ),
    Fixture(
        "session-error.json",
        SessionError,
        "a failure report naming the frame it concerns",
    ),
    Fixture(
        "session-close.json",
        SessionClose,
        "an orderly close",
    ),
)

_BY_NAME: Final[Mapping[str, Fixture]] = MappingProxyType(
    {fixture.name: fixture for fixture in FIXTURES},
)


def _golden_directory() -> Traversable:
    """Locate the committed fixture directory.

    Returns:
        The `golden/` directory as an importable resource, which works from an
        installed wheel as well as from a source checkout.
    """
    return resources.files(_PACKAGE).joinpath(_GOLDEN)


def fixture_for(name: str) -> Fixture:
    """Look a fixture up in the corpus by file name.

    Args:
        name: The fixture's file name.

    Returns:
        The manifest entry for that fixture.

    Raises:
        KeyError: If no fixture by that name is registered, which is also what
            keeps this module from reading a path the corpus does not declare.
    """
    try:
        return _BY_NAME[name]
    except KeyError:
        message = f"no such fixture: {name!r}"
        raise KeyError(message) from None


def fixture_bytes(name: str) -> bytes:
    """Read a fixture exactly as it is committed.

    Args:
        name: The fixture's file name.

    Returns:
        The file's bytes, unaltered — this is the wire format itself, so
        nothing here decodes, strips or re-encodes them.
    """
    return _golden_directory().joinpath(fixture_for(name).name).read_bytes()


def load_fixture[ModelT: WireModel](name: str, model: type[ModelT]) -> ModelT:
    """Parse a fixture into the message type the caller expects.

    Args:
        name: The fixture's file name.
        model: The message type to parse it as.

    Returns:
        The parsed message.
    """
    return model.from_wire(fixture_bytes(name))


def round_trip(fixture: Fixture) -> tuple[WireModel, bytes]:
    """Parse a fixture and serialise it straight back.

    The bytes returned are what a caller compares against `fixture_bytes` to
    assert that the wire format is unchanged. Byte equality rather than value
    equality is the point: two messages can be equal while one of them writes a
    field the other omits, or writes the same fields in a different order, and
    a consumer of the other implementation would then fail to parse a message
    its own tests called correct.

    Args:
        fixture: The manifest entry to round-trip.

    Returns:
        The parsed message and its re-serialised bytes.
    """
    message = fixture.model.from_wire(fixture_bytes(fixture.name))
    return message, message.to_wire()


def golden_file_names() -> tuple[str, ...]:
    """List every file present in the fixture directory.

    Returns:
        The file names, sorted. A name here that the manifest does not carry is
        a fixture nothing exercises, which is what the corpus test checks for.
    """
    return tuple(sorted(entry.name for entry in _golden_directory().iterdir()))
