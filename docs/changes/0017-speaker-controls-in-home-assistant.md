# 0017: Speaker controls in Home Assistant

## Summary

Put the speaker's volume and its software boost on the device page in Home
Assistant, as controls in the Configuration group beside `Mic Volume`, kept in
step with the media-player entity and persisted across restarts.

**Spec:** [ha-satellite](../specs/ha-satellite/)
**Status:** draft
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

**The muted rule follows from it, and is the part a reviewer will ask about.**
The vendored MUTE branch sets `ServerState.volume` to 0 and persists that;
UNMUTE restores it from `previous_volume`. So while the device is muted:

- the Number **reports 0**, because that is the level in effect;
- a value **set** into it is *remembered rather than applied* —
  `apply_volume_from_state` stores it into `previous_volume`, nothing is
  persisted, and nothing reaches the speaker;
- **unmuting restores it**, which is the vendored UNMUTE branch doing what it
  already did.

Home Assistant's slider therefore snaps back to 0 after a set made while muted,
which is honest rather than a bug: the device is muted, and 0 is the level in
effect.

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
first: nothing is read from post-command state except `previous_volume`, which
neither MUTE nor UNMUTE modifies.

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
- [x] `scripts/vendored_provenance.py` — the new test file exempted, along with
      four pre-existing omissions that had left the guard exiting 1 on `main`.
- [x] This document, its two index entries, and the operator-facing paragraphs in
      `docs/setup/home-assistant.md` §4 — where both controls are, what a change
      to either writes, and that the environment variable is still how the
      starting value is set. No requirement is added and nothing under
      `docs/specs/` is edited.
- [ ] Verify on the robot and in Home Assistant, and record it — both controls
      present in the Configuration group, the volume slider moving with the
      media-player card in both directions, muting reading 0, and a boost set
      from Home Assistant surviving a restart. Capture the transcript into
      `docs/setup/home-assistant.md` §4, where a marked placeholder is waiting
      for it.

## Outcome

⏳ **Pending hardware verification.** Nothing in this repository has a Reachy
Mini or a Home Assistant instance attached, so what is established here is what
the test suite can establish: the messages the entities declare and answer, the
level the two views agree on across a sweep of commands, the muted rule in each
of its three states, the clamping, the read-back, and the file the boost is
written to.

Whether Home Assistant renders the two controls in the Configuration group where
this document says it will is the one claim that needs the robot, and it is the
last task above. `docs/setup/home-assistant.md` §4 carries a marked placeholder
where the observed output belongs; the transcript replaces it before this change
is complete, scrubbed the way the runbook convention requires. No output has been
invented in the meantime.
