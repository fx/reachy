# Tasks

Catch-all task list for work not tracked in a specific [change document](changes/).

## Backlog

- [ ] **Run the end-to-end session against a real Reachy Mini, and replace every
      `⏳ PENDING HARDWARE VERIFICATION` marker in the runbooks with what the
      command actually printed.** Every change document from 0004 onwards defers
      something to this session, and
      [0015](changes/0015-docs-and-runbooks.md) is where the list was gathered.
      Nothing in this repository has a robot attached, so these steps have never
      been run; the markers exist so a reader can tell which parts of a runbook
      are transcribed and which are not.

      Grep for the marker to find them: `grep -rn "PENDING HARDWARE" docs/`.
      What the session has to execute, and what it is answering:

      **The robot itself**
      - `reachyctl provision --preview` and `reachyctl provision` against a
        stock robot, and a second application reporting `changed=0`
        (provisioning REQ-060, so far proved only against the container target)
      - `reachyctl provision --remove`
      - `reachyctl deploy --preview`, `reachyctl deploy`, and the version the
        robot reports afterwards
      - `reachyctl config get`, `diff`, `apply --preview`, `apply`, `set`
      - `reachyctl app start`, `stop`, `logs`
      - `reachyctl doctor --robot …`, with every check passing rather than
        skipped
      - Confirm the daemon's application control really is
        `python -m reachy_mini.apps` — change 0009 records it as the one
        provisional interface, and `--daemon-control` is what a robot that
        spells it differently costs

      **The satellite on the robot**
      - It starts, appears in the daemon's application list, and the dashboard
        links to its settings page (ha-satellite REQ-049)
      - `/config`, `/status` and the settings page answer
      - **Somebody says the wake word and the robot wakes** (ha-satellite
        REQ-044), and says it again with the network unplugged. The detection
        loop is proved here against fake models, and against the real ones only
        with the threshold forced below anything they can report — there is no
        recording of the phrase in this repository, so *recognition* has never
        been exercised anywhere. Say "stop" over a response and check that it
        stops; then check that saying it at an idle robot does nothing
      - The three microphone settings audibly do something: turn Home
        Assistant's `mic_volume` down and confirm it transcribes a quieter
        signal, and turn `mic_gain` and `mic_noise` up — the entities behind
        `mic_auto_gain` and `mic_noise_suppression` — and confirm the
        conditioner improves a noisy room rather than damaging a quiet one. All
        three were inert until the detection loop landed and none of them has
        been near a microphone
      - The three required antenna motions read as distinct across a room —
        still, opposed, together — and the head tracks smoothly at the tuned
        deadzone and smoothing
      - A face is tracked end to end, through the groundstation

      **Home Assistant**
      - It discovers the robot over mDNS and registers it under the announced
        identity
      - An upgrade **keeps** the entity history rather than registering a second
        device. This is the one that matters
      - A voice pipeline runs: wake word, listening, processing, responding

      **The benchmarks**
      - `just bench photon-to-head` and `just bench robot-load`, the two
        excluded from the default selection because they need hardware
        (benchmarks REQ-072), and re-argue `robot-load`'s 25% tolerance from the
        data rather than from judgement
      - The accelerated groundstation variant on a host with an NVIDIA GPU, and
        whether it is in fact faster than the CPU path
      - The default image on an aarch64 host, rather than under emulation

- [ ] **Reconcile the robot's configuration vocabulary with the settings the
      satellite actually reads.** Found while writing the runbooks for
      [0015](changes/0015-docs-and-runbooks.md), and documented as a known gap
      in [the robot runbook](setup/robot.md) and
      [the Home Assistant runbook](setup/home-assistant.md) rather than worked
      around.

      `reachy_contracts.ROBOT_SETTINGS` declares seven names.
      `reachyctl config` validates against it, the Ansible `daemon_env` role
      writes it, and the satellite reads exactly one of them
      (`REACHY_SATELLITE_LOG_LEVEL`). `REACHY_SATELLITE_FRAME_INTERVAL_MS`,
      `REACHY_SATELLITE_JPEG_QUALITY` and
      `REACHY_SATELLITE_RESULT_STALENESS_SECONDS` are under its prefix and are
      accepted and reported as having no effect;
      `REACHY_GROUNDSTATION_URL`, `REACHY_GROUNDSTATION_CREDENTIAL` and
      `REACHY_HOME_ASSISTANT_IDENTITY` are not under its prefix at all, so it
      never looks at them.

      Two things follow, and the second is the serious one:

      - A robot provisioned from a declaration alone has a satellite that
        **refuses to start**, because `REACHY_SATELLITE_DEVICE_NAME` has no
        default and `validate_settings` refuses to write a name the vocabulary
        does not declare. The only route today is a second systemd drop-in
        written by hand beside the managed one.
      - `doctor`'s `home-assistant.identity` check reads
        `REACHY_HOME_ASSISTANT_IDENTITY` and calls it the identity the satellite
        announces. The satellite announces `REACHY_SATELLITE_DEVICE_NAME`. The
        check can therefore report `passed` while the two disagree — on the one
        failure the whole documentation set is built around.

      This is a change to the [reachyctl](specs/reachyctl/) and
      [ha-satellite](specs/ha-satellite/) specs, so it is a `/spec-writer`
      proposal and a change document rather than an edit. It is here because it
      belongs to no existing change document.

- [ ] **Make `Requirements traceability` a required status check.**
      [0002](changes/0002-ci-and-hygiene-gates.md)'s completion notes list six
      checks to require and deliberately exclude this one, because it passed
      vacuously while no specification was registered. As of
      [0015](changes/0015-docs-and-runbooks.md) all eight specs are registered
      and all 73 requirements are traced, so the exclusion no longer applies.
      A repository setting rather than a file, which is why it is a task here.

- [ ] Correct the Overview of **all eight** [specs](specs/), every one of which
      still says nothing is implemented yet:

      ```
      grep -rn "implemented yet" docs/specs/
      ```

      Every change document in the plan is now complete, so the sentence is
      false in all eight — five of them say it outright, and
      [architecture](specs/architecture/), [robot-link](specs/robot-link/) and
      [ha-satellite](specs/ha-satellite/) carry it inside a longer sentence. It
      is here rather than in a change document because a spec edit is its own
      proposal, made through `/spec-writer`;
      [0014](changes/0014-benchmarks-and-gates.md) raised it for the benchmarks
      spec alone and it is the same defect everywhere.

## Completed

- [x] Record a baseline profile for the `github-ubuntu-latest` runner class in
      `bench/baseline.json`, so the timing half of the benchmark gate judges
      something rather than reporting the class as unbaselined. Recorded in
      [0014](changes/0014-benchmarks-and-gates.md) from the first real run of
      `bench.yml`, which is the only way to get figures for a pool nobody can
      run on locally, and the job now passes `--require-profile` so the class
      cannot silently stop being recorded.
