# Vendored from the Home Assistant project's Linux voice assistant.
#
#   upstream-project: OHF-Voice/linux-voice-assistant
#   upstream-url:     https://github.com/OHF-Voice/linux-voice-assistant
#   upstream-path:    tests/unit/conftest.py
#   upstream-commit:  d1f5761f7591495794734e79c98f7199100153c0
#   upstream-licence: Apache-2.0
#
# Carried because it exercises code this repository kept. LICENSE and NOTICE in
# this directory record the upstream project, every file derived from it, the
# commit they were taken at, and what was changed.
"""Shared test fixtures/helpers for the unit test suite."""

from dataclasses import MISSING, fields
from pathlib import Path
from queue import Queue
from unittest.mock import MagicMock, patch


def _auto_value_for(f):
    """Best-effort placeholder for a required (no-default) dataclass field."""
    type_str = f.type if isinstance(f.type, str) else getattr(f.type, "__name__", "")

    if type_str == "str":
        return f"/mock/{f.name}"
    if type_str == "Path":
        return Path(f"/mock/{f.name}")
    if type_str in ("float",):
        return 0.0
    if type_str in ("int",):
        return 0
    if type_str in ("bool",):
        return False
    if type_str.startswith("Set"):
        return set()
    if type_str.startswith(("List", "list")):
        return []
    if type_str.startswith(("Dict", "dict")):
        return {}
    # Fallback for anything else (players, queues, protobuf-ish types, etc.)
    return MagicMock()


def make_state(tmp_path=None, **overrides):
    """Build a minimal ServerState, auto-filling any required fields
    that aren't explicitly provided via defaults/overrides.
    """
    from reachy_mini_ha_satellite.esphome.models import Preferences, ServerState

    if tmp_path is None:
        # Upstream made a real temporary directory here. Nothing under this
        # default is ever written — the `tmp_path` fixture in conftest.py hands
        # every caller a fake-filesystem directory instead — so a plain path is
        # enough, and it keeps the no-input-or-output rule intact.
        tmp_path = Path("/reachy-satellite-tests")

    stop_word = MagicMock()
    stop_word.id = "stop"
    stop_word.is_active = False

    values = {
        "name": "lva-test",
        "friendly_name": "LVA Test",
        "mac_address": "aa:bb:cc:dd:ee:ff",
        "ip_address": "192.0.2.1",
        "network_interface": "eth0",
        "version": "1.0.0",
        "esphome_version": "42.0.0",
        "audio_queue": Queue(),
        "entities": [],
        "available_wake_words": {},
        "wake_words": {},
        "active_wake_words": set(),
        "stop_word": stop_word,
        "music_player": MagicMock(),
        "tts_player": MagicMock(),
        "wakeup_sound": "/sounds/wake.flac",
        "start_listening_sound": "/sounds/start_listening.flac",
        "processing_sound": "/sounds/processing.wav",
        "timer_finished_sound": "/sounds/timer.flac",
        "mute_sound": "/sounds/mute.flac",
        "unmute_sound": "/sounds/unmute.flac",
        "button_double_press_sound": "/sounds/double_press.flac",
        "button_triple_press_sound": "/sounds/triple_press.flac",
        "button_long_press_sound": "/sounds/long_press.flac",
        "preferences": Preferences(),
        "preferences_path": tmp_path / "preferences.json",
        "download_dir": tmp_path / "downloads",
        "volume": 1.0,
        "mic_volume": 100,
        "mic_auto_gain": 0,
        "mic_noise_suppression": 0,
    }
    values.update(overrides)

    # Auto-fill any other required (no-default) field we didn't anticipate.
    for f in fields(ServerState):
        if f.name in values:
            continue
        no_default = f.default is MISSING and f.default_factory is MISSING  # type: ignore[misc]
        if no_default:
            values[f.name] = _auto_value_for(f)

    return ServerState(**values)


def make_satellite(tmp_path=None, state_overrides=None):
    """Build a VoiceSatelliteProtocol with heavy dependencies mocked."""
    state = make_state(tmp_path, **(state_overrides or {}))

    with (
        patch("reachy_mini_ha_satellite.esphome.satellite.WakeWord1SensitivityNumberEntity", MagicMock()),
        patch("reachy_mini_ha_satellite.esphome.satellite.WakeWord2SensitivityNumberEntity", MagicMock()),
        patch("reachy_mini_ha_satellite.esphome.satellite.StopWordSensitivityNumberEntity", MagicMock()),
    ):
        from reachy_mini_ha_satellite.esphome.satellite import VoiceSatelliteProtocol

        satellite = VoiceSatelliteProtocol(state)

    satellite._writelines = MagicMock()
    satellite._loop = None
    return satellite
