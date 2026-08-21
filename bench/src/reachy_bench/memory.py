"""Reading how much memory the service actually holds, from outside it.

Measured from a subprocess rather than from inside this one, and that is the
whole design. A resident set read from within the benchmark process would
include the harness, the test dependencies and whatever else `uv run` put on the
path, so it would answer "how big is this run" rather than "how big is the
service" — and the figure this is compared against is one about a deployed
process.

The service is started exactly as a deployment starts it, through its own module
entry point and configured entirely by environment variables, and it is asked
for its resident set once it reports itself *ready* rather than merely alive.
Readiness means every capability has warmed up, so the model is loaded and its
arenas are allocated: reading earlier would report a service still growing.

Linux only, and it says so. `/proc/<pid>/status` is where a process publishes
its resident set, and a platform without it is reported as unable to measure
rather than as measuring zero.
"""

from __future__ import annotations

import contextlib
import os
import socket

# The one call site below starts this repository's own service with a fixed
# argument vector and no shell.
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from reachy_bench.registry import Options

__all__ = ["free_port", "read_resident_mebibytes", "resident_memory_of_service"]

_KIBIBYTE: Final = 1024

# How long to wait for the service to report itself ready. Warm-up loads the
# model and pays for one inference, which is seconds rather than milliseconds,
# and a shared machine can make it several.
_READY_TIMEOUT_SECONDS: Final = 180.0

# How long to give the service to stop before it is killed. It has one model
# runtime and a thread to release.
_STOP_TIMEOUT_SECONDS: Final = 30.0

_POLL_SECONDS: Final = 0.25

# What a ready service answers with.
_HTTP_OK: Final = 200


def free_port() -> int:
    """Find a port nothing is listening on.

    The service has to be polled for readiness, so it cannot be given port zero
    and asked to pick: this binds a port, learns its number and releases it. The
    gap between releasing and the service binding is a race in principle; in
    practice the kernel does not immediately reissue a port it has just handed
    out, and losing the race fails the benchmark loudly rather than producing a
    wrong number.

    Returns:
        A port number.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def read_resident_mebibytes(status: str) -> int:
    """Read a resident set out of a Linux process status dump.

    Args:
        status: The contents of `/proc/<pid>/status`.

    Returns:
        The resident set in mebibytes.

    Raises:
        ValueError: If the dump carries no `VmRSS`, which is what a platform
            that does not publish one looks like. Reported rather than
            defaulted: a footprint of zero would pass every tolerance.
    """
    for line in status.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() == "VmRSS":
            parts = value.split()
            if parts and parts[0].isdigit():
                return int(parts[0]) // _KIBIBYTE
    message = "no VmRSS in the process status; this platform is not Linux"
    raise ValueError(message)


def _ready(url: str) -> bool:
    """Ask the service whether it is ready to be sent work.

    Args:
        url: The readiness endpoint.

    Returns:
        True once it answers 200. Any refusal, timeout or non-200 is "not yet"
        rather than a failure: the service is expected to refuse while it warms
        up, and the caller's deadline is what turns a permanent refusal into an
        error.
    """
    try:
        with urllib.request.urlopen(url, timeout=_POLL_SECONDS * 4) as answer:  # noqa: S310  # a loopback http:// URL this function built from a port it chose; no caller-supplied scheme can reach it
            return int(answer.status) == _HTTP_OK
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def resident_memory_of_service(  # pragma: no cover
    options: Options,
) -> tuple[int, str]:
    """Start the service, wait for it to be ready, and read its resident set.

    Not unit-tested, and excluded from coverage rather than mocked: it starts
    the service as a real subprocess, so a unit test of it would be a unit test
    of the operating system. The two functions above it — reading a resident set
    out of a status dump, and finding a free port — are the parts with logic in
    them, and they are tested. What exercises this one is `just bench`, which
    the benchmark workflow runs on every pull request.

    Args:
        options: What the run was configured with.

    Returns:
        The resident set in mebibytes, and one line describing what was
        measured.

    Raises:
        RuntimeError: If the service exited while starting, or never reported
            itself ready within the bound.
        ValueError: If this platform does not publish a resident set.
    """
    port = free_port()
    environment = dict(os.environ)
    # Every variable the service reads it reads from here, and an unrecognised
    # one under its prefix is fatal by design — so the environment is set
    # explicitly rather than inherited with additions.
    environment.update(
        {
            "REACHY_GROUNDSTATION_CREDENTIAL": "benchmark-placeholder",
            "REACHY_GROUNDSTATION_MODELS_DIR": str(options.models_dir),
            "REACHY_GROUNDSTATION_HOST": "127.0.0.1",
            "REACHY_GROUNDSTATION_PORT": str(port),
            "REACHY_GROUNDSTATION_LOG_LEVEL": "warning",
        },
    )
    # A fixed argument vector running this repository's own module: no shell,
    # and no caller-supplied text anywhere in it.
    service = subprocess.Popen(
        [sys.executable, "-m", "reachy_groundstation"],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
        url = f"http://127.0.0.1:{port}/readyz"
        while not _ready(url):
            if service.poll() is not None:
                complaint = (service.stderr.read() if service.stderr else "").strip()
                message = (
                    f"the groundstation exited with status {service.returncode} "
                    f"while starting: {complaint or 'it said nothing'}"
                )
                raise RuntimeError(message)
            if time.monotonic() > deadline:
                message = (
                    "the groundstation did not report itself ready within "
                    f"{_READY_TIMEOUT_SECONDS:.0f}s"
                )
                raise RuntimeError(message)
            time.sleep(_POLL_SECONDS)
        status = Path(f"/proc/{service.pid}/status").read_text(encoding="utf-8")
    finally:
        service.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            service.wait(timeout=_STOP_TIMEOUT_SECONDS)
        if service.poll() is None:
            service.kill()
        if service.stderr is not None:
            service.stderr.close()
    return (
        read_resident_mebibytes(status),
        "reachy_groundstation, once it reported itself ready",
    )
