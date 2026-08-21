"""The two audio seams are cut, and both are still open.

This change defines the seams; change 0012 fills them. What is worth asserting
now is that the hole is the right shape and that nothing has quietly filled it:
the vendored package imports with no audio library present, no implementation of
either protocol ships, and the carried tests — which run alongside these —
exercise the protocol layer through them.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from dataclasses import fields

import pytest

from reachy_mini_ha_satellite.esphome import models, satellite
from reachy_mini_ha_satellite.esphome.seams import (
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    AudioCapture,
    MediaPlayback,
)


def _required_members(protocol: type) -> set[str]:
    """The member names a `Protocol` requires an implementation to supply.

    CPython records these on every `Protocol` class from 3.12 onwards, which is
    this repository's floor; typeshed does not declare the attribute, hence the
    suppression.
    """
    return set(protocol.__protocol_attrs__)  # type: ignore[attr-defined]  # undeclared CPython internal


class _Playback:
    """The smallest thing that satisfies `MediaPlayback`, for the test's sake."""

    def play(
        self,
        url: str | list[str],
        done_callback: Callable[[], None] | None = None,
        stop_first: bool = False,
    ) -> None: ...

    def pause(self) -> None: ...

    def resume(self) -> None: ...

    def stop(self) -> None: ...

    @property
    def is_playing(self) -> bool:
        return False

    def set_volume(self, volume: float) -> None: ...

    def duck(self, factor: float = 0.5) -> None: ...

    def unduck(self) -> None: ...


class _Capture:
    """The smallest thing that satisfies `AudioCapture`."""

    @property
    def channels(self) -> int:
        return 2

    @property
    def samples_per_chunk(self) -> int:
        return 160

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def read_chunk(self) -> Sequence[bytes] | None:
        return None


class TestSeamsAreOpen:
    """Nothing in this package implements either seam yet."""

    @pytest.mark.parametrize("library", ["mpv", "soundcard"])
    def test_no_audio_library_is_reachable(self, library: str) -> None:
        """The libraries upstream used are not dependencies and never load.

        `conftest.py` imports every vendored module before any test runs, so if
        importing the protocol layer still dragged in a playback or capture
        library it would be in `sys.modules` by now. It is not, which is what
        "the seam is open" means concretely.
        """
        assert library not in sys.modules

    def test_the_vendored_protocol_imported(self) -> None:
        """The seams being unfilled does not stop the package importing."""
        assert satellite.VoiceSatelliteProtocol is not None

    def test_capture_is_unset_by_default(self) -> None:
        """`ServerState` starts with no capture source, and that is legal."""
        capture = next(
            f for f in fields(models.ServerState) if f.name == "audio_capture"
        )
        assert capture.default is None


class TestPlaybackSeam:
    """What change 0012's playback adapter has to satisfy."""

    def test_a_minimal_implementation_satisfies_it(self) -> None:
        """Structural typing: an adapter never imports the vendored code."""
        assert isinstance(_Playback(), MediaPlayback)

    def test_an_unrelated_object_does_not(self) -> None:
        """The protocol is narrow enough to reject something arbitrary."""
        assert not isinstance(object(), MediaPlayback)

    def test_it_covers_exactly_what_the_protocol_layer_calls(self) -> None:
        """Guards the seam against widening past what upstream actually used."""
        assert _required_members(MediaPlayback) == {
            "duck",
            "is_playing",
            "pause",
            "play",
            "resume",
            "set_volume",
            "stop",
            "unduck",
        }


class TestCaptureSeam:
    """What change 0012's capture adapter has to satisfy."""

    def test_a_minimal_implementation_satisfies_it(self) -> None:
        """Structural typing: an adapter never imports the vendored code."""
        assert isinstance(_Capture(), AudioCapture)

    def test_an_unrelated_object_does_not(self) -> None:
        """The protocol is narrow enough to reject something arbitrary."""
        assert not isinstance(object(), AudioCapture)

    def test_it_covers_exactly_what_an_adapter_must_supply(self) -> None:
        """Guards the seam against widening past what upstream actually used."""
        assert _required_members(AudioCapture) == {
            "channels",
            "read_chunk",
            "samples_per_chunk",
            "start",
            "stop",
        }

    def test_the_wire_format_is_the_one_the_models_expect(self) -> None:
        """16 kHz signed 16-bit PCM: what the carried wake-word models want."""
        assert SAMPLE_RATE == 16000
        assert SAMPLE_WIDTH == 2
