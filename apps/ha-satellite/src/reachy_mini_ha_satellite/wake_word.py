"""Running the wake-word models over captured audio.

`esphome/wake_word.py` *loads* the models; this module *runs* them. Until it
existed the two halves never met: `build_server_state` loaded `Okay Nabu` and
`Stop`, announced both to Home Assistant, and nothing ever fed either one a
sample — so ha-satellite REQ-044 was satisfied on paper and a robot on a desk
ignored everybody who spoke to it. The streaming half was there
(`VoiceSatelliteProtocol.handle_audio` forwards audio *once a pipeline is
already running*), and the half that starts a pipeline was not.

Upstream keeps this in a two-hundred-line `process_audio` inside the
command-line entry point change 0011 did not carry. What is carried here is its
behaviour, not its shape: **everything that is a decision over values lives in
this module and is driven from tests with fake model objects**, and the only
things the seam hides are the two calls that need a real model —
`features.process_streaming(chunk)` and `model.process_streaming(features)`.
Thresholds, the refractory window, the muted check and rebuilding the active
list are all ordinary code with ordinary tests, because a wake word that fires
twice in a row or ignores the mute switch is a defect nobody can catch on a
robot without a person standing in front of it.

Three deliberate differences from upstream, each of which is a bug fix rather
than a preference:

* **The rebuild is guarded by a flag, not by the list being empty.** Upstream
  re-enters its rebuild on every block for as long as no wake word is active,
  which re-resolves the thresholds and pushes three `NumberStateResponse`
  messages to Home Assistant a hundred times a second. Here the rebuild happens
  once and again whenever `wake_words_changed` is set, which is the event it is
  actually about.
* **Which engine a model belongs to comes from `available_wake_words`**, the
  same registry the default threshold comes from, rather than from an
  `isinstance` check against the two runtime classes. It is the same answer for
  a real model and it is a value a test can state.
* **Activation is logged here.** Upstream's log line lives inside the runtime,
  behind a `debug_probabilities` flag it turns on only *after* a detection — so
  the line never appears for the first one, which is the one anybody debugging
  is looking for.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

# The one protobuf message this module sends. `satellite.send_messages` is
# thread-safe — it hops to the event loop when it is called from anywhere else —
# which is what lets the detection thread announce a resolved threshold.
from aioesphomeapi.api_pb2 import (  # type: ignore[attr-defined]  # the generated module has no stubs, as `esphome/satellite.py` records at its own import of it
    NumberStateResponse,
)
from pymicro_wakeword import MicroWakeWordFeatures
from pyopen_wakeword import OpenWakeWordFeatures

from reachy_mini_ha_satellite.esphome.models import WakeWordType

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    import numpy.typing as npt

    from reachy_mini_ha_satellite.esphome.models import ServerState

__all__ = [
    "SENSITIVITY_SLOTS",
    "Activations",
    "MicroWakeWordModel",
    "ModelInput",
    "OpenWakeWordModel",
    "WakeWordDetector",
    "WakeWordFeatures",
    "WakeWordModel",
    "phrase_of",
]

_LOGGER: Final = logging.getLogger(__name__)

# What a wake word past the second one is judged against. The protocol carries
# exactly two sensitivity entities, so a third model has no control of its own
# and upstream's fallback is the one this matches.
FALLBACK_THRESHOLD: Final = 0.7

# How many sensitivity sliders Home Assistant gets. The protocol announces
# `max_active_wake_words=2` and carries two entities, and
# `preferences.active_wake_words` is a two-element list with holes rather than a
# list of what is active — which is why a slot is not an index. See
# `WakeWordDetector._assign_slots`.
SENSITIVITY_SLOTS: Final = 2

# The slot of a model that has no slider of its own.
_NO_SLOT: Final = SENSITIVITY_SLOTS


type ModelInput = npt.NDArray[Any]
"""One input to a wake-word model.

