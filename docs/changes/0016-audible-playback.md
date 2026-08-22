# 0016: Audible playback

## Summary

Make the robot's speaker loud enough to hold a conversation with, by decoding
playback ourselves and applying gain to the samples — and make Home Assistant's
volume control do something, which today it does not.

**Spec:** [ha-satellite](../specs/ha-satellite/)
**Status:** in-progress
**Depends On:** 0013

## Motivation

`adapters/audio_reachy.py` says this about itself, in its own module docstring,
and has since 0012:

> **There is no output gain.** `set_volume` and `duck` record what was asked for
> and report it back, and change nothing audible. Home Assistant's volume control
> therefore round-trips through the media-player entity correctly and does
> nothing, which is stated here rather than hidden because the fix is a choice
> between the daemon growing a volume control and this adapter moving to the
> daemon's push-based playback path, and neither can be decided without the
> robot. […] they are the first things the end-to-end session has to settle.

The end-to-end session has now settled it. The measurements below were taken on
the robot, and they close both halves of that open question.

**The hardware is already at maximum, so there is no gain left to ask it for.**
The robot's speaker is one USB device with exactly one playback control, and
that control is at unity:

```
$ cat /proc/asound/cards
 0 [Audio          ]: USB-Audio - Reachy Mini Audio

$ amixer -c 0 scontrols
Simple mixer control 'PCM',0
Simple mixer control 'Headset',0        # capture

$ amixer -c 0 sget PCM
  Limits: Playback 0 - 60
  Front Left: Playback 60 [100%] [0.00dB] [on]
```

There is no `Master`, no `Speaker`, and no control with headroom above `0.00dB`.

**The daemon does have a volume control, and raising it to its maximum is not
enough.** It was found at 62 of 100 and nothing in this application had ever
moved it, because nothing in this application knows it exists:

```
$ curl -s http://127.0.0.1:8000/api/volume/current
{"volume":62,"platform":"Linux","device":"Reachy Mini Audio"}
```

Set to 100 and asked to play its own test sound, the result was still reported
as too quiet to be useful. So the coarse control is worth driving — a control
sitting at 62 that the operator cannot reach is its own defect — but driving it
does not make the robot audible on its own.

That leaves amplifying the samples, above unity, in software. It is the one
remaining place the loudness can come from, and it is what the application this
one replaces did: a separate speaker-volume control with a large default boost.

There is a second defect underneath the first, and it is the reason the first
one is not a one-line fix. Playback today hands a **file path** to the daemon:

```python
self._media.play_sound(sound.path)
```

Nothing in that path ever holds a sample, so there is nowhere to multiply.
The daemon's media interface also exposes a push-based path —
`start_playing()`, `push_audio_sample(NDArray[np.float32])`, `stop_playing()` —
and that is the one where the samples are ours. Moving to it is what makes gain
expressible at all, which is why this change is a playback rework rather than a
volume setting.

Moving also settles a third thing the adapter documents as a known cost. Because
`play_sound` reports nothing about progress, completion is currently a **timer**
sized from the file's own header, and a format whose length cannot be read is
scheduled at `UNKNOWN_LENGTH_SECONDS` — five minutes — so a `done_callback` is
late rather than lost. Pushing samples ourselves means the end of the stream is
an observed fact, and the timer and its magic constant go away.

## Requirements

### Testing Requirements

