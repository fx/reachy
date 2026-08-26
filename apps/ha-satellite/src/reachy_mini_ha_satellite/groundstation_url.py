"""One owner of the groundstation address: its source, its file and its order.

The settings page and the Home Assistant text entity both change the same
address, and until this module existed neither could: `apply_settings_change`
persists first and adopts second, and the thing that would have to be adopted —
a session client bound to a URL at construction — is built once by the
composition root and never replaced.

**Persisting first is the wrong order for this setting**, and the reason is what
the rest of this module is shaped by. A durable write that lands and a runtime
adoption that then fails leaves the next start reading an address the running
application refused, so a robot recovers into the configuration that broke it.
The order here is therefore prepare, retire, start, **commit**, publish: the
atomic file replacement `OverrideStore.save` already performs is the commit
point, and everything before it is undone rather than recorded.

**Construction failure belongs here rather than to the connectivity
supervisor.** `RemotePerception` supervises a session; it cannot supervise an
object that failed before it existed. So this owner keeps one bounded, capped
and cancellable reconstruction state, closes any partially built source before
the next attempt, and hands exactly one complete source to that supervisor —
after which ordinary connect and reconnect are its business and not this
module's.

**One generation, one lock, one source.** A later operator write cancels and
awaits the reconstruction state before it prepares anything, shutdown does the
same without starting anything, and a factory result that returns after either
is closed rather than installed. That is what excludes two clients existing at
once, which is the failure this ordering exists to prevent and the one a
compensating state machine can otherwise produce.

**Every await in the transition is followed by a validity check**, because
`aclose` marks the owner closed *before* it takes the lock: shutdown therefore
becomes true between two statements of a transition already under way, not only
at its start. Without the checks, a submission that had begun first would go on
to start a client, write the durable file and publish an address after the
application had begun releasing the chain. Losing the check compensates —
closing whatever was built — rather than proceeding.

**An unconfirmed close is unavailability, not retirement.** A source whose
`aclose` raised may still hold a session, so it is recorded as outstanding and
nothing else is built or installed until it is gone. One further close is
attempted at the next transition, which is where the ordinary cause — a
groundstation that went away mid-close — resolves; a source that still refuses
leaves remote health unavailable with the preceding address durable and local
detection working, which is the honest state rather than a second client
alongside a first that may be alive.

**A transition that would leave no source at all is refused.** Whether a session
exists is decided by settings that take effect at the next start, so a
submission changing one of them together with the address would close the
running source, install nothing, commit and report success. The decision is read
off the factory's own answer — no candidate while one is running — rather than
off a list of the settings that produce it, which is a list that goes stale
without anything noticing.

**One lock, and the merge happens inside it.** The settings page submits a whole
form and Home Assistant's control submits one setting, so the control's merge
against the stored file is performed here rather than by the caller: two writers
computing a merge from a copy read before the lock is how one of them silently
drops the other's setting.

This is compensation, not a transaction. A filesystem and a network cannot be
committed together, and claiming they can is how the claim stops being checked.
What is guaranteed is narrower and testable: after every outcome the durable
value, the sole eligible remote source and the effective read-back describe the
same address, and local fallback and bounded reconnection still work.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Final

from reachy_mini_ha_satellite.config import (
    GROUNDSTATION_URL_SETTING,
    ConfigurationError,
    apply_settings_change,
    load_settings,
    validate_groundstation_url_length,
)
from reachy_mini_ha_satellite.ports import Detections
from reachy_session_client import DEFAULT_BACKOFF

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from reachy_mini_ha_satellite.adapters.perception_source import ConnectableSource
    from reachy_mini_ha_satellite.config import OverrideStore, Resolution, Settings
    from reachy_session_client import Backoff

__all__ = [
    "DEFAULT_RESTORE_ATTEMPTS",
    "GroundstationUrlOwner",
    "ReplaceableRemoteSource",
    "SourceFactory",
]

_LOGGER: Final = logging.getLogger(__name__)

# How many times the preceding source is rebuilt before the owner stops trying.
# Bounded because the alternative is a robot spending its remaining cores on a
# groundstation that is not coming back, and capped rather than immediate
# because the ordinary cause is a restart that finishes in a second or two. The
# delays are the session client's own backoff, so "how long between attempts"
# has one answer on this robot rather than two.
DEFAULT_RESTORE_ATTEMPTS: Final = 5

# What a transition says when shutdown overtook it. One sentence for every
# awaiting point, because the operator's question is the same at all of them and
# the answer is too: the durable file holds what it held before.
_OVERTAKEN_MESSAGE: Final = "the application is shutting down; nothing was changed"

# What a transition says when a preceding source has still not closed. Refused
# rather than performed: building another one is the overlap this owner exists
# to exclude, and the operator can retry once the groundstation has let go.
_OUTSTANDING_MESSAGE: Final = (
    "a preceding groundstation source has not closed, so no replacement was "
    "built; the address in effect is unchanged"
)

# What a transition says when the submission would leave the robot with no
# groundstation source at all while one is running. The settings that decide
# whether a session exists take effect at the next start, so a save that changes
# one of them *and* the address would retire the running source into nothing —
# and the page would report success over a satellite that had stopped seeing.
# The message names no setting: which ones they are is the factory's business,
# and a list here is one that goes stale silently.
_RETIRES_INTO_NOTHING_MESSAGE: Final = (
    "the submitted configuration opens no groundstation session while one is "
    "running, so the address was not changed. The settings that decide whether "
    "a session exists at all take effect at the next start; submit them without "
    "the address."
)

# What builds a remote source from a candidate configuration. Asynchronous
# because the interesting failure is a result that arrives *after* the request
# that asked for it was superseded or shut down, and a synchronous factory has
# nowhere for that to happen. `None` is the local-only composition: an operator
# who selected the robot's own detector still has a durable address and a Home
# Assistant entity, and manufacturing a session client for them would open a
# connection nobody asked for.
#
# A `type` statement rather than an assignment: `Callable` is imported for type
# checking only, and an assigned alias would evaluate it at import time.
type SourceFactory = Callable[[Settings], Awaitable[ConnectableSource | None]]


class ReplaceableRemoteSource:
    """The one remote source the perception chain holds, whoever built it.

    `FallbackPerception` and the behaviour layer are handed this once and never
    learn that the object behind it changed. That indirection is what makes a
    replacement possible at all: both hold their source for the lifetime of the
    application, so swapping the `RemotePerception` itself would leave them
    talking to the retired one.

    With no delegate it reports itself disconnected and sees nothing, which is
    exactly the state a local-fallback composition acts on — so a reconstruction
    that has not yet succeeded, or has exhausted its attempts, leaves local
    detection available rather than leaving the robot blind.
    """

    def __init__(self, delegate: ConnectableSource | None = None) -> None:
        """Hold the source the composition root built, if it built one.

        Args:
            delegate: The initial remote source, or `None` for a composition
                that has no groundstation.
        """
        self._delegate = delegate
        self._started = False

    @property
    def started(self) -> bool:
        """Whether the application has started the perception chain.

        Returns:
            True once `start` has been called. The owner reads it so that a
            replacement prepared before startup is installed rather than
            started — starting one then would open a session before the
            application had begun running.
        """
        return self._started

    @property
    def delegate(self) -> ConnectableSource | None:
        """The source currently answering.

        Returns:
            It, or `None` while there is none — which is a remote health of
            unavailable rather than an error.
        """
        return self._delegate

    def install(self, delegate: ConnectableSource) -> None:
        """Adopt a source the owner has already prepared and started.

        Args:
            delegate: The new source. The owner detaches and closes the previous
                one first, so this never replaces a live source silently.
        """
        self._delegate = delegate

    def detach(self) -> ConnectableSource | None:
        """Stop answering from the current source and hand it back.

        Returns:
            The retired source, for its owner to close, or `None`.
        """
        delegate, self._delegate = self._delegate, None
        return delegate

    @property
    def connected(self) -> bool:
        """Whether a session is up right now.

        Returns:
            What the current source says, or False while there is none.
        """
        delegate = self._delegate
        return delegate is not None and delegate.connected

    async def start(self) -> None:
        """Start the current source, and remember that the chain is running."""
        self._started = True
        delegate = self._delegate
        if delegate is not None:
            await delegate.start()

    def latest(self) -> Detections:
        """Say what the current source last saw.

        Returns:
            Its view, or the empty not-fresh view while there is no source —
            which is what makes the head neutral rather than held.
        """
        delegate = self._delegate
        return Detections() if delegate is None else delegate.latest()

    async def aclose(self) -> None:
        """Close whatever source is installed, once."""
        delegate = self.detach()
        self._started = False
        if delegate is not None:
            await delegate.aclose()


class GroundstationUrlOwner:
    """The serialized transition, the durable commit and the retry state.

    One instance for the life of the application, whether or not a remote source
    exists. Every settings submission passes through `submit`, so a change of
    the address and a change of anything else cannot interleave.
    """

    def __init__(
        self,
        *,
        store: OverrideStore,
        resolution: Resolution,
        source: ReplaceableRemoteSource,
        factory: SourceFactory,
        environ: Mapping[str, str] | None = None,
        apply_live: Callable[[Settings], None] | None = None,
        backoff: Backoff = DEFAULT_BACKOFF,
        attempts: int = DEFAULT_RESTORE_ATTEMPTS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Retain the factory, the resolution and the source, and nothing else.

        Args:
            store: Where the durable overrides are kept. Its atomic replacement
                is this transition's commit point.
            resolution: The settings in effect, which is also the preceding
                resolution every compensation rebuilds from.
            source: The stable remote source the perception chain holds.
            factory: How to build a remote source for a candidate configuration.
            environ: The environment a candidate resolves against. Defaults to
                the process environment, as `load_settings` does.
            apply_live: What adopts the rest of a resolved configuration. `None`
                when nothing is running yet.
            backoff: The increasing, capped delay between reconstruction
                attempts.
            attempts: How many reconstruction attempts there are in total,
                including the one performed inline by the compensation itself.
            sleep: How to wait between attempts. Injected so the suite drives an
                exhausted retry without spending one.
        """
        self._store = store
        self._resolution = resolution
        self._source = source
        self._factory = factory
        self._environ = environ
        self._apply_live = apply_live
        self._backoff = backoff
        self._attempts = attempts
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._generation = 0
        self._restoration: asyncio.Task[None] | None = None
        # A source the factory has produced and nothing has installed yet.
        # Cancelling a reconstruction cannot await inside the task it just
        # cancelled, so the canceller closes this instead — which is what stops
        # a cancelled attempt leaking the client it had just built.
        self._partial: ConnectableSource | None = None
        # Sources whose close raised, so nothing knows whether the session
        # behind them is gone. Retained rather than forgotten: one of them may
        # still be a live client, and building another alongside it is exactly
        # what this owner promises never to do.
        self._outstanding: list[ConnectableSource] = []
        # Submissions reserved from the protocol's message loop, which cannot
        # await one. Retained so shutdown finishes them rather than leaving a
        # transition running against a chain that is being released.
        self._requested: set[asyncio.Task[None]] = set()
        self._publish: Callable[[], None] = _publish_nothing
        self._closed = False

    @property
    def effective_url(self) -> str:
        """The address in effect, which is also the durable one.

        Returns:
            What every surface reports. It advances only after a commit, so a
            refused or compensated change reads back as the preceding address.
        """
        return self._resolution.settings.groundstation_url

    @property
    def resolution(self) -> Resolution:
        """The configuration in effect after the last successful submission.

        Returns:
            It, for the settings page to render.
        """
        return self._resolution

    @property
    def remote_available(self) -> bool:
        """Whether a remote source exists to be connected at all.

        Returns:
            False while reconstruction has not yet installed one, which is
            "remote health unavailable" rather than a failure — local fallback
            is unaffected.
        """
        return self._source.delegate is not None

    def publish_changes(self, publish: Callable[[], None]) -> None:
        """Hand over what tells Home Assistant the address in effect.

        Separate from the constructor for the reason
        `SatelliteApplication.publish_live_changes` is: the entity that reports
        the address is built *from* this owner and so does not exist yet.

        Args:
            publish: What to call once the read-back may have changed. Called
                after a commit and after an asynchronous refusal, so a Home
                Assistant control that optimistically showed the requested
                address is corrected either way.
        """
        self._publish = publish

    def reserve_submission(self, url: str) -> bool:
        """Refuse or schedule one replacement, from the protocol's own loop.

        The ESPHome message loop is synchronous and must not be held while a
        session is retired and another opened, so this validates what it can
        without waiting and schedules the rest — the same arrangement
        `MotorGroupCoordinator.reserve_transition` uses, and for the same
        reason.

        Args:
            url: The address Home Assistant submitted.

        Returns:
            True when a transition was scheduled. False when the address was
            refused before anything was built or written, which leaves the
            preceding effective value as the read-back.
        """
        if self._closed:
            return False
        try:
            validate_groundstation_url_length(url)
        except ConfigurationError as error:
            # Reported rather than raised: this runs inside the protocol's
            # message loop and a refused address must not drop the connection.
            _LOGGER.error("the groundstation address was refused: %s", error)
            return False
        task = asyncio.get_running_loop().create_task(
            self._submit_requested(url),
            name="satellite-groundstation-url",
        )
        self._requested.add(task)
        task.add_done_callback(self._requested.discard)
        return True

    async def _submit_requested(self, url: str) -> None:
        """Merge one address into the stored overrides and submit them.

        Args:
            url: The address, already known to be short enough.
        """
        try:
            await self.submit_url(url)
        except ConfigurationError as error:
            # `OverrideStore.load` raises this for a file somebody hand-edited
            # into invalid JSON, which is exactly when a control is likeliest to
            # be reached for, and `submit` raises it for everything it refused
            # or compensated. Neither may escape into the loop.
            _LOGGER.error("the groundstation address could not be changed: %s", error)
            # A success has already pushed the committed address from `_apply`,
            # which is the one call site for that. This is the other outcome: a
            # control that optimistically moved is told the preceding address is
            # still the one in effect — unless the refusal *is* shutdown, when
            # pushing into a protocol layer being released helps nobody.
            if not self._closed:
                with contextlib.suppress(Exception):
                    self._publish()

    async def submit(self, wanted: Mapping[str, str]) -> Resolution:
        """Apply one complete set of overrides, in the one order that is safe.

        The settings page's write path. Serialized against every other
        submission and against shutdown, so rapid successive writes queue rather
        than interleave.

        Args:
            wanted: The complete set of overrides to store, by setting name.

        Returns:
            The settings in effect after the change.

        Raises:
            ConfigurationError: If the overrides do not resolve, if the
                replacement could not be prepared, retired, started or
                committed, if a preceding source is still outstanding, or if
                the owner is closed. Every failure before the commit leaves the
                durable file untouched and the preceding address effective.
        """
        async with self._lock:
            return await self._apply(wanted)

    async def submit_url(self, url: str) -> Resolution | None:
        """Change one address, merging it into whatever else is stored.

        Home Assistant's control owns one setting, so it submits one rather than
        a whole form — and the merge that turns it into a complete set of
        overrides happens **under the lock**, after the stored file is read
        there. Two surfaces write that file now; a merge computed from a copy
        read before the lock silently drops whichever setting the other surface
        committed in between.

        Args:
            url: The address, already known to be short enough.

        Returns:
            The settings in effect afterwards, or `None` when the stored
            overrides already said this and nothing was written.

        Raises:
            ConfigurationError: For everything `submit` raises, and for stored
                overrides that no longer parse.
        """
        async with self._lock:
            if self._closed:
                raise ConfigurationError(_OVERTAKEN_MESSAGE)
            previous = self._store.load()
            wanted = {**previous, GROUNDSTATION_URL_SETTING: url}
            if wanted == previous:
                # The stored overrides already say this, so the address in
                # effect already is this. Re-resolving and rewriting the file
                # would spend an erase cycle on the robot's card to arrive at
                # what is there — the same write `build_boost_setter` and the
                # vendored `ServerState.persist_volume` both decline.
                await self._restore_if_unavailable()
                return None
            return await self._apply(wanted)

    async def _apply(self, wanted: Mapping[str, str]) -> Resolution:
        """Apply one complete set of overrides, with the lock already held.

        Args:
            wanted: The complete set of overrides to store, by setting name.

        Returns:
            The settings in effect after the change.

        Raises:
            ConfigurationError: As `submit` describes.
        """
        if self._closed:
            raise ConfigurationError(_OVERTAKEN_MESSAGE)
        submitted = wanted.get(GROUNDSTATION_URL_SETTING)
        if submitted is not None:
            # Before `load_settings`, which has a length check of its own — but
            # that one is the **startup migration**, and it tells an operator to
            # remove an entry from a file this submission has not written and to
            # restart an application that is running. This is the runtime
            # refusal, and putting it here is what makes it the first thing both
            # surfaces reach: the entity path runs it earlier still, without a
            # loop, because `reserve_submission` cannot await.
            validate_groundstation_url_length(submitted)
        resolved = load_settings(self._environ, wanted)
        if resolved.settings.groundstation_url == self.effective_url:
            # Nothing to retire or start, so the released order is the right
            # one and this is the one definition of it.
            applied = apply_settings_change(
                wanted,
                store=self._store,
                environ=self._environ,
                apply_live=self._apply_live,
            )
            self._resolution = applied
            await self._restore_if_unavailable()
            return applied
        return await self._replace(wanted, resolved)

    async def _restore_if_unavailable(self) -> None:
        """Begin one fresh restoration for a submission that changed no address.

        Resubmitting the address already in effect is what an operator does once
        a groundstation they were told is unreachable comes back, and short of a
        restart it is the only recovery there is: the branch above returns
        before the factory is ever asked, so without this the page would answer
        "saved" over a satellite that stays disconnected.
        `docs/ops/satellite-deployment.md` records that a later submission
        begins a fresh attempt, and this is where that becomes true.

        Bounded like every other restoration — the same attempt count and the
        same backoff — and it starts by cancelling whatever is already running,
        so repeated saves cannot accumulate reconstruction states.
        """
        if self.remote_available:
            return
        await self._cancel_restoration()
        # The same post-await checks `_replace` makes, for the same reason.
        # `_superseded` against the current generation is the "has shutdown
        # begun" question here: no later write can be in flight, because this
        # runs with the lock held.
        if self._superseded(self._generation):
            return
        if self._outstanding and not await self._settle_outstanding():
            self._report_outstanding()
            return
        if self._superseded(self._generation):
            return
        self._generation += 1
        await self._restore_preceding(self._generation)

    async def _replace(
        self,
        wanted: Mapping[str, str],
        resolved: Resolution,
    ) -> Resolution:
        """Carry out the transition, compensating whatever step fails.

        Args:
            wanted: The overrides to commit.
            resolved: What they resolve to.

        Returns:
            The resolution now in effect.

        Raises:
            ConfigurationError: If any step failed, if shutdown overtook the
                transition, or if a preceding source is still outstanding. The
                preceding address remains durable, effective and the read-back.
        """
        await self._cancel_restoration()
        # The first of the checks that follow every await here. Cancelling the
        # restoration awaits a task, and `aclose` marks the owner closed before
        # it takes the lock this call holds, so shutdown lands *between*
        # statements rather than only before the first one.
        await self._abort_if_overtaken(self._generation, None)
        if self._outstanding and not await self._settle_outstanding():
            # A source that may still hold a session is outstanding, so nothing
            # is built: the durable value stays effective and the operator can
            # submit again once the groundstation has let go.
            self._report_outstanding()
            raise ConfigurationError(_OUTSTANDING_MESSAGE)
        await self._abort_if_overtaken(self._generation, None)
        self._generation += 1
        generation = self._generation

        try:
            candidate = await self._factory(resolved.settings)
        except Exception as error:
            # Nothing was retired, so the running source is still the preceding
            # one and there is nothing to restore.
            message = (
                f"the replacement groundstation source could not be prepared: {error}"
            )
            raise ConfigurationError(message) from error
        # A factory result that arrives after shutdown is the case this owner's
        # asynchronous factory exists to make representable. Nothing has been
        # retired, so closing the candidate is the whole compensation.
        await self._abort_if_overtaken(generation, candidate)

        if candidate is None and self._source.delegate is not None:
            # The submitted configuration opens no session while one is running.
            # Retiring into nothing would close the live source, install
            # nothing, commit and report success — a satellite that has stopped
            # seeing, with no surface saying so. Refused instead, and decided
            # from the factory's own answer rather than from a list of the
            # settings that produce it, so it keeps holding when that list
            # grows.
            raise ConfigurationError(_RETIRES_INTO_NOTHING_MESSAGE)

        retired = self._source.detach()
        if retired is not None:
            refused = await self._discard(retired)
            if refused is not None:
                # The retired source's close neither returned nor is known to
                # have finished, so it may still be a live client. Rebuilding
                # the preceding address now would put a second one beside it,
                # so nothing is rebuilt: the durable value stays effective,
                # remote health reads unavailable and local detection is
                # unaffected until a later transition settles the outstanding
                # one.
                await self._discard(candidate)
                self._report_outstanding()
                message = (
                    "the preceding groundstation source could not be retired: "
                    f"{refused}"
                )
                raise ConfigurationError(message) from refused
            await self._abort_if_overtaken(generation, candidate)

        try:
            await self._start_prepared(candidate)
        except Exception as error:
            await self._compensate(candidate, generation)
            message = (
                f"the replacement groundstation source could not be started: {error}"
            )
            raise ConfigurationError(message) from error
        # Starting opened the session; installing is what makes the chain answer
        # from it, and committing is what makes the address survive a restart.
        # Shutdown arriving between the start and those two stops here, with the
        # started candidate closed rather than published.
        await self._abort_if_overtaken(generation, candidate)
        if candidate is not None:
            self._source.install(candidate)

        try:
            self._store.save(wanted)
        except ConfigurationError:
            # The commit point. Everything above it is undone: the candidate is
            # closed and the preceding source rebuilt, so the durable file, the
            # sole eligible source and the read-back stay on the preceding
            # address together.
            self._source.detach()
            await self._compensate(candidate, generation)
            raise

        # Nothing below suspends, so no further check is possible or needed: the
        # commit and everything the operator sees of it happen together.
        self._resolution = resolved
        if self._apply_live is not None:
            self._apply_live(resolved.settings)
        _LOGGER.info("groundstation.replaced")
        with contextlib.suppress(Exception):
            self._publish()
        return resolved

    async def _start_prepared(self, source: ConnectableSource | None) -> None:
        """Start a prepared source, if there is one and the chain is running.

        `None` is the composition that opens no session, which reaches here so
        that "there is nothing to start" is one branch rather than a condition
        repeated at each call site.

        Args:
            source: What was prepared, or `None`.
        """
        if source is None:
            return
        # Only once the chain is running. Before that, starting a source would
        # open a session before the application began; installing it is enough,
        # because the chain's own `start` reaches whatever is installed.
        if self._source.started:
            await source.start()

    async def _abort_if_overtaken(
        self,
        generation: int,
        candidate: ConnectableSource | None,
    ) -> None:
        """Stop a transition shutdown or a later write has overtaken.

        Called after every await in `_replace`, before the next act anything
        outside this owner could observe. Losing the race compensates rather
        than proceeding: whatever had been built is closed here, because the
        ordinary compensation rebuilds the preceding source and must not run
        into a chain that is being released.

        Args:
            generation: The transition being carried out.
            candidate: The replacement built so far, or `None` when nothing has
                been built yet.

        Raises:
            ConfigurationError: If the transition was overtaken.
        """
        if not self._superseded(generation):
            return
        await self._discard(candidate)
        raise ConfigurationError(_OVERTAKEN_MESSAGE)

    async def _compensate(
        self,
        candidate: ConnectableSource | None,
        generation: int,
    ) -> None:
        """Undo one failed transition: close the candidate, restore the old source.

        Args:
            candidate: The replacement nothing is going to use.
            generation: The transition this compensation belongs to.
        """
        if await self._discard(candidate) is not None:
            # The candidate's close is unconfirmed, so it may still hold a
            # session and a rebuilt preceding source would be the second client.
            # Nothing is rebuilt; the caller's refusal still leaves the
            # preceding address durable and effective.
            self._report_outstanding()
            return
        if self._superseded(generation):
            # Shutdown landed while the candidate was closing. Restoring now
            # would install a client into a chain that is being released.
            return
        await self._restore_preceding(generation)

    async def _restore_preceding(self, generation: int) -> None:
        """Put the preceding address's source back, or start trying to.

        The first attempt is made here rather than handed to a task, so the
        ordinary case — a source that rebuilds immediately — is complete by the
        time the refusal reaches the operator. Only a failed first attempt costs
        a task.

        Args:
            generation: The transition this compensation belongs to. A later
                write or shutdown advances the generation and every attempt
                stops.
        """
        settings = self._resolution.settings
        if await self._install_fresh(settings, generation):
            return
        if self._superseded(generation):
            # The attempt failed and shutdown landed while it did. A retry state
            # created now is one `aclose` has already passed the point of
            # cancelling, so it would be the late client this owner excludes.
            return
        if self._attempts <= 1:
            # The whole budget was the inline attempt, so there is nothing for a
            # task to do but log; doing it here spends no task at all.
            self._report_exhausted()
            return
        self._restoration = asyncio.create_task(
            self._reconstruct(settings, generation),
            name="satellite-groundstation-restore",
        )

    async def _reconstruct(self, settings: Settings, generation: int) -> None:
        """Keep rebuilding the preceding source until it works or the cap is hit.

        Args:
            settings: The preceding resolution, retained rather than re-read.
            generation: The transition these attempts belong to.
        """
        attempt = 1
        while attempt < self._attempts:
            attempt += 1
            await self._sleep(self._backoff.delay(attempt))
            if self._closed or generation != self._generation:
                return
            if await self._install_fresh(settings, generation):
                return
        self._report_exhausted()

    def _report_exhausted(self) -> None:
        """Say that the bound was reached, and what is true afterwards."""
        _LOGGER.error(
            "the preceding groundstation source could not be rebuilt in %d "
            "attempts; the address in effect is unchanged, remote health is "
            "unavailable and local detection remains available",
            self._attempts,
        )

    async def _install_fresh(self, settings: Settings, generation: int) -> bool:
        """Build, start and install one source, closing it if it is not wanted.

        Args:
            settings: What to build it from.
            generation: The transition this attempt belongs to.

        Returns:
            True when the attempt settled the matter — a source was installed,
            the composition has no remote source to build, the attempt was
            superseded, or a preceding source is outstanding. False when it
            failed and another may be made.
        """
        if self._outstanding:
            if not await self._settle_outstanding():
                # A source whose close was never confirmed may still hold a
                # session, and another one beside it is the overlap this owner
                # excludes. Settled rather than failed: retrying would spend the
                # budget on an attempt that cannot be allowed to succeed.
                self._report_outstanding()
                return True
            if self._superseded(generation):
                return True
        source: ConnectableSource | None = None
        try:
            source = await self._factory(settings)
            if source is None:
                # The local-only composition: there was no remote source before
                # and there is none now, which is not a failure to retry.
                return True
            self._partial = source
            if self._superseded(generation):
                return await self._abandon(source)
            if self._source.started:
                await source.start()
            if self._superseded(generation):
                return await self._abandon(source)
            self._source.install(source)
            self._partial = None
        except asyncio.CancelledError:
            # The partial stays recorded; whoever cancelled closes it, because
            # awaiting a close inside a cancelled task would be interrupted at
            # its first suspension and leak the client anyway.
            raise
        except Exception:
            _LOGGER.exception("rebuilding the groundstation source failed")
            refused = await self._discard(source)
            self._partial = None
            if refused is not None:
                # The failed attempt's own source will not close. Another
                # attempt would build a second client beside it, so this one
                # settles as unavailable instead.
                self._report_outstanding()
                return True
            return False
        return True

    def _superseded(self, generation: int) -> bool:
        """Whether a later write or shutdown has overtaken this attempt.

        Args:
            generation: The transition the attempt belongs to.

        Returns:
            True when whatever the factory produced must not be installed.
        """
        return self._closed or generation != self._generation

    async def _abandon(self, source: ConnectableSource) -> bool:
        """Close a source that arrived too late to be installed.

        Args:
            source: What the factory produced.

        Returns:
            True: the attempt is settled, and not by failing. A close that
            refused settles it too — the source is outstanding rather than
            retired, and the record of that is what stops a successor.
        """
        if await self._discard(source) is not None:
            self._report_outstanding()
        self._partial = None
        return True

    async def _discard(self, source: ConnectableSource | None) -> Exception | None:
        """Close a source nothing is going to use, and say whether it is gone.

        An unconfirmed close is **not** a retirement. The object may still hold
        a session, so it is recorded as outstanding rather than forgotten and
        every caller stops short of building or installing another one.

        Args:
            source: The retired, partially built or superseded source, or
                `None`.

        Returns:
            `None` when the close was confirmed, or the exception it raised —
            which the caller reports as the reason it went no further.
        """
        if source is None:
            return None
        try:
            await source.aclose()
        except Exception as error:
            _LOGGER.exception("an unused groundstation source would not close")
            self._outstanding.append(source)
            return error
        return None

    async def _settle_outstanding(self) -> bool:
        """Try once more to close whatever refused, and say what is left.

        One further attempt per transition rather than a loop: these sources
        have already refused once, and a robot spending its remaining cores
        re-closing them is the unbounded work REQ-095 forbids. The retry is
        worth making at all because the ordinary cause — a groundstation that
        went away while its session was being torn down — is one the next
        attempt against the same object resolves.

        Returns:
            True when nothing is outstanding any more, so a successor may be
            built.
        """
        # Taken rather than iterated: `aclose` suspends, and the reconstruction
        # task can record another outstanding source while this one runs.
        pending, self._outstanding = self._outstanding, []
        for source in pending:
            try:
                await source.aclose()
            except Exception:
                _LOGGER.exception("a groundstation source still will not close")
                self._outstanding.append(source)
        return not self._outstanding

    def _report_outstanding(self) -> None:
        """Say what a source that will not close leaves true."""
        _LOGGER.error(
            "%d groundstation source(s) would not close; the address in effect "
            "is unchanged, no replacement is built or installed while one is "
            "outstanding, remote health is unavailable and local detection "
            "remains available",
            len(self._outstanding),
        )

    async def _cancel_restoration(self) -> None:
        """Stop reconstruction and close whatever it had already built."""
        restoration, self._restoration = self._restoration, None
        if restoration is not None:
            restoration.cancel()
            # Every exception, not only the cancellation: a task that had
            # already failed is not cancelled by `cancel`, and awaiting it here
            # would re-raise its failure out of a write or a shutdown.
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await restoration
        partial, self._partial = self._partial, None
        if await self._discard(partial) is not None:
            # Reported rather than raised: this runs from a write and from
            # shutdown, and neither may fail here. The write refuses at its own
            # outstanding check a moment later; shutdown has nothing to install.
            self._report_outstanding()

    async def aclose(self) -> None:
        """Cancel and await reconstruction before anything closes the source.

        Idempotent, and it does **not** close the source itself: the perception
        chain owns that, and `SatelliteApplication.aclose` closes this owner
        first so no attempt can install a client into a chain that is about to
        be released.
        """
        if self._closed:
            return
        self._closed = True
        # Awaited before the lock is taken, not while it is held: a submission
        # already inside `submit` holds the lock, and waiting for it from inside
        # would be waiting for a lock this call is about to want.
        for requested in tuple(self._requested):
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await requested
        async with self._lock:
            self._generation += 1
            await self._cancel_restoration()
            # One last bounded attempt at whatever refused to close, so an
            # ordinary shutdown does not leave a session behind. Reported and
            # not raised: a shutdown that failed here is a shutdown that wedges,
            # and there is nothing left for an outstanding source to overlap.
            if not await self._settle_outstanding():
                self._report_outstanding()


def _publish_nothing() -> None:
    """Push nothing, for an owner no entity is reporting the address from."""
