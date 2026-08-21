r"""Check, from inside a running container, what the groundstation image must be.

Three properties, and none of them can be checked from outside.

**Nothing the model source needs is reachable.** Groundstation REQ-023 says the
service loads every model from a file already in its artifact and never fetches
weights at run time, and the only way to test that guarantee is to deny the
network and watch the service come up anyway. This resolves every source out of
the model registry — so a model added later is covered without editing anything
here — and fails if any of them can be connected to.

**There is no toolchain and no package manager.** A runtime stage that grew a
compiler or a `pip` would still build, still start and still pass every other
check, so the absence is asserted rather than described. It looks for the files
rather than running them: a shell that does not understand `--version` exits
non-zero and would otherwise read as absent.

**The accelerated variant can actually load its CUDA provider.** ONNX Runtime
drops an execution provider it cannot load and carries on with the CPU one,
saying so in a log line nobody reads, so a CUDA image built on a base with the
wrong CUDA major version starts, serves, and is not accelerated. Loading the
provider library directly turns that into a failure. The one library it is
allowed to be missing is the driver, `libcuda.so.1`, which the container runtime
injects from the host and which is therefore absent wherever there is no GPU —
including every runner this is likely to run on.

It is mounted into the container by `just image-verify` and run there; it is not
part of the image. Run it by hand the same way:

    docker exec <container> /opt/reachy/venv/bin/python \\
        /verify/scripts/probe_groundstation_container.py --unreachable-sources
"""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import os
import socket
import struct
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

from reachy_groundstation.models.registry import MODELS

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

__all__ = [
    "ProbeError",
    "cuda_provider_library",
    "cuda_provider_problem",
    "main",
    "needed_libraries",
    "reachable_sources",
    "toolchain_found",
]

# How long to wait for a connection that must not succeed. Long enough that a
# slow route is not mistaken for no route, short enough that a probe of a
# firewalled host does not hold up a build.
_CONNECT_TIMEOUT_SECONDS: Final = 5.0

# What must not be in the image. Absolute paths for the ones whose location is
# fixed by the base image or by this build, plus bare names that are looked for
# on `PATH`, so a toolchain installed somewhere unexpected is still found.
_FORBIDDEN_PATHS: Final[tuple[str, ...]] = (
    "/bin/sh",
    "/bin/bash",
    "/usr/bin/apt",
    "/usr/bin/apt-get",
    "/usr/bin/dpkg",
    "/usr/bin/rpm",
    "/opt/reachy/venv/bin/pip",
    "/opt/reachy/venv/bin/uv",
)
# What `onnxruntime-gpu` calls the CUDA execution provider's library. Where it
# is is found from the installed package rather than written down, so this holds
# whatever path the environment ended up at.
_CUDA_PROVIDER_NAME: Final = "libonnxruntime_providers_cuda.so"

# The NVIDIA driver library. It is not in any image: the container runtime mounts
# it in from the host when a GPU is requested, so a build machine without one
# cannot load it and its absence is not a packaging fault.
_DRIVER_LIBRARY: Final = "libcuda.so.1"

# The parts of the ELF format `needed_libraries` reads: a loadable segment, the
# dynamic section, and the three dynamic tags that name a dependency, end the
# table and locate the strings the names live in. Each entry in that table is a
# tag and a value, eight bytes each.
_PT_LOAD: Final = 1
_PT_DYNAMIC: Final = 2
_DT_NULL: Final = 0
_DT_NEEDED: Final = 1
_DT_STRTAB: Final = 5
_DYNAMIC_ENTRY_BYTES: Final = 16

_FORBIDDEN_NAMES: Final[tuple[str, ...]] = (
    "apt",
    "apt-get",
    "cc",
    "dpkg",
    "gcc",
    "ld",
    "make",
    "pip",
    "pip3",
    "sh",
    "uv",
)


class ProbeError(RuntimeError):
    """The container is not what a deployed groundstation image must be."""


def reachable_sources(timeout: float = _CONNECT_TIMEOUT_SECONDS) -> tuple[str, ...]:
    """Try to reach every model source, and report the ones that answered.

    Args:
        timeout: How long to wait for each connection.

    Returns:
        `host:port` for every model source that could be connected to, and
        empty when none of them could — which is the only acceptable answer on
        a host with no outbound internet access.
    """
    reachable: list[str] = []
    for model in MODELS:
        parts = urlsplit(model.source)
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if host is None:  # pragma: no cover - every registered source has a host
            continue
        try:
            socket.create_connection((host, port), timeout=timeout).close()
        except OSError as error:
            sys.stdout.write(
                f"container-probe: {host}:{port} is unreachable, "
                f"as it must be: {error}\n",
            )
        else:
            reachable.append(f"{host}:{port}")
    return tuple(reachable)