An array whose shape and dtype are the runtime's own business — microWakeWord
wants a window of spectrogram features and openWakeWord wants embeddings — so
this names what crosses the seam without pretending to know what is in it.
"""


class WakeWordFeatures(Protocol):
    """Turns raw audio into whatever the models it belongs to consume.

    One instance is long-lived per engine and shared by every model of that
    engine, which is not an optimisation: the feature extractor is a streaming
    frontend holding a sample buffer and a window, so a second instance fed the
    same audio would produce a different, worse answer.
    """

    def process_streaming(self, audio_chunk: bytes, /) -> Iterable[ModelInput]:
        """Turn one chunk of audio into zero or more model inputs.

        Positional-only, because the two runtimes name this argument
        differently and a keyword name is not part of what this seam is about.

        Args:
            audio_chunk: 16 kHz signed 16-bit little-endian mono samples.

        Returns:
            The inputs to hand each model, possibly none: a frontend needs a
            full window before it can produce anything.
        """
        ...


class WakeWordModel(Protocol):
    """One loaded wake-word model, whichever runtime it came from.

    Deliberately thin. The two runtimes agree on almost nothing — one answers
    whether it fired and the other answers with probabilities, one owns a
    cutoff and the other does not — so what they have in common is an
    identifier, and the rest is on the two protocols below. Which of them a
    model is comes from `ServerState.available_wake_words`, the same registry
    the default threshold comes from, rather than from guessing at the answer.

    The spoken phrase is not declared here either: microWakeWord carries it as
    an attribute and `AvailableWakeWord.load` attaches one to an openWakeWord
    model after building it, which no type checker can see. `phrase_of` reads
    it.
    """

    id: str


class MicroWakeWordModel(WakeWordModel, Protocol):
    """A microWakeWord model, which judges its own answer against a cutoff."""

    probability_cutoff: float

    def process_streaming(self, features: ModelInput, /) -> bool | None:
        """Run the model over one input from its feature extractor.

        Args:
            features: One input, as produced by `WakeWordFeatures`.

        Returns:
            Whether the wake word fired, or `None` while the sliding window is
            still filling.
        """
        ...


class OpenWakeWordModel(WakeWordModel, Protocol):
    """An openWakeWord model, whose answer a caller compares itself."""

    def process_streaming(self, embeddings: ModelInput, /) -> Iterable[float]:
        """Run the model over one input from its feature extractor.

        Args:
            embeddings: One input, as produced by `WakeWordFeatures`.

        Returns:
            The probabilities it computed, to be compared against a threshold.
        """
        ...


def phrase_of(model: WakeWordModel) -> str:
    """Say what a model listens for, in words.

    microWakeWord carries the phrase as an attribute of the model;
    `AvailableWakeWord.load` attaches one to an openWakeWord model after
    building it, which a type checker cannot see. Reading it defensively is
    what lets one log line serve both, and lets a test's fake model omit it.

    Args:
        model: The loaded model.

    Returns:
        The spoken phrase, or the model's identifier when it carries none.
    """
    phrase = getattr(model, "wake_word", None)
    return str(phrase) if phrase else model.id


@dataclass(frozen=True, slots=True)
class Activations:
    """What one chunk of audio turned out to contain.

    Already filtered: a model that fired while the robot was muted, or inside
    the refractory window after the last one, is not reported here. The caller
    acts on what this carries and makes no further decision, which is what
    keeps the decisions in code a test can drive.

    Attributes:
        woken: The models that fired and should start a pipeline, in the order
            they are active. Normally none, occasionally one.
        stopped: Whether the stop word fired and is currently listened for,
            which is what silences a response in progress.
    """

    woken: tuple[WakeWordModel, ...] = ()
    stopped: bool = False


#:= docs/specs/ha-satellite/index.md#req-044-wake-word-detection-runs-on-the-robot
#:% Wake-word detection MUST run locally on the robot, without depending on the
#:% groundstation or on Home Assistant.
class WakeWordDetector:
    """Feeds captured audio to the loaded models and says what fired.

    This is where REQ-044 is actually satisfied. Every model runs in this
    process, off files that ship in the wheel; nothing here opens a socket,
    consults the groundstation or asks Home Assistant anything, so the wake
    word fires on a robot whose network has failed and the failure surfaces
    later, at the point the pipeline needs Home Assistant.
    """

    def __init__(
        self,
        state: ServerState,
        *,
        micro_features: Callable[[], WakeWordFeatures] = MicroWakeWordFeatures,
        open_features: Callable[[], WakeWordFeatures] = (
            OpenWakeWordFeatures.from_builtin
        ),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Hold the state to detect against, without loading a frontend.

        Args:
            state: The vendored server state: which models are loaded, which
                are active, what they are judged against, and whether the robot
                is muted. Read live rather than copied, because Home Assistant
                changes all four of those through entities while this runs.
            micro_features: How to build the microWakeWord frontend. Called at
                most once, the first time a chunk arrives, because building it
                loads a native library and a detector is constructed during
                startup.
            open_features: The same for openWakeWord, and called only if an
                openWakeWord model is ever active. No model this wheel ships is
                one; an operator can drop one into the external wake-word
                directory, and upstream's loader will load it.
            clock: The monotonic source the refractory window is measured on.
        """
        self._state = state
        self._micro_features = micro_features
        self._open_features = open_features
        self._clock = clock
        self._micro: WakeWordFeatures | None = None
        self._open: WakeWordFeatures | None = None
        self._models: list[WakeWordModel] = []
        self._engines: list[WakeWordType] = []
        self._slots: list[int] = []
        # Whether the active list has ever been built. Deliberately not "is the
        # list non-empty": switching every wake word off in Home Assistant is a
        # legitimate state, and treating it as "not built yet" is what makes
        # upstream rebuild — and re-announce three entities — on every block.
        self._built = False
        # When the last wake word fired, on `clock`. One window across every
        # model, as upstream has it: two models that both hear the same phrase
        # should start one conversation between them, not two.
        self._last_active: float | None = None

    @property
    def active(self) -> tuple[WakeWordModel, ...]:
        """The models this is currently running, in order.

        Returns:
            The active models. Empty until the first chunk has been processed,
            because the list is built from the state as it stands then.
        """
        return tuple(self._models)

    def process(self, chunk: bytes) -> Activations:
        """Run every active model over one chunk, and say what fired.

        Args:
            chunk: One chunk of channel 0, as 16 kHz signed 16-bit
                little-endian mono samples.

        Returns:
            The models that should start a pipeline and whether the stop word
            fired, both already filtered by the mute switch and the refractory
            window.
        """
        self._rebuild_if_asked()

        micro_inputs = list(self._micro_frontend().process_streaming(chunk))
        oww_inputs: list[ModelInput] = []
        if WakeWordType.OPEN_WAKE_WORD in self._engines:
            oww_inputs = list(self._open_frontend().process_streaming(chunk))

        woken: list[WakeWordModel] = []
        for index, model in enumerate(self._models):
            # Every model is run over every input, whether or not an earlier
            # one fired: they are streaming models holding their own window,
            # and one that is fed only some of the audio is one whose next
            # answer is about audio that never happened.
            if not self._fired(index, model, micro_inputs, oww_inputs):
                continue
            if self._state.muted:
                continue
            now = self._clock()
            if (
                self._last_active is not None
                and (now - self._last_active) <= self._state.refractory_seconds
            ):
                _LOGGER.debug(
                    "wake word %r fired inside the refractory window; ignoring it",
                    phrase_of(model),
                )
                continue
            self._last_active = now
            _LOGGER.info("wake word detected: %s", phrase_of(model))
            woken.append(model)

        return Activations(tuple(woken), self._stop_fired(micro_inputs))

    def _micro_frontend(self) -> WakeWordFeatures:
        """Return the microWakeWord frontend, building it the first time.

        Returns:
            The frontend, which is long-lived: it holds the sample buffer the
            models' windows are cut from.
        """
        if self._micro is None:
            self._micro = self._micro_features()
        return self._micro

    def _open_frontend(self) -> WakeWordFeatures:
        """Return the openWakeWord frontend, building it the first time.

        Returns:
            The frontend, built only once an openWakeWord model is active.
        """
        if self._open is None:
            self._open = self._open_features()
        return self._open

    def _rebuild_if_asked(self) -> None:
        """Adopt the active wake words, when there is a reason to.

        Which is the first chunk, and any chunk after Home Assistant changed
        the selection — `VoiceSatelliteProtocol` sets `wake_words_changed` when
        it does. Rebuilding resolves each model's threshold and tells Home
        Assistant what it resolved to, so the sensitivity entities show the
        number the model is actually judged against rather than the protocol's
        default.
        """
        if self._built and not self._state.wake_words_changed:
            return
        self._state.wake_words_changed = False
        self._built = True

        state = self._state
        self._models = [
            model
            for model in state.wake_words.values()
            if model.id in state.active_wake_words
        ]
        self._engines = [self._engine_of(model) for model in self._models]
        self._slots = self._assign_slots(self._models)
        _LOGGER.info(
            "wake words now active: %s",
            [phrase_of(model) for model in self._models] or "none",
        )

        for model, slot in zip(self._models, self._slots, strict=True):
            if slot >= SENSITIVITY_SLOTS:
                continue
            threshold = self._resolve_threshold(slot, model)
            if slot == 0:
                state.wake_word_1_threshold = threshold
            else:
                state.wake_word_2_threshold = threshold
        self._publish_thresholds()

    def _assign_slots(self, models: list[WakeWordModel]) -> list[int]:
        """Say which sensitivity slider each active model sits behind.

        **Not the position in the active list**, and the difference is one Home
        Assistant can produce in two clicks. `preferences.active_wake_words` is
        a two-element list with holes, and the protocol keeps a wake word where
        it was: switch the first of two off and the survivor stays in the
        *second* slot, with `None` in the first. Numbering the survivors from
        zero would judge it by the slider the operator moved for the wake word
        they just removed.

        Args:
            models: The active models, in the order they are run.

        Returns:
            One slot per model. `SENSITIVITY_SLOTS` or more means "no slider of
            its own", which is what a third active wake word gets.
        """
        saved = list(self._state.preferences.active_wake_words or [])
        slots = [_NO_SLOT] * len(models)
        taken: set[int] = set()
        for index, model in enumerate(models):
            if model.id not in saved:
                continue
            slot = saved.index(model.id)
            if slot < SENSITIVITY_SLOTS and slot not in taken:
                slots[index] = slot
                taken.add(slot)
        # Anything the preferences do not place — a fresh installation, or a
        # wake word chosen by configuration rather than by Home Assistant —
        # takes the lowest slider nothing else has claimed.
        free = [slot for slot in range(SENSITIVITY_SLOTS) if slot not in taken]
        for index, slot in enumerate(slots):
            if slot == _NO_SLOT and free:
                slots[index] = free.pop(0)
        return slots

    def _engine_of(self, model: WakeWordModel) -> WakeWordType:
        """Say which runtime a loaded model belongs to.

        Read out of the same registry the default threshold comes from rather
        than tested with `isinstance`, which keeps it a value a test can state
        and keeps this module from having to name the two runtime classes.

        Args:
            model: The loaded model.

        Returns:
            Its engine. A model this wheel ships is a microWakeWord one, and so
            is anything the registry has no entry for — which is the stop word,
            loaded by path rather than discovered.
        """
        available = self._state.available_wake_words.get(model.id)
        return available.type if available is not None else WakeWordType.MICRO_WAKE_WORD

    def _resolve_threshold(self, slot: int, model: WakeWordModel) -> float:
        """Work out what a wake word should be judged against.

        Args:
            slot: Which of the two sensitivity entities this model sits behind.
            model: The loaded model.

        Returns:
            What Home Assistant's sensitivity entity was last set to, or —
            where it has never been set — the cutoff the model's own
            configuration declares.
        """
        saved = (
            self._state.preferences.wake_word_1_sensitivity
            if slot == 0
            else self._state.preferences.wake_word_2_sensitivity
        )
        if saved is not None:
            return float(saved)
        available = self._state.available_wake_words.get(model.id)
        if available is not None:
            return float(available.probability_cutoff)
        return FALLBACK_THRESHOLD

    def _publish_thresholds(self) -> None:
        """Tell Home Assistant what the three sensitivity entities resolved to.

        Nothing happens when no client is connected: the entities carry the
        resolved values already, and Home Assistant reads them when it
        subscribes.
        """
        satellite = self._state.satellite
        if satellite is None:
            return
        entities = [
            self._state.sensitivity_1_number_entity,
            self._state.sensitivity_2_number_entity,
            self._state.stop_sensitivity_number_entity,
        ]
        updates = []
        for entity in entities:
            if entity is None:
                continue
            entity.sync_with_state()
            updates.append(NumberStateResponse(key=entity.key, state=entity.value))
        if updates:
            satellite.send_messages(updates)

    def _threshold(self, slot: int) -> float:
        """Read what a wake word is judged against right now.

        Read per chunk rather than remembered from the rebuild, because Home
        Assistant's sensitivity entity writes straight into the state and an
        operator sliding it expects the next word they say to be judged by it.

        Args:
            slot: Which sensitivity entity this model sits behind.

        Returns:
            The threshold, or the fallback for a model with no sensitivity
            entity of its own.
        """
        if slot == 0:
            return self._state.wake_word_1_threshold
        if slot == 1:
            return self._state.wake_word_2_threshold
        return FALLBACK_THRESHOLD

    def _fired(
        self,
        index: int,
        model: WakeWordModel,
        micro_inputs: list[ModelInput],
        oww_inputs: list[ModelInput],
    ) -> bool:
        """Run one model over this chunk's inputs.

        Args:
            index: Which of the active models this is, which is what decides
                the threshold.
            model: The model to run.
            micro_inputs: This chunk's microWakeWord inputs.
            oww_inputs: This chunk's openWakeWord inputs, if any.

        Returns:
            Whether it fired.
        """
        threshold = self._threshold(self._slots[index])
        if self._engines[index] == WakeWordType.OPEN_WAKE_WORD:
            # Each cast is what the engine lookup has just established. The two
            # runtimes share no `process_streaming` signature, so a caller has
            # to know which one it is holding, and this is where it says so.
            open_model = cast("OpenWakeWordModel", model)
            fired = False
            for model_input in oww_inputs:
                for probability in open_model.process_streaming(model_input):
                    fired = fired or probability > threshold
            return fired

        # microWakeWord compares against the cutoff itself, so the threshold is
        # written onto the model rather than applied to its answer.
        micro = cast("MicroWakeWordModel", model)
        micro.probability_cutoff = threshold
        fired = False
        for model_input in micro_inputs:
            fired = bool(micro.process_streaming(model_input)) or fired
        return fired

    def _stop_fired(self, micro_inputs: list[ModelInput]) -> bool:
        """Run the stop word over this chunk, and say whether it should act.

        The model is run over every chunk whatever the answer, and whether or
        not the stop word is currently listened for: it is a streaming model,
        and one fed only the audio arriving while a response is playing has no
        window to judge that audio against.

        Args:
            micro_inputs: This chunk's microWakeWord inputs.

        Returns:
            Whether a response in progress should be stopped.
        """
        stop_word = self._state.stop_word
        stop_word.probability_cutoff = self._state.stop_word_threshold
        fired = False
        for model_input in micro_inputs:
            fired = bool(stop_word.process_streaming(model_input)) or fired
        if not fired:
            return False
        if self._state.muted:
            return False
        # The protocol adds the stop word to the active set while a response is
        # playing and discards it afterwards, which is how "stop" means
        # something during a response and nothing the rest of the time.
        return stop_word.id in self._state.active_wake_words
