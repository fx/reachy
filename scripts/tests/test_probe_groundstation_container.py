"""The container probe's judgement, exercised without a container.

Each of the three checks has a judgement in it that is worth pinning: which
sources count as reachable, what counts as a toolchain, and which missing library
is a packaging fault rather than a machine without a GPU. Every one of them is a
gate — a check nobody has watched fail is a check that does not exist — so each
is driven to both answers here.

Nothing opens a socket and nothing touches the real filesystem: the connector,
the search path and the library loader are all arguments.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import probe_groundstation_container
import pytest
from probe_groundstation_container import (
    cuda_provider_problem,
    main,
    reachable_sources,
    toolchain_found,
)

if TYPE_CHECKING:
    from pyfakefs.fake_filesystem import FakeFilesystem


def _loads(path: str) -> None:
    """Stand in for a loader that opens the library without complaint.

    Args:
        path: What was asked for, which is not opened.
    """
    del path


def _no_problem() -> None:
    """Stand in for a CUDA provider check that found nothing wrong.

    Returns:
        Nothing, which is what "no problem" is.
    """


def test_a_network_with_no_route_to_the_model_source_reports_nothing_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The passing case: the container cannot reach where the weights came from."""

    def _refuse(address: tuple[str, int], timeout: float) -> None:
        del address, timeout
        raise OSError(-3, "Temporary failure in name resolution")

    monkeypatch.setattr(
        "probe_groundstation_container.socket.create_connection",
        _refuse,
    )
    assert reachable_sources() == ()


def test_a_network_that_can_still_reach_the_model_source_names_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A test of offline start that ran online would prove nothing at all."""

    class _Connected:
        def close(self) -> None:
            """Close the connection that should not have opened."""

    def _connect(address: tuple[str, int], timeout: float) -> _Connected:
        del address, timeout
        return _Connected()

    monkeypatch.setattr(
        "probe_groundstation_container.socket.create_connection",
        _connect,
    )
    reachable = reachable_sources()
    assert reachable
    assert all(entry.endswith(":443") for entry in reachable)


def test_an_image_with_no_shell_or_package_manager_reports_nothing_found(
    fs: FakeFilesystem,
) -> None:
    """The passing case, against a filesystem holding none of them."""
    fs.create_file("/opt/reachy/venv/bin/python")
    assert toolchain_found(search_path="/opt/reachy/venv/bin") == ()


def test_a_package_manager_on_the_search_path_is_found(fs: FakeFilesystem) -> None:
    """The runtime stage is an interpreter, an environment and the weights."""
    fs.create_file("/usr/bin/apt-get")
    found = toolchain_found(search_path="/usr/bin")
    assert "/usr/bin/apt-get" in found


def test_a_shell_at_a_fixed_path_is_found_even_when_it_is_not_on_the_path(
    fs: FakeFilesystem,
) -> None:
    """Looking for the file, not running it: a shell need not answer `--version`."""
    fs.create_file("/bin/sh")
    assert toolchain_found(search_path="") == ("/bin/sh",)


def test_a_cuda_provider_that_only_wants_the_driver_is_not_a_problem(
    fs: FakeFilesystem,
) -> None:
    """Every runner without a GPU lacks `libcuda.so.1`; that is not a defect."""
    library = Path("/opt/reachy/providers_cuda.so")
    fs.create_file(library)

    def _no_driver(path: str) -> object:
        del path
        raise OSError("libcuda.so.1: cannot open shared object file")

    assert cuda_provider_problem(library, _no_driver) is None


def test_a_cuda_provider_missing_a_cuda_library_is_a_problem(
    fs: FakeFilesystem,
) -> None:
    """A CUDA base of the wrong major version builds, starts, and is not CUDA."""
    library = Path("/opt/reachy/providers_cuda.so")
    fs.create_file(library)

    def _wrong_cuda(path: str) -> object:
        del path
        raise OSError("libcublasLt.so.13: cannot open shared object file")

    problem = cuda_provider_problem(library, _wrong_cuda)
    assert problem is not None
    assert "libcublasLt.so.13" in problem


def test_a_cuda_provider_that_is_not_in_the_image_is_a_problem(
    fs: FakeFilesystem,
) -> None:
    """The default variant is not an accelerated one that lost its provider."""
    del fs
    problem = cuda_provider_problem(Path("/opt/reachy/absent.so"), _loads)
    assert problem is not None
    assert "not an accelerated build" in problem


def test_a_loadable_cuda_provider_is_no_problem(fs: FakeFilesystem) -> None:
    """On a host with a GPU and the right base, the library simply opens."""
    library = Path("/opt/reachy/providers_cuda.so")
    fs.create_file(library)
    assert cuda_provider_problem(library, _loads) is None


def test_being_asked_to_check_nothing_is_a_usage_error() -> None:
    """A probe that silently checked nothing would pass every image."""
    assert main([]) == 2


def test_a_reachable_model_source_fails_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exit status is what `just image-verify` reads."""
    monkeypatch.setattr(
        probe_groundstation_container,
        "reachable_sources",
        lambda: ("huggingface.co:443",),
    )
    assert main(["--unreachable-sources"]) == 1


def test_a_clean_image_passes_every_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """All three answers together, which is what CI actually runs."""
    monkeypatch.setattr(probe_groundstation_container, "reachable_sources", tuple)
    monkeypatch.setattr(probe_groundstation_container, "toolchain_found", tuple)
    monkeypatch.setattr(
        probe_groundstation_container,
        "cuda_provider_problem",
        _no_problem,
    )
    assert main(["--unreachable-sources", "--no-toolchain", "--cuda-provider"]) == 0
