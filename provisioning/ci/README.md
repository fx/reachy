# The idempotency gate's container target

`just provision-idempotency` builds the image beside this file, starts it, and
applies `site.yml` against it twice. The check is that the second application
reports zero changed steps, which is
[REQ-061](../../docs/specs/provisioning/index.md#req-061-idempotency-is-enforced-automatically).

Idempotency is the property that decays first as roles are edited and the one
that is invisible without a check: a task written non-idempotently works
perfectly, converges the robot, and reports a change on every run forever after.
Nobody notices until a run that was supposed to change nothing restarts the
daemon in the middle of a conversation.

## What it models

Exactly what the roles touch, and the list is short because the roles are:

| Modelled | Why the roles need it |
|---|---|
| A real systemd instance, running as PID 1 | `daemon-reload`, `restart`, and `systemctl show` are what the roles read and write; a stub `systemctl` would make the gate a test of the stub |
| A `reachy-mini-daemon.service` unit whose `ExecStart` names `/opt/reachy/venv/bin/python` | The roles ask systemd which interpreter the daemon runs rather than assuming the configured one. A target that declared no command would exercise only the fallback |
| `/etc/systemd/system/reachy-mini-daemon.service.d/` | Where the managed drop-in goes. The directory does **not** exist in the image, so a run has to create it — which is what REQ-062 says a stock image is allowed to be like |
| An application environment at `/opt/reachy/venv` with `pip` | `app_install` installs a wheel into the environment the daemon runs |
| A `reachy-mini` distribution in that environment, at a version | What the daemon check reports, and what the shared registry reads through the daemon's own interpreter |
| `python -m reachy_mini.apps status --json <application>` | How the `application.running` check is asked. In the container it answers from what the daemon recorded at its last start, which is what makes "install a wheel, then restart" observable |
| SSH with a non-interactive sudo | The transport a real run uses. A container connection plugin would exercise one no robot ever sees |

## What it does not model, and where those are proved instead

| Not modelled | Why | Where it is proved |
|---|---|---|
| Any hardware — camera, microphone, motors | A container has none, and neither do the roles: everything they do is a file, a unit and an install | The deferred session against a real Reachy Mini |
| The vendor's real daemon | It is not distributable, and reproducing its behaviour would make the gate a test of the reproduction | The deferred session against a real Reachy Mini; `reachy_mini.apps` is the one interface the roles use, and meeting a robot that spells it differently costs a variable |
| The groundstation link | A session opened from the machine running the playbook measures that machine's route, not the robot's | `reachyctl doctor --url`, and the `groundstation.session` check reports itself skipped here saying so |
| The model files | They belong to the groundstation's artifact and are not on the robot | `just image-verify`, and the `models.files` check reports itself skipped |
| aarch64 | The gate runs on the runner's architecture | The wheels this repository publishes are built and tested for aarch64; see the release workflow |
| A first boot from a freshly flashed image | The container starts from a built image rather than an installer | The deferred session against a real Reachy Mini |

The `reachy_mini` package under `stub/` is **not** the Reachy Mini SDK, shares
nothing with it but a name, and is installed nowhere except inside this
container. It is excluded from this repository's type checking for that reason —
see the `exclude` entry in the root `pyproject.toml`, which says so.

## Running it by hand

```
just provision-target-up          # build, start, and print how to reach it
just provision-idempotency        # the gate: apply twice, fail on any change
just provision-target-down        # stop and remove it
```

`provision-target-up` leaves an inventory and an SSH key under a directory it
prints, so an ordinary `ansible-playbook` run — `--check`, `--tags`,
`remove.yml` — can be driven against the container the same way it would be
driven against a robot.
