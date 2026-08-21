"""Running the wake-word models: what fires, what is ignored, and what is told.

**Everything except one class here is a unit test over fakes**, and that is the
whole point of the seam under test. A wake word that fires twice in a row, or
fires while the robot is muted, or is judged against the wrong number, is a
defect that can only be seen on a robot with somebody standing in front of it
speaking — which is exactly how a satellite that ran no models at all reached
one. Fake model objects that fire when the test says so turn every one of those
into an assertion.

`TestAgainstTheRealModels` is the exception and carries
`@pytest.mark.filesystem`, because the models it loads are the two `.tflite`
files this wheel ships: microWakeWord needs no downloaded weights, so the real
runtime, the real feature frontend and the real detector run here over synthetic
audio. What it proves is that the plumbing is right — the frontend produces
inputs the model accepts, an activation reaches `Activations`, and the threshold
is what decides. **What it cannot prove is recognition**: nobody in this
repository has a recording of the phrase, so the activation is forced by putting
the threshold below every probability the model can report. That the model says
"Okay Nabu" when a person says "okay nabu" is proved on hardware and nowhere
else.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import numpy as np
import pytest
from satellite_support import (
    FakeMicroWakeWord,
    FakeOpenWakeWord,
    FakeWakeWordFeatures,
    ManualClock,
    available_wake_word,
    vendored_server_state,
)

from reachy_mini_ha_satellite.esphome.models import (
    Preferences,
    WakeWordType,
)
from reachy_mini_ha_satellite.wake_word import (
    FALLBACK_THRESHOLD,
    Activations,
    WakeWordDetector,
    phrase_of,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from reachy_mini_ha_satellite.esphome.models import ServerState

# One chunk of audio. Its contents are never looked at by anything in this
# module except the real models, which have their own chunks: for the fakes it
# is a token that a chunk went past.
CHUNK: Final = b"\x00\x01" * 160


class FakeEntity:
    """One of the three sensitivity entities, without a protocol behind it."""

    def __init__(self, key: int, read: str) -> None:
        """Point the entity at the field of the state it reports.

        Args:
            key: The entity key Home Assistant addresses it by.
            read: Which field of the server state it reports.
        """
        self.key = key
        self.value = 0.0
        self.syncs = 0
        self._read = read
        self._state: ServerState | None = None

    def attach(self, state: ServerState) -> FakeEntity:
        """Bind the entity to the state it reads.

        Args:
            state: The server state.

        Returns:
            This entity, so a caller can build and bind in one expression.
        """
        self._state = state
        return self

    def sync_with_state(self) -> None:
        """Adopt whatever the state now says."""
        assert self._state is not None
        self.syncs += 1
        self.value = float(getattr(self._state, self._read))


class FakeSatellite:
    """Enough of the vendored protocol for the detector to announce a value."""

    def __init__(self) -> None:
        """Start with nothing having been sent."""
        self.sent: list[Any] = []

    def send_messages(self, msgs: Iterable[Any]) -> None:
        """Record what would have gone to Home Assistant.

        Args:
            msgs: The protobuf messages.
        """
        self.sent.extend(msgs)


def detector_over(
    state: ServerState,
    *,
    clock: ManualClock | None = None,
) -> tuple[WakeWordDetector, FakeWakeWordFeatures, list[FakeWakeWordFeatures]]:
    """Build a detector whose two feature frontends are fakes.

    Args:
        state: The server state to detect against.
        clock: The monotonic source, or a fresh manual one.

    Returns:
        The detector, its microWakeWord frontend, and a list that the
        openWakeWord frontend is appended to if one is ever built — which is
        how a test asserts that it was not.
    """
    micro = FakeWakeWordFeatures()
    built: list[FakeWakeWordFeatures] = []

    def _open() -> FakeWakeWordFeatures:
        """Build the openWakeWord frontend and record that it happened.

        Returns:
            The frontend.
        """
        frontend = FakeWakeWordFeatures()
        built.append(frontend)
        return frontend

    return (
        WakeWordDetector(
            state,
            micro_features=lambda: micro,
            open_features=_open,
            clock=clock if clock is not None else ManualClock(),
        ),
        micro,
        built,
    )


def state_with(
    model: FakeMicroWakeWord | FakeOpenWakeWord,
    **overrides: object,
) -> ServerState:
    """Build a server state with one active wake word and a silent stop word.

    Args:
        model: The wake word to activate.
        overrides: Anything else to set on the state.

    Returns:
        The state.
    """
    values: dict[str, object] = {
        "wake_words": {model.id: model},
        "active_wake_words": {model.id},
        "available_wake_words": {model.id: available_wake_word(model.id)},
        "stop_word": FakeMicroWakeWord("stop"),
        "preferences": Preferences(),
    }
    values.update(overrides)
    return vendored_server_state(**values)


class TestWhatFires:
    """Which models run, and which of them are reported as having woken."""

    def test_it_reports_a_model_that_fired(self) -> None:
        """The whole of the defect: nothing anywhere ran the models."""
        model = FakeMicroWakeWord("okay_nabu", fires=[True])
        detector, _micro, _open = detector_over(state_with(model))

        activations = detector.process(CHUNK)

        assert activations == Activations((model,), stopped=False)

    def test_it_reports_nothing_when_no_model_fired(self) -> None:
        """Which is every ten milliseconds of an ordinary robot's day."""
        model = FakeMicroWakeWord("okay_nabu")
        detector, _micro, _open = detector_over(state_with(model))

        assert detector.process(CHUNK) == Activations()

    def test_it_does_not_run_a_model_that_is_not_active(self) -> None:
        """Loaded and switched off in Home Assistant is switched off."""
        listening = FakeMicroWakeWord("okay_nabu")
        idle = FakeMicroWakeWord("hey_jarvis", fires=[True])
        state = state_with(
            listening,
            wake_words={listening.id: listening, idle.id: idle},
            active_wake_words={listening.id},
        )
        detector, _micro, _open = detector_over(state)

        activations = detector.process(CHUNK)

        assert activations.woken == ()
        assert idle.inputs == []

    def test_it_feeds_every_input_to_the_model(self) -> None:
        """A streaming model fed some of the audio judges audio that never was."""
        model = FakeMicroWakeWord("okay_nabu")
        detector, micro, _open = detector_over(state_with(model))

        detector.process(CHUNK)
        detector.process(CHUNK)

        assert micro.chunks == [CHUNK, CHUNK]
        assert len(model.inputs) == 2