This change MUST satisfy the project's standing testing rules (see
[Testing conventions](../specs/architecture/index.md#testing-conventions)). CI
enforces these as merge gates:

- Tests run with `pytest`, with async strict mode enabled.
- Coverage MUST be gated on the diff rather than on the whole tree.
- Type checking MUST run in strict mode for new modules.
- No test may require a speaker. The decoder and the gain are exercised over
  bytes and arrays; the daemon's media interface is reached through the existing
  fake.

### Behaviour

- **R1 — Gain is applied to the samples.** Every sample pushed to the daemon is
  multiplied by an effective gain, so a robot at the default is audible across a
  room.
- **R2 — Home Assistant's volume control is real.** `set_volume`, `duck` and
  `unduck` change what is heard. The media-player entity already round-trips
  correctly; this change makes the round trip mean something.
- **R3 — The boost is a declared setting with a default, not a constant.** It is
  configuration, resolved and logged like every other setting, so a louder or
  quieter robot is a configuration change rather than a rebuild.
- **R4 — The peaks are limited, not squared off.** Below a knee the boost is
  exactly linear; above it the signal is compressed smoothly and only then
  bounded. **Hard clipping is specifically rejected**: the predecessor tried it
  and recorded the result in the source — plain `np.clip` on a boosted signal
  "squares off every peak and sounds harsh". A limiter is therefore a requirement
  here rather than a refinement to come later.
- **R5 — A cue is not given the gain that a voice needs.** The boost is makeup
  gain for Home Assistant's text-to-speech, which arrives quiet — the predecessor
  measured it around **−15 dBFS**. The wheel's own cues are mastered far hotter:
  `timer_finished.flac` peaks at **0.0 dBFS** and the wake chime at **−3.1
  dBFS**. Handing those the same multiplier drives them deep into the limiter and
  "turns a chime into a blare", so each source is capped at the gain that brings
  it to full scale and no further.
- **R6 — What the limiter did is reported.** Each utterance logs the peak going
  in, the peak coming out, and the proportion of samples that were limited, in
  dBFS. "It sounds distorted" and "it is still too quiet" are then different
  lines in a log rather than two guesses.
- **R7 — The coarse control is driven too.** The daemon's own volume is set to
  its maximum once, at startup, so the software gain begins from the loudest
  signal the hardware will pass rather than from whatever the last operator left.
- **R8 — Completion is observed.** `done_callback` fires when the stream ends,
  is stopped, or is superseded — not when a timer sized from a header expires.
  `UNKNOWN_LENGTH_SECONDS` is removed.
- **R9 — Every format the robot is actually sent still plays.** Home Assistant's
  text-to-speech proxy serves **MP3**; the wheel's own assets are **FLAC** and
  **WAV**. Output is resampled to the rate the daemon reports rather than
  assumed.

### Non-goals

- **No equaliser.** The dynamics handling is the knee and the limiter in R4, and
  nothing shapes the spectrum.
- **No per-source volume entities.** Music and speech keep the one control they
  have. R5 caps each source automatically; it does not expose a slider per source.
- **No change to capture.** The microphone path was settled in the wake-word
  change and is not touched here.
- **No new Home Assistant entity.** The boost is application configuration. An
  operator-facing boost control is a follow-up if the default turns out wrong.

## Design

### Decoding

`av` (PyAV) is already resolved into the robot's application environment and
decodes all three formats plus resampling, so it becomes a declared dependency of
this member rather than an ambient one. The alternative considered was
`soundfile`, which is also present and handles FLAC and WAV natively but is a
poor fit for the MP3 that Home Assistant actually sends.

The decode runs off the event loop, on the thread that already resolves a sound,
so a slow fetch or a long file cannot stall the ESPHome protocol.

### Effective gain

```
requested = (volume / 100.0) * boost * duck_factor
effective = min(requested, headroom_of(source))
```

`volume` is what Home Assistant set and `duck_factor` is 1.0 or the ducking
factor. `headroom_of` is R5: the largest gain that keeps this particular source
at or below full scale, never below 1.0, taken from its peak.

`boost` is expressed in percent, to match the control it replaces. **The
predecessor's numbers are adopted rather than re-derived**, because they were
tuned by ear against this exact speaker and this repository has no listening
test: default **500%**, range **100–800%**, where 800% is +18 dB and is
described in that source as past the point where the measured −15.5 dBFS
text-to-speech peaks sit hard in the limiter.

### The limiter

Below `KNEE = 0.6` the boosted signal is untouched. Above it, the excess above
the knee is passed through `tanh` and scaled back into the remaining headroom, so
the curve is continuous at the knee and asymptotic at full scale; a final clip
catches nothing in ordinary use and exists so the contract holds unconditionally.

This is lifted, with its constants, from the predecessor's `output_gain.py` —
the one part of that application whose behaviour was established against real
speech through this speaker, and re-deriving it by taste would be discarding the
only evidence available.

### Where it lives

`ReachyPlayback` in `adapters/audio_reachy.py` keeps its public shape — it
satisfies `esphome.seams.MediaPlayback` structurally and the vendored layer must
go on seeing the same object. What changes is its inside: `_begin` pushes
decoded, amplified chunks instead of handing over a path, and `_finished` is
driven by the end of the push loop instead of by a timer.

## Tasks

- [x] Decode to float PCM and push it, replacing `play_sound` — `ReachyPlayback`
      moves to `start_playing` / `push_audio_sample` / `stop_playing`, decoding
      through `av` and resampling to the daemon's reported output rate. Keeps
      `play`, `pause`, `resume`, `stop`, supersede and `release` behaving as they
      do now, with their existing tests still passing against the fake. Removes
      the completion timer and `UNKNOWN_LENGTH_SECONDS` (R8, R9).
- [x] Gain, the knee and the limiter — a module of its own, tested over arrays
      rather than through the adapter: the soft knee at 0.6, the `tanh` curve
      above it, the per-source headroom cap, and the level meter that reports
      peak in, peak out and the proportion limited. Tests pin the shape rather
      than a sample: continuous at the knee, monotonic, never leaving
      `[-1.0, 1.0]`, and a hot cue capped where a quiet voice is not (R1, R4, R5,
      R6).
- [x] Wire it into playback — `set_volume`, `duck` and `unduck` stop being inert,
      and the docstring paragraph quoted in the motivation above is replaced by
      what the code now does (R2).
- [x] Declare the boost as configuration — a setting in percent, default 500,
      clamped to 100–800, resolved and logged like the rest, plus the daemon's
      coarse volume driven to maximum at startup (R3, R7).
- [ ] Verify on the robot and record it — a conversation at a normal speaking
      distance, the volume entity audibly changing the level, and ducking audible
      during a wake word. Update `docs/tasks.md`, and update the runbook step
      that is currently marked pending hardware verification.
