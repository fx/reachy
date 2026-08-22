# 0017: Speaker controls in Home Assistant

## Summary

Put the speaker's volume and its software boost on the device page in Home
Assistant, as controls in the Configuration group beside `Mic Volume`, kept in
step with the media-player entity and persisted across restarts.

**Spec:** [ha-satellite](../specs/ha-satellite/)
**Status:** complete
**Depends On:** 0016

## Motivation

This is the follow-up change 0016 promised itself. Its non-goals say, in full:

> **No new Home Assistant entity.** The boost is application configuration. An
> operator-facing boost control is a follow-up if the default turns out wrong.

It turned out wrong in a milder way than that sentence anticipated — not the
number, but the reach. An operator looking at the robot's device page finds
`Mic Volume` in the Configuration group and nothing beside it for the speaker,
which reads as a robot whose speaker cannot be adjusted from Home Assistant at
all.

**The volume was never inert, and it is worth being exact about the defect.**
The media-player entity advertises `VOLUME_SET` and `VOLUME_MUTE`, carries a
working `volume_level`, persists what it is set to, and — since 0016 — changes
what reaches the speaker. What it is not is a *control in the Configuration
group*. The gap is where the control is, not whether the volume works. The
boost is the other half and is a plain gap: it is a `Settings` field, reachable
from the application's own settings page and from the environment, and from
Home Assistant not at all.

## Requirements

### Testing Requirements

