# Tasks

Catch-all task list for work not tracked in a specific [change document](changes/).

## Backlog

- [ ] **Run the remaining end-to-end steps against a real Reachy Mini, and replace
      each outstanding `⏳ PENDING HARDWARE VERIFICATION` marker in the runbooks
      with what the command actually printed.**
      [0015](changes/0015-docs-and-runbooks.md) gathered the original list. Later
      hardware-backed changes completed some of that work; the items below are
      only the unrelated steps whose runbook verification remains outstanding.

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
        been near a microphone. **These are the capture side and are still
        outstanding**; the speaker's own volume and ducking were settled by
        [0016](changes/0016-audible-playback.md)
      - The three required antenna motions read as distinct across a room —
        still, opposed, together

      **Home Assistant**
      - It discovers the robot over mDNS and registers it under the announced
        identity
      - An upgrade **keeps** the entity history rather than registering a second
        device. This is the one that matters
      - A voice pipeline runs: wake word, listening, processing, responding.
        **The responding leg is done** — announcements played through the real
        speaker, judged audible at a normal speaking distance, with the volume
        control and the ducking measured across their range;
        [0016](changes/0016-audible-playback.md) records the numbers. What is
        left of this bullet is the three legs in front of it
      - The four controls
        [0020](changes/0020-home-assistant-configuration-and-camera-feed.md)
        added: **Groundstation URL** replacing a live groundstation without a
        restart, and the three motor switches taking torque off and putting it
        back one group at a time with the head held. **The motor switches need a
        robot whose daemon environment carries the forked `reachy-mini` that
        change's completion notes name**; on a stock robot they are correctly
        absent, and confirming *that* is the cheaper half of this bullet
      - The camera: Home Assistant's MJPEG IP Camera integration pointed at the
        groundstation's `/stream.mjpg`, showing a picture, and the endpoint's
        three refusals — no robot, two robots, a fifth viewer — observed against
        a live deployment rather than a fixture

      **The benchmarks**
      - `just bench photon-to-head` and `just bench robot-load`, the two
        excluded from the default selection because they need hardware
        (benchmarks REQ-072), and re-argue `robot-load`'s 25% tolerance from the
        data rather than from judgement
      - The accelerated groundstation variant on a host with an NVIDIA GPU, and
        whether it is in fact faster than the CPU path
      - The default image on an aarch64 host, rather than under emulation

- [ ] **Contribute the correlated motor-torque read-back upstream, then move the
      `reachy-mini` pin off the fork.**
      [0020](changes/0020-home-assistant-configuration-and-camera-feed.md) needed
      a daemon call that correlates a selective torque request with its completion
      and reads physical torque back per motor. No released `reachy-mini` has one.
      The capability was implemented and reviewed on the branch
      `feat/correlated-motor-torque-readback` of the fork at
      https://github.com/fx/reachy_mini, which is what a robot has to run for the
      three motor switches to appear at all — and **no upstream pull request is
      open yet**, which is the half of that change's own prerequisite it did not
      close.

      Two steps, in order: open the upstream pull request and see it merged and
      released; then move the pins in `pyproject.toml` and
      `apps/ha-satellite/pyproject.toml` to that release and delete the notes
      pointing at the fork — `README.md`,
      [the robot runbook](setup/robot.md),
      [the Home Assistant runbook](setup/home-assistant.md),
      [`docs/ops/deploy.md`](ops/deploy.md),
      [`docs/ops/satellite-deployment.md`](ops/satellite-deployment.md),
      `apps/ha-satellite/AGENTS.md` and `.duvet/config.toml`'s registration
      rationale all say the same thing and all stop being true together. Until
      then nothing here pins the fork: a git dependency on an unmerged branch
      would put an unreleasable resolution in `uv.lock`.

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
      vacuously while no specification was registered.
      [0019](changes/0019-predictive-gaze-and-coordinated-motion.md) registered
      the ninth spec with its final safety and deterministic acceptance evidence
      present and
      [0020](changes/0020-home-assistant-configuration-and-camera-feed.md) the
      tenth with its annotations, so all ten specs and all 98 requirements are
      traced and the exclusion no longer applies. A repository setting rather
      than a file, which is why it is a task here.

- [ ] Correct the stale Overview text in the **seven** [specs](specs/) that still
      say nothing is implemented yet:

      ```
      grep -rn "implemented yet" docs/specs/
      ```

      Every change document in the original plan is complete, so the sentence
      is false in all seven matches — five say it outright, while
      [architecture](specs/architecture/) and [robot-link](specs/robot-link/)
      carry it inside a longer sentence. It is here rather than in a change
      document because a spec edit is its own proposal, made through
      `/spec-writer`;
      [0014](changes/0014-benchmarks-and-gates.md) raised it for the benchmarks
      spec alone and it is the same defect everywhere.

      **An eighth match the grep above does not find.**
      [home-assistant-configuration-and-camera-feed](specs/home-assistant-configuration-and-camera-feed/)
      says "The behavior described here is proposed and not yet implemented",
      which spells it the other way round and which
      [0020](changes/0020-home-assistant-configuration-and-camera-feed.md) made
      false: all six of its requirements are implemented, annotated and
      registered. 0020 asked for no changelog row, and a changelog row is the one
      spec edit an implementing change may make, so it was left alone rather than
      corrected in passing. Search for `not yet implemented` as well as
      `implemented yet` when this is picked up.

## Completed

- [x] Record a baseline profile for the `github-ubuntu-latest` runner class in
      `bench/baseline.json`, so the timing half of the benchmark gate judges
      something rather than reporting the class as unbaselined. Recorded in
      [0014](changes/0014-benchmarks-and-gates.md) from the first real run of
      `bench.yml`, which is the only way to get figures for a pool nobody can
      run on locally, and the job now passes `--require-profile` so the class
      cannot silently stop being recorded.
