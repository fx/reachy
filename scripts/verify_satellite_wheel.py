"""Check that the built satellite wheel is the artifact it is supposed to be.

A wheel that builds is not a wheel that works, and the two ways this one can be
wrong are invisible to a successful build.

**The entry point.** ha-satellite REQ-041 says installing the wheel is
sufficient for the daemon to find the application, and what makes that true is
one line of `entry_points.txt` inside the distribution's metadata. A packaging
change that dropped it would produce a wheel that installs perfectly and never
appears in the daemon's list.

**The assets.** The wake-word models and sounds ship as package data, and the
licence texts that have to travel with them ship beside them. `just check-assets`
verifies the tree; this verifies the *wheel*, which is what actually reaches
somebody else's robot — and it checks the digests, so a file substituted between
the registry and the build is visible.

**What is read from where, because the distinction is the whole point.** The
*artifact* is read out of the wheel and only out of the wheel: every path, every
digest and the entry-point metadata come from the zip file. The *declaration* is
imported from the source tree — `assets.registry` — because that is what the
wheel is being checked against, and a registry read out of the wheel would make
this a check that the wheel agrees with itself. Importing the registry is
therefore not importing the package under test: the package under test is the
built distribution, and nothing here imports, installs or executes it.

Nothing reaches the network.
"""

from __future__ import annotations

import hashlib
import re
import sys
import zipfile
from importlib.metadata import Distribution
from pathlib import Path

from reachy_mini_ha_satellite.assets.registry import ASSETS, UNREGISTERED

# Where the package lands inside a wheel.
_PACKAGE = "reachy_mini_ha_satellite"

# The entry-point group the Reachy Mini daemon enumerates, and what this
# application has to be registered in it as.
ENTRY_POINT_GROUP = "reachy_mini_apps"
ENTRY_POINT_TARGET = "reachy_mini_ha_satellite.daemon_app:ReachyMiniHaSatellite"

# Where an installer reads entry points from, per the wheel specification: one
# `.dist-info` directory at the root of the archive, and nothing nested. Anchored
# at both ends so that a file of the same name shipped as package data — which no
# installer ever reads — cannot stand in for the real declaration.
_DIST_INFO_METADATA = re.compile(r"^[^/]+\.dist-info/entry_points\.txt$")

# Licence text that has to travel with what it covers. CC BY 4.0 requires its
# notice reach everyone the sounds reach, and Apache-2.0 requires a copy of the
# licence reach everyone the models and the vendored protocol layer reach.
REQUIRED_TEXTS = (
    f"{_PACKAGE}/assets/NOTICE.md",
    f"{_PACKAGE}/assets/sounds/LICENSE.md",
    f"{_PACKAGE}/assets/wakewords/LICENSE",
    f"{_PACKAGE}/esphome/LICENSE",
    f"{_PACKAGE}/esphome/NOTICE",
)


class _MetadataOnly(Distribution):
    """One wheel's `entry_points.txt`, read by the daemon's own machinery.

    `importlib.metadata.Distribution` is an abstract reader over a
    distribution's metadata files, and `entry_points` is the public property
    built on top of it. Supplying the text directly is what lets this script ask
    the real parser about a wheel that is not installed — and it is why the
    answer is the daemon's answer rather than a second opinion from a parser
    that merely resembles it. See `_entry_point_problems` for the two ways
    `configparser` disagreed.

    Nothing is installed, imported or executed: the only file this claims to
    have is the one string it was handed, and every other request answers
    `None`.
    """

    def __init__(self, entry_points: str) -> None:
        """Hold the declaration this distribution consists of.

        Args:
            entry_points: The contents of the wheel's `entry_points.txt`.
        """
        self._entry_points = entry_points

    def read_text(self, filename: str) -> str | None:
        """Return the one metadata file this distribution has.

        Args:
            filename: Which metadata file is wanted.

        Returns:
            The entry-point declaration, or `None` for anything else — which
            is how `Distribution` expects a missing metadata file to be
            reported.
        """
        if filename == "entry_points.txt":
            return self._entry_points
        return None

    def locate_file(self, path: object) -> Path:
        """Refuse to place a file, because this distribution is not on disk.

        Args:
            path: Unused.

        Raises:
            NotImplementedError: Always. `entry_points` never calls this;
                anything that did would be asking for a real installation,
                and answering with a plausible path would be a lie.
        """
        del path
        message = "a metadata-only distribution has no files on disk"
        raise NotImplementedError(message)


