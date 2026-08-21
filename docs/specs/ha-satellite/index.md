# HA Satellite

## Overview

The HA Satellite is the application that runs on the robot and makes it a Home
Assistant voice satellite. It speaks the ESPHome native API, so Home Assistant
discovers it as a voice device with entities, runs its voice pipeline through
it, and drives it from automations — while the robot's head and antennas react
to what the conversation is doing.

It also holds the robot-side end of the [robot link](../robot-link/) session,
which is how face tracking gets its detections without the robot paying for
them.

Distributed as a wheel on GitHub Releases and installed into the robot's shared
application environment. Nothing is implemented yet.

## Background

The predecessor was a fork of a third-party application. It worked and had real
range — wake words, audio playback, motion, entities — but it could not be
carried forward. Its upstream ships no licence file while declaring one in
package metadata, which makes redistribution in a public repository
indefensible regardless of intent.

So this is a clean-room rewrite. The ESPHome protocol layer derives instead from
the Home Assistant project's own Linux voice satellite, which is Apache-2.0. It
is not published to a package index, so it is vendored rather than depended on,
under the attribution rules in
[architecture REQ-007](../architecture/index.md#req-007-vendored-third-party-code-is-attributed-in-place).

The vendored portion is roughly 3.5k lines covering the protocol, entity model,
wake-word handling and discovery. What is not carried over is its audio
plumbing: it captures through a desktop sound library and plays back through a
media player process, and on this robot both belong to the Reachy Mini daemon.
Those two seams, and its command-line entry point, are replaced.

## Requirements

### REQ-040: The announced device identity is configuration

The identity the satellite announces to Home Assistant MUST be read from
configuration rather than derived from the package name, the host name, or any
other value that changes when the software is repackaged.

#### Scenario: The application is renamed and redeployed

- **GIVEN** a Home Assistant installation with automations and dashboards
  referencing this device's entities
- **WHEN** the application is replaced by a build with a different package name,
  configured with the previous announced identity
- **THEN** Home Assistant continues to recognise the same device, entity
  identifiers are unchanged, and history remains attached

#### Scenario: A second robot joins the network

- **GIVEN** one satellite already registered with Home Assistant
- **WHEN** a second robot is deployed with its own announced identity
- **THEN** Home Assistant registers it as a distinct device rather than
  colliding with the first

### REQ-041: The application is discoverable by the robot daemon

The application MUST advertise itself through the daemon's application entry
point mechanism so that installing the wheel is sufficient for the daemon to
find it.

#### Scenario: The wheel is installed

- **GIVEN** a robot whose daemon is running
- **WHEN** the wheel is installed into the shared application environment and
  the daemon is restarted
- **THEN** the application appears in the daemon's list of installed
  applications without further registration

### REQ-042: Decision logic is free of input and output

The logic that maps voice-pipeline events and detections to motion intents MUST
be implemented without performing input or output.

#### Scenario: The behaviour suite runs in continuous integration

- **GIVEN** a continuous integration runner with no robot, microphone, or
  network access to a groundstation
- **WHEN** the behaviour test suite runs
- **THEN** every state transition and motion mapping is exercised without a fake
  needing to emulate a socket or a device

### REQ-043: Hardware access goes through the daemon's media layer

Microphone capture and audio playback MUST be performed through the robot
daemon's media interface rather than by opening audio devices directly.

#### Scenario: Another component holds the audio device

- **GIVEN** a robot whose daemon owns the microphone array and speaker
- **WHEN** the application captures audio and plays a response
- **THEN** both succeed without contending for the device, and the daemon's own
  audio behaviour is unaffected

### REQ-044: Wake-word detection runs on the robot

Wake-word detection MUST run locally on the robot, without depending on the
groundstation or on Home Assistant.

#### Scenario: The network is down

- **GIVEN** a robot whose network connection has failed
- **WHEN** the wake word is spoken
- **THEN** the wake word is detected locally, and the failure surfaces at the
  point the pipeline needs Home Assistant, rather than at detection

### REQ-045: Speech and intent processing stay in Home Assistant

The application MUST NOT perform speech-to-text, text-to-speech, or intent
resolution locally.

#### Scenario: A user changes voice provider

- **GIVEN** a Home Assistant installation configured with one speech provider
- **WHEN** the administrator switches to another provider in Home Assistant
- **THEN** the satellite uses it with no change to the robot and no
  reconfiguration of the application

### REQ-046: Voice pipeline state is expressed through movement

The application MUST produce a distinct, observable movement for entering
listening, for processing, and for responding.

#### Scenario: A full exchange

- **GIVEN** an idle robot
- **WHEN** a user speaks the wake word, asks a question, and receives an answer
- **THEN** the robot's movement distinguishes the three phases, so a person in
  the room can tell what it is doing without watching Home Assistant

### REQ-047: Detection source is selectable

The source of face detections MUST be selectable between the groundstation, the
robot's own detector, and the groundstation with local fallback.

#### Scenario: An operator runs without a groundstation

- **GIVEN** an installation with no groundstation deployed
- **WHEN** the operator selects local detection
- **THEN** face tracking works using the robot's own detector, and no session is
  attempted

### REQ-048: The head returns to neutral when tracking data goes stale

When results stop arriving within the staleness window, the application MUST
return the head to its neutral position rather than holding its last commanded
pose.

#### Scenario: The groundstation stops responding mid-track

- **GIVEN** a robot tracking a face
- **WHEN** results stop arriving and the staleness window elapses
- **THEN** the head returns to neutral, making the failure visible rather than
  leaving the robot staring at where a person used to be

### REQ-049: Settings are changeable without a shell

Every operator-facing setting MUST be changeable through the application's own
web interface, and MUST be readable there except where the setting is marked
secret, which is reported as set or unset without its value.

#### Scenario: An operator changes the groundstation address

- **GIVEN** a running application configured with one groundstation address
- **WHEN** the operator opens the settings interface and enters another
- **THEN** the change takes effect without connecting to the robot over a remote
  shell

#### Scenario: An operator replaces a credential

- **GIVEN** a running application configured with a groundstation credential
- **WHEN** the operator opens the settings interface
- **THEN** the credential shows as set without revealing it, and entering a new
  one replaces it — so the interface is usable for rotation without becoming a
  way to read the current value

### REQ-050: Shutdown is graceful and leaves the robot safe

On receiving a termination signal the application MUST stop commanding movement,
release the media interface, and exit.

#### Scenario: The daemon stops the application

- **GIVEN** a running application actively tracking a face
- **WHEN** the daemon signals it to stop
- **THEN** it stops commanding movement, releases audio, and exits, leaving the
  daemon free to return the robot to its default position

## Design

### Structure

```
apps/ha-satellite/src/reachy_mini_ha_satellite/
├─ main.py         # daemon application entry point — wiring only
├─ ports.py        # AudioPort · MotionPort · PerceptionPort
├─ esphome/        # vendored, with its own LICENCE, NOTICE and per-file
│                  # provenance headers
├─ adapters/
│  ├─ audio_reachy.py       # capture and playback via the daemon media layer
│  ├─ motion_reachy.py      # head, antennas, gaze
│  ├─ groundstation.py      # the robot link session client
│  └─ perception_local.py   # the SDK's own detector
├─ behaviour/      # pure: events and detections → motion intents
├─ web/            # settings interface
└─ config.py       # settings, validated at import
```

### Why ports and adapters

The robot is one device on a desk. Anything that can only be tested on it is
effectively untested, so the boundary is drawn to keep the interesting logic off
the hardware: `behaviour/` decides, adapters act, and REQ-042 is what keeps the
line from eroding.

This is also what makes the vendored protocol layer tractable. Its audio seams
are replaced with the audio port, so the substitution happens at one named
interface rather than being threaded through the protocol code.

### Voice pipeline

Wake word runs on the robot; everything downstream runs in Home Assistant. That
split is not an optimisation but the point of the ESPHome protocol — the
satellite is a microphone, a speaker and a set of entities, and the intelligence
is centrally configured. It means changing speech providers or the language
model is a Home Assistant concern and never a robot deployment.

### Motion

Motion intents are produced by the behaviour layer and applied by the motion
adapter. Face tracking converts a normalised centre into a gaze target;
conversation state produces the movements REQ-046 requires.

The staleness behaviour in REQ-048 is deliberate. Holding the last pose looks
like successful tracking of a person who has left, which is worse than visibly
giving up: a neutral head is an honest signal that something upstream stopped.

### Home Assistant device identity

This is the one migration hazard in the component, and it is worth stating
plainly because it is invisible until it has already happened.

Home Assistant keys an ESPHome device on the identity it announces. Change that
identity and Home Assistant does not update the existing device — it registers a
new one. Every entity acquires a suffixed identifier, history detaches from the
old entity, and every automation, script and dashboard card referencing the old
identifiers silently stops matching anything.

The rename from the predecessor package therefore has to leave the announced
identity untouched, which is exactly why REQ-040 makes it configuration rather
than something derived from the package. Renaming the software is safe; renaming
what it announces is not.

### Packaging and deployment

The wheel is published on GitHub Releases and installed into the robot's shared
application environment, where the daemon discovers it through its entry point
mechanism. There is no Hugging Face Space involved; the daemon's Space-based
installation path is one way to deliver a wheel, not the only one.

The daemon runs one application at a time and supplies it with a connected robot
handle and a stop signal. The application inherits the daemon's environment,
which is why configuration validation matters here as much as in the
groundstation — see
[architecture REQ-009](../architecture/index.md#req-009-configuration-is-validated-and-self-reporting).

### Decision Records

#### Clean-room rewrite rather than carrying the fork forward

The forked application declares Apache-2.0 in its package metadata and ships no
licence file, so the default position is all rights reserved. Publishing those
files in a public repository is not defensible on the strength of a metadata
field, and the ambiguity cannot be resolved from this side. Rejected
alternative: vendoring the fork pending an upstream licence clarification, which
makes the repository's publishability contingent on someone else's action.

#### Vendoring the ESPHome layer rather than tracking it as a subtree

Keeping the protocol layer syncable with upstream is only worth the machinery if
the local patches stay small, and they do not: both audio seams are replaced and
the command-line entry point is discarded, leaving roughly 3.5k of 4.9k lines
carried and substantially re-wired. Every sync would be a conflict resolution
regardless of where the code sits, so a separate syncable member would buy the
appearance of a mirror without the property. It is vendored in place with
per-file provenance instead, which is honest about it being a derivation.
Rejected alternative: a separate workspace member with an upstream sync
workflow.

## Constraints

- The robot has four cores and is running motion control and audio alongside
  this application. The predecessor's remote-detection configuration left it at
  1.52 of four cores; local detection saturated it.
- The application runs inside a shared environment managed by the daemon, so its
  dependencies coexist with the daemon's and with other applications.
- Wake-word models and sound assets ship inside the wheel, so their licences are
  subject to the same bar as the models in [perception](../perception/).
- The robot's WLAN is measured at 100–170 ms idle round-trip with 700 ms spikes,
  which bounds how responsive any Home Assistant round trip can feel regardless
  of this application.

## Open Questions

- **Which wake words ship by default.** The upstream satellite carries a set
  under its own terms, and the licence position for each needs confirming before
  they ship in a wheel. Current default: carry the smallest set that makes the
  application usable, and document each one's terms.
- **Whether direction-of-arrival steers the head during conversation.** The
  microphone array reports it and the predecessor used it. It interacts with
  face tracking, and no arbitration between the two is designed. Current
  default: face tracking drives the head; direction of arrival is unused.
- **Whether multi-room audio playback is in scope.** The predecessor had it. It
  is a substantial surface with no bearing on voice assistance. Current default:
  out of scope.

## References

- [architecture](../architecture/) — vendoring, configuration and testing rules
- [robot-link](../robot-link/) — the session this application holds
- [perception](../perception/) — what the detections mean
- [reachyctl](../reachyctl/) — how this application is deployed and inspected
- [Linux voice assistant](https://github.com/OHF-Voice/linux-voice-assistant) — the Apache-2.0 upstream of the vendored protocol layer

## Changelog

| Date | Change | Document |
|------|--------|----------|
| 2026-08-20 | Initial spec created | — |