def toolchain_found(
    paths: Sequence[str] = _FORBIDDEN_PATHS,
    names: Sequence[str] = _FORBIDDEN_NAMES,
    search_path: str | None = None,
) -> tuple[str, ...]:
    """Look for a compiler, a package manager or a shell.

    Args:
        paths: Absolute paths that must not exist.
        names: Executable names that must not be found on the search path.
        search_path: The directories to search, colon separated, or `None` to
            read `PATH` from the environment.

    Returns:
        Every path that was found, and empty when the image carries none of
        them.
    """
    found: list[str] = []
    found.extend(path for path in paths if Path(path).exists())

    directories = os.environ.get("PATH", "") if search_path is None else search_path
    for directory in directories.split(os.pathsep):
        if not directory:
            continue
        for name in names:
            candidate = Path(directory) / name
            if candidate.exists() and str(candidate) not in found:
                found.append(str(candidate))
    return tuple(found)


def cuda_provider_library() -> Path | None:
    """Locate the CUDA execution provider inside the installed model runtime.

    `find_spec` rather than an import: `onnxruntime` is tens of megabytes of
    shared library and this only needs to know where it lives.

    Returns:
        The path the provider library would be at, or `None` when the model
        runtime is not installed at all.
    """
    spec = importlib.util.find_spec("onnxruntime")
    if spec is None or spec.origin is None:  # pragma: no cover - always installed here
        return None
    return Path(spec.origin).parent / "capi" / _CUDA_PROVIDER_NAME


