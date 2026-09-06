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
Everything else is derived from a directory layout only an environment has: the
`bin` beside the `lib/pythonX.Y/site-packages` the daemon's own code was found
in, or the `bin` the start program itself sits in. A launcher named
`launcher.sh` is therefore never run, and no ordering of these rules can make it
run.

**The order is the order of authority, not of convenience.** What an operator
supplied comes first, because they are answering the question rather than
helping the tool guess at it. A unit that genuinely starts an interpreter comes
next, so a robot that worked before this module existed resolves to exactly the
path it resolved to then. The derivations follow, and the caller stops at the
first candidate the robot confirms.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["Candidate", "candidates", "names_an_interpreter"]

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
        repeated. It is empty when nothing on this robot suggested one, and an
        empty answer is a real answer: the caller fails with it rather than
        reaching for a path it invented.
    """
    directory = PurePosixPath(exec_start).parent
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
    if exec_start and directory.name == _BIN:
        derived.append(
            (
                f"{directory}/{_INTERPRETER}",
                "the bin directory the unit's start program is in",
            ),
        )
    found: dict[str, Candidate] = {}
    for path, source in derived:
        # First reason wins, so a path derived two ways is named by the
        # strongest thing that suggested it rather than by the last one.
        found.setdefault(path, Candidate(path=path, source=source))
    return tuple(found.values())
