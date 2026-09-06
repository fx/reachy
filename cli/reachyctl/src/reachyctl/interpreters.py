"""Which interpreter owns the environment the daemon's packages are installed in.

A daemon's service unit names the program that **starts** the daemon, and on
only some images is that program a Python interpreter. On ReachyMiniOS v0.2.3 it
is a shell launcher that lives inside the daemon's own virtual environment, and
a tool that read the unit's start program as an interpreter and handed it
`-c '<python source>'` ran the launcher — which started a *second* daemon, which
then contended with the first for its network port, its serial device and its
camera before dying. That is the failure REQ-105 and REQ-106 name, and it is why
this module exists: the question worth asking is not "what does the unit start"
but "which interpreter owns the environment the daemon's packages are installed
in", and those are the same answer on some images and not on others.

**Nothing here executes anything.** It turns what systemd already reported into
an ordered list of candidate paths, each carrying the reason it was derived, and
`reachyctl.daemon` is what asks the robot whether one of them really is an
interpreter. The split is deliberate: the derivation is the part with the rules
in it, and it is decided without a robot.

**A path becomes a candidate only when something other than hope says it is an
interpreter.** The unit's start program qualifies on its *name* — `python`,
`python3`, `python3.12` — and never on the bare fact that the unit starts it,
because that is precisely the assumption that produced the second daemon.
Everything else is derived from something that identifies an *environment*: the
`VIRTUAL_ENV` the unit declares, or the `bin` beside the
`lib/pythonX.Y/site-packages` the daemon's own code was found in. A launcher
named `launcher.sh` is therefore never run, and no ordering of these rules can
make it run.

**A rule that finds an interpreter is not good enough; it has to find the right
one.** There is deliberately no rule taking the `bin` directory a start program
merely *sits in*, because a `bin` alone is not an environment: a console script
installed at `/usr/local/bin/reachy-mini-daemon` would yield
`/usr/local/bin/python`, which exists, answers `-V`, and may be nothing to do
with the environment the daemon's packages are in. Installing into it and then
verifying against it would agree with itself while both looked at the wrong
place, which is the failure reachyctl REQ-051 exists to catch — worse than
failing, because failing is visible. Such a unit resolves nothing and says so,
and `--python` answers it in one step.

**The order is the order of authority, not of convenience.** What an operator
supplied comes first, because they are answering the question rather than
helping the tool guess at it. A unit that genuinely starts an interpreter comes
next, so a robot that worked before this module existed resolves to exactly the
path it resolved to then. The derivations follow, and the caller stops at the
first candidate the robot confirms.

**The name gate outranks the order, and it outranks the operator too.** It is a
precondition on EVERY candidate rather than a rule about the unit's start
program, and that is a deliberate strengthening: a rule phrased as "not the
launcher" has to compare two paths, and two paths that name the same file can be
spelled differently — a `..` in the middle, a symlink, a trailing slash — so the
comparison is only ever as good as a normalisation nothing here can do without
asking the robot. Phrased as "its name is one CPython gives an interpreter", the
rule needs no comparison at all: an alias of `launcher.sh` is still called
`launcher.sh`, and it is refused for the same reason the original is. What it
costs is an operator whose interpreter is at a name CPython never gives one, who
must point `--python` at a link named `python` instead; the failure says so.

**Where the gate stops, and why it stops there.** A name gate cannot see through
a rename. An operator who puts a link called `python` in front of their daemon's
launcher, and then passes that link to `--python`, gets the launcher run — and
nothing short of running something can tell the two apart, because *proving a
program is an interpreter means executing it*. What this module can do, and
does, is bound the consequence: the only thing ever executed on an unproven path
is `-V`, which is a flag and not source, and everything that follows waits on
that answer. Chasing the remaining case would mean resolving symlinks on the
robot, which brings back the path comparison this design removed, needs another
round trip, and still would not catch a launcher *copied* rather than linked. It
is a recorded limit of a non-adversarial threat model — the failure this exists
to stop is a stock image's ordinary layout, not an operator disguising their own
daemon as an interpreter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "Candidate",
    "application_candidates",
    "candidates",
    "names_an_interpreter",
]

# What CPython installs its own executables as: `python`, `python3`,
# `python3.12`. Anchored at both ends, so `python-config`, `pythonize` and a
# launcher some image called `python-daemon.sh` are all outside it. This is a
# gate on what may be EXECUTED at all, so it is deliberately narrow: a real
# interpreter under an unusual name costs an operator one `--python`, and a
# wrapper admitted under a loose pattern costs them a second daemon.
_INTERPRETER_NAME: Final = re.compile(r"\Apython\d*(?:\.\d+)*\Z")

# `<prefix>/lib/python3.12/site-packages/<package>/...`, which is where an
# installed distribution's own files live. The prefix is the environment's root,
# and `<prefix>/bin/python` is the interpreter that owns it. Non-greedy, so a
# path that somehow contains two of these resolves to the outermost — the one
# whose `bin` is a sibling of the `lib` the packages are under. `lib64` is
# matched too: some distributions put a 64-bit environment's packages there.
_SITE_PACKAGES: Final = re.compile(
    r"\A(?P<prefix>/.*?)/lib(?:64)?/python\d+(?:\.\d+)*/site-packages/",
)

# The variable a virtual environment sets to its own root. When a unit declares
# it, the unit is saying which environment it means, which is the question.
_VIRTUAL_ENV: Final = "VIRTUAL_ENV"

# What an environment's interpreter is called inside its `bin`. A virtual
# environment always has this name, whichever interpreter created it.
_INTERPRETER: Final = "python"

# The directory an environment keeps its executables in.
_BIN: Final = "bin"

# What the vendor's own installer calls the environment it puts applications in.
# It is a SIBLING of the daemon's own environment, not a child of it, and the
# name is upstream's: `reachy_mini/apps/sources/local_common_venv.py` derives it
# as `parent_of(the daemon's venv) / "apps_venv"`. A public identifier of a
# third-party dependency, which the repository's rule on tracked identifiers
# allows, and the only vendor-specific string in this module.
_APPLICATION_ENVIRONMENT: Final = "apps_venv"


@dataclass(frozen=True, slots=True, kw_only=True)
class Candidate:
    """A path that might be the daemon's interpreter, and why it might be.

    The reason travels with the path because it is what the failure message is
    made of. An operator told only that three paths were tried learns nothing
    they can act on; one told that the environment holding the daemon's code has
    no `bin/python` in it knows which robot they are looking at.

    Attributes:
        path: Where the interpreter would be.
        source: What suggested it, phrased to be read inside a sentence
            listing everything that was tried.
    """

    path: str
    source: str

    def describe(self) -> str:
        """Say what this candidate is, for a message.

        Returns:
            The path and the reason it was derived.
        """
        return f"{self.path} ({self.source})"


#:= docs/specs/stock-robot-installation/index.md#req-105-the-daemon-s-start-program-is-not-assumed-to-be-an-interpreter
#:% `reachyctl` MUST NOT execute the program named by a robot's daemon service unit
#:% as though it were a Python interpreter.
def names_an_interpreter(path: str) -> bool:
    """Say whether a path's file name claims to be a Python interpreter.

    This is the gate on what may be executed at all, and it is the whole of the
    protection against the wrapper: `launcher.sh` does not pass it, so nothing
    downstream ever gets the chance to hand it Python source.

    Args:
        path: An absolute path on the robot.

    Returns:
        True when its file name is one CPython gives an interpreter.
    """
    return _INTERPRETER_NAME.match(PurePosixPath(path).name) is not None


#:= docs/specs/stock-robot-installation/index.md#req-105-the-daemon-s-start-program-is-not-assumed-to-be-an-interpreter
#:% `reachyctl` MUST NOT execute the program named by a robot's daemon service unit
#:% as though it were a Python interpreter.
def candidates(
    *,
    configured: str | None,
    exec_start: str,
    environment: Mapping[str, str],
) -> tuple[Candidate, ...]:
    """Work out which paths could be the interpreter, best answer first.

    Args:
        configured: What `--python` named, or `None` when the operator named
            nothing. First, because it is an answer rather than a guess.
        exec_start: The program the unit starts, or an empty string when the
            unit declares none — which is what systemd reports for a unit that
            is not installed.
        environment: The environment the unit declares, as systemd reported it.

    Returns:
        The candidates, in the order they should be tried, with no path
        repeated and every one of them named as CPython names an interpreter.
        It is empty when nothing on this robot suggested one, and an empty
        answer is a real answer: the caller fails with it rather than reaching
        for a path it invented.
    """
    enclosing = _SITE_PACKAGES.match(exec_start)
    virtual_env = environment.get(_VIRTUAL_ENV, "").rstrip("/")
    derived: list[tuple[str, str]] = []
    if configured:
        derived.append((configured, "the interpreter --python names"))
    if exec_start and names_an_interpreter(exec_start):
        derived.append((exec_start, "the interpreter the unit's ExecStart names"))
    if virtual_env:
        derived.append(
            (
                f"{virtual_env}/{_BIN}/{_INTERPRETER}",
                f"the {_VIRTUAL_ENV} the unit declares",
            ),
        )
    if enclosing is not None:
        derived.append(
            (
                f"{enclosing.group('prefix')}/{_BIN}/{_INTERPRETER}",
                "the environment the unit's start program is installed in",
            ),
        )
    found: dict[str, Candidate] = {}
    for path, source in derived:
        # The gate, applied to every candidate and not only to the unit's start
        # program. Every derived path is a `bin/python` and passes by
        # construction; the operator's own answer is the only one that can fail,
        # and refusing it here is what makes an alias of the launcher — the same
        # file spelled with a `..` or reached through a symlink — impossible to
        # smuggle past a path comparison this module could not do correctly.
        if not names_an_interpreter(path):
            continue
        # First reason wins, so a path derived two ways is named by the
        # strongest thing that suggested it rather than by the last one.
        found.setdefault(path, Candidate(path=path, source=source))
    return tuple(found.values())


def application_candidates(daemon: str) -> tuple[Candidate, ...]:
    """Work out which interpreter owns the environment applications are in.

    **A daemon does not have to run its applications in its own environment,
    and the released one does not.** ReachyMiniOS installs the daemon into one
    virtual environment and applications into a SIBLING of it, so asking the
    daemon's own interpreter what version of the application is installed gets
    the true answer to the wrong question: the application is not there, and
    reporting that as "not installed" is a false negative about a robot that is
    running it. Upstream's own installer derives the answer the same way this
    does — `parent_of(the daemon's venv) / "apps_venv"` — and it also has a mode
    where the two are one environment, which is the second rule below.

    Args:
        daemon: The interpreter that owns the daemon's own environment, already
            resolved and already confirmed to be one.

    Returns:
        The candidates, best answer first: the sibling environment the vendor's
        installer uses, and then the daemon's own, which is where an image that
        keeps one environment for both puts them. Never empty — the daemon's own
        interpreter is always the last answer — so an application reported as
        absent has been looked for everywhere it could be.
    """
    executable = PurePosixPath(daemon)
    derived: list[tuple[str, str]] = []
    if executable.parent.name == _BIN:
        sibling = executable.parent.parent.parent / _APPLICATION_ENVIRONMENT
        derived.append(
            (
                f"{sibling}/{_BIN}/{_INTERPRETER}",
                "the environment the daemon installs applications into",
            ),
        )
    derived.append((daemon, "the daemon's own environment"))
    found: dict[str, Candidate] = {}
    for path, source in derived:
        # The same gate, for the same reason: nothing whose name is not one
        # CPython gives an interpreter is ever executed. Both rules produce one
        # by construction, and the gate stays so that cannot quietly stop being
        # true if a rule is added.
        if not names_an_interpreter(path):
            continue
        found.setdefault(path, Candidate(path=path, source=source))
    return tuple(found.values())
