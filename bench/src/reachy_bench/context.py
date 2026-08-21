"""What a result was measured on, so an unexpected number can be explained.

Benchmarks REQ-068's scenario is a result substantially faster than the previous
one, and the question it asks is whether the code got faster or the measurement
conditions changed. Answering that from the result alone means the result has to
carry the host, the software versions and the configuration — so it does, and
this module is where those are gathered.

**Nothing here records who or where.** This repository is public and a benchmark
result is exactly the artifact a machine name leaks through, so the host is
described by its *class* — operating system, kernel release, architecture, CPU
model, core count, memory — and never by its identity. There is no hostname, no
user, no address and no path outside the repository in a result document.
`ALLOWED_HOST_FIELDS` below names every field that may appear, and a unit test
holds the rendered document to exactly that set, so a field added later is a red
run rather than a leak nobody noticed.

**The host class is also the baseline key.** Continuous integration hardware is
not deployment hardware, so a measurement is only comparable against a baseline
taken on the same class of machine — see `reachy_bench.compare`. The label is
derived from the fields above, and it can be overridden, because the honest
label for a runner is the runner pool's name rather than whatever the kernel
happens to report about the virtual machine it landed on.

Every reader this module needs is an argument with a default. That is what keeps
the harness's own tests free of input and output: they hand it strings.
"""

from __future__ import annotations

import os
import platform

# The one call site below runs a fixed argument vector with no shell, and it is
# the only way to read the commit a result was measured at.
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

__all__ = [
    "ALLOWED_HOST_FIELDS",
    "RECORDED_DISTRIBUTIONS",
    "HostContext",
    "RunContext",
    "SoftwareContext",
    "collect_context",
    "host_profile",
    "read_cpu_model",
]

# Every key a host record may carry. The set is asserted in the tests rather
# than merely intended: this is the part of a result document that would leak a
# machine's identity, and "we were careful" is not a control.
ALLOWED_HOST_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "profile",
        "system",
        "release",
        "machine",
        "cpu_model",
        "cpu_count",
        "memory_mib",
    },
)

# The versions that decide what a timing means. Every one of them is a public
# third-party identifier or a member of this repository, so recording them is
# not a disclosure of anything.
RECORDED_DISTRIBUTIONS: Final[tuple[str, ...]] = (
    "numpy",
    "onnxruntime",
    "opencv-python-headless",
    "prometheus-client",
    "reachy-contracts",
    "reachy-groundstation",
    "reachy-session-client",
    "uvicorn",
    "websockets",
)

# What a version reads as when the distribution is not installed. A benchmark
# run in an environment missing one of the above still produces a result; the
# result says which one was missing rather than failing to be written.
ABSENT: Final = "absent"

_KIBIBYTE: Final = 1024
_CPUINFO: Final = Path("/proc/cpuinfo")
_MEMINFO: Final = Path("/proc/meminfo")

# How long to let `git` take. A repository in a state where `rev-parse` blocks
# is not a reason for a benchmark run to hang.
_GIT_TIMEOUT_SECONDS: Final = 10.0