class TestTheMuteSwitch:
    """A muted robot hears nothing, whatever the models say."""

    def test_a_muted_robot_does_not_wake(self) -> None:
        """`switch.mute` is the one control an operator expects to be absolute."""
        model = FakeMicroWakeWord("okay_nabu", fires=[True])
        detector, _micro, _open = detector_over(state_with(model, muted=True))

        assert detector.process(CHUNK).woken == ()

    def test_a_muted_robot_still_runs_the_models(self) -> None:
        """Otherwise the first word after unmuting is judged against silence."""
        model = FakeMicroWakeWord("okay_nabu", fires=[True])
        detector, _micro, _open = detector_over(state_with(model, muted=True))

        detector.process(CHUNK)

        assert len(model.inputs) == 1

    def test_a_muted_robot_does_not_stop_on_the_stop_word(self) -> None:
        """Mute is not a way to interrupt a response that is already playing."""
        model = FakeMicroWakeWord("okay_nabu")
        stop = FakeMicroWakeWord("stop", fires=[True])
        state = state_with(
            model,
            stop_word=stop,
            active_wake_words={model.id, "stop"},
            muted=True,
        )
        detector, _micro, _open = detector_over(state)

        assert detector.process(CHUNK).stopped is False


class TestTheRefractoryWindow:
    """One phrase starts one conversation, however loudly it was said."""

    def test_it_does_not_wake_twice_inside_the_window(self) -> None:
        """A wake word held across two chunks is one wake word."""
        model = FakeMicroWakeWord("okay_nabu", fires=[True, True])
        clock = ManualClock()
        state = state_with(model, refractory_seconds=2.0)
        detector, _micro, _open = detector_over(state, clock=clock)

        first = detector.process(CHUNK)
        clock.now += 1.5
        second = detector.process(CHUNK)

        assert first.woken == (model,)
        assert second.woken == ()

    def test_it_wakes_again_once_the_window_has_passed(self) -> None:
        """Otherwise the robot answers once and never again."""
        model = FakeMicroWakeWord("okay_nabu", fires=[True, True])
        clock = ManualClock()
        state = state_with(model, refractory_seconds=2.0)
        detector, _micro, _open = detector_over(state, clock=clock)

        detector.process(CHUNK)
        clock.now += 2.5
        second = detector.process(CHUNK)

        assert second.woken == (model,)

    def test_the_window_is_shared_by_every_model(self) -> None:
        """Two models that heard the same phrase start one conversation."""
        first = FakeMicroWakeWord("okay_nabu", fires=[True])
        second = FakeMicroWakeWord("hey_jarvis", fires=[True])
        state = state_with(
            first,
            wake_words={first.id: first, second.id: second},
            active_wake_words={first.id, second.id},
            available_wake_words={
                first.id: available_wake_word(first.id),
                second.id: available_wake_word(second.id),
            },
            refractory_seconds=2.0,
        )
        detector, _micro, _open = detector_over(state)

        assert detector.process(CHUNK).woken == (first,)