def _entry_point_problems(wheel: zipfile.ZipFile, names: set[str]) -> list[str]:
    """Say whether the daemon would find this application.

    Three things make this a check rather than a search, and each closes a way
    of passing a wheel whose real entry point is missing or wrong.

    **The path is the wheel specification's, not a suffix.** Installer metadata
    lives at `<name>-<version>.dist-info/entry_points.txt` and nowhere else, so
    that is what is matched. A plain "ends with `/entry_points.txt`" also
    matches a file of that name shipped as *package data* — which an installer
    never reads and which would therefore let a wheel with no real declaration
    pass on the strength of a decoy.

    **There must be exactly one.** A wheel carries one `.dist-info`; two means
    something is wrong with the build, and picking either of them would be
    picking arbitrarily between two answers to a question with one.

    **It is parsed by the machinery that will actually read it, not by a
    lookalike.** A substring check passes on a wheel naming the right target
    under the *wrong* group — a console script, say — while the group the daemon
    enumerates declares something else entirely, which is the one arrangement
    that looks correct and installs an application nothing starts. So the file
    is parsed; but *which* parser is itself load-bearing, because this check
    stands behind REQ-041's claim about what the daemon would discover.

    `entry_points.txt` looks like a `configparser` file and is not one.
    `importlib.metadata` — which is what the daemon enumerates through — parses
    it with its own reader, and `configparser` disagrees with that reader in
    both directions:

    * **`%` is ordinary text here, and interpolation makes it fatal.** A
      `ConfigParser` left on its default `BasicInterpolation` raises on a value
      containing a lone `%`, and rewrites `%%` to `%`. Neither happens in the
      real reader, so a legal wheel would be reported unparseable and a value
      carrying `%%` would be compared in a spelling nothing ever wrote. Worse,
      interpolation is lazy: the raise lands on `items()`, not on the read, so
      it escaped the `except` clause that was meant to turn it into a finding
      and came out as a traceback.
    * **`[DEFAULT]` is not a section here, and `configparser` merges it into
      every other one.** A wheel declaring the right target under `[DEFAULT]`
      with an empty `[reachy_mini_apps]` reads, to `configparser`, as a correct
      declaration — and to the daemon as no declaration at all. That is a false
      *pass*, which is the direction that actually ships a broken wheel.

    Asking `importlib.metadata` removes both at once, and keeps removing
    whatever else the two would have disagreed about, because the question this
    function asks is by construction the question the daemon asks.

    Args:
        wheel: The built wheel, open for reading.
        names: What is in it.

    Returns:
        One line per problem.
    """
    declarations = sorted(name for name in names if _DIST_INFO_METADATA.match(name))
    if not declarations:
        return [
            "no .dist-info/entry_points.txt is in the wheel, so the daemon "
            "would never find this application however it was installed",
        ]
    if len(declarations) > 1:
        return [
            f"the wheel carries {len(declarations)} .dist-info/entry_points.txt "
            f"files ({', '.join(declarations)}), so which one an installer would "
            f"read is not a question with one answer",
        ]

    try:
        declared = wheel.read(declarations[0]).decode("utf-8")
    except UnicodeDecodeError as error:
        return [f"{declarations[0]} is not UTF-8 text: {error}"]

    entry_points = _MetadataOnly(declared).entry_points
    targets = [
        entry_point.value
        for entry_point in entry_points
        if entry_point.group == ENTRY_POINT_GROUP
    ]
    if not targets:
        return [
            f"the wheel declares no {ENTRY_POINT_GROUP} entry point, so the "
            f"daemon would not discover it",
        ]
    if ENTRY_POINT_TARGET not in targets:
        return [
            f"the {ENTRY_POINT_GROUP} entry point resolves to {targets} rather "
            f"than to {ENTRY_POINT_TARGET}",
        ]
    return []


def check(wheel: zipfile.ZipFile) -> list[str]:
    """Report every way this wheel is not the artifact it claims to be.

    Args:
        wheel: The built wheel, open for reading.

    Returns:
        One line per problem, or an empty list when there are none.
    """
    problems: list[str] = []
    names = set(wheel.namelist())

    for asset in ASSETS:
        path = f"{_PACKAGE}/assets/{asset.path}"
        if path not in names:
            problems.append(
                f"{path}: registered as a shipped asset but not in the wheel"
            )
            continue
        digest = hashlib.sha256(wheel.read(path)).hexdigest()
        if digest != asset.sha256:
            problems.append(
                f"{path}: digest {digest} does not match the registered "
                f"{asset.sha256} — the wheel does not carry what the registry records",
            )

    for text in REQUIRED_TEXTS:
        if text not in names:
            problems.append(f"{text}: licence text missing from the wheel")

    problems.extend(_entry_point_problems(wheel, names))

    # A file under the asset directory that the registry does not know about has
    # shipped without anyone having agreed to its terms. The exemption list is
    # the same closed literal `just check-assets` reads.
    registered = {f"{_PACKAGE}/assets/{asset.path}" for asset in ASSETS}
    exempt = {f"{_PACKAGE}/assets/{path}" for path in UNREGISTERED}
    prefix = f"{_PACKAGE}/assets/"
    for name in sorted(names):
        if not name.startswith(prefix) or name.endswith("/"):
            continue
        if name not in registered and name not in exempt:
            problems.append(
                f"{name}: shipped in the wheel but not in the asset registry",
            )

    return problems


def main(argv: list[str]) -> int:
    """Verify one wheel and say what is wrong with it.

    Args:
        argv: The wheel's path, and nothing else.

    Returns:
        The process exit status.
    """
    if len(argv) != 1:
        sys.stderr.write("usage: verify_satellite_wheel.py <wheel>\n")
        return 2

    path = Path(argv[0])
    if not path.is_file():
        sys.stderr.write(f"{path} is not a file\n")
        return 2

    with zipfile.ZipFile(path) as wheel:
        problems = check(wheel)

    for problem in problems:
        sys.stderr.write(f"satellite wheel: {problem}\n")
    if problems:
        return 1
    print(
        f"satellite wheel: {path.name} carries {len(ASSETS)} registered assets, "
        f"their licence texts, and the {ENTRY_POINT_GROUP} entry point",
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main(sys.argv[1:]))
