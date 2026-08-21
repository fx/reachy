# Assets notice

The wake-word models under `wakewords/` and the audio under `sounds/` are
third-party work that ships inside the `reachy-mini-ha-satellite` wheel.
`registry.py` is the machine-readable record — path, kind, SPDX licence,
attribution, source URL and digest for each one — and this file is the same
thing in prose, for an auditor who opens the directory. `registry.py`,
`verify.py`, `__init__.py` and this notice are original to this repository and
are not assets.

All of the third-party material was taken from the Home Assistant project's
Linux Voice Assistant at commit `d1f5761f7591495794734e79c98f7199100153c0`, the
same commit as the vendored code in `../esphome/`. Both licences travel with it:
`wakewords/LICENSE` is the Apache-2.0 text, and `sounds/LICENSE.md` is upstream's
own statement of the sounds' CC BY 4.0 terms. Neither is optional — Apache-2.0
requires a copy of the licence reach everyone the models reach, which is
everyone who installs the wheel, and CC BY requires the attribution travel with
the work.

## `wakewords/`

microWakeWord models and their configurations, by Kevin Ahrendt
(<https://www.kevinahrendt.com/>), distributed in the Apache-2.0 upstream
repository with no separate notice, so the repository licence covers them. The
model project is <https://github.com/kahrendt/microWakeWord>.

| File | Licence |
|---|---|
| `okay_nabu.json`, `okay_nabu.tflite` | Apache-2.0 (`wakewords/LICENSE`) |
| `stop.json`, `stop.tflite` | Apache-2.0 (`wakewords/LICENSE`) |

Two models, not the ten upstream carries: the default wake word, and the stop
word the protocol needs to interrupt a response or silence a timer. That is the
smallest set that makes the application usable, and it keeps roughly half a
megabyte of binary out of the wheel and out of this repository's history. Home
Assistant can add wake words at run time — the vendored protocol downloads an
external model on demand — and anything downloaded is not shipped, so it is not
this registry's concern.

## `sounds/`

Home Assistant Voice Preview Edition Sounds © 2024 by
[Clayton Charles Tapp](https://www.cctaudio.com/), licensed under
[Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/).
`sounds/LICENSE.md` is upstream's own statement of those terms, carried
verbatim.

| File | Played when |
|---|---|
| `wake_word_triggered.flac` | the wake word is detected |
| `start_listening_button.flac` | a button starts the pipeline |
| `processing.wav` | the pipeline is resolving an intent |
| `timer_finished.flac` | a timer rings |
| `mute_switch_on.flac`, `mute_switch_off.flac` | the microphone is muted or unmuted |
| `button_double_press.flac`, `button_triple_press.flac`, `button_long_press.flac` | a peripheral button is pressed |

These nine are exactly the sounds the carried code names. Upstream's superseded
`*_old.wav` variants are not carried.

## Adding an asset

Add the file, add its registry entry, and run `just check-assets`. An asset with
no entry, a missing file, or a digest that no longer matches fails that check; an
asset whose licence is outside `registry.ALLOWED_LICENCES` fails the unit test
over the registry. Widening the allowlist is a licensing decision — make it in
review, deliberately.