class TestTheThresholds:
    """What each model is judged against, and where the number comes from."""

    def test_it_uses_the_sensitivity_home_assistant_set(self) -> None:
        """An operator who moved the slider expects the next word to obey it."""
        model = FakeMicroWakeWord("okay_nabu")
        state = state_with(
            model,
            available_wake_words={model.id: available_wake_word(model.id, cutoff=0.85)},
            preferences=Preferences(wake_word_1_sensitivity=0.42),
        )
        detector, _micro, _open = detector_over(state)

        detector.process(CHUNK)

        assert state.wake_word_1_threshold == pytest.approx(0.42)
        assert model.cutoffs == [pytest.approx(0.42)]

    def test_it_falls_back_to_the_model_s_own_cutoff(self) -> None:
        """Which is the number the model was trained and tuned with."""
        model = FakeMicroWakeWord("okay_nabu")
        state = state_with(
            model,
            available_wake_words={model.id: available_wake_word(model.id, cutoff=0.85)},
        )
        detector, _micro, _open = detector_over(state)

        detector.process(CHUNK)

        assert state.wake_word_1_threshold == pytest.approx(0.85)

    def test_a_model_the_registry_has_never_heard_of_gets_the_default(self) -> None:
        """Loaded by path rather than discovered, which is what the stop word is."""
        model = FakeMicroWakeWord("okay_nabu")
        state = state_with(model, available_wake_words={})
        detector, _micro, _open = detector_over(state)

        detector.process(CHUNK)

        assert state.wake_word_1_threshold == pytest.approx(FALLBACK_THRESHOLD)

    def test_the_second_model_is_judged_by_the_second_sensitivity(self) -> None:
        """Two wake words, two sliders, and they must not be crossed."""
        first = FakeMicroWakeWord("okay_nabu")
        second = FakeMicroWakeWord("hey_jarvis")
        state = state_with(
            first,
            wake_words={first.id: first, second.id: second},
            active_wake_words={first.id, second.id},
            available_wake_words={
                first.id: available_wake_word(first.id, cutoff=0.85),
                second.id: available_wake_word(second.id, cutoff=0.55),
            },
        )
        detector, _micro, _open = detector_over(state)

        detector.process(CHUNK)

        assert state.wake_word_1_threshold == pytest.approx(0.85)
        assert state.wake_word_2_threshold == pytest.approx(0.55)

    def test_a_third_model_is_judged_by_the_fallback(self) -> None:
        """The protocol has two sensitivity entities; a third has no slider."""
        models = [FakeMicroWakeWord(f"word_{index}") for index in range(3)]
        state = state_with(
            models[0],
            wake_words={model.id: model for model in models},
            active_wake_words={model.id for model in models},
            available_wake_words={
                model.id: available_wake_word(model.id) for model in models
            },
        )
        detector, _micro, _open = detector_over(state)

        detector.process(CHUNK)

        assert detector.active == tuple(models)
        assert [model.cutoffs[-1] for model in models] == [
            pytest.approx(state.wake_word_1_threshold),
            pytest.approx(state.wake_word_2_threshold),
            pytest.approx(FALLBACK_THRESHOLD),
        ]

    def test_a_wake_word_left_in_the_second_slot_keeps_its_slider(self) -> None:
        """A slot is not the same thing as a position in the active list.

        Two clicks in Home Assistant produce the difference.
        `preferences.active_wake_words` is a two-element list with holes, and
        the protocol keeps a wake word where it was — so switching the first of
        two off leaves the survivor in slot two with `None` in slot one.
        Numbering the survivors from zero would judge it by the slider the
        operator moved for the wake word they had just removed.
        """
        model = FakeMicroWakeWord("okay_nabu")
        state = state_with(
            model,
            preferences=Preferences(
                active_wake_words=[None, "okay_nabu"],
                wake_word_1_sensitivity=0.9,
                wake_word_2_sensitivity=0.31,
            ),
        )
        detector, _micro, _open = detector_over(state)

        detector.process(CHUNK)

        assert state.wake_word_2_threshold == pytest.approx(0.31)
        assert model.cutoffs == [pytest.approx(0.31)]

    def test_a_wake_word_the_preferences_do_not_place_takes_a_free_slot(
        self,
    ) -> None:
        """A fresh installation, or one wake word chosen by configuration."""
        placed = FakeMicroWakeWord("okay_nabu")
        unplaced = FakeMicroWakeWord("hey_jarvis")
        state = state_with(
            placed,
            wake_words={placed.id: placed, unplaced.id: unplaced},
            active_wake_words={placed.id, unplaced.id},
            available_wake_words={
                placed.id: available_wake_word(placed.id),
                unplaced.id: available_wake_word(unplaced.id),
            },
            preferences=Preferences(
                active_wake_words=[None, "okay_nabu"],
                wake_word_1_sensitivity=0.9,
                wake_word_2_sensitivity=0.31,
            ),
        )
        detector, _micro, _open = detector_over(state)

        detector.process(CHUNK)

        assert placed.cutoffs == [pytest.approx(0.31)]
        assert unplaced.cutoffs == [pytest.approx(0.9)]

    def test_two_models_never_share_a_slider(self) -> None:
        """A preferences file naming one wake word twice is still two models."""
        first = FakeMicroWakeWord("okay_nabu")
        second = FakeMicroWakeWord("hey_jarvis")
        state = state_with(
            first,
            wake_words={first.id: first, second.id: second},
            active_wake_words={first.id, second.id},
            available_wake_words={
                first.id: available_wake_word(first.id),
                second.id: available_wake_word(second.id),
            },
            preferences=Preferences(
                active_wake_words=["okay_nabu", "okay_nabu"],
                wake_word_1_sensitivity=0.9,
                wake_word_2_sensitivity=0.31,
            ),
        )
        detector, _micro, _open = detector_over(state)

        detector.process(CHUNK)

        assert first.cutoffs == [pytest.approx(0.9)]
        assert second.cutoffs == [pytest.approx(0.31)]

    def test_it_reads_the_threshold_again_on_every_chunk(self) -> None:
        """Home Assistant writes straight into the state while this runs."""
        model = FakeMicroWakeWord("okay_nabu")
        state = state_with(model)
        detector, _micro, _open = detector_over(state)

        detector.process(CHUNK)
        state.wake_word_1_threshold = 0.11
        detector.process(CHUNK)

        assert model.cutoffs[-1] == pytest.approx(0.11)


