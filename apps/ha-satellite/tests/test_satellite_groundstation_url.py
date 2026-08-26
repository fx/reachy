"""The groundstation address: one bound, one order, one source, one file.

Every scenario of the proposed REQ-095 is driven here, plus the boundary lengths
and failure points the change document names. Nothing opens a socket, reads a
clock or sleeps: the source is a fake, the factory is a fake, the delay between
reconstruction attempts is a recorded call, and the durable file is a real file
in an in-memory filesystem — which is what lets "the durable file was not
touched" be asserted rather than described.

The addresses are all from the RFC 5737 documentation range, and the long ones
are built rather than written out. Test module names are globally unique across
the workspace — see the root `AGENTS.md`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from satellite_support import address_of_length

from reachy_mini_ha_satellite.adapters.perception_source import FallbackPerception
from reachy_mini_ha_satellite.config import (
    ENV_PREFIX,
    GROUNDSTATION_URL_MAX_LENGTH,
    GROUNDSTATION_URL_SETTING,
    ConfigurationError,
    OverrideStore,
    Settings,
    load_settings,
)
from reachy_mini_ha_satellite.groundstation_url import (
    GroundstationUrlOwner,
    ReplaceableRemoteSource,
)
from reachy_mini_ha_satellite.ports import Detections, DetectionSource, SourceSelection

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pyfakefs.fake_filesystem import FakeFilesystem

FIRST_URL: Final = "ws://192.0.2.10:8080/v1/session"
SECOND_URL: Final = "ws://192.0.2.20:8080/v1/session"
THIRD_URL: Final = "ws://192.0.2.30:8080/v1/session"

ENVIRONMENT: Final[dict[str, str]] = {
    f"{ENV_PREFIX}DEVICE_NAME": "reachy-mini-1",
    f"{ENV_PREFIX}GROUNDSTATION_URL": FIRST_URL,
    f"{ENV_PREFIX}GROUNDSTATION_CREDENTIAL": "example-credential",
}

_OVERRIDES: Final = Path("/reachy-satellite-url/settings.json")


class FakeSource:
    """A remote source that records its lifecycle and fails where told to."""

    def __init__(
        self,
        url: str,
        *,
        start_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        """Describe a source without starting anything.

        Args:
            url: Which address this source was built for, so a test can assert
                which one is answering.
            start_error: What `start` raises, if anything.
            close_error: What `aclose` raises, if anything.
        """
        self.url = url
        self.starts = 0
        self.closes = 0
        self.connected = False
        self.start_gate: asyncio.Event | None = None
        self._start_error = start_error
        self._close_error = close_error

    async def start(self) -> None:
        """Begin, after any gate a test installed, or fail as instructed.

        Raises:
            Exception: Whatever `start_error` was given.
        """
        if self.start_gate is not None:
            await self.start_gate.wait()
        self.starts += 1
        if self._start_error is not None:
            raise self._start_error
        self.connected = True

    def latest(self) -> Detections:
        """Report one detection carrying this source's identity.

        Returns:
            A fresh remote view whose `sequence` is this source's identity, so a
            test can tell which source answered.
        """
        return Detections(
            fresh=True,
            source=DetectionSource.REMOTE,
            sequence=len(self.url),
        )

    async def aclose(self) -> None:
        """Stop, or fail as instructed.

        Raises:
            Exception: Whatever `close_error` was given.
        """
        self.closes += 1
        self.connected = False
        if self._close_error is not None:
            raise self._close_error


class FakeFactory:
    """Hands out queued sources, or raises, once per call."""

    def __init__(self, *results: FakeSource | Exception | None) -> None:
        """Queue what each call produces, in order.

        Args:
            results: A source to return, an exception to raise, or `None` for
                the composition that opens no session. The last entry is
                repeated once the queue is exhausted, so a test that only cares
                about the first few calls need not enumerate the rest.
        """
        self.results = list(results)
        self.asked: list[str] = []

    async def __call__(self, settings: Settings) -> FakeSource | None:
        """Produce the next queued result.

        Args:
            settings: The candidate configuration, recorded for assertions.

        Returns:
            The queued source, or `None`.

        Raises:
            Exception: Whatever was queued in this position.
        """
        self.asked.append(settings.groundstation_url)
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


class RecordingSleep:
    """A wait that records what it was asked for and never spends it."""

    def __init__(self) -> None:
        """Start having waited for nothing."""
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        """Record one delay and yield the loop.

        Args:
            seconds: What was asked for.
        """
        self.delays.append(seconds)
        await asyncio.sleep(0)


def build_owner(
    *,
    factory: FakeFactory,
    initial: FakeSource | None,
    environ: Mapping[str, str] | None = None,
    started: bool = True,
    attempts: int = 3,
    sleep: RecordingSleep | None = None,
) -> tuple[GroundstationUrlOwner, ReplaceableRemoteSource, OverrideStore]:
    """Assemble an owner over a fake source and a real overrides file.

    Args:
        factory: What builds replacements and rebuilds.
        initial: The source the composition root built, or `None`.
        environ: The environment to resolve against.
        started: Whether the perception chain is already running.
        attempts: How many reconstruction attempts there are in total.
        sleep: How the reconstruction state waits.

    Returns:
        The owner, the stable source it swaps behind, and the store.
    """
    source = ReplaceableRemoteSource(initial)
    if started:
        # The application's own `start` is what sets this in production; a
        # test that called it would start the fake before the scenario does.
        source._started = True
    store = OverrideStore(_OVERRIDES)
    resolution = load_settings(environ or ENVIRONMENT, store.load())
    owner = GroundstationUrlOwner(
        store=store,
        resolution=resolution,
        source=source,
        factory=factory,
        environ=environ or ENVIRONMENT,
        attempts=attempts,
        sleep=sleep or RecordingSleep(),
    )
    return owner, source, store


def stored(store: OverrideStore) -> dict[str, str]:
    """Read the durable overrides back.

    Args:
        store: Where they are kept.

    Returns:
        What the file holds, empty when it was never written.
    """
    return store.load()


class TestOneSharedBound:
    """REQ-095's shared 255-character contract, at its boundaries."""

    @pytest.mark.asyncio
    async def test_the_boundary_length_is_accepted(self, fs: FakeFilesystem) -> None:
        """255 is the limit, so 255 is a value that works.

        Args:
            fs: The in-memory filesystem the overrides file lives in.
        """
        del fs
        url = address_of_length(GROUNDSTATION_URL_MAX_LENGTH)
        replacement = FakeSource(url)
        factory = FakeFactory(replacement)
        owner, _source, store = build_owner(
            factory=factory,
            initial=FakeSource(FIRST_URL),
        )

        await owner.submit({GROUNDSTATION_URL_SETTING: url})

        assert owner.effective_url == url
        assert stored(store)[GROUNDSTATION_URL_SETTING] == url

    @pytest.mark.asyncio
    @pytest.mark.parametrize("length", [256, 512])
    async def test_an_overlong_runtime_request_changes_nothing(
        self,
        fs: FakeFilesystem,
        length: int,
    ) -> None:
        """REQ-095's overlong-runtime-request scenario, above and at the old cap.

        Args:
            fs: The in-memory filesystem.
            length: How long the submitted address is.
        """
        del fs
        running = FakeSource(FIRST_URL)
        factory = FakeFactory(FakeSource(SECOND_URL))
        owner, source, store = build_owner(factory=factory, initial=running)

        with pytest.raises(ConfigurationError) as raised:
            await owner.submit(
                {GROUNDSTATION_URL_SETTING: address_of_length(length)},
            )

        message = str(raised.value)
        assert str(GROUNDSTATION_URL_MAX_LENGTH) in message
        assert "192.0.2.10" not in message
        assert owner.effective_url == FIRST_URL
        assert stored(store) == {}
        assert factory.asked == []
        assert source.delegate is running
        assert running.closes == 0

    def test_an_overlong_entity_request_is_refused_before_anything_is_built(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """The entity path refuses without a loop, a source or a durable write.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        factory = FakeFactory(FakeSource(SECOND_URL))
        owner, source, store = build_owner(
            factory=factory,
            initial=FakeSource(FIRST_URL),
        )

        assert not owner.reserve_submission(address_of_length(256))
        assert owner.effective_url == FIRST_URL
        assert stored(store) == {}
        assert factory.asked == []
        assert source.connected is False

    @pytest.mark.asyncio
    async def test_nothing_is_truncated_to_fit(self, fs: FakeFilesystem) -> None:
        """A shortened address is a different groundstation, so none is written.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        overlong = address_of_length(300)
        factory = FakeFactory(FakeSource(SECOND_URL))
        owner, _source, store = build_owner(
            factory=factory,
            initial=FakeSource(FIRST_URL),
        )

        with pytest.raises(ConfigurationError):
            await owner.submit({GROUNDSTATION_URL_SETTING: overlong})

        assert overlong[:GROUNDSTATION_URL_MAX_LENGTH] not in str(stored(store))
        assert stored(store) == {}


class TestEitherSurfaceChangesGroundstations:
    """REQ-095's replacement scenario, from the one path both surfaces use."""

    @pytest.mark.asyncio
    async def test_durable_effective_and_read_back_advance_together(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """The whole of the success case, asserted on all three at once.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        running = FakeSource(FIRST_URL)
        replacement = FakeSource(SECOND_URL)
        factory = FakeFactory(replacement)
        owner, source, store = build_owner(factory=factory, initial=running)
        pushed: list[str] = []
        owner.publish_changes(lambda: pushed.append(owner.effective_url))

        resolved = await owner.submit({GROUNDSTATION_URL_SETTING: SECOND_URL})

        assert resolved.settings.groundstation_url == SECOND_URL
        assert owner.effective_url == SECOND_URL
        assert stored(store)[GROUNDSTATION_URL_SETTING] == SECOND_URL
        assert source.delegate is replacement
        assert replacement.starts == 1
        assert running.closes == 1
        assert pushed == [SECOND_URL]

    @pytest.mark.asyncio
    async def test_the_rest_of_the_configuration_is_adopted_after_the_commit(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """A submission carries more than the address, and the rest still applies.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        owner, _source, _store = build_owner(
            factory=FakeFactory(FakeSource(SECOND_URL)),
            initial=FakeSource(FIRST_URL),
        )
        adopted: list[Settings] = []
        owner._apply_live = adopted.append

        await owner.submit(
            {GROUNDSTATION_URL_SETTING: SECOND_URL, "idle_seconds": "9.0"},
        )

        assert [settings.groundstation_url for settings in adopted] == [SECOND_URL]
        assert [settings.idle_seconds for settings in adopted] == [9.0]

    @pytest.mark.asyncio
    async def test_a_source_that_will_not_close_does_not_end_a_compensation(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """Discarding an unused source is a best effort, reported and continued.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        candidate = FakeSource(
            SECOND_URL,
            start_error=RuntimeError("refused"),
            close_error=RuntimeError("will not close"),
        )
        rebuilt = FakeSource(FIRST_URL)
        owner, source, store = build_owner(
            factory=FakeFactory(candidate, rebuilt),
            initial=FakeSource(FIRST_URL),
        )

        with pytest.raises(ConfigurationError, match="could not be started"):
            await owner.submit({GROUNDSTATION_URL_SETTING: SECOND_URL})

        assert candidate.closes == 1
        assert source.delegate is rebuilt
        assert stored(store) == {}

    @pytest.mark.asyncio
    async def test_detections_come_only_from_the_replacement(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """A late result from the retired source has nowhere to arrive.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        running = FakeSource(FIRST_URL)
        replacement = FakeSource(SECOND_URL)
        owner, source, _store = build_owner(
            factory=FakeFactory(replacement),
            initial=running,
        )

        await owner.submit({GROUNDSTATION_URL_SETTING: SECOND_URL})

        assert source.latest().sequence == len(SECOND_URL)
        assert source.connected is True
        assert running.closes == 1

    @pytest.mark.asyncio
    async def test_local_fallback_and_reconnection_survive_the_change(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """The composed chain never learns that the object behind it changed.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        running = FakeSource(FIRST_URL)
        replacement = FakeSource(SECOND_URL)
        owner, source, _store = build_owner(
            factory=FakeFactory(replacement),
            initial=running,
        )
        local = _LocalSource()
        chain = FallbackPerception(source, local)

        await owner.submit({GROUNDSTATION_URL_SETTING: SECOND_URL})

        assert chain.latest().sequence == len(SECOND_URL)
        replacement.connected = False
        await chain.check()
        assert chain.falling_back is True

    @pytest.mark.asyncio
    async def test_a_change_that_leaves_the_address_alone_persists_first(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """Nothing is retired for a submission that changes something else.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        running = FakeSource(FIRST_URL)
        factory = FakeFactory(FakeSource(SECOND_URL))
        owner, source, store = build_owner(factory=factory, initial=running)
        adopted: list[Settings] = []
        # The composition root supplies this; injecting it is what makes the
        # adoption observable without assembling a whole application.
        owner._apply_live = adopted.append

        await owner.submit({"idle_seconds": "9.0"})

        assert stored(store) == {"idle_seconds": "9.0"}
        assert factory.asked == []
        assert source.delegate is running
        assert running.closes == 0
        assert [settings.idle_seconds for settings in adopted] == [9.0]

    @pytest.mark.asyncio
    async def test_rapid_successive_writes_are_serialized(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """Two writes at once leave one address, one file and one source.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        running = FakeSource(FIRST_URL)
        second = FakeSource(SECOND_URL)
        third = FakeSource(THIRD_URL)
        factory = FakeFactory(second, third)
        owner, source, store = build_owner(factory=factory, initial=running)

        await asyncio.gather(
            owner.submit({GROUNDSTATION_URL_SETTING: SECOND_URL}),
            owner.submit({GROUNDSTATION_URL_SETTING: THIRD_URL}),
        )

        assert owner.effective_url == THIRD_URL
        assert stored(store)[GROUNDSTATION_URL_SETTING] == THIRD_URL
        assert source.delegate is third
        assert second.closes == 1
        assert running.closes == 1


class TestFailureBeforeDurableCommit:
    """REQ-095's compensation scenario, once per step that can fail."""

    @pytest.mark.asyncio
    async def test_preparation_failure_keeps_the_running_source(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """Nothing was retired, so there is nothing to restore.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        running = FakeSource(FIRST_URL)
        factory = FakeFactory(RuntimeError("no route to that groundstation"))
        owner, source, store = build_owner(factory=factory, initial=running)

        with pytest.raises(ConfigurationError, match="could not be prepared"):
            await owner.submit({GROUNDSTATION_URL_SETTING: SECOND_URL})

        assert owner.effective_url == FIRST_URL
        assert stored(store) == {}
        assert source.delegate is running
        assert running.closes == 0

    @pytest.mark.asyncio
    async def test_retirement_failure_rebuilds_the_preceding_source(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """A source that would not close is replaced, never kept alongside.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        running = FakeSource(FIRST_URL, close_error=RuntimeError("stuck"))
        candidate = FakeSource(SECOND_URL)
        rebuilt = FakeSource(FIRST_URL)
        factory = FakeFactory(candidate, rebuilt)
        owner, source, store = build_owner(factory=factory, initial=running)

        with pytest.raises(ConfigurationError, match="could not be retired"):
            await owner.submit({GROUNDSTATION_URL_SETTING: SECOND_URL})

        assert owner.effective_url == FIRST_URL
        assert stored(store) == {}
        assert source.delegate is rebuilt
        assert candidate.closes == 1
        assert factory.asked == [SECOND_URL, FIRST_URL]

    @pytest.mark.asyncio
    async def test_startup_failure_closes_the_candidate_and_restores(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """A candidate that will not start is closed rather than installed.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        running = FakeSource(FIRST_URL)
        candidate = FakeSource(SECOND_URL, start_error=RuntimeError("refused"))
        rebuilt = FakeSource(FIRST_URL)
        owner, source, store = build_owner(
            factory=FakeFactory(candidate, rebuilt),
            initial=running,
        )

        with pytest.raises(ConfigurationError, match="could not be started"):
            await owner.submit({GROUNDSTATION_URL_SETTING: SECOND_URL})

        assert owner.effective_url == FIRST_URL
        assert stored(store) == {}
        assert candidate.closes == 1
        assert source.delegate is rebuilt

    @pytest.mark.asyncio
    async def test_commit_failure_closes_the_candidate_and_rebuilds(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """The one failure that happens after the candidate is already live.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        running = FakeSource(FIRST_URL)
        candidate = FakeSource(SECOND_URL)
        rebuilt = FakeSource(FIRST_URL)
        owner, source, store = build_owner(
            factory=FakeFactory(candidate, rebuilt),
            initial=running,
        )
        # A directory where the file belongs is what an operator's own mistake
        # looks like, and it is the failure `OverrideStore.save` reports.
        # The store is the commit point; substituting a failing one is the
        # only way to fail exactly it and nothing before it.
        owner._store = _UnwritableStore(store.path)

        with pytest.raises(ConfigurationError, match="cannot be written"):
            await owner.submit({GROUNDSTATION_URL_SETTING: SECOND_URL})

        assert owner.effective_url == FIRST_URL
        assert stored(store) == {}
        assert candidate.closes == 1
        assert source.delegate is rebuilt

    @pytest.mark.asyncio
    async def test_remote_health_is_unavailable_until_a_rebuild_succeeds(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """A source that cannot yet be rebuilt is a state, not an error.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        running = FakeSource(FIRST_URL)
        candidate = FakeSource(SECOND_URL, start_error=RuntimeError("refused"))
        owner, source, _store = build_owner(
            factory=FakeFactory(candidate, RuntimeError("still down")),
            initial=running,
            attempts=1,
        )

        with pytest.raises(ConfigurationError):
            await owner.submit({GROUNDSTATION_URL_SETTING: SECOND_URL})

        # A budget of one attempt spends no task at all, which is what "no
        # unbounded recovery work" means at its smallest.
        assert owner._restoration is None
        assert owner.remote_available is False
        assert source.connected is False
        assert source.latest() == Detections()


class TestBoundedReconstruction:
    """REQ-095's repeated-restoration and exhaustion scenarios."""

    @pytest.mark.asyncio
    async def test_repeated_failure_then_success_installs_exactly_one_source(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """The satellite stays disconnected until one rebuild works.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        candidate = FakeSource(SECOND_URL, start_error=RuntimeError("refused"))
        rebuilt = FakeSource(FIRST_URL)
        sleep = RecordingSleep()
        owner, source, _store = build_owner(
            factory=FakeFactory(
                candidate,
                RuntimeError("down"),
                RuntimeError("down"),
                rebuilt,
            ),
            initial=FakeSource(FIRST_URL),
            attempts=4,
            sleep=sleep,
        )

        with pytest.raises(ConfigurationError):
            await owner.submit({GROUNDSTATION_URL_SETTING: SECOND_URL})
        before = owner.remote_available

        await _settle(owner)

        assert before is False
        assert source.delegate is rebuilt
        assert rebuilt.starts == 1
        assert owner.remote_available is True
        assert len(sleep.delays) == 2
        assert sleep.delays == sorted(sleep.delays)

    @pytest.mark.asyncio
    async def test_exhaustion_stops_and_leaves_local_detection_available(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """REQ-095's bound scenario: no unbounded work, no overlapping client.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        candidate = FakeSource(SECOND_URL, start_error=RuntimeError("refused"))
        factory = FakeFactory(candidate, RuntimeError("down"))
        sleep = RecordingSleep()
        owner, source, store = build_owner(
            factory=factory,
            initial=FakeSource(FIRST_URL),
            attempts=3,
            sleep=sleep,
        )
        local = _LocalSource()
        chain = FallbackPerception(source, local)

        with pytest.raises(ConfigurationError):
            await owner.submit({GROUNDSTATION_URL_SETTING: SECOND_URL})
        await _settle(owner)

        assert owner.effective_url == FIRST_URL
        assert stored(store) == {}
        assert owner.remote_available is False
        # Three restoration attempts: the inline one and two from the retry
        # state, after the one that built the candidate.
        assert factory.asked == [SECOND_URL, FIRST_URL, FIRST_URL, FIRST_URL]
        assert len(sleep.delays) == 2
        await chain.check()
        assert chain.falling_back is True

    @pytest.mark.asyncio
    async def test_a_partial_source_is_closed_before_the_next_attempt(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """A source built and then refused must not survive into the next try.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        candidate = FakeSource(SECOND_URL, start_error=RuntimeError("refused"))
        partial = FakeSource(FIRST_URL, start_error=RuntimeError("refused"))
        rebuilt = FakeSource(FIRST_URL)
        owner, source, _store = build_owner(
            factory=FakeFactory(candidate, partial, rebuilt),
            initial=FakeSource(FIRST_URL),
            attempts=3,
        )

        with pytest.raises(ConfigurationError):
            await owner.submit({GROUNDSTATION_URL_SETTING: SECOND_URL})
        await _settle(owner)

        assert partial.closes == 1
        assert source.delegate is rebuilt


class TestSupersessionAndShutdown:
    """REQ-095's superseded-or-shut-down scenario."""

    @pytest.mark.asyncio
    async def test_a_later_write_cancels_and_awaits_reconstruction(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """The pending restoration cannot publish a source after a new write.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        candidate = FakeSource(SECOND_URL, start_error=RuntimeError("refused"))
        third = FakeSource(THIRD_URL)
        factory = FakeFactory(candidate, RuntimeError("down"), third)
        owner, source, store = build_owner(
            factory=factory,
            initial=FakeSource(FIRST_URL),
            attempts=9,
        )

        with pytest.raises(ConfigurationError):
            await owner.submit({GROUNDSTATION_URL_SETTING: SECOND_URL})
        # The retry state is private, and its existence is exactly what this
        # asserts a later write finishes.
        restoration = owner._restoration
        assert restoration is not None

        await owner.submit({GROUNDSTATION_URL_SETTING: THIRD_URL})

        assert restoration.done()
        assert owner.effective_url == THIRD_URL
        assert stored(store)[GROUNDSTATION_URL_SETTING] == THIRD_URL
        assert source.delegate is third
        assert third.starts == 1

    @pytest.mark.asyncio
    async def test_shutdown_cancels_reconstruction_and_installs_nothing(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """Shutdown awaits the retry state before anything closes the source.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        candidate = FakeSource(SECOND_URL, start_error=RuntimeError("refused"))
        late = FakeSource(FIRST_URL)
        owner, source, _store = build_owner(
            factory=FakeFactory(candidate, RuntimeError("down"), late),
            initial=FakeSource(FIRST_URL),
            attempts=9,
        )

        with pytest.raises(ConfigurationError):
            await owner.submit({GROUNDSTATION_URL_SETTING: SECOND_URL})
        restoration = (
            owner._restoration
        )  # asserting that shutdown finished the retry state needs the task itself

        await owner.aclose()

        assert restoration is not None
        assert restoration.done()
        assert late.starts == 0
        assert source.delegate is None

    @pytest.mark.asyncio
    async def test_a_partial_source_held_by_a_cancelled_attempt_is_closed(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """A source the factory produced and cancellation interrupted is closed.

        The attempt cannot close it itself: awaiting a close inside a cancelled
        task is interrupted at its first suspension, so whoever cancelled does
        it. That is the difference between "no late client is installed" and
        "no late client exists".

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        candidate = FakeSource(SECOND_URL, start_error=RuntimeError("refused"))
        late = FakeSource(FIRST_URL)
        late.start_gate = asyncio.Event()
        owner, source, _store = build_owner(
            factory=FakeFactory(candidate, RuntimeError("down"), late),
            initial=FakeSource(FIRST_URL),
            attempts=9,
        )

        with pytest.raises(ConfigurationError):
            await owner.submit({GROUNDSTATION_URL_SETTING: SECOND_URL})
        # Let the retry state reach the gate inside the late source's start.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        await owner.aclose()

        assert late.closes == 1
        assert late.starts == 0
        assert source.delegate is None

    @pytest.mark.asyncio
    async def test_a_factory_result_from_a_stale_generation_is_not_installed(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """The generation check is what makes supersession final.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        late = FakeSource(FIRST_URL)
        owner, source, _store = build_owner(
            factory=FakeFactory(late),
            initial=FakeSource(FIRST_URL),
        )
        source.detach()

        # One attempt is the unit here: driving it through a public write
        # would hide which of the two checks rejected the result.
        settled = await owner._install_fresh(
            owner.resolution.settings,
            # A generation a later write has already advanced past.
            owner._generation - 1,
        )

        assert settled is True
        assert late.closes == 1
        assert late.starts == 0
        assert source.delegate is None

    @pytest.mark.asyncio
    async def test_a_generation_that_advances_during_startup_rejects_the_source(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """The second check: cancellation can land while the source is starting.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        owner, source, _store = build_owner(
            factory=FakeFactory(None),
            initial=FakeSource(FIRST_URL),
        )
        late = _SupersedingSource(FIRST_URL, owner)
        owner._factory = FakeFactory(late)
        source.detach()

        settled = await owner._install_fresh(
            owner.resolution.settings,
            owner._generation,
        )

        assert settled is True
        assert late.starts == 1
        assert late.closes == 1
        assert source.delegate is None

    @pytest.mark.asyncio
    async def test_a_submission_after_shutdown_is_refused(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """Nothing is built or written once shutdown has begun.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        factory = FakeFactory(FakeSource(SECOND_URL))
        owner, _source, store = build_owner(
            factory=factory,
            initial=FakeSource(FIRST_URL),
        )
        await owner.aclose()

        with pytest.raises(ConfigurationError, match="shutting down"):
            await owner.submit({GROUNDSTATION_URL_SETTING: SECOND_URL})
        assert owner.reserve_submission(SECOND_URL) is False
        assert factory.asked == []
        assert stored(store) == {}

    @pytest.mark.asyncio
    async def test_shutdown_finishes_a_reserved_submission_before_returning(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """A transition reserved from the protocol loop is nobody else's to await.

        Nothing outside this owner holds that task, so shutdown finishing it is
        what stops a replacement starting against a chain being released — and
        what stops it outliving the application. One reserved and not yet begun
        is refused rather than started, which is the same rule `submit` applies.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        replacement = FakeSource(SECOND_URL)
        factory = FakeFactory(replacement)
        owner, source, store = build_owner(
            factory=factory,
            initial=FakeSource(FIRST_URL),
        )

        assert owner.reserve_submission(SECOND_URL) is True
        await owner.aclose()

        assert owner._requested == set()
        assert factory.asked == []
        assert owner.effective_url == FIRST_URL
        assert stored(store) == {}
        assert replacement.starts == 0
        assert source.delegate is not replacement

    @pytest.mark.asyncio
    async def test_resubmitting_the_stored_address_writes_nothing(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """A scene re-sending the value already stored spends no erase cycle.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        factory = FakeFactory(FakeSource(SECOND_URL))
        owner, _source, store = build_owner(
            factory=factory,
            initial=FakeSource(FIRST_URL),
        )
        assert owner.reserve_submission(SECOND_URL) is True
        await _drain(owner)
        writes = _CountingStore(store.path)
        owner._store = writes

        assert owner.reserve_submission(SECOND_URL) is True
        await _drain(owner)

        assert writes.saves == 0
        assert owner.effective_url == SECOND_URL

    @pytest.mark.asyncio
    async def test_closing_twice_is_harmless(self, fs: FakeFilesystem) -> None:
        """Shutdown runs once however many times it is asked for.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        owner, _source, _store = build_owner(
            factory=FakeFactory(FakeSource(SECOND_URL)),
            initial=FakeSource(FIRST_URL),
        )

        await owner.aclose()
        await owner.aclose()


class TestRestartAgreement:
    """REQ-095's restart scenario, and the local-only composition."""

    @pytest.mark.asyncio
    async def test_the_adopted_address_is_effective_after_a_restart(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """The durable file is what the next start reads, with no redeployment.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        owner, _source, store = build_owner(
            factory=FakeFactory(FakeSource(SECOND_URL)),
            initial=FakeSource(FIRST_URL),
        )

        await owner.submit({GROUNDSTATION_URL_SETTING: SECOND_URL})
        restarted = load_settings(ENVIRONMENT, OverrideStore(store.path).load())

        assert restarted.settings.groundstation_url == SECOND_URL
        assert owner.effective_url == restarted.settings.groundstation_url

    @pytest.mark.asyncio
    async def test_a_refused_change_is_not_what_the_next_start_reads(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """Persisting first would make restart adopt what runtime rejected.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        owner, _source, store = build_owner(
            factory=FakeFactory(
                FakeSource(SECOND_URL, start_error=RuntimeError("refused")),
                FakeSource(FIRST_URL),
            ),
            initial=FakeSource(FIRST_URL),
        )

        with pytest.raises(ConfigurationError):
            await owner.submit({GROUNDSTATION_URL_SETTING: SECOND_URL})
        restarted = load_settings(ENVIRONMENT, OverrideStore(store.path).load())

        assert restarted.settings.groundstation_url == FIRST_URL

    @pytest.mark.asyncio
    async def test_a_local_only_composition_writes_without_a_remote_instance(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """The robot's own detector still has a durable, changeable address.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        environ = {
            **ENVIRONMENT,
            # The dotted member name reads as an mDNS hostname suffix to the
            # leak scanner; it is this repository's own enumeration.
            f"{ENV_PREFIX}DETECTION_SOURCE": SourceSelection.LOCAL.value,  # leak-scan:allow
            f"{ENV_PREFIX}LOCAL_MODEL_PATH": "/models/face.tflite",
        }
        factory = FakeFactory(None)
        owner, source, store = build_owner(
            factory=factory,
            initial=None,
            environ=environ,
        )

        await owner.submit({GROUNDSTATION_URL_SETTING: SECOND_URL})

        assert owner.effective_url == SECOND_URL
        assert stored(store)[GROUNDSTATION_URL_SETTING] == SECOND_URL
        assert source.delegate is None
        assert owner.remote_available is False
        assert factory.asked == [SECOND_URL]


class TestReplaceableRemoteSource:
    """The one reference the composed chain keeps for the whole application."""

    @pytest.mark.asyncio
    async def test_it_answers_nothing_while_it_has_no_delegate(self) -> None:
        """Which is remote health unavailable, not a robot that cannot see."""
        source = ReplaceableRemoteSource()

        await source.start()

        assert source.connected is False
        assert source.latest() == Detections()
        await source.aclose()

    @pytest.mark.asyncio
    async def test_starting_it_starts_the_source_it_holds(self) -> None:
        """And records that the chain is running, which the owner reads."""
        delegate = FakeSource(FIRST_URL)
        source = ReplaceableRemoteSource(delegate)

        await source.start()

        assert source.started is True
        assert delegate.starts == 1
        assert source.connected is True

    @pytest.mark.asyncio
    async def test_closing_it_closes_the_source_it_holds_once(self) -> None:
        """The perception chain owns the close; the owner never duplicates it."""
        delegate = FakeSource(FIRST_URL)
        source = ReplaceableRemoteSource(delegate)
        await source.start()

        await source.aclose()
        await source.aclose()

        assert delegate.closes == 1

    @pytest.mark.asyncio
    async def test_a_replacement_prepared_before_startup_is_not_started(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """Starting one then would open a session before the application ran.

        Args:
            fs: The in-memory filesystem.
        """
        del fs
        replacement = FakeSource(SECOND_URL)
        owner, source, _store = build_owner(
            factory=FakeFactory(replacement),
            initial=FakeSource(FIRST_URL),
            started=False,
        )

        await owner.submit({GROUNDSTATION_URL_SETTING: SECOND_URL})

        assert source.delegate is replacement
        assert replacement.starts == 0
        await source.start()
        assert replacement.starts == 1


class _SupersedingSource(FakeSource):
    """A source whose own startup is overtaken by a later transition."""

    def __init__(self, url: str, owner: GroundstationUrlOwner) -> None:
        """Hold the owner whose generation this source advances.

        Args:
            url: Which address it was built for.
            owner: Whose generation to advance from inside `start`.
        """
        super().__init__(url)
        self._owner = owner

    async def start(self) -> None:
        """Start, and let a later write land while doing so."""
        await super().start()
        self._owner._generation += 1


class _LocalSource:
    """The robot's own detector, as the fallback composition sees it."""

    def __init__(self) -> None:
        """Start having produced nothing."""
        self.started = False

    async def start(self) -> None:
        """Begin."""
        self.started = True

    def latest(self) -> Detections:
        """Report one local detection.

        Returns:
            A fresh local view.
        """
        # As above: the dotted member name is an enumeration of this
        # repository's, not a hostname.
        return Detections(fresh=True, source=DetectionSource.LOCAL)  # leak-scan:allow

    async def aclose(self) -> None:
        """Stop."""
        self.started = False


class _CountingStore(OverrideStore):
    """A store that counts what was written through it."""

    def __init__(self, path: Path) -> None:
        """Start having written nothing.

        Args:
            path: Where the overrides are kept.
        """
        super().__init__(path)
        self.saves = 0

    def save(self, overrides: Mapping[str, str]) -> None:
        """Count and perform one write.

        Args:
            overrides: What to write.
        """
        self.saves += 1
        super().save(overrides)


class _UnwritableStore(OverrideStore):
    """A store whose commit fails the way an unwritable state directory does."""

    def save(self, overrides: Mapping[str, str]) -> None:
        """Refuse to write.

        Args:
            overrides: What would have been written.

        Raises:
            ConfigurationError: Always, naming the file as the real store does.
        """
        del overrides
        message = f"the settings overrides at {self.path} cannot be written: refused"
        raise ConfigurationError(message)


async def _drain(owner: GroundstationUrlOwner) -> None:
    """Let every submission reserved from the protocol loop finish.

    Args:
        owner: Whose reserved work to wait for. It has no public handle,
            deliberately: production awaits it only at shutdown.
    """
    for requested in tuple(owner._requested):
        await requested


async def _settle(owner: GroundstationUrlOwner) -> None:
    """Let the bounded reconstruction state run to its own conclusion.

    Args:
        owner: Whose retry state to wait for. Its delays are recorded rather
            than spent, so this returns as soon as the attempts are exhausted or
            one of them succeeds.
    """
    # Driving the retry state to its fixed point is what these tests are
    # about, and it has no public handle by design.
    restoration = owner._restoration
    if restoration is not None:
        await restoration
