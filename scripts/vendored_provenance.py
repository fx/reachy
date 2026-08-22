#!/usr/bin/env python3
"""Collect the upstream provenance recorded in the vendored files' headers.

Every vendored file carries `upstream-url`, `upstream-path` and `upstream-commit`
in a comment header, which makes the headers the single source of truth about
where the code came from — a second manifest listing the same thing would only
be a thing to forget to update. This reads them back out for the drift workflow,
and fails if they disagree with each other, because provenance that says two
different things records nothing.

Prints three `key=value` lines on standard output: `upstream-url`,
`upstream-commit`, and `upstream-paths` — the paths **in the upstream
repository**, which is what a comparison against upstream needs. The local file
each one was derived from is not in the output; that direction is recorded in
the local file's own header and in its directory's `NOTICE`.

`just vendored-drift` is what reads these — it parses the three values out and
drives the comparison — and the scheduled workflow appends that recipe's output
to the job summary. Nothing here is a step output, so run it by hand and you see
exactly what the recipe sees.

Two rules govern everything below, because a provenance tool that reports success
over something it did not actually check is worse than no tool at all:

* **Nothing is dropped quietly.** A malformed, repeated, empty or unreadable
  header, a root that matches no files, an exemption for a file that is no longer
  there — each of them fails rather than being skipped. Attribution that was
  never checked must not be certified.
* **Every failure names the thing.** The file, the key, the path, the value. The
  reader should be able to act on the message without re-deriving what this
  script already knew.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

#: Every directory that holds vendored files. The header format is the contract,
#: not the location, but a directory has to be listed here to be watched at all.
VENDORED_ROOTS = (
    "apps/ha-satellite/src/reachy_mini_ha_satellite/esphome",
    "apps/ha-satellite/tests",
)

#: Files inside those directories that carry no provenance header, and are not
#: expected to: code and fixtures original to this repository, and the attribution
#: material itself. Every other file there must record where it came from, and one
#: that does not fails the check rather than being skipped — a dropped header
#: would otherwise take a derived file out of drift reporting silently, which is
#: the one failure this script exists to make impossible.
#:
#: Two converses are checked as well, because an exemption is a claim and a stale
#: claim is how this list rots: a file listed here that grows an upstream header
#: fails, since it is one or the other, and an entry naming a file that is no
#: longer on disk fails, since it would otherwise sit here exempting nothing while
#: a renamed file slipped past under its new name.
EXEMPT_FILES = frozenset(
    {
        # Attribution material, which records the provenance rather than carrying
        # it.
        "apps/ha-satellite/src/reachy_mini_ha_satellite/esphome/LICENSE",
        "apps/ha-satellite/src/reachy_mini_ha_satellite/esphome/NOTICE",
        "apps/ha-satellite/tests/LICENSE",
        "apps/ha-satellite/tests/NOTICE",
        # Original to this repository. `apps/ha-satellite/tests` is a vendored
        # root because the carried-over upstream tests live directly in it, so
        # every test this repository writes for the same member lands there too
        # and has to say so here. The vendored ones are exactly the
        # `*_esphome_*` files plus `esphome_test_support.py`; everything below
        # is ours.
        "apps/ha-satellite/src/reachy_mini_ha_satellite/esphome/__init__.py",
        "apps/ha-satellite/src/reachy_mini_ha_satellite/esphome/seams.py",
        "apps/ha-satellite/tests/conftest.py",
        "apps/ha-satellite/tests/fixtures/behaviour_boundary_probe.py.txt",
        "apps/ha-satellite/tests/fixtures/vendored_boundary_probe.py.txt",
        "apps/ha-satellite/tests/support/satellite_support.py",
        "apps/ha-satellite/tests/test_satellite_asset_registry.py",
        "apps/ha-satellite/tests/test_satellite_asset_verify.py",
        "apps/ha-satellite/tests/test_satellite_audio_adapter.py",
        "apps/ha-satellite/tests/test_satellite_audio_entities.py",
        "apps/ha-satellite/tests/test_satellite_audio_seams.py",
        "apps/ha-satellite/tests/test_satellite_behaviour_movement.py",
        "apps/ha-satellite/tests/test_satellite_behaviour_pipeline.py",
        "apps/ha-satellite/tests/test_satellite_behaviour_satellite.py",
        "apps/ha-satellite/tests/test_satellite_behaviour_tracking.py",
        "apps/ha-satellite/tests/test_satellite_config.py",
        "apps/ha-satellite/tests/test_satellite_daemon_app.py",
        "apps/ha-satellite/tests/test_satellite_daemon_volume.py",
        "apps/ha-satellite/tests/test_satellite_decode.py",
        "apps/ha-satellite/tests/test_satellite_main.py",
        "apps/ha-satellite/tests/test_satellite_motion_adapter.py",
        "apps/ha-satellite/tests/test_satellite_network.py",
        "apps/ha-satellite/tests/test_satellite_perception_contract.py",
        "apps/ha-satellite/tests/test_satellite_perception_local.py",
        "apps/ha-satellite/tests/test_satellite_perception_remote.py",
        "apps/ha-satellite/tests/test_satellite_perception_source.py",
        "apps/ha-satellite/tests/test_satellite_pipeline_events.py",
        "apps/ha-satellite/tests/test_satellite_output_gain.py",
        "apps/ha-satellite/tests/test_satellite_ports.py",
        "apps/ha-satellite/tests/test_satellite_sounds.py",
        "apps/ha-satellite/tests/test_satellite_vendored_wiring.py",
        "apps/ha-satellite/tests/test_satellite_wake_word.py",
        "apps/ha-satellite/tests/test_satellite_web_settings.py",
    }
)

#: A header line: the key, then everything after the colon. The value is captured
#: loosely on purpose — an empty or whitespace-bearing value is rejected below
#: with a message that says so, where a stricter pattern would simply fail to
#: match and be reported as a missing key, which is a different and misleading
#: complaint.
_KEY = re.compile(r"^#\s+(upstream-(?:url|path|commit)):(.*)$")

_REQUIRED_KEYS = frozenset({"upstream-url", "upstream-path", "upstream-commit"})

#: Only the first few lines of a file are a header. Reading further would pick up
#: an unrelated mention of the word in a docstring.
_HEADER_LINES = 20


@dataclass(frozen=True)
class Record:
    """One vendored file and the provenance its header records."""

    local_path: str
    """Path in this repository, relative to the repository root."""

    url: str
    """The upstream repository the file was derived from."""

    commit: str
    """The upstream commit it was taken at."""

    upstream_path: str
    """Its path in that upstream repository."""


def _header(path: Path, local_path: str) -> dict[str, str] | None:
    """Return the provenance keys in a file's header, or None if it is not text.

    Raises when the header is present but self-contradictory: a key given twice,
    or a value that is empty or carries whitespace. A repeated key is the sharpest
    of those — silently keeping the last one would let a file record two different
    commits and still pass the very check that exists to catch exactly that.
    """
    found: dict[str, str] = {}
    try:
        with path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                if number > _HEADER_LINES:
                    break
                match = _KEY.match(line)
                if not match:
                    continue
                key, raw = match.group(1), match.group(2)
                if key in found:
                    raise SystemExit(
                        f"{local_path}:{number}: {key} appears more than once in "
                        f"its header, recording {found[key]!r} and then "
                        f"{raw.strip()!r}. Provenance that says two different "
                        f"things records nothing — leave one line."
                    )
                value = raw.strip()
                if not value:
                    raise SystemExit(
                        f"{local_path}:{number}: {key} is present but empty. A "
                        f"key with no value is not provenance; give it one or "
                        f"remove the line."
                    )
                if len(value.split()) > 1:
                    raise SystemExit(
                        f"{local_path}:{number}: {key} is {value!r}, which "
                        f"contains whitespace. The values travel to "
                        f"`just vendored-drift` as a whitespace-separated list, "
                        f"so one would silently split into two."
                    )
                found[key] = value
    except UnicodeDecodeError:
        # Not text, so it cannot carry a header at all. The caller decides what
        # that means: fine for an exempt file, a failure for anything else.
        return None
    return found


def _first_examples(by_value: dict[str, list[str]]) -> str:
    """Render `value (first local file naming it)` pairs, for an error message."""
    return "; ".join(
        f"{value} (in {sorted(files)[0]})" for value, files in sorted(by_value.items())
    )


def _claimants(records: list[Record], upstream_path: str) -> list[str]:
    """The local files whose headers name one upstream path."""
    return sorted(r.local_path for r in records if r.upstream_path == upstream_path)


def _group(records: list[Record], attribute: str) -> dict[str, list[str]]:
    return {
        value: [r.local_path for r in records if getattr(r, attribute) == value]
        for value in {getattr(r, attribute) for r in records}
    }


def _records_under(root: str) -> list[Record]:
    """Every vendored file under one root, with the provenance it records."""
    root_path = REPOSITORY_ROOT / root
    if not root_path.is_dir():
        # `rglob` on a missing directory yields nothing, so a renamed root would
        # take every file under it out of drift reporting while the remaining
        # root kept the run green.
        raise SystemExit(
            f"{root}: listed as a vendored root but not a directory. Either the "
            f"vendored code moved and VENDORED_ROOTS needs updating, or it is "
            f"gone and its provenance went with it."
        )

    records: list[Record] = []
    seen_any = False

    for path in sorted(root_path.rglob("*")):
        if path.is_dir() or "__pycache__" in path.parts:
            continue
        local_path = path.relative_to(REPOSITORY_ROOT).as_posix()
        if not path.is_file():
            raise SystemExit(
                f"{local_path}: under a vendored root but not a readable file — "
                f"a broken symbolic link, or a device. Nothing can record where "
                f"it came from, so it cannot be here."
            )
        seen_any = True
        header = _header(path, local_path)
        if local_path in EXEMPT_FILES:
            if header:
                raise SystemExit(
                    f"{local_path}: exempt from carrying provenance, yet it "
                    f"carries an upstream header. It is one or the other — drop "
                    f"the header, or drop the entry from EXEMPT_FILES."
                )
            continue
        if header is None:
            raise SystemExit(
                f"{local_path}: not UTF-8 text, so it cannot carry a provenance "
                f"header, and it is not in EXEMPT_FILES. A binary under a "
                f"vendored directory has to be exempted deliberately."
            )
        missing = _REQUIRED_KEYS - header.keys()
        if missing:
            raise SystemExit(
                f"{local_path}: no {', '.join(sorted(missing))} in the first "
                f"{_HEADER_LINES} lines. A file under a vendored directory "
                f"either records where it came from or is listed in "
                f"EXEMPT_FILES; a file that does neither would drop out of "
                f"drift reporting unnoticed."
            )
        records.append(
            Record(
                local_path=local_path,
                url=header["upstream-url"],
                commit=header["upstream-commit"],
                upstream_path=header["upstream-path"],
            )
        )

    if not seen_any:
        raise SystemExit(
            f"{root}: a vendored root holding no files at all. Either the "
            f"vendored code moved and VENDORED_ROOTS needs updating, or this "
            f"root would report a clean comparison over nothing."
        )
    return records


def collect() -> tuple[str, str, list[str]]:
    """Return the upstream URL, the recorded commit, and the upstream paths.

    The third element is the set of paths **in the upstream repository** that
    the headers name — `linux_voice_assistant/satellite.py`, not the path of the
    file in this repository that was derived from it. That is what the drift
    comparison needs, because it diffs upstream against itself; the local side
    of each pair lives in the header of the local file and in the directory's
    `NOTICE`.
    """
    records: list[Record] = []
    for root in VENDORED_ROOTS:
        records.extend(_records_under(root))

    if not records:
        raise SystemExit(
            f"no file under {', '.join(VENDORED_ROOTS)} carries a provenance "
            f"header; either the vendored code moved or its provenance was lost"
        )

    found_paths = {
        path.relative_to(REPOSITORY_ROOT).as_posix()
        for root in VENDORED_ROOTS
        for path in (REPOSITORY_ROOT / root).rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    stale = EXEMPT_FILES - found_paths
    if stale:
        raise SystemExit(
            f"EXEMPT_FILES names {len(stale)} file(s) that are not there: "
            f"{', '.join(sorted(stale))}. A stale exemption covers nothing, and "
            f"hides the renamed file it used to cover."
        )

    by_url = _group(records, "url")
    if len(by_url) != 1:
        raise SystemExit(
            f"vendored files name more than one upstream: {_first_examples(by_url)}"
        )

    by_commit = _group(records, "commit")
    if len(by_commit) != 1:
        raise SystemExit(
            f"vendored files name more than one upstream commit: "
            f"{_first_examples(by_commit)}. Re-vendoring updates every header, "
            f"not some of them."
        )

    upstream_paths = [record.upstream_path for record in records]
    repeated = {path for path, count in Counter(upstream_paths).items() if count > 1}
    if repeated:
        collisions = "; ".join(
            f"{path} claimed by {', '.join(_claimants(records, path))}"
            for path in sorted(repeated)
        )
        raise SystemExit(
            f"two vendored files claim the same upstream path: {collisions}"
        )

    return records[0].url, records[0].commit, upstream_paths


def main() -> int:
    """Print the provenance as `key=value` lines on standard output."""
    url, commit, upstream_paths = collect()
    print(f"upstream-url={url}")
    print(f"upstream-commit={commit}")
    print(f"upstream-paths={' '.join(upstream_paths)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