class TestAnnouncingTheThresholds:
    """What Home Assistant is told the sliders resolved to."""

    def test_it_syncs_and_pushes_the_three_sensitivity_entities(self) -> None:
        """Otherwise the slider shows a default the model is not judged by."""
        model = FakeMicroWakeWord("okay_nabu")
        state = state_with(
            model,
            available_wake_words={model.id: available_wake_word(model.id, cutoff=0.85)},
        )
        satellite = FakeSatellite()
        state.satellite = satellite  # type: ignore[assignment]  # the detector calls one method on it, and a real one would need a socket
        state.sensitivity_1_number_entity = FakeEntity(
            1, "wake_word_1_threshold"
        ).attach(state)  # type: ignore[assignment]  # the same, for an entity whose real class reaches a protocol server
        state.stop_sensitivity_number_entity = FakeEntity(
            3, "stop_word_threshold"
        ).attach(state)  # type: ignore[assignment]  # the same

        detector, _micro, _open = detector_over(state)
        detector.process(CHUNK)

        assert [message.key for message in satellite.sent] == [1, 3]
        assert satellite.sent[0].state == pytest.approx(0.85)

    def test_it_says_nothing_when_nobody_is_connected(self) -> None:
        """The entities carry the values already; Home Assistant reads them."""
        model = FakeMicroWakeWord("okay_nabu")
        state = state_with(model)
        entity = FakeEntity(1, "wake_word_1_threshold").attach(state)
        state.sensitivity_1_number_entity = entity  # type: ignore[assignment]  # as above
        state.satellite = None

        detector, _micro, _open = detector_over(state)
        detector.process(CHUNK)

        assert entity.syncs == 0