def _read(path: Path) -> str:
    """Read a file, treating an unreadable one as absent.

    Args:
        path: The file to read.

    Returns:
        Its contents, or an empty string when it is not there or not readable.
        Both are the same thing here: this module is describing a host, and a
        host that will not describe itself is recorded as unknown rather than
        stopping the run.
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def read_cpu_model(cpuinfo: str) -> str:
    """Pick the processor model out of a Linux `cpuinfo` dump.

    The model name is a hardware identifier and not a host identifier — every
    machine of the same kind reports the same string — which is why it is
    recorded when the hostname beside it is not.

    Args:
        cpuinfo: The contents of `/proc/cpuinfo`, or an empty string.

    Returns:
        The model name, or an empty string when the dump does not carry one.
    """
    for line in cpuinfo.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() == "model name":
            return value.strip()
    return ""


def _read_memory_mib(meminfo: str) -> int:
    """Read total memory out of a Linux `meminfo` dump.

    Args:
        meminfo: The contents of `/proc/meminfo`, or an empty string.

    Returns:
        Total memory in mebibytes, or 0 when the dump does not carry it.
    """
    for line in meminfo.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() == "MemTotal":
            digits = value.strip().split()
            if digits and digits[0].isdigit():
                return int(digits[0]) // _KIBIBYTE
    return 0


def host_profile(system: str, machine: str, cpu_count: int) -> str:
    """Name the class of machine a measurement was taken on.

    The core count is part of the label rather than a detail beside it, because
    it is the property that most changes what a timing means: a four-core shared
    runner and a thirty-two-core workstation are the same operating system on
    the same architecture and are not the same measurement condition. A baseline
    keyed on the operating system alone would compare the two and call the
    difference a regression.

    Args:
        system: The operating system, as `platform.system` reports it.
        machine: The architecture, as `platform.machine` reports it.
        cpu_count: How many logical processors are visible.

    Returns:
        The label, lowercased, such as `linux-x86_64-32c`.
    """
    return f"{system}-{machine}-{cpu_count}c".lower()


@dataclass(frozen=True, slots=True, kw_only=True)
class HostContext:
    """The class of machine a measurement was taken on, never its identity.

    Attributes:
        profile: The label a baseline is keyed on.
        system: The operating system.
        release: The kernel release.
        machine: The processor architecture.
        cpu_model: The processor model, which is a kind of machine and not a
            particular one.
        cpu_count: How many logical processors were visible.
        memory_mib: Total memory in mebibytes.
    """

    profile: str
    system: str
    release: str
    machine: str
    cpu_model: str
    cpu_count: int
    memory_mib: int

    def as_document(self) -> dict[str, str | int]:
        """Render the host for the result document.

        Returns:
            A JSON-serialisable mapping whose keys are exactly
            `ALLOWED_HOST_FIELDS`.
        """
        return {
            "profile": self.profile,
            "system": self.system,
            "release": self.release,
            "machine": self.machine,
            "cpu_model": self.cpu_model,
            "cpu_count": self.cpu_count,
            "memory_mib": self.memory_mib,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class SoftwareContext:
    """What was installed when the measurement was taken.

    Attributes:
        python: The interpreter version.
        commit: The repository revision, or an empty string when it could not
            be read. A public commit identifier, which is what makes a result
            traceable to the tree that produced it.
        versions: Distribution name to version, for the distributions whose
            version changes what a timing means.
    """

    python: str
    commit: str
    versions: Mapping[str, str]

    def as_document(self) -> dict[str, object]:
        """Render the software versions for the result document.

        Returns:
            A JSON-serialisable mapping.
        """
        return {
            "python": self.python,
            "commit": self.commit,
            "versions": dict(sorted(self.versions.items())),
        }


#:= docs/specs/benchmarks/index.md#req-068-every-result-records-the-context-it-was-measured-in
#:% Each benchmark result MUST record the hardware, the software versions, and the
#:% configuration it was produced under.
@dataclass(frozen=True, slots=True, kw_only=True)
class RunContext:
    """Everything about the run that is not one of its measurements.

    The hardware and the software versions are here, once for the run; the
    configuration is on each `BenchmarkResult`, because two benchmarks in one
    run are configured differently and a single merged block would say neither
    accurately. One result document therefore answers REQ-068's scenario in
    full — the host, the thread count and the model in use are all readable from
    it without another run.

    Attributes:
        host: The class of machine.
        software: What was installed.
        started_at: When the run began, in UTC, to the second.
        network: How the link the measurement crossed was behaving, in the
            operator's own words. Recorded rather than controlled — see the
            benchmarks spec — and empty when the run crossed no network.
    """

    host: HostContext
    software: SoftwareContext
    started_at: str
    network: str = ""

    def as_document(self) -> dict[str, object]:
        """Render the context for the result document.

        Returns:
            A JSON-serialisable mapping.
        """
        return {
            "host": self.host.as_document(),
            "software": self.software.as_document(),
            "started_at": self.started_at,
            "network": self.network,
        }


def _read_commit(
    run: Callable[[Sequence[str]], str],
) -> str:
    """Ask git which revision this is.

    Args:
        run: How to run a command and read its output.

    Returns:
        The commit identifier, or an empty string when git could not answer —
        an exported tree with no repository, most often, which is a fact about
        the checkout rather than a reason to abandon a measurement.
    """
    try:
        return run(("git", "rev-parse", "HEAD")).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _git(argv: Sequence[str]) -> str:
    """Run a fixed command and return its standard output.

    Args:
        argv: The argument vector. Never a string and never through a shell,
            which is why bandit's `subprocess` finding is suppressed at the
            import above rather than argued about here.

    Returns:
        The command's standard output.

    Raises:
        subprocess.SubprocessError: If the command failed or timed out.
        OSError: If it could not be run at all.
    """
    completed = subprocess.run(  # noqa: S603  # a fixed argument vector, no shell, no caller-supplied text
        list(argv),
        capture_output=True,
        text=True,
        check=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    return completed.stdout


def _versions(
    distributions: Sequence[str],
    lookup: Callable[[str], str],
) -> dict[str, str]:
    """Read the installed version of each distribution that matters.

    Args:
        distributions: What to look up.
        lookup: How to look one up.

    Returns:
        Distribution name to version, with `ABSENT` for anything not installed.
    """
    found: dict[str, str] = {}
    for name in distributions:
        try:
            found[name] = lookup(name)
        except metadata.PackageNotFoundError:
            found[name] = ABSENT
    return found


def collect_context(
    *,
    profile: str = "",
    network: str = "",
    cpu_count: int | None = None,
    cpuinfo: str | None = None,
    meminfo: str | None = None,
    run_command: Callable[[Sequence[str]], str] = _git,
    version_of: Callable[[str], str] = metadata.version,
    now: Callable[[], datetime] | None = None,
) -> RunContext:
    """Describe the machine and the tree this run is happening on.

    Every reader is an argument with a default, so the harness's own tests
    exercise this function without reading a file, running a process or asking
    the clock — which is the standing rule for a unit test here.

    Args:
        profile: A host-class label to use instead of the derived one. This is
            how a continuous integration job names its runner pool, which is a
            more honest class than whatever the kernel reports about the virtual
            machine the job landed on.
        network: How the link behaved, when the run crossed one.
        cpu_count: How many logical processors to record. Read from the host
            when omitted.
        cpuinfo: The contents of `/proc/cpuinfo`. Read from the host when
            omitted.
        meminfo: The contents of `/proc/meminfo`. Read from the host when
            omitted.
        run_command: How to run git.
        version_of: How to read an installed distribution's version.
        now: What the clock says. Defaults to the wall clock in UTC.

    Returns:
        The context every result in this run is reported under.
    """
    system = platform.system()
    machine = platform.machine()
    cores = cpu_count if cpu_count is not None else (_cpu_count())
    host = HostContext(
        profile=profile or host_profile(system, machine, cores),
        system=system,
        release=platform.release(),
        machine=machine,
        cpu_model=read_cpu_model(_read(_CPUINFO) if cpuinfo is None else cpuinfo),
        cpu_count=cores,
        memory_mib=_read_memory_mib(_read(_MEMINFO) if meminfo is None else meminfo),
    )
    software = SoftwareContext(
        python=platform.python_version(),
        commit=_read_commit(run_command),
        versions=_versions(RECORDED_DISTRIBUTIONS, version_of),
    )
    moment = (now or (lambda: datetime.now(UTC)))()
    return RunContext(
        host=host,
        software=software,
        started_at=moment.replace(microsecond=0).isoformat(),
        network=network,
    )


def _cpu_count() -> int:
    """Count the processors this run may actually use.

    `os.cpu_count` reports what the machine has; the scheduler's affinity mask
    reports what this process is allowed on, which is what a model runtime will
    actually spread across and is therefore the honest number to key a host
    class on. The mask exists on Linux and not everywhere, so the machine's own
    count is the fallback.

    Returns:
        The number of logical processors available, and at least one.
    """
    affinity = getattr(os, "sched_getaffinity", None)
    if affinity is not None:
        return max(1, len(affinity(0)))
    return max(1, os.cpu_count() or 1)
