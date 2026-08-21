"""Putting the registered models in place, at build time, and never after.

This is the build-time half of the model store. It runs while the image is being
built, on a machine that still has a network, and it is the only thing in this
package that reaches one — `store.py`, which the running service uses, cannot.

What makes it a gate rather than a download is the verification.
Groundstation REQ-024 requires that the build fail when a fetched file's hash
does not match the pinned value, so a file whose digest disagrees is deleted and
the run exits non-zero. Nothing partially written is left where a later stage
could find it and mistake it for a verified model: the bytes land beside the
destination under a `.part` name and are renamed only once they have been
checked.

Run it as a module:

    python -m reachy_groundstation.models.fetch <directory>

or through `just models`, which is what continuous integration calls before the
test suite so that the perception integration tests have weights to run against.
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from reachy_groundstation.models.registry import MODELS, Model
from reachy_groundstation.models.store import digest_of

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

__all__ = [
    "Fetcher",
    "ModelFetchError",
    "fetch",
    "fetch_all",
    "main",
]

# What retrieves the bytes at a URL, given how many of them are worth holding.
# Injected so that the verification this module exists for is testable without a
# network: a test supplies a fetcher that returns the wrong bytes and watches the
# build refuse them.
#
# The limit is part of the signature rather than a constant inside the default
# fetcher, because the number that matters is per model and the registry already
# pins it. Without it a mirror serving a body of any size at all is a build that
# exhausts the runner's memory before anything checks a digest.
type Fetcher = Callable[[str, int], bytes]

# Long enough that a slow mirror is not mistaken for a broken one, short enough
# that a build that will never finish says so rather than hanging a runner.
_TIMEOUT_SECONDS = 120


class ModelFetchError(RuntimeError):
    """A model could not be retrieved, or is not the file the registry pins."""


def _https_get(url: str, limit: int) -> bytes:
    """Retrieve a URL over HTTPS, reading no more than it is worth reading.

    Args:
        url: What to fetch. Must be `https`.
        limit: How many bytes to read at most. The caller passes one more than
            the registry pins, so an oversized body is rejected by the size
            check with a readable message rather than held in memory in full.

    Returns:
        The response body, truncated at the limit.

    Raises:
        ModelFetchError: If the URL is not an HTTPS one. Every registered source
            is, and a plain-HTTP source would put the digest check at the mercy
            of whoever is between the runner and the origin.
    """
    scheme = urlsplit(url).scheme
    if scheme != "https":
        message = f"refusing to fetch a model over {scheme!r}: {url}"
        raise ModelFetchError(message)
    # S310 is bandit's "audit URL open for permitted schemes" rule. The scheme
    # is checked immediately above and the URLs come from this package's own
    # registry rather than from input, which is the exposure the rule is about.
    with urllib.request.urlopen(url, timeout=_TIMEOUT_SECONDS) as response:  # noqa: S310  # the scheme is verified https just above and the URL is a pinned constant from models/registry.py, not input
        body: bytes = response.read(limit)
    return body


#:= docs/specs/groundstation/index.md#req-024-model-provenance-is-recorded-and-verified
#:% Every model file MUST be pinned by content hash, and the build MUST fail when a
#:% fetched file's hash does not match the pinned value.
def fetch(
    model: Model,
    directory: Path,
    fetcher: Fetcher = _https_get,
) -> Path:
    """Retrieve one model and refuse it unless it is the pinned file.

    A file already in place and already matching is left alone, so a rebuilt
    layer or a re-run of `just models` costs nothing.

    Args:
        model: The registered model to fetch.
        directory: Where to put it.
        fetcher: What retrieves the bytes. Defaults to an HTTPS request; a test
            passes something that performs no input or output.

    Returns:
        The path to the verified file.

    Raises:
        ModelFetchError: If retrieval failed, if the file is the wrong size, or
            if its digest is not the one the registry pins. In the last two
            cases the partial file is removed rather than left behind.
    """
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / model.filename
    if destination.is_file() and digest_of(destination) == model.sha256:
        return destination

    try:
        # One byte past the pinned size: enough for `_verify` to see that the
        # body is too long and say so, and not enough for an unbounded one to
        # matter.
        payload = fetcher(model.source, model.size_bytes + 1)
    except ModelFetchError:
        raise
    except Exception as error:
        message = f"{model.name}: could not retrieve {model.source}: {error!r}"
        raise ModelFetchError(message) from error

    partial = directory / f"{model.filename}.part"
    partial.write_bytes(payload)
    try:
        _verify(model, partial, len(payload))
    except ModelFetchError:
        partial.unlink(missing_ok=True)
        raise
    partial.replace(destination)
    return destination


def _verify(model: Model, path: Path, size: int) -> None:
    """Check retrieved bytes against everything the registry pins about them.

    Args:
        model: The registered model.
        path: Where the bytes were written.
        size: How many bytes arrived.

    Raises:
        ModelFetchError: If the size or the digest disagrees with the registry.
            The size is checked first so that a truncated download is reported
            as truncated rather than as a substitution.
    """
    if size != model.size_bytes:
        message = (
            f"{model.name}: {model.source} returned {size} bytes, but the "
            f"registry pins {model.size_bytes}"
        )
        raise ModelFetchError(message)
    actual = digest_of(path)
    if actual != model.sha256:
        message = (
            f"{model.name}: {model.source} hashes to {actual}, but the registry "
            f"pins {model.sha256}. Upstream is serving different bytes; do not "
            f"repin without reviewing what changed."
        )
        raise ModelFetchError(message)


def fetch_all(
    directory: Path,
    models: Sequence[Model],
    fetcher: Fetcher,
) -> tuple[Path, ...]:
    """Retrieve every registered model, stopping at the first that fails.

    Neither the registry nor the fetcher is defaulted, and that is deliberate: a
    default argument is bound once when this module is imported, so a caller
    that substituted either — the entry point below, or a test — would be
    substituting something nothing ever reads.

    Args:
        directory: Where to put them.
        models: What to fetch.
        fetcher: What retrieves the bytes.

    Returns:
        The verified paths, in registry order.

    Raises:
        ModelFetchError: On the first model that could not be verified.
    """
    return tuple(fetch(model, directory, fetcher) for model in models)


def main(argv: Sequence[str] | None = None) -> int:
    """Fetch every registered model into a directory.

    Args:
        argv: Command-line arguments, or `None` to read the real ones.

    Returns:
        The process exit status: 0 when every model verified, 1 otherwise.
    """
    parser = argparse.ArgumentParser(
        prog="python -m reachy_groundstation.models.fetch",
        description=(
            "Fetch every registered model and verify it against its pinned "
            "hash. Fails without writing anything when a hash does not match."
        ),
    )
    parser.add_argument(
        "directory",
        type=Path,
        help="where to write the model files",
    )
    arguments = parser.parse_args(argv)

    try:
        paths = fetch_all(arguments.directory, MODELS, _https_get)
    except ModelFetchError as error:
        sys.stderr.write(f"model fetch: {error}\n")
        return 1
    for path in paths:
        sys.stdout.write(f"model fetch: verified {path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