class TestRebuildingTheActiveList:
    """When the detector adopts a change Home Assistant made."""

    def test_it_adopts_a_change_the_protocol_announced(self) -> None:
        """`wake_words_changed` is what the protocol sets on a selection."""
        first = FakeMicroWakeWord("okay_nabu")
        second = FakeMicroWakeWord("hey_jarvis", fires=[True])
        state = state_with(
            first,
            wake_words={first.id: first, second.id: second},
            active_wake_words={first.id},
            available_wake_words={
                first.id: available_wake_word(first.id),
                second.id: available_wake_word(second.id),
            },
        )
        detector, _micro, _open = detector_over(state)
        detector.process(CHUNK)

        state.active_wake_words = {second.id}
        state.wake_words_changed = True
        activations = detector.process(CHUNK)

        assert activations.woken == (second,)

    def test_it_does_not_rebuild_on_every_chunk_when_nothing_is_active(self) -> None:
        """Upstream does, which re-announces three entities a hundred times a second."""
        model = FakeMicroWakeWord("okay_nabu")
        state = state_with(model, active_wake_words=set())
        satellite = FakeSatellite()
        state.satellite = satellite  # type: ignore[assignment]  # as above
        entity = FakeEntity(1, "wake_word_1_threshold").attach(state)
        state.sensitivity_1_number_entity = entity  # type: ignore[assignment]  # as above

        detector, _micro, _open = detector_over(state)
        for _ in range(5):
            detector.process(CHUNK)

        assert entity.syncs == 1
        assert len(satellite.sent) == 1


class TestTheStopWord:
    """What silences a response that is already playing."""

    def test_it_stops_a_response_it_is_listened_for_during(self) -> None:
        """The protocol adds the stop word to the active set while one plays."""
        model = FakeMicroWakeWord("okay_nabu")
        stop = FakeMicroWakeWord("stop", fires=[True])
        state = state_with(
            model,
            stop_word=stop,
            active_wake_words={model.id, "stop"},
        )
        detector, _micro, _open = detector_over(state)

        assert detector.process(CHUNK).stopped is True

    def test_it_does_nothing_when_no_response_is_playing(self) -> None:
        """Saying "stop" at an idle robot is saying nothing to it."""
        model = FakeMicroWakeWord("okay_nabu")
        stop = FakeMicroWakeWord("stop", fires=[True])
        state = state_with(model, stop_word=stop, active_wake_words={model.id})
        detector, _micro, _open = detector_over(state)

        assert detector.process(CHUNK).stopped is False

    def test_it_runs_even_when_no_response_is_playing(self) -> None:
        """A model fed only the audio during a response has no window."""
        model = FakeMicroWakeWord("okay_nabu")
        stop = FakeMicroWakeWord("stop")
        state = state_with(model, stop_word=stop, active_wake_words={model.id})
        detector, _micro, _open = detector_over(state)

        detector.process(CHUNK)

        assert len(stop.inputs) == 1

    def test_it_is_judged_by_its_own_sensitivity(self) -> None:
        """Which is a separate entity from the two wake-word sliders."""
        model = FakeMicroWakeWord("okay_nabu")
        stop = FakeMicroWakeWord("stop")
        state = state_with(model, stop_word=stop, stop_word_threshold=0.33)
        detector, _micro, _open = detector_over(state)

        detector.process(CHUNK)

        assert stop.cutoffs == [pytest.approx(0.33)]


class TestOpenWakeWord:
    """The other runtime, which answers with probabilities rather than a verdict."""

    def test_it_fires_when_a_probability_beats_the_threshold(self) -> None:
        """The comparison openWakeWord leaves to its caller."""
        model = FakeOpenWakeWord("hey_jarvis_v0.1", probabilities=[0.9])
        state = state_with(
            model,
            available_wake_words={
                model.id: available_wake_word(
                    model.id,
                    cutoff=0.5,
                    kind=WakeWordType.OPEN_WAKE_WORD,
                ),
            },
        )
        detector, _micro, opened = detector_over(state)

        activations = detector.process(CHUNK)

        assert activations.woken == (model,)
        assert len(opened) == 1

    def test_it_does_not_fire_below_the_threshold(self) -> None:
        """A threshold that is not applied is a wake word that fires at noise."""
        model = FakeOpenWakeWord("hey_jarvis_v0.1", probabilities=[0.2])
        state = state_with(
            model,
            available_wake_words={
                model.id: available_wake_word(
                    model.id,
                    cutoff=0.5,
                    kind=WakeWordType.OPEN_WAKE_WORD,
                ),
            },
        )
        detector, _micro, _open = detector_over(state)

        assert detector.process(CHUNK).woken == ()

    def test_its_frontend_is_never_built_when_no_such_model_is_active(self) -> None:
        """It loads two more models, and no wake word this wheel ships needs it."""
        model = FakeMicroWakeWord("okay_nabu")
        detector, _micro, opened = detector_over(state_with(model))

        detector.process(CHUNK)

        assert opened == []