This change MUST satisfy the project's standing testing rules (see
[Testing conventions](../specs/architecture/index.md#testing-conventions)). CI
enforces these as merge gates:

- Tests run with `pytest`, with async strict mode enabled.
- Coverage MUST be gated on the diff rather than on the whole tree.
- Type checking MUST run in strict mode for new modules.
- No test may require a speaker or a Home Assistant instance. The entities are
  exercised by handing them real protobuf messages and reading the responses
  back, and the ordering claims are driven through the real
  `VoiceSatelliteProtocol` fan-out.

### Behaviour

- **R1 — The speaker's volume is a control.** A Number in the Configuration
  group, 0–100%, reporting and setting the same level the media-player entity
  reports and sets. Not a second store of that level: one number underneath
  both.
- **R2 — The boost is a control.** A Number in the Configuration group over the
  range `output_gain` declares — 100% to 800% — reporting what is in effect now
  rather than what the application started with.
- **R3 — A boost chosen from Home Assistant survives a restart.** It is written
  through the overrides layer, which is where the settings page writes, so the
  two surfaces cannot disagree about what the boost is.
- **R4 — A boost chosen from Home Assistant is heard at once.** It reaches both
  outputs through `apply_live`, so there is one path from "a boost was chosen"
  to "the outputs heard about it" whichever surface chose it.
- **R5 — The two volume views never disagree.** Whatever Home Assistant does —
  set the media player's volume, mute, unmute, move the new slider — the level
  the control reports and the level the media player reports are the same
  number afterwards.
- **R6 — A refused write is reported as refused.** An overrides file that cannot
  be written must not raise out of the protocol's message loop and drop the
  connection, and must not leave Home Assistant showing a value that is not in
  effect.

### Non-goals

- **No change to the boost's default or to the limiter.** 500%, the 100–800
  range and the soft knee were settled by 0016 against the real speaker and are
  not re-tuned here.
- **No microphone-side control.** The four microphone settings the vendored
  layer already exposes are untouched.
- **No `BROWSE_MEDIA` and no protocol-version change.** See the latent trap
  below.
- **No new requirement in `docs/specs/`.** ha-satellite REQ-049 is about the
  application's own settings interface, and this change adds no claim to any
  spec. Nothing under `docs/specs/` is edited.

## Design

### Where the two entities live

`apps/ha-satellite/src/reachy_mini_ha_satellite/audio_entities.py`, at the
package's top level beside `wake_word.py` — **not** in `esphome/entity.py`.

That file is derived from the upstream Linux voice assistant and every departure
from upstream is enumerated in the `NOTICE` beside it, so a class added there
would be a line in a provenance record for code upstream never wrote.

**The directory says of itself what it holds.** `esphome/__init__.py:11` ends its
docstring with "The two audio seams are `seams.py`, which is the one file here
that is not vendored", and the `NOTICE` beside it records the same shape from the
other direction: `__init__.py` and `seams.py` are the originals, and both exist
because *vendored code imports them*. Adding a third original module there — one
nothing vendored imports — would make the package's own docstring false, and the
sentence a reviewer reads to learn what the directory is would have to be
rewritten to accommodate a file that had no reason to be there. The two entities
are imported by the composition root and by nothing vendored, so they belong at
the package's top level beside `wake_word.py`, which is the same arrangement for
the same reason. **No file under `esphome/` is modified by this change**, and
nothing is added inside it: `git diff --name-only main..HEAD -- '*/esphome/*'`
prints nothing.

Keeping them outside also means `just lint-boundary`'s TID251 ban does not apply
to them, so the bounds are imported from `adapters/output_gain.py` directly
rather than restated — which is what stops the slider's range and the gain
module's range drifting apart.

### One level, two views

The volume control holds no level of its own. It reports
`ServerState.volume * 100`, and it writes through `MediaPlayerEntity`'s public
`apply_volume_from_state` plus `ServerState.persist_volume` — the same two the
vendored layer uses. That is what makes R5 true by construction rather than by
synchronisation.

**What muting does to it is the part a reviewer will ask about, and the
guarantee is R5 rather than any particular number.** The vendored MUTE branch
sets `ServerState.volume` to 0 and persists that; UNMUTE restores it from
`previous_volume`. So after a mute:

- the Number **reports 0**, because that is the level in effect;
- a value **set through the Number** is *remembered rather than applied* —
  `apply_volume_from_state` stores it into `previous_volume`, nothing is
  persisted, nothing reaches the speaker, and the Number goes on reporting 0;
- **unmuting restores it**, which is the vendored UNMUTE branch doing what it
  already did.

Home Assistant's slider therefore snaps back to 0 after a set made *through the
Number* while muted, which is honest rather than a bug: the device is muted, and
0 is the level in effect.

**A media-player `VOLUME_SET` while muted is a different path, and the Number
does not report 0 after one.** The vendored `has_volume` branch applies that
level to both outputs and persists it with the mute flag left set, so
`ServerState.volume` is non-zero while `muted` is true — and the Number mirrors
that level rather than contradicting the state and the media player both.
Forcing it back to 0 there would be R5 broken to preserve a sentence. What that
leaves the vendored player in is recorded under [Known
limitations](#known-limitations) below.

`ServerState.volume` is the one number both entities read **in every one of
those states** — muted, unmuted, and mid-change — so the control and the media
player cannot report different levels. There is no second store to synchronise
and therefore no window in which the two could disagree. That is R5 held by
construction rather than by keeping two copies in step.

The control also answers a `MediaPlayerCommandRequest` addressed to the media
player, so a level set from the media-player card moves the slider without
waiting for the next subscription. It derives that level **from the request**
rather than reading it back from the media player, which is what makes the
answer independent of whether the protocol's fan-out reached the media player
first: the one thing read from post-command state is `previous_volume`, and only
in the UNMUTE branch — which is the one command that does not itself change it,
so that read gives the same answer whichever of the two entities ran first.

### Where a boost goes

Through the overrides layer — `config.OverrideStore` — and not through
`Preferences`, which is where the vendored layer keeps the volume and the four
microphone settings.

`Settings` already declares `speaker_boost_percent`, so a `Preferences` field
for it would be a second store free to disagree with the settings page: an
operator would set 400% in Home Assistant, open the page, and read 500%. One
store, two surfaces.

`config.apply_settings_change` is the sequence both surfaces now perform —
resolve, write, adopt, in that order, because resolving before writing is what
stops an unresolvable value becoming the file the next start reads. It was the
settings page's inline body before this change; a second copy of it for the
entity would have been free to drift, most damagingly in the order.

One deliberate difference between the two callers, because a reviewer will
compare them: the entity **always** writes an override, even for a value equal
to the environment's, where the page's `_overrides_from` drops one that matches
the layer beneath. A form renders every field and submits values nobody touched;
a slider only moves when somebody moves it, and there is no "revert to the
environment" gesture on a slider to undo a pin with.

`build_boost_setter` catches `ConfigurationError` and logs it (R6). It runs
inside the ESPHome protocol's message loop, where raising would drop the
connection, and the entity's read-back then reports the value actually in effect
rather than the one that was asked for.

### Telling Home Assistant about a boost it did not choose

The boost is the one of the two values that changes without Home Assistant
having asked for it. An operator who moves it on the application's own settings
page — which is where §4 of the runbook sends them — hears the change from the
next pushed chunk, and a slider still showing the previous number until the next
reconnect is R2 failing for exactly the client this control was added for.

So `SpeakerBoostNumberEntity.publish` sends the value in effect through
`ServerState.broadcast`, and `SatelliteApplication.apply_live` calls it once a
change has been adopted. `apply_live` is the one funnel both surfaces pass
through — the settings page's `save` and `reset`, and `build_boost_setter` — so
a single call site covers both origins, and the composition root is where the
two are tied to each other:
`application.publish_live_changes(boost.publish)`.

`broadcast` and not `self.server`, which is `None` for both of these entities by
construction: an asynchronous state change has to reach every subscribed client
rather than whichever connection an entity happens to hold, which is what the
vendored `MediaPlayerEntity._broadcast_state` uses it for.

Two consequences, both deliberate. A boost chosen *in* Home Assistant is pushed
as well as answered, which repeats a value that client already has and keeps
adoption at one call site instead of two. And **any** live setting being adopted
publishes, not only a boost — the alternative is `SatelliteApplication` knowing
which setting which entity reports, and the cost is a repeat of a number Home
Assistant already has.

**The volume control has no equivalent and needs none.** `ServerState.volume`
moves only when the ESPHome protocol moves it, and the message that moves it is
one this control is itself answering — the reply is already the push. The
settings page does not set it, and the vendored peripheral API, which is the
other writer of it upstream, is never started here: this application fills that
slot with a `PipelineEventTap`.

### Registration and the key renumbering

Both entities are appended to `ServerState.entities` in `build_application`,
immediately after the `SatelliteApplication` is constructed — the boost setter
needs `application.apply_live`, so anywhere earlier would be a forward
reference. That is once, before any service is built and long before any
connection.

Appending before a `VoiceSatelliteProtocol` exists is safe: that layer's three
de-duplication branches match its **own** classes by `isinstance` and never see
these. It numbers what it builds from `len(state.entities)`, so ours take keys 0
and 1 and the media player, the mute switch and the rest shift up by two.

**That renumbering is invisible to Home Assistant, and no history is orphaned by
it.** Home Assistant registers an ESPHome entity under a unique identifier of
`{mac}-{entity_type}-{object_id}` — the numeric key appears in it nowhere. The
key is a connection-scoped address, re-learned from `ListEntitiesResponse` on
every connect, so the media player being key 0 before this change and key 2 after
it is not a fact any stored record holds. An existing installation therefore
keeps every entity's identity, its history and its automations across the
upgrade, and gains two rows.

What must never change is `object_id`, which is the part of that identifier this
repository decides. The two here — `speaker_volume` and `speaker_boost` — are
bound as named constants for exactly that reason, so that changing one is an
edit somebody has to make deliberately rather than a string that drifts.

`OverrideStore` moves out of the `if settings.web_enabled:` branch to the top of
`build_application`, because the boost control needs it whether or not the
settings page is served.

### A latent trap, recorded rather than fixed

The vendored `esphome/api_server.py:72-73` answers `HelloRequest` with
`api_version_major=1, api_version_minor=10`, and that number is load-bearing in a
way nothing near it says.

Below **1.11**, `aioesphomeapi` takes a legacy-compatibility branch: it *ignores*
the `supported_formats` and feature flags the device declares and synthesises a
feature set of its own, which grants `VOLUME_SET` unconditionally. So the volume
Home Assistant has been offering all along comes from that branch, not from what
this repository declares. From 1.11 onwards it stops synthesising and believes
the device — at which point `SUPPORTED_MEDIA_PLAYER_FEATURES`
(`esphome/entity.py:59-67`) becomes authoritative, and that constant lists
`PLAY`, `PAUSE`, `STOP`, `PLAY_MEDIA`, `VOLUME_SET`, `VOLUME_MUTE` and
`MEDIA_ANNOUNCE` — **and not `BROWSE_MEDIA`**.

The consequence is a trap rather than a bug: raising `api_version_minor` to 1.11
or beyond, for any reason at all and with no other change, would silently drop
media browsing from the device. Whoever does it must add `BROWSE_MEDIA` to that
constant **in the same commit**, or the version bump and the regression will land
separately and be diagnosed separately.

Neither is done here — the version stays at 1.10 and the constant is untouched.
This is written down so the next change that touches the version knows what it is
standing on, because nothing in either file would tell it.

### Known limitations

**A `VOLUME_SET` after a MUTE leaves the vendored media player muted and
audible, and this change does not fix it.** `MediaPlayerEntity.handle_message`'s
`has_volume` branch calls `_apply_volume(..., persist=True)`, which sets both
outputs' volume and `self.volume`, and it never clears `self.muted`. So
`MUTE` followed by `VOLUME_SET(0.5)` leaves the player reporting `muted=True`
with `volume=0.5`, having genuinely set both outputs to 50%: Home Assistant
shows the device muted while it is audible.

That is upstream behaviour in `esphome/entity.py`, which is vendored and which
this change does not touch — `git diff --name-only main..HEAD -- '*/esphome/*'`
prints nothing. It predates this change and neither of the new controls causes
or worsens it; the Speaker Volume Number reports the same level the media player
does throughout, which is R5. Correcting the vendored mute flag is a separate
proposal, because it is a departure from upstream and belongs in the `NOTICE`
beside that file with the rest of them.

## Tasks

- [x] `AudioPort.set_boost`, and `ReachyPlayback`/`ReachyAudio` implementing it —
      set under the same lock `_gain` reads the boost under, so a change is heard
      from the next pushed chunk. On `AudioPort` rather than `PlaybackPort`,
      because that port is congruent with the vendored playback seam and the
      vendored layer never asks for a boost (R4).
- [x] `speaker_boost_percent` joins `LIVE_SETTINGS`, and `apply_live` hands it to
      the audio port. `SatelliteApplication.settings` becomes readable, so an
      entity reports what is in effect now rather than a snapshot (R2, R4).
- [x] `config.apply_settings_change` — one definition of resolve, write, adopt,
      called by the settings page's `save` and `reset` and by the boost setter
      (R3).
- [x] `audio_entities.py` — the two Number entities, strictly typed, with no
      pydantic import and no file under `esphome/` touched (R1, R2, R5, R6).
- [x] Registration in `build_application`, with `OverrideStore` hoisted out of
      the web branch, and `build_boost_setter` as the one path a chosen boost
      travels (R3, R4, R6).
- [x] `SpeakerBoostNumberEntity.publish`, called from `apply_live` through
      `SatelliteApplication.publish_live_changes`, so a boost chosen on the
      settings page moves Home Assistant's slider rather than leaving it showing
      the previous number until the next reconnect (R2).
- [x] `scripts/vendored_provenance.py` — the new test file exempted, along with
      four pre-existing omissions that had left the guard exiting 1 on `main`.
- [x] This document, its two index entries, and the operator-facing paragraphs in
      `docs/setup/home-assistant.md` §4 — where both controls are, what a change
      to either writes, and that the environment variable is still how the
      starting value is set. No requirement is added and nothing under
      `docs/specs/` is edited.
- [x] Verify on the robot and in Home Assistant, and record it — both controls
      present in the Configuration group, the volume slider moving with the
      media-player card in both directions, muting reading 0, and a boost set
      from Home Assistant surviving a restart. Capture the transcript into
      `docs/setup/home-assistant.md` §4, where a marked placeholder was waiting
      for it.

## Outcome

**Verified against a real Reachy Mini and a real Home Assistant.** The session is
transcribed in
[`docs/setup/home-assistant.md` §4](../setup/home-assistant.md#what-that-looks-like-on-a-real-robot),
scrubbed the way the runbook convention requires; what follows is what it
settles, requirement by requirement.

- **R1, R2 and the registration.** Home Assistant read both controls as `number`
  entities with the declared bounds, step, mode, unit and icon — Speaker Volume
  over 0–100% at step 1, Speaker Boost over 100–800% at step 10 — on a device
  that already carried every vendored entity. The new pair took keys 0 and 1 and
  the vendored entities shifted up by two, and **no existing entity changed
  identity, lost history or dropped an automation**, which is the renumbering
  argument under [Registration and the key
  renumbering](#registration-and-the-key-renumbering) confirmed on an
  installation rather than reasoned about. The Configuration group itself is
  Home Assistant's rendering of the declared `entity_category=CONFIG` and was
  not separately transcribed.
- **R5.** Across the sweep the two views never differed: setting the Number moved
  the media player, setting the media player moved the Number, and a mute took
  both to 0 with an unmute restoring both to the level from before it.
- **R3.** A boost chosen in Home Assistant created the overrides file — which had
  not existed until then — with `speaker_boost_percent` in it, and a boost
  standing in that file was what Home Assistant reported after the application
  was stopped and started again. The write and the survival were observed as two
  steps against the same file rather than as one continuous sequence.
- **R4.** The daemon's uptime was unchanged across a boost change, so the new
  value was adopted by the running application rather than by a restart. And a
  boost submitted on the application's own settings page was what Home Assistant
  read next, with no reconnection in between, which is
  `publish` through `broadcast` doing what [Telling Home Assistant about a boost
  it did not choose](#telling-home-assistant-about-a-boost-it-did-not-choose)
  says it does.

**What is still not evidence, and it is the obvious thing.** Nobody listened to
the robot. Every observation above is an API reading, so this change is verified
as *correct* — the right numbers reported, applied and persisted — and not as
*audible*. The 500% default is inherited from
[0016](0016-audible-playback.md), which is where the listening was done; it was
neither re-tuned nor re-judged here, and the non-goal saying so still holds.
R6's refused write was likewise not provoked on hardware: it stays established by
the test suite, as do the messages the entities declare and answer, the clamping
and the read-back.
