"""Regeneration of every published contract artifact under `docs/contracts/`.

The drift gate this feeds is simple: regenerate, then fail if the working tree
changed. That only works if regeneration is a real operation with a real output,
so this module is the registry the generators plug into rather than a
placeholder the gate skips over.

`CONTRACTS` holds one JSON Schema per robot link message type, derived from the
same declarations that validate those messages at run time. One declaration
producing both is the point: a schema written by hand beside a type is a second
statement of the contract, free to disagree with the first, and the drift gate
would have nothing to compare that disagreement against.

It is not the only registry. `reachy_checks.checks_export` holds the `doctor`
check reference, because this package declares exactly one dependency and
`reachy-checks` depends on *it* — importing the checks package from here would
close that loop. `scripts/export_contracts.py` hands both registries to `export`
in one call, which is what makes the index below list every artifact rather than
whichever half ran last.

Registering a further generator is still the whole of the work. Append a
`Contract` — to this registry, or to whichever one owns its source — with the
path it writes under `docs/contracts/` and a callable that renders its content,
and the gate covers it with no further change here or in the workflow.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from reachy_contracts.session import (
    FrameHeader,
    ResultEnvelope,
    SessionAgreement,
    SessionClose,
    SessionError,
    SessionOffer,
)
from reachy_contracts.values import (
    FACE_CAPABILITY,
    GESTURE_CAPABILITY,
    CapabilityName,
    FaceDetections,
    GestureDetections,
    WireModel,
)

__all__ = ["CONTRACTS", "INDEX_PATH", "Contract", "export", "render_all"]

INDEX_PATH: Final = "index.md"


@dataclass(frozen=True, slots=True)
class Contract:
    """One generated artifact and the source that produces it.

    Attributes:
        path: Where the artifact is written, relative to the output directory.
        summary: One line describing what the artifact pins.
        render: Produces the artifact's full content.
    """

    path: str
    summary: str
    render: Callable[[], str]


# Where the robot link schemas are written, relative to `docs/contracts/`.
_SCHEMA_DIRECTORY: Final = "robot-link"

# Declared so a consumer reading a published schema knows which dialect's rules
# apply to it. pydantic emits 2020-12 shapes but does not stamp the document.
_DIALECT: Final = "https://json-schema.org/draft/2020-12/schema"


def _schema_of(
    model: type[WireModel],
    capability: CapabilityName | None = None,
) -> Callable[[], str]:
    """Build a renderer for one message type's JSON Schema.

    The validation schema rather than the serialisation one. The two agree for
    every message type here except the session offer, where the serialisation
    schema drops the credential's minimum length and the fact that it is a
    secret — so publishing it would describe a contract laxer than the one this
    package enforces, and a second implementation written against it could emit
    a message this package refuses.

    A result envelope is published once per capability, and each published copy
    pins its `capability` field to that name. Without it the two result schemas
    are identical documents that both admit either name, so a producer written
    against `face-result.schema.json` could emit a gesture payload under the
    face name — a message `ResultEnvelope` rejects at run time, which is exactly
    the disagreement between the code and its published contract that generating
    one from the other is supposed to remove.

    Args:
        model: The message type to describe.
        capability: The capability this parameterisation answers for, when the
            message type is a result envelope.

    Returns:
        A callable rendering that type's schema, keys sorted so the output
        depends on the declaration and not on dictionary ordering.
    """

    def render() -> str:
        schema = model.model_json_schema(mode="validation")
        if capability is not None:
            schema["properties"]["capability"]["const"] = capability
        document = {"$schema": _DIALECT, **schema}
        text = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False)
        return f"{text}\n"

    return render


@dataclass(frozen=True, slots=True)
class _Published:
    """One message type and how it is published.

    Attributes:
        slug: The schema's file name, without its extension.
        model: The message type to describe.
        summary: One line for the generated index.
        capability: The capability a result envelope answers for, or `None` for
            a message type that is not a result envelope.
    """

    slug: str
    model: type[WireModel]
    summary: str
    capability: CapabilityName | None = None


# One schema per message type. `face-result` and `gesture-result` are the same
# envelope carrying different payloads, which is what makes a new capability a
# new row here rather than a change to `ResultEnvelope`.
_MESSAGE_TYPES: Final[tuple[_Published, ...]] = (
    _Published(
        "session-offer",
        SessionOffer,
        "the client's credential and the capabilities it can speak",
    ),
    _Published(
        "session-agreement",
        SessionAgreement,
        "the capabilities both sides settled on",
    ),
    _Published(
        "frame-header",
        FrameHeader,
        "a frame's sequence number and its opaque capture token",
    ),
    _Published(
        "face-result",
        ResultEnvelope[FaceDetections],
        "face detections answering one frame",
        FACE_CAPABILITY,
    ),
    _Published(
        "gesture-result",
        ResultEnvelope[GestureDetections],
        "gesture detections answering one frame",
        GESTURE_CAPABILITY,
    ),
    _Published(
        "session-error",
        SessionError,
        "a failure report, optionally naming the frame it concerns",
    ),
    _Published(
        "session-close",
        SessionClose,
        "the last message on a session and why it ended",
    ),
)

CONTRACTS: Final[tuple[Contract, ...]] = tuple(
    Contract(
        path=f"{_SCHEMA_DIRECTORY}/{published.slug}.schema.json",
        summary=published.summary,
        render=_schema_of(published.model, published.capability),
    )
    for published in _MESSAGE_TYPES
)

_PREAMBLE: Final = """\
# Generated contracts

