# Vendored from the Home Assistant project's Linux voice assistant.
#
#   upstream-project: OHF-Voice/linux-voice-assistant
#   upstream-url:     https://github.com/OHF-Voice/linux-voice-assistant
#   upstream-path:    linux_voice_assistant/webrtc.py
#   upstream-commit:  d1f5761f7591495794734e79c98f7199100153c0
#   upstream-licence: Apache-2.0 (LICENSE, in this directory)
#
# This is a derived work, not a copy: see NOTICE in this directory for what was
# changed and why. Keep the diff from upstream small and deliberate — the
# scheduled drift job reads the keys above to find what this file came from.
#
# Vendored code is an explicit, recorded exception to this repository's
# strict-typing rule: it is type-checked under the `[[tool.mypy.overrides]]`
# block in the repository-root pyproject.toml that names this directory as
# vendored, and it is left formatted the way upstream formats it so the drift
# job compares like with like.
import logging

_LOGGER = logging.getLogger(__name__)


class WebRTCProcessor:
    def __init__(self, agc_level: int = 0, ns_level: int = 0):
        from webrtc_noise_gain import AudioProcessor  # type: ignore[import-untyped]

        self.apm = AudioProcessor(agc_level, ns_level)
        self.agc_level = agc_level
        self.ns_level = ns_level
        self._buffer = bytearray()
        self.FRAME_SIZE_BYTES = 320  # 160 samples * 2 bytes (16-bit PCM)

    def update_settings(self, agc_level: int, ns_level: int):
        """Re-initialize processor if settings changed."""
        if self.agc_level != agc_level or self.ns_level != ns_level:
            from webrtc_noise_gain import AudioProcessor

            _LOGGER.debug("Updating WebRTC settings: Gain=%s, NS=%s", agc_level, ns_level)
            self.apm = AudioProcessor(agc_level, ns_level)
            self.agc_level = agc_level
            self.ns_level = ns_level

    def process(self, raw_bytes: bytes) -> bytes:
        """
        Buffer and process audio.
        Returns processed bytes (may be shorter than input if buffering).
        """
        self._buffer.extend(raw_bytes)
        processed_chunks: list[bytes] = []

        while len(self._buffer) >= self.FRAME_SIZE_BYTES:
            frame = bytes(self._buffer[: self.FRAME_SIZE_BYTES])
            del self._buffer[: self.FRAME_SIZE_BYTES]  # drain in-place

            result = self.apm.Process10ms(frame)
            processed_chunks.append(result.audio)

        return b"".join(processed_chunks)
