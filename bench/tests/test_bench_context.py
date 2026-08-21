"""What a result records about the machine — and, more importantly, what it does not.

This repository is public, and a benchmark result is precisely the artifact a
machine name or an account leaks through: it describes a host, so somebody
writing one reaches for `platform.uname()` without thinking about which of its
fields is an identity. The first test below is the control for that. It holds
the rendered host record to `ALLOWED_HOST_FIELDS` exactly, so a field added
later fails the suite instead of reaching a public result document.

Every reader `collect_context` uses is an argument, so nothing here reads
`/proc`, runs `git` or asks the clock.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib import metadata
from typing import TYPE_CHECKING

import pytest

from reachy_bench.context import (
    ABSENT,
    ALLOWED_HOST_FIELDS,
    RECORDED_DISTRIBUTIONS,
    RunContext,
    collect_context,
    host_profile,
    read_cpu_model,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

# A `/proc/cpuinfo` dump carrying a model name, and nothing that identifies the
# machine it came from.
_CPUINFO = """processor\t: 0
vendor_id\t: AuthenticAMD
model name\t: Example Processor 1000
cpu MHz\t\t: 2400.000
"""

_MEMINFO = "MemTotal:       16384000 kB\nMemFree:         1000000 kB\n"


def _no_git(argv: Sequence[str]) -> str:
    """Stand in for git without running one.

    Args:
        argv: The argument vector, unused.

    Returns:
        A commit identifier.
    """
    assert argv[0] == "git"
    return "0123456789abcdef0123456789abcdef01234567\n"


def _version(name: str) -> str:
    """Stand in for the installed-version lookup.

    Args:
        name: The distribution.

    Returns:
        A version, or a refusal for one distribution so the absent path is
        exercised.

    Raises:
        PackageNotFoundError: For `onnxruntime`, standing in for an environment
            that has not got it.
    """
    if name == "onnxruntime":
        raise metadata.PackageNotFoundError(name)
    return "1.2.3"


def _context(
    *,
    profile: str = "",
    network: str = "",
    meminfo: str = _MEMINFO,
    run_command: Callable[[Sequence[str]], str] = _no_git,
) -> RunContext:
    """Collect a context with every reader faked.

    Args:
        profile: A host-class label to state instead of the derived one.
        network: How the link behaved.
        meminfo: The memory dump to read.
        run_command: How to run git.

    Returns:
        The context.
    """
    return collect_context(
        profile=profile,
        network=network,
        cpu_count=4,
        cpuinfo=_CPUINFO,
        meminfo=meminfo,
        run_command=run_command,
        version_of=_version,
        now=lambda: datetime(2026, 8, 21, 12, 0, 0, 123456, tzinfo=UTC),
    )


def test_the_host_record_carries_exactly_the_fields_it_is_allowed_to() -> None:
    """The control on this repository being public.

    A result describes a host. The fields it may describe it with are named in
    one place, and this is what holds the rendered document to them — so a
    hostname, a user or an address added later is a red run rather than a leak.
    """
    context = _context()

    assert set(context.host.as_document()) == ALLOWED_HOST_FIELDS


def test_no_field_of_the_host_record_names_the_machine() -> None:
    """A model name is a kind of machine; a hostname is a particular one."""
    document = _context().host.as_document()

    assert "node" not in document
    assert "hostname" not in document
    assert document["cpu_model"] == "Example Processor 1000"


def test_the_model_name_is_read_out_of_the_processor_dump() -> None:
    """The dump has many lines and only one of them is the model."""
    assert read_cpu_model(_CPUINFO) == "Example Processor 1000"


def test_a_dump_with_no_model_name_reports_none_rather_than_guessing() -> None:
    """A host that will not describe itself is unknown, not stopping the run."""
    assert read_cpu_model("processor\t: 0\n") == ""
    assert read_cpu_model("") == ""


def test_the_host_class_carries_the_core_count() -> None:
    """A four-core runner and a thirty-two-core workstation are not one class.

    A baseline keyed on the operating system alone would compare the two and
    call the difference a regression.
    """
    assert host_profile("Linux", "x86_64", 4) == "linux-x86_64-4c"
    assert host_profile("Linux", "x86_64", 32) == "linux-x86_64-32c"


def test_a_stated_profile_replaces_the_derived_one() -> None:
    """A runner pool's name is a more honest class than the virtual machine's."""
    context = _context(profile="github-ubuntu-latest")

    assert context.host.profile == "github-ubuntu-latest"


def test_the_memory_total_is_read_in_mebibytes() -> None:
    """The dump is in kibibytes and the record is in mebibytes."""
    assert _context().host.memory_mib == 16000


def test_a_dump_with_no_memory_total_reports_zero() -> None:
    """A platform that does not publish one is described as unknown."""
    assert _context(meminfo="").host.memory_mib == 0


def test_every_recorded_distribution_is_looked_up() -> None:
    """The versions that decide what a timing means are all in the record."""
    versions = _context().software.versions

    assert set(versions) == set(RECORDED_DISTRIBUTIONS)


def test_a_distribution_that_is_not_installed_says_so() -> None:
    """A run in an environment missing one still produces a result."""
    versions = _context().software.versions

    assert versions["onnxruntime"] == ABSENT
    assert versions["numpy"] == "1.2.3"


def test_the_commit_is_recorded_so_a_result_is_traceable_to_a_tree() -> None:
    """A public commit identifier, which discloses nothing."""
    assert _context().software.commit == ("0123456789abcdef0123456789abcdef01234567")


def test_a_checkout_with_no_repository_records_no_commit() -> None:
    """An exported tree is a fact about the checkout, not a broken run."""

    def _refuses(argv: Sequence[str]) -> str:
        """Stand in for git failing to run.

        Args:
            argv: The argument vector, unused.

        Returns:
            Nothing; it always raises.

        Raises:
            OSError: Always.
        """
        del argv
        raise OSError(2, "no such file")

    assert _context(run_command=_refuses).software.commit == ""


def test_the_moment_is_recorded_to_the_second() -> None:
    """Microseconds make two result files differ where nothing happened."""
    assert _context().started_at == "2026-08-21T12:00:00+00:00"


def test_the_network_context_travels_with_the_run() -> None:
    """Recorded rather than controlled — the benchmarks spec's decision."""
    context = _context(network="2.4 GHz WLAN, 120 ms idle round-trip")

    assert context.network == "2.4 GHz WLAN, 120 ms idle round-trip"
    assert context.as_document()["network"] == context.network


@pytest.mark.parametrize("section", ["host", "software", "started_at", "network"])
def test_the_run_context_renders_every_section(section: str) -> None:
    """A section that stopped rendering would silently lose its half of REQ-068.

    Args:
        section: The section that must be present.
    """
    assert section in _context().as_document()
