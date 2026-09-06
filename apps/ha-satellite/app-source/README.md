---
title: Reachy Mini HA Satellite
emoji: 🏠
colorFrom: blue
colorTo: green
sdk: static
pinned: false
short_description: A Home Assistant voice satellite for the Reachy Mini
tags:
  - reachy_mini
  - reachy_mini_python_app
---

# Reachy Mini HA Satellite

A Home Assistant voice satellite for the Reachy Mini. The robot detects the wake
word itself, streams the audio to Home Assistant's voice pipeline, plays the
response back, and looks at whoever is talking to it. Its antennas say what the
pipeline is doing, in a way that is legible from across a room.

Installing this application needs no shell on the robot, no file copied onto it,
and no change to any file its image ships.

## What gets installed

Nothing here is code. This source names one released wheel and installs it:
version **0.2.0** of `reachy_mini_ha_satellite`. <!-- x-release-please-version -->

The wheel is a release asset of the public repository the application is
developed in, so it is fetched without a credential and its checksum is
published beside it. The wheel declares the `reachy_mini_apps` entry point, and
that entry point is how the robot's daemon finds the application after the
install — the same mechanism it uses for any application, whatever route the
files arrived by.

## After installing: the robot is not configured yet

**Nothing is announced to Home Assistant until you give the robot a name.** That
is deliberate, and it is the one decision on this page worth slowing down for:
Home Assistant keys an ESPHome device on the identity it announces, so a name
changed later registers a *second* device and detaches the first one's history
from everything referring to it.

So the application starts with no identity, announces nothing at all, and serves
its own settings page saying so. Open it at port **8088** on the robot, enter the
name, and start the application again from the robot's dashboard. Choosing the
name is the whole of the first-time configuration; everything else has a working
default.

The settings page is also where the optional off-robot detection service is
configured, where the wake word is chosen, and where the fully resolved
configuration is shown with secrets reported as set or unset rather than by
value.

## What it needs

- **Home Assistant** on the same layer-2 network as the robot, which is how it
  discovers the satellite over mDNS.
- **Optionally**, an off-robot service to run face detection on. Without one the
  robot uses its own detector where a model has been configured, and says on its
  settings page that the remote source is unconfigured rather than failed.

## Where the code and the documentation are

Everything — the source, the runbooks, the specifications this is built against,
and the release the wheel above comes from — is at
<https://github.com/fx/reachy>. This Space is published from the directory
`apps/ha-satellite/app-source/` in that repository, and every file it installs
from is byte-for-byte what is committed there — so what you install here is
reviewable there. The only file on the Space that does not come from that
directory is the `.gitattributes` Hugging Face creates with every repository,
which the robot never downloads.

Issues and pull requests are welcome in that repository rather than here.