Every file in this directory is generated by `just contracts` from the
declarations that produce the behaviour it describes — the wire types in
`packages/reachy-contracts`, and the check registry in
`packages/reachy-checks`. Do not edit one by hand: the contract-drift gate
regenerates them and fails on any difference, so a manual edit is reverted by
the next run at best and blocks a merge at worst.
"""


_EMPTY_REGISTRY: Final = """\
No contract artifacts are registered. The registry in
`reachy_contracts.contracts_export` is empty, so this index is the only file the
drift gate has to compare against.
"""


def _render_index(contracts: Sequence[Contract]) -> str:
    """Render the index that lists every generated artifact.

    Args:
        contracts: The registered contracts.

    Returns:
        The full content of the index file.
    """
    if not contracts:
        return f"{_PREAMBLE}\n{_EMPTY_REGISTRY}"
    rows = "\n".join(
        f"| [`{contract.path}`]({contract.path}) | {contract.summary} |"
        for contract in sorted(contracts, key=lambda contract: contract.path)
    )
    return f"{_PREAMBLE}\n| Artifact | What it pins |\n|---|---|\n{rows}\n"


def render_all(contracts: Sequence[Contract] = CONTRACTS) -> dict[str, str]:
    """Render every contract artifact and the index that lists them.

    Args:
        contracts: The registered contracts.

    Returns:
        A mapping of output path, relative to the output directory, to content.

    Raises:
        ValueError: If two contracts claim the same output path, which would
            make the generated tree depend on registration order.
    """
    rendered = {INDEX_PATH: _render_index(contracts)}
    for contract in contracts:
        if contract.path in rendered:
            message = f"two contracts claim the same path: {contract.path}"
            raise ValueError(message)
        rendered[contract.path] = contract.render()
    return rendered


def export(
    out_dir: Path,
    contracts: Sequence[Contract] = CONTRACTS,
) -> list[Path]:  # pragma: no cover - writes to the filesystem
    """Write every rendered artifact under an output directory, and only those.

    **Anything already under `out_dir` that this run did not write is deleted**,
    along with any directory left empty by that. Writing without pruning would
    let a contract that was removed or renamed keep its committed artifact
    forever: the drift gate compares the tree against what regeneration
    produced, so it would go on passing over a published interface nothing
    generates any more — which is precisely the drift REQ-008 exists to catch,
    arriving by the one route a write-only generator cannot see.

    That makes the directory this writes into fully owned. It is
    `docs/contracts/`, whose generated index says in so many words that every
    file in it is generated; pointing this at a directory holding anything else
    would delete it.

    Args:
        out_dir: The directory to write into, created if it does not exist.
        contracts: The registered contracts.

    Returns:
        The paths written, in sorted order.
    """
    rendered = render_all(contracts)
    written: list[Path] = []
    for relative, content in sorted(rendered.items()):
        destination = out_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        written.append(destination)
    _prune(out_dir, set(written))
    return written


def _prune(
    out_dir: Path,
    written: set[Path],
) -> None:  # pragma: no cover - reads and writes the filesystem
    """Delete everything under a directory that this run did not write.

    Deepest entries first, so a directory is considered only after its contents
    and is removed exactly when it is left empty — a directory still holding a
    generated artifact survives, and one whose last artifact was withdrawn does
    not.

    **A symlink is handled before either of those branches, and that ordering is
    the whole of why it is here.** `is_file` and `is_dir` follow a link, so a
    broken one is neither and would be left behind — a stale artifact surviving
    the very sweep that exists to remove it. A link to a directory is worse than
    that: it answers `is_dir` truthfully about its target, and `rmdir` on the
    link itself then raises `NotADirectoryError` and fails the run. Neither
    shape has any business being in a generated directory, which is exactly why
    the sweep has to remove one rather than trip over it.

    Enumeration happens up front, so removing a directory or a link can leave a
    later entry pointing at nothing. Each is checked for existence without
    following links before it is touched.

    Args:
        out_dir: The directory to prune.
        written: The paths this run wrote.
    """
    if not out_dir.exists():
        return
    for path in sorted(
        out_dir.rglob("*"), key=lambda path: len(path.parts), reverse=True
    ):
        if path.is_symlink():
            path.unlink()
        elif not path.exists():
            continue
        elif path.is_file():
            if path not in written:
                path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()


if __name__ == "__main__":  # pragma: no cover - module entry point
    export(Path(sys.argv[1] if len(sys.argv) > 1 else "docs/contracts"))
