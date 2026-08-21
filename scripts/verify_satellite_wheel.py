"""Check that the built satellite wheel is the artifact it is supposed to be.

A wheel that builds is not a wheel that works, and the three ways this one can
be wrong are invisible to a successful build.

**The entry point.** ha-satellite REQ-041 says installing the wheel is
sufficient for the daemon to find the application, and what makes that true is
one line of `entry_points.txt` inside the distribution's metadata. A packaging
change that dropped it would produce a wheel that installs perfectly and never
appears in the daemon's list.

**The launch.** The line existing is not the whole of REQ-041, and believing it
was cost a robot an evening. The daemon does not import the entry point and
instantiate the class it names: it takes the **module** half — everything left
of the colon — and starts the application as a subprocess,
`python -u -m <module>`. A module with no `if __name__ == "__main__":` block
imports under that command, does nothing and exits 0; the daemon reports the
application as finished, successfully, seconds after starting it and with no
output at all. So the module is executed here the way the daemon executes it,
and a wheel whose entry module exits 0 having done nothing is refused. See
`_execution_problems` for why that outcome is cleanly distinguishable from a
working one without a robot anywhere near it.

**The assets.** The wake-word models and sounds ship as package data, and the
licence texts that have to travel with them ship beside them. `just check-assets`
verifies the tree; this verifies the *wheel*, which is what actually reaches
somebody else's robot — and it checks the digests, so a file substituted between
the registry and the build is visible.

**What is read from where, because the distinction is the whole point.** The
*artifact* is read out of the wheel and only out of the wheel: every path, every
digest and the entry-point metadata come from the zip file, and the launch check
runs the module it extracted from that zip file rather than the one in this
checkout — `_resolution_problems` is what makes that a fact instead of a hope.
The *declaration* is imported from the source tree — `assets.registry`, and
`config` for the environment-variable prefix — because that is what the wheel is
being checked against, and a registry read out of the wheel would make this a
check that the wheel agrees with itself. Importing the registry is therefore not
importing the package under test: the package under test is the built
distribution, which is extracted and executed in a subprocess and never imported
into this process.

Nothing reaches the network.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from importlib.metadata import Distribution
from pathlib import Path

from reachy_mini_ha_satellite.assets.registry import ASSETS, UNREGISTERED
from reachy_mini_ha_satellite.config import ENV_PREFIX, variable_for

# Where the package lands inside a wheel.
_PACKAGE = "reachy_mini_ha_satellite"

# The entry-point group the Reachy Mini daemon enumerates, and what this
# application has to be registered in it as.
ENTRY_POINT_GROUP = "reachy_mini_apps"
ENTRY_POINT_TARGET = "reachy_mini_ha_satellite.daemon_app:ReachyMiniHaSatellite"

# The half of that the daemon actually launches. Its application manager asks
# for the module — everything left of the colon — and runs `python -u -m` on it;
# the object half is never imported and never instantiated by the daemon. So
# this, and not the class, is the thing that has to be runnable, and it is
# derived from the target rather than written out a second time so the two
# cannot disagree about which module that is.
ENTRY_POINT_MODULE = ENTRY_POINT_TARGET.split(":", 1)[0]

# What the entry module's `main` returns when the configuration is unusable:
# EX_CONFIG. It is the status the launch check expects, because a satellite
# started with nothing configured refuses to start and says why — see
# `_execution_problems`.
EX_CONFIG = 78

# How long the entry module gets before the check gives up on it. It refuses
# within about a second, because the refusal happens before anything is built;
# this only stops a wheel whose module blocks from hanging a release.
LAUNCH_TIMEOUT_SECONDS = 120.0

# The Reachy Mini SDK, in the shape the entry module uses it and nothing more.
#
# The entry module imports `reachy_mini.apps.app`, and importing the real one
# executes `import gi` three modules away — PyGObject and the whole GStreamer
# stack, which architecture REQ-005 keeps off this side of the line. Standing in
# for that one import is what lets the daemon's launch be reproduced on a
# machine that is not a robot, and it is the *only* substitution: the `__main__`
# guard, `main`, the configuration layer and the refusal that comes out of it are
# all the wheel's own code, run from the wheel's own files.
#
# `wrapped_run` is the SDK's contract with an application — the daemon calls it,
# and it calls `run` with a robot handle — so the stub calls `run` too. The
# handle is a bare object because the configuration is read before anything
# reaches for it, which is the whole reason an unconfigured start is a clean
# signal rather than a crash somewhere in the robot layer.
_SDK_STUB = '''"""A stand-in for the Reachy Mini SDK's application base class."""

import threading


class ReachyMiniApp:
    """What the daemon's applications subclass."""

    custom_app_url = None
    dont_start_webserver = False
    request_media_backend = None

    def __init__(self, running_on_wireless=False):
        """Prepare, without looking for a daemon to talk to."""
        self.running_on_wireless = running_on_wireless
        self.stop_event = threading.Event()
        self.error = ""

    def wrapped_run(self, *args, **kwargs):
        """Run the application with a robot handle, as the daemon does."""
        self.run(object(), self.stop_event)

    def stop(self):
        """Ask the application to stop."""
        self.stop_event.set()
