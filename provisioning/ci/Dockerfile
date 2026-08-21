# The idempotency gate's target: a container approximating the stock robot's
# filesystem and unit layout.
#
# It is not a robot and does not try to be. What it models is exactly what the
# roles touch — a systemd instance with a `reachy-mini-daemon.service` unit, the
# drop-in directory that unit reads, an application environment at
# `/opt/reachy/venv` whose interpreter the unit declares, and an account reached
# over SSH with a non-interactive sudo. What it does not model is the hardware,
# the network to a groundstation, and the aarch64 architecture. See README.md
# beside this file, which is the list.
#
# Pinned by digest rather than by tag, for the reason every `uses:` in this
# repository's workflows is pinned by commit: a tag is mutable, and a retargeted
# one runs different code in CI with no diff here to review.
FROM debian:trixie-slim@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258

# systemd is the point of this image. `systemd-sysv` is what puts `/sbin/init`
# there; `dbus` is what `systemctl` talks to for anything beyond reading unit
# files. openssh-server because the robot is reached over SSH and the gate should
# differ from a real run in as few ways as it can — a container connection plugin
# would exercise a transport no robot ever sees.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        dbus \
        openssh-server \
        python3 \
        python3-venv \
        sudo \
        systemd \
        systemd-sysv \
    && rm --recursive --force /var/lib/apt/lists/*

# The account a run connects as. A placeholder name — this repository is public
# and no real account belongs in a tracked file. Its public key is installed at
# run time by the gate, which generates an ephemeral pair per run and throws it
# away; nothing resembling a credential is baked into this image.
RUN useradd --create-home --shell /bin/bash --user-group reachy \
    && install --directory --owner=reachy --group=reachy --mode=0700 \
        /home/reachy/.ssh \
    && printf 'reachy ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/reachy \
    && chmod 0440 /etc/sudoers.d/reachy

# The application environment, at the path the vendor's image uses and the path
# the unit's `ExecStart` names. `setuptools` is here because the stand-in
# daemon's own build needs it; `pip` is here because installing a wheel into this
# environment is what the `app_install` role does.
COPY stub /opt/reachy/stub
RUN python3 -m venv /opt/reachy/venv \
    && /opt/reachy/venv/bin/pip install --no-cache-dir --upgrade pip setuptools \
    && /opt/reachy/venv/bin/pip install --no-cache-dir /opt/reachy/stub \
    && printf 'A container stand-in, not a robot. See provisioning/ci/README.md.\n' \
        > /opt/reachy/README

COPY reachy-mini-daemon.service /etc/systemd/system/reachy-mini-daemon.service
RUN systemctl enable reachy-mini-daemon.service ssh.service

# systemd's own stop signal. Without it `docker stop` sends SIGTERM, which
# systemd reads as "start the default target" rather than "shut down".
STOPSIGNAL SIGRTMIN+3

CMD ["/sbin/init"]
