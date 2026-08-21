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

import struct
from pathlib import Path
from typing import TYPE_CHECKING

import probe_groundstation_container
import pytest
from probe_groundstation_container import (
    ProbeError,
    cuda_provider_problem,
    main,
    needed_libraries,
    reachable_sources,
    toolchain_found,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pyfakefs.fake_filesystem import FakeFilesystem

# The driver the container runtime injects, which a provider is allowed to be
# missing and every other dependency is not.
_DRIVER = "libcuda.so.1"


def _elf_with_needed(names: Sequence[str]) -> bytes:
    """Assemble the smallest 64-bit little-endian ELF that declares dependencies.

    Real provider libraries are hundreds of megabytes and are not committable,
    and `needed_libraries` reads the ELF by hand — so the cases worth pinning
    are assembled here rather than mocked away. One loadable segment covers the
    whole file at virtual address zero, which makes every virtual address its
    own file offset.

    Args:
        names: The `DT_NEEDED` entries to declare, in order.

    Returns:
        The bytes of the object.
    """
    header_size, program_header_size = 64, 56
    dynamic_at = header_size + 2 * program_header_size
    # One entry per dependency, one for the string table, one terminator.
    dynamic_size = (len(names) + 2) * 16
    strings_at = dynamic_at + dynamic_size

    table = b"\x00"
    offsets: list[int] = []
    for name in names:
        offsets.append(len(table))
        table += name.encode("utf-8") + b"\x00"

    total = strings_at + len(table)

    identity = b"\x7fELF\x02\x01\x01\x00" + bytes(8)
    elf_header = identity + struct.pack(
        "<HHIQQQIHHHHHH",
        3,  # e_type: a shared object
        0x3E,  # e_machine, which nothing here reads
        1,  # e_version
        0,  # e_entry
        header_size,  # e_phoff
        0,  # e_shoff: no section headers, which are not needed to load
        0,  # e_flags
        header_size,  # e_ehsize
        program_header_size,  # e_phentsize
        2,  # e_phnum
        0,  # e_shentsize
        0,  # e_shnum
        0,  # e_shstrndx
    )

    def _program_header(kind: int, offset: int, size: int) -> bytes:
        """Describe one segment.

        Args:
            kind: `PT_LOAD` or `PT_DYNAMIC`.
            offset: Where it starts in the file.
            size: How long it is.

        Returns:
            The 56 bytes of the entry.
        """
        return struct.pack(
            "<IIQQQQQQ",
            kind,
            4,  # p_flags: readable
            offset,
            offset,  # p_vaddr, equal to the offset so the mapping is the identity
            offset,  # p_paddr
            size,
            size,
            1,  # p_align
        )

    dynamic = (
        b"".join(struct.pack("<qQ", 1, offset) for offset in offsets)
        + struct.pack("<qQ", 5, strings_at)
        + struct.pack("<qQ", 0, 0)
    )

    return (
        elf_header
        + _program_header(1, 0, total)
        + _program_header(2, dynamic_at, dynamic_size)
        + dynamic
        + table
    )


def _provider(fs: FakeFilesystem, *names: str) -> Path:
    """Put a provider library declaring these dependencies on the filesystem.

    Args:
        fs: The in-memory filesystem.
        names: The `DT_NEEDED` entries it declares.

    Returns:
        Where it was written.
    """
    library = Path("/opt/reachy/capi/libonnxruntime_providers_cuda.so")
    fs.create_file(library, contents=_elf_with_needed(names))
    return library


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


def test_the_declared_dependencies_are_read_back_in_order(
    fs: FakeFilesystem,
) -> None:
    """The reader is what the whole CUDA check rests on, so it is read back.

    Against an object assembled here rather than a real one. A real provider
    library is hundreds of megabytes and belongs to somebody else's wheel, so
    reading one would make this suite depend on a third-party binary's
    internals — and there is already a place where the reader meets a real
    object: `just image-verify` runs this probe inside the built image, against
    the provider library that image actually ships.
    """
    library = _provider(fs, "libcublasLt.so.13", _DRIVER, "libc.so.6")
    assert needed_libraries(library) == (
        "libcublasLt.so.13",
        _DRIVER,
        "libc.so.6",
    )


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"not an elf at all", "not a 64-bit little-endian ELF"),
        (b"\x7fELF\x01\x01\x01\x00" + bytes(56), "not a 64-bit little-endian ELF"),
    ],
)
def test_something_that_is_not_a_shared_object_is_reported_as_such(
    fs: FakeFilesystem,
    content: bytes,
    expected: str,
) -> None:
    """A provider that is not an object at all is its own answer."""
    library = Path("/opt/reachy/capi/libonnxruntime_providers_cuda.so")
    fs.create_file(library, contents=content)
    with pytest.raises(ProbeError, match=expected):
        needed_libraries(library)
    problem = cuda_provider_problem(library, _loads)
    assert problem is not None
    assert expected in problem


def test_a_cuda_provider_that_only_wants_the_driver_is_not_a_problem(
    fs: FakeFilesystem,
) -> None:
    """Every runner without a GPU lacks `libcuda.so.1`; that is not a defect."""
    library = _provider(fs, _DRIVER, "libc.so.6")

    def _everything_but_the_driver(path: str) -> object:
        if _DRIVER in path:
            message = f"{_DRIVER}: cannot open shared object file"
            raise OSError(message)
        return None

    assert cuda_provider_problem(library, _everything_but_the_driver) is None


def test_a_cuda_library_missing_behind_the_driver_is_still_found(
    fs: FakeFilesystem,
) -> None:
    """The finding this check exists for, and the one loading it would miss.

    `ctypes.CDLL` stops at the first dependency it cannot resolve, so a provider
    that declares the driver before a CUDA runtime of the wrong major version
    would report only the driver — and a broken image would pass on every runner
    without a GPU, which is every runner. The declarations are enumerated
    instead, so the order does not matter.
    """
    library = _provider(fs, _DRIVER, "libcublasLt.so.13", "libc.so.6")

    def _no_cuda_at_all(path: str) -> object:
        if "libc.so.6" in path:
            return None
        message = f"{Path(path).name}: cannot open shared object file"
        raise OSError(message)

    problem = cuda_provider_problem(library, _no_cuda_at_all)
    assert problem is not None
    assert "libcublasLt.so.13" in problem
    assert _DRIVER not in problem


def test_a_dependency_beside_the_provider_counts_as_resolved(
    fs: FakeFilesystem,
) -> None:
    """A sibling reached through the provider's own run path is not missing."""
    library = _provider(fs, "libonnxruntime_providers_shared.so")
    sibling = library.parent / "libonnxruntime_providers_shared.so"

    def _only_by_full_path(path: str) -> object:
        if path != str(sibling):
            message = f"{path}: cannot open shared object file"
            raise OSError(message)
        return None

    assert cuda_provider_problem(library, _only_by_full_path) is None


def test_a_cuda_provider_that_is_not_in_the_image_is_a_problem(
    fs: FakeFilesystem,
) -> None:
    """The default variant is not an accelerated one that lost its provider."""
    del fs
    problem = cuda_provider_problem(Path("/opt/reachy/absent.so"), _loads)
    assert problem is not None
    assert "not an accelerated build" in problem


def test_a_loadable_cuda_provider_is_no_problem(fs: FakeFilesystem) -> None:
    """On a host with a GPU and the right base, everything simply opens."""
    library = _provider(fs, _DRIVER, "libcublasLt.so.13", "libc.so.6")
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