def needed_libraries(library: Path) -> tuple[str, ...]:
    """Read the shared objects an ELF file declares that it needs.

    This is `DT_NEEDED`, read out of the dynamic section by hand. It exists
    because `ctypes.CDLL` is not enough: the loader stops at the FIRST
    dependency it cannot resolve, so a library that needs both the driver and a
    CUDA runtime of the wrong major version reports whichever of them happens to
    come first, and a broken image passes wherever the driver is reported first.
    Enumerating the declarations and checking each one separately has no such
    ordering.

    Only 64-bit little-endian ELF is understood, which is both architectures
    this repository publishes and every architecture ONNX Runtime ships a CUDA
    build for.

    Args:
        library: The shared object to read.

    Returns:
        The declared dependency names, in declaration order.

    Raises:
        ProbeError: If the file is not a 64-bit little-endian ELF, or declares
            no dynamic section. Either means this is not the library it was
            supposed to be, which is itself the answer worth reporting.
    """
    data = library.read_bytes()
    if data[:4] != b"\x7fELF" or data[4:6] != b"\x02\x01":
        message = f"{library} is not a 64-bit little-endian ELF object"
        raise ProbeError(message)

    (header_offset,) = struct.unpack_from("<Q", data, 0x20)
    entry_size, count = struct.unpack_from("<HH", data, 0x36)

    loads: list[tuple[int, int, int]] = []
    dynamic: tuple[int, int] | None = None
    for index in range(count):
        at = header_offset + index * entry_size
        (kind,) = struct.unpack_from("<I", data, at)
        offset, address = struct.unpack_from("<QQ", data, at + 8)
        (size,) = struct.unpack_from("<Q", data, at + 32)
        if kind == _PT_LOAD:
            loads.append((address, offset, size))
        elif kind == _PT_DYNAMIC:
            dynamic = (offset, size)
    if dynamic is None:
        message = f"{library} declares no dynamic section, so it needs nothing"
        raise ProbeError(message)

    def _file_offset(address: int) -> int:
        """Map a virtual address to where it lives in the file.

        Args:
            address: The virtual address, as the dynamic section records it.

        Returns:
            The offset into the file.

        Raises:
            ProbeError: If no loadable segment covers it.
        """
        for start, offset, size in loads:
            if start <= address < start + size:
                return offset + (address - start)
        message = f"{library}: virtual address {address:#x} is in no loaded segment"
        raise ProbeError(message)

    offsets: list[int] = []
    strings: int | None = None
    section, length = dynamic
    for index in range(length // _DYNAMIC_ENTRY_BYTES):
        tag, value = struct.unpack_from(
            "<qQ",
            data,
            section + index * _DYNAMIC_ENTRY_BYTES,
        )
        if tag == _DT_NULL:
            break
        if tag == _DT_NEEDED:
            offsets.append(value)
        elif tag == _DT_STRTAB:
            strings = value
    if strings is None:
        message = f"{library} declares dependencies but no string table"
        raise ProbeError(message)

    table = _file_offset(strings)
    names: list[str] = []
    for offset in offsets:
        start = table + offset
        names.append(data[start : data.index(b"\x00", start)].decode("utf-8"))
    return tuple(names)


def cuda_provider_problem(
    library: Path | None = None,
    loader: Callable[[str], object] = ctypes.CDLL,
) -> str | None:
    """Say why the CUDA execution provider could not be loaded, if it could not.

    Every dependency the provider declares is resolved separately rather than by
    loading the provider itself, because the loader stops at the first one it
    cannot find — see `needed_libraries`.

    Args:
        library: The provider library the accelerated variant ships, or `None`
            to find it in the installed model runtime.
        loader: What opens a dependency. Injected so a test can exercise both
            answers without a CUDA image to open.

    Returns:
        A description of the problem, or `None` when every dependency but the
        driver resolves. The driver is skipped rather than checked: the
        container runtime injects it from a host with a GPU, so it is absent on
        every machine without one and its absence says nothing about how the
        image was built.
    """
    if library is None:
        library = cuda_provider_library()
    if library is None:
        return "no model runtime is installed, so there is no provider to load"
    if not library.is_file():
        return (
            f"{library} is not in this image, so it is not an accelerated "
            f"build — the CUDA variant installs onnxruntime-gpu, which ships it"
        )

    try:
        declared = needed_libraries(library)
    except ProbeError as error:
        return str(error)

    missing: list[str] = []
    for name in declared:
        if name == _DRIVER_LIBRARY:
            continue
        try:
            loader(name)
        except OSError:
            # A dependency may resolve only through the provider's own run
            # path, which names the directory it sits in. Look there before
            # calling it missing.
            beside = library.parent / name
            try:
                loader(str(beside))
            except OSError:
                missing.append(name)
    if missing:
        return (
            f"{library.name} needs {missing}, which this image has not got. "
            f"The base image and the model runtime disagree about the CUDA "
            f"version; an image like this starts, serves, and silently runs on "
            f"the CPU provider."
        )
    sys.stdout.write(
        f"container-probe: every library {library.name} needs is here, bar "
        f"{_DRIVER_LIBRARY}, which the container runtime injects from a host "
        f"with a GPU\n",
    )
    return None


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    """Read the command line.

    Args:
        argv: The arguments, or `None` to read the real ones.

    Returns:
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(
        prog="probe_groundstation_container.py",
        description=(
            "Assert, from inside a running groundstation container, that it "
            "carries no toolchain and that no model source is reachable."
        ),
    )
    parser.add_argument(
        "--unreachable-sources",
        action="store_true",
        help="fail unless every model source is unreachable from here",
    )
    parser.add_argument(
        "--no-toolchain",
        action="store_true",
        help="fail if a compiler, package manager or shell is present",
    )
    parser.add_argument(
        "--cuda-provider",
        action="store_true",
        help=(
            "fail unless the CUDA execution provider's library loads, ignoring "
            "the driver the container runtime injects on a host with a GPU"
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the requested checks inside the container.

    Args:
        argv: Command-line arguments, or `None` to read the real ones.

    Returns:
        The process exit status: 0 when every requested check passed.
    """
    arguments = _parse_arguments(argv)
    if not (
        arguments.unreachable_sources
        or arguments.no_toolchain
        or arguments.cuda_provider
    ):
        sys.stderr.write("container-probe: asked to check nothing; name a check\n")
        return 2

    try:
        if arguments.unreachable_sources:
            reachable = reachable_sources()
            if reachable:
                message = (
                    f"{list(reachable)} answered, so this container is not on a "
                    f"network without outbound access and the offline start was "
                    f"not tested"
                )
                raise ProbeError(message)
        if arguments.no_toolchain:
            found = toolchain_found()
            if found:
                message = (
                    f"the runtime stage carries {list(found)}; it is meant to "
                    f"hold an interpreter, an environment and the weights"
                )
                raise ProbeError(message)
            sys.stdout.write(
                "container-probe: no compiler, package manager or shell is here\n",
            )
        if arguments.cuda_provider:
            problem = cuda_provider_problem()
            if problem is not None:
                raise ProbeError(problem)
            sys.stdout.write(
                "container-probe: the CUDA execution provider's library is "
                "complete in this image\n",
            )
    except ProbeError as error:
        sys.stderr.write(f"container-probe: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