class TestThePhrase:
    """What a model is called in a log line."""

    def test_it_reads_the_attribute_when_there_is_one(self) -> None:
        """Both runtimes end up carrying the phrase, by different routes."""
        assert phrase_of(FakeMicroWakeWord("okay_nabu", wake_word="Okay Nabu")) == (
            "Okay Nabu"
        )

    def test_it_falls_back_to_the_identifier(self) -> None:
        """A model built without one is still worth naming in the log."""
        assert phrase_of(FakeMicroWakeWord("okay_nabu", wake_word="")) == "okay_nabu"


@pytest.mark.filesystem
class TestAgainstTheRealModels:
    """The real runtime, over the two models this wheel ships.

    Not a unit test: it reads the `.tflite` files out of the wheel's own asset
    directory, which is the point — microWakeWord needs no downloaded weights,
    so the whole path from a chunk of audio to an `Activations` runs here for
    real.

    **These do not prove recognition.** There is no recording of anybody saying
    "okay nabu" in this repository, so the audio is synthetic and the activation
    in the second test is forced by putting the sensitivity below every
    probability the model can report. That the model answers `True` to the
    phrase and `False` to a conversation in the next room is a property of the
    model, proved on hardware and not here.
    """

    def test_synthetic_audio_does_not_wake_the_robot(self) -> None:
        """Noise at the model's own cutoff is not a wake word."""
        state = self._real_state()
        detector = WakeWordDetector(state)

        activations = [detector.process(chunk) for chunk in _noise(seconds=1.0)]

        assert all(result == Activations() for result in activations)

    def test_a_detection_reaches_the_caller(self) -> None:
        """Below every probability the model can report, everything activates.

        Which is what proves the plumbing: the real frontend produced inputs
        the real model accepted, the real model's verdict was read, and it
        arrived in `Activations` as the model object the pump hands to
        `wakeup`. The threshold is doing the work here, not the audio.

        The sensitivity is negative rather than zero because the model's
        quantised output on this synthetic noise is exactly 0.0, and the
        runtime treats a probability equal to the cutoff as no detection. It is
        outside the range Home Assistant's slider offers, deliberately: what is
        being forced is the path, not a setting anybody would choose.
        """
        state = self._real_state()
        state.preferences.wake_word_1_sensitivity = -1.0
        detector = WakeWordDetector(state)

        woken = [
            model
            for chunk in _noise(seconds=1.0)
            for model in detector.process(chunk).woken
        ]

        assert [model.id for model in woken] == ["okay_nabu"]

    def _real_state(self) -> ServerState:
        """Load the wheel's own models into a server state.

        Returns:
            The state, with `okay_nabu` active and the real stop word loaded.
        """
        from reachy_mini_ha_satellite.assets.registry import assets_dir
        from reachy_mini_ha_satellite.esphome.wake_word import (
            find_available_wake_words,
            load_stop_model,
            load_wake_models,
        )

        directories = [assets_dir() / "wakewords"]
        found = find_available_wake_words(directories, "stop")
        models, active, _fell_back = load_wake_models(found, ["okay_nabu"], "okay_nabu")
        stop_word = load_stop_model(directories, "stop")
        assert stop_word is not None
        return vendored_server_state(
            available_wake_words=found,
            wake_words=models,
            active_wake_words=active,
            stop_word=stop_word,
            preferences=Preferences(),
            # Long enough that the refractory window cannot be what a failing
            # assertion is about: one activation in a second of audio is what
            # the forced-threshold test expects to see.
            refractory_seconds=3600.0,
        )


def _noise(*, seconds: float) -> list[bytes]:
    """Make deterministic synthetic audio, in the chunks the capture port uses.

    Args:
        seconds: How much of it.

    Returns:
        Chunks of 160 samples of 16 kHz signed 16-bit little-endian audio.
    """
    generator = np.random.default_rng(seed=20260821)
    samples = generator.integers(
        -8000,
        8000,
        size=int(16000 * seconds),
        dtype=np.int16,
    )
    return [
        samples[start : start + 160].astype("<i2").tobytes()
        for start in range(0, len(samples) - 159, 160)
    ]