'''

# Asks the interpreter which file `python -m <module>` would run, and whether
# that name is a package — a package would run its `__main__` submodule instead,
# which is a different file from the one the entry point names.
_RESOLUTION_PROBE = """\
import importlib.util
import json
import sys

spec = importlib.util.find_spec(sys.argv[1])
print(
    json.dumps(
        {
            "origin": None if spec is None else spec.origin,
            "package": spec is not None and bool(spec.submodule_search_locations),
        },
    ),
)
"""

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


def _launch_environment(
    *,
    stub_root: Path,
    wheel_root: Path,
    state_dir: Path,
) -> dict[str, str]:
    """Build the environment the entry module is launched in.

    Three things are arranged, and each of them is what makes the answer mean
    something.

    **`PYTHONPATH` is replaced, not extended.** Any inherited value would be
    this checkout's own source tree, and a check that ran the source instead of
    the wheel would pass on a wheel that does not contain the fix. The stub SDK
    comes first and the extracted wheel second; the interpreter's own
    `site-packages` still supplies the third-party dependencies, exactly as the
    daemon's shared application environment does on the robot.

    **Every `REACHY_SATELLITE_*` variable is dropped.** The check reads a
    refusal to start as the proof that the module runs, so a machine that
    happens to have the satellite configured must not turn that refusal into a
    real startup.

    **The state directory is pointed at somewhere empty.** `state_dir` is a
    bootstrap setting read straight from the environment, and it is where the
    settings interface's overrides file lives. Left alone, a robot's real
    overrides would be read and could supply the one setting whose absence this
    relies on.

    Args:
        stub_root: The directory holding the stand-in SDK.
        wheel_root: The directory the wheel was extracted into.
        state_dir: An empty directory to use as the satellite's state
            directory.

    Returns:
        The environment for the subprocesses.
    """
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(ENV_PREFIX)
    }
    environment["PYTHONPATH"] = os.pathsep.join((str(stub_root), str(wheel_root)))
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment[variable_for("state_dir")] = str(state_dir)
    return environment


def _python(
    arguments: list[str],
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    """Run this interpreter with the given arguments and capture what it says.

    Args:
        arguments: What to pass the interpreter.
        environment: The environment to run in.

    Returns:
        The finished process.

    Raises:
        subprocess.TimeoutExpired: If it does not finish in time.
    """
    return subprocess.run(  # noqa: S603  # a fixed argument vector built from this module's own literals and `sys.executable`; no shell, and nothing a caller supplies reaches it
        [sys.executable, "-u", *arguments],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=LAUNCH_TIMEOUT_SECONDS,
    )


def _resolution_problems(
    module: str,
    wheel_root: Path,
    completed: subprocess.CompletedProcess[str],
) -> list[str]:
    """Say whether `python -m <module>` would run the file the wheel shipped.

    Without this the launch check could be satisfied by something that is not
    the wheel at all. Two ways, and both have happened to somebody:

    * **A different copy of the module.** This repository installs its members
      editable, so an interpreter here can reach the source tree as well as the
      extraction directory. Comparing the resolved file against the extraction
      directory is what makes the run a run of the artifact.
    * **A `__main__.py` standing in for it.** If the entry point named a
      *package*, `python -m` would run that package's `__main__` submodule
      instead — a different file, which may well have an execution path while
      the module the daemon's `get_app_module` returns has none. That is
      precisely this application's arrangement, so the check that would have
      been fooled by it is the check worth writing.

    Args:
        module: The module the entry point names.
        wheel_root: The directory the wheel was extracted into.
        completed: The finished resolution probe.

    Returns:
        One line per problem.
    """
    if completed.returncode != 0:
        return [
            f"{module} could not be resolved from the wheel: {_tail(completed.stderr)}",
        ]
    try:
        resolved = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        return [f"the resolution probe for {module} said nothing usable: {error}"]

    origin = resolved["origin"]
    if origin is None:
        return [
            f"the wheel declares {ENTRY_POINT_GROUP} entry point "
            f"{ENTRY_POINT_TARGET}, but {module} is not in the wheel, so the "
            f"daemon would launch a module that does not exist",
        ]
    if resolved["package"]:
        return [
            f"{module} is a package, so `python -m {module}` would run its "
            f"__main__ submodule rather than the module the entry point names",
        ]

    expected = wheel_root.joinpath(*module.split(".")).with_suffix(".py")
    if Path(origin).resolve() != expected.resolve():
        return [
            f"`python -m {module}` would run {origin} rather than the wheel's "
            f"own {expected}, so the launch below would not be a check of this "
            f"wheel",
        ]
    return []


def _execution_problems(
    module: str,
    completed: subprocess.CompletedProcess[str],
) -> list[str]:
    """Say whether the daemon's launch actually starts anything.

    **The distinction this rests on, and why it needs no robot.** The module is
    run with nothing configured, which the satellite refuses: `device_name` has
    no default and cannot have one, so `main` catches the `ConfigurationError`,
    writes it to standard error and returns EX_CONFIG. That refusal is not a
    disappointment here — it is the evidence, because reaching it means the
    module's `__main__` guard ran, called `main`, and got as far as the code
    that reads a configuration.

    A module with no `__main__` guard cannot produce any of that. Under
    `python -m` it imports, finds nothing to do and exits **0, silently**. The
    two outcomes differ in both the status and the presence of output, neither
    of which needs a robot, a network or a configured environment — which is
    why this check can stand behind REQ-041 in an ordinary build.

    Anything else — an import error, a crash, a module that blocks — is neither
    outcome and is reported with what it said, because a launch check that
    passed on "well, it exited non-zero" would pass on a wheel that cannot
    import.

    Args:
        module: The module the entry point names.
        completed: The finished launch.

    Returns:
        One line per problem.
    """
    if completed.returncode == 0:
        return [
            f"`python -m {module}` exited 0 having done nothing, which is how "
            f"the daemon launches this application: it takes the module half of "
            f"the {ENTRY_POINT_GROUP} entry point and runs it as a subprocess. "
            f'The module needs an `if __name__ == "__main__":` block; without '
            f"one the daemon reports the application as finished, successfully, "
            f"seconds after starting it",
        ]
    if completed.returncode != EX_CONFIG:
        return [
            f"`python -m {module}` exited {completed.returncode} rather than "
            f"refusing an empty configuration with {EX_CONFIG}: "
            f"{_tail(completed.stderr)}",
        ]
    if not completed.stderr.strip():
        return [
            f"`python -m {module}` exited {EX_CONFIG} without saying why, so "
            f"whatever an operator saw would not tell them what to configure",
        ]
    return []


def _tail(output: str, limit: int = 2000) -> str:
    """Keep the end of a captured stream, which is where the reason is.

    Args:
        output: What the process wrote.
        limit: How many characters to keep.

    Returns:
        The tail of it, or a note that there was nothing.
    """
    stripped = output.strip()
    if not stripped:
        return "(it said nothing)"
    return stripped[-limit:]


def check_launch(path: Path, module: str = ENTRY_POINT_MODULE) -> list[str]:
    """Run the wheel's entry module the way the daemon runs it.

    The wheel is extracted rather than installed, and executed in a subprocess
    rather than imported, so this process never loads the artifact it is
    judging. Nothing is written outside a temporary directory that is removed
    afterwards, and nothing reaches the network.

    Args:
        path: The built wheel.
        module: The module the daemon would launch. Defaults to the one the
            entry point names; a parameter so the tests can point it elsewhere.

    Returns:
        One line per problem, or an empty list when the launch works.
    """
    with tempfile.TemporaryDirectory(prefix="satellite-wheel-") as scratch:
        root = Path(scratch)
        wheel_root = root / "wheel"
        with zipfile.ZipFile(path) as archive:
            # This repository's own freshly built wheel, and the whole of it,
            # into a temporary directory removed on the way out. Extracting a
            # subset would be extracting what the check expects rather than
            # what the wheel contains, and the module's imports need its
            # siblings anyway.
            archive.extractall(wheel_root)
        stub_root = _write_sdk_stub(root / "sdk")
        state_dir = root / "state"
        state_dir.mkdir()
        environment = _launch_environment(
            stub_root=stub_root,
            wheel_root=wheel_root,
            state_dir=state_dir,
        )

        try:
            probe = _python(["-c", _RESOLUTION_PROBE, module], environment)
        except subprocess.TimeoutExpired:
            return [f"resolving {module} from the wheel did not finish"]
        problems = _resolution_problems(module, wheel_root, probe)
        if problems:
            return problems

        try:
            launched = _python(["-m", module], environment)
        except subprocess.TimeoutExpired:
            return [
                f"`python -m {module}` did not finish within "
                f"{LAUNCH_TIMEOUT_SECONDS:.0f} seconds, so the daemon would "
                f"start an application that neither runs nor refuses",
            ]
        return _execution_problems(module, launched)


def _write_sdk_stub(root: Path) -> Path:
    """Lay out the stand-in SDK the entry module imports.

    Args:
        root: Where to put it.

    Returns:
        The directory to put on the path, which is `root` itself.
    """
    apps = root / "reachy_mini" / "apps"
    apps.mkdir(parents=True)
    (root / "reachy_mini" / "__init__.py").write_text("", encoding="utf-8")
    (apps / "__init__.py").write_text("", encoding="utf-8")
    (apps / "app.py").write_text(_SDK_STUB, encoding="utf-8")
    return root


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

    # Only when the declaration is right. Launching the module the entry point
    # names is meaningless while the entry point itself is wrong, and the
    # findings above are the ones that say what to fix.
    if not problems:
        problems = check_launch(path)

    for problem in problems:
        sys.stderr.write(f"satellite wheel: {problem}\n")
    if problems:
        return 1
    print(
        f"satellite wheel: {path.name} carries {len(ASSETS)} registered assets, "
        f"their licence texts, and the {ENTRY_POINT_GROUP} entry point, whose "
        f"module {ENTRY_POINT_MODULE} starts and refuses an empty configuration "
        f"when run the way the daemon runs it",
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main(sys.argv[1:]))
