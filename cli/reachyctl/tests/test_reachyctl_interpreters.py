"""What may be executed as a Python interpreter, and what may only be derived.

This is the half of the resolution that needs no robot: given what systemd
reported, which paths are worth asking, in which order, and — the part that
matters — which paths are never asked at all. `test_reachyctl_daemon.py` drives
the other half, where a robot answers.

The gate is a name. A unit's start program becomes a candidate only when its
file name is one CPython gives an interpreter, so `launcher.sh` cannot be
returned by any of these rules, whatever else about the unit is true. That is
the whole of REQ-105's protection and it is asserted here directly rather than
inferred from a resolution that happened to come out right.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

from typing import Final

import pytest

from reachyctl.interpreters import (
    application_candidates,
    candidates,
    names_an_interpreter,
)

# The stock image's unit: a shell launcher, three directories below the
# `site-packages` of the environment the daemon's own code is installed in.
LAUNCHER: Final = (
    "/venvs/mini_daemon/lib/python3.12/site-packages/reachy_mini/daemon/app/"
    "services/wireless/launcher.sh"
)
INTERPRETER: Final = "/venvs/mini_daemon/bin/python"


@pytest.mark.parametrize(
    "path",
    [
        "/usr/bin/python",
        "/usr/bin/python3",
        "/venvs/mini_daemon/bin/python3.12",
        "/opt/reachy/venv/bin/python2.7",
        "/usr/bin/python3.13t",
        "/usr/bin/python3.12d",
        "/usr/bin/python3.13td",
        # No CPython has shipped these yet. The gate has to outlive today's
        # version numbers, so a future major and a distant minor both pass on
        # shape; what it will not do is guess at a major that cannot exist.
        "/usr/bin/python4",
        "/usr/bin/python3.100",
    ],
)
def test_a_name_cpython_gives_an_interpreter_is_one(path: str) -> None:
    """These are the names CPython installs its own executables under.

    `python`, `python3`, a major.minor pair, and those with the ABI flags a
    free-threaded or a debug build appends. The last three are real
    interpreters, and refusing a real interpreter is a failure too — a visible
    one, but a failure. A patch-versioned executable is not something CPython
    ships, so it belongs in the refused list below rather than here.

    Args:
        path: A path whose file name claims to be one.
    """
    assert names_an_interpreter(path) is True


@pytest.mark.parametrize(
    "path",
    [
        LAUNCHER,
        "/venvs/mini_daemon/bin/python-config",
        "/venvs/mini_daemon/bin/pythonish",
        "/venvs/mini_daemon/bin/reachy-mini-daemon",
        "/usr/local/bin/python.sh",
        "/opt/reachy/venv/bin/python3.12.1",
        "/usr/bin/python3.13x",
        # A run of digits with no separator. CPython never omits the dot, and
        # this is the shape a wrapper picks precisely because it looks close
        # enough to the real thing.
        "/usr/bin/python312",
        "/usr/bin/python27",
        "/usr/bin/python03",
        "/usr/bin/python3123",
        # The one major version that cannot exist. Refusing it is not encoding
        # today's version numbers — `python4` and `python9` still pass.
        "/usr/bin/python0",
        # An ABI flag floating free of a full version. CPython names its
        # free-threaded and debug executables after `major.minor` —
        # `python3.13t`, never `python3t`.
        "/usr/bin/python3t",
        "/usr/bin/python2d",
        "/usr/bin/python0td",
        "/usr/bin/pythont",
        "",
    ],
)
def test_anything_else_is_not_run_however_much_it_looks_like_one(path: str) -> None:
    """The gate is deliberately narrow, and this is the list it has to refuse.

    A real interpreter under an unusual name costs an operator one `--python`.
    A wrapper admitted by a loose pattern costs them a second daemon. The
    separator-less spellings are the ones worth pinning: `python312` is not a
    name CPython uses, and a pattern that took it would be admitting the shape
    a wrapper reaches for because it looks close enough.

    Args:
        path: A path whose file name does not claim to be an interpreter.
    """
    assert names_an_interpreter(path) is False


def test_a_wrapper_is_never_offered_and_its_environment_is() -> None:
    """The stock image, reduced to the derivation that fixes it."""
    found = candidates(configured=None, exec_start=LAUNCHER, environment={})

    assert [candidate.path for candidate in found] == [INTERPRETER]
    assert LAUNCHER not in [candidate.path for candidate in found]


def test_a_unit_that_starts_an_interpreter_offers_exactly_it_first() -> None:
    """A robot that worked before this module existed resolves to the same path."""
    found = candidates(
        configured=None,
        exec_start="/opt/reachy/venv/bin/python",
        environment={},
    )

    assert found[0].path == "/opt/reachy/venv/bin/python"


def test_an_interpreter_derived_twice_is_offered_once() -> None:
    """`<venv>/bin/python` is both the start program and the bin beside it."""
    found = candidates(
        configured=None,
        exec_start="/opt/reachy/venv/bin/python",
        environment={"VIRTUAL_ENV": "/opt/reachy/venv"},
    )

    assert [candidate.path for candidate in found] == ["/opt/reachy/venv/bin/python"]
    assert found[0].source == "the interpreter the unit's ExecStart names"


def test_what_an_operator_named_comes_before_anything_derived() -> None:
    """`--python` is an answer to the question, not help with a guess."""
    found = candidates(
        configured="/usr/bin/python3",
        exec_start=LAUNCHER,
        environment={"VIRTUAL_ENV": "/venvs/declared"},
    )

    assert [candidate.path for candidate in found] == [
        "/usr/bin/python3",
        "/venvs/declared/bin/python",
        INTERPRETER,
    ]


@pytest.mark.parametrize(
    "named",
    [
        LAUNCHER,
        "/venvs/mini_daemon/lib/python3.12/site-packages/reachy_mini/daemon/app/"
        "services/wireless/../wireless/launcher.sh",
        "/usr/local/bin/py312",
    ],
)
def test_the_gate_outranks_the_operator_and_needs_no_path_comparison(
    named: str,
) -> None:
    """A rule phrased as "not the launcher" would have to compare two paths.

    Two spellings of one file — a `..` in the middle, a symlink, a trailing
    slash — are not equal as strings, and normalising them properly needs the
    robot. Phrased as a name instead, the rule needs no comparison: an alias of
    `launcher.sh` is still called `launcher.sh`. The third case is the price,
    and it is stated rather than hidden — an interpreter under a name CPython
    never gives one is refused too, and the failure says to link it.

    Args:
        named: What `--python` named.
    """
    found = candidates(configured=named, exec_start=LAUNCHER, environment={})

    assert [candidate.path for candidate in found] == [INTERPRETER]


def test_a_bin_directory_alone_is_not_an_environment_and_is_not_guessed_at() -> None:
    """Finding *an* interpreter is not good enough; it has to be the right one.

    A console script installed system-wide would yield `/usr/local/bin/python`,
    which exists and answers `-V` and may be nothing to do with the environment
    the daemon's packages are in. Installing into it and verifying against it
    would agree with itself while both looked at the wrong place, which is the
    failure reachyctl REQ-051 exists to catch. Failing is visible; that is not.
    """
    assert (
        candidates(
            configured=None,
            exec_start="/usr/local/bin/reachy-mini-daemon",
            environment={},
        )
        == ()
    )


def test_a_console_script_inside_a_declared_environment_still_resolves() -> None:
    """What replaces the guess is the unit saying which environment it means."""
    found = candidates(
        configured=None,
        exec_start="/venvs/mini_daemon/bin/reachy-mini-daemon",
        environment={"VIRTUAL_ENV": "/venvs/mini_daemon"},
    )

    assert [candidate.path for candidate in found] == [INTERPRETER]


def test_a_unit_declaring_no_start_program_suggests_nothing() -> None:
    """An empty answer is a real answer: the caller fails rather than inventing one."""
    assert candidates(configured=None, exec_start="", environment={}) == ()


def test_every_candidate_says_what_suggested_it() -> None:
    """The reason is what the failure message is made of."""
    found = candidates(
        configured=None,
        exec_start=LAUNCHER,
        environment={"VIRTUAL_ENV": "/venvs/declared"},
    )

    assert [candidate.describe() for candidate in found] == [
        "/venvs/declared/bin/python (the VIRTUAL_ENV the unit declares)",
        f"{INTERPRETER} (the environment the unit's start program is installed in)",
    ]


def test_the_application_environment_is_a_sibling_of_the_daemon_s() -> None:
    """The released image, and the false negative this removes.

    Asking the daemon's own interpreter what version of the application is
    installed gets the true answer to the wrong question — the application is
    not there — and reporting that as "not installed" tells an operator to
    install something their robot is already running.
    """
    found = application_candidates(INTERPRETER)

    assert [candidate.path for candidate in found] == [
        "/venvs/apps_venv/bin/python",
        INTERPRETER,
    ]
    assert found[0].source == "the environment the daemon installs applications into"


def test_the_daemon_s_own_environment_is_always_the_last_answer() -> None:
    """An image keeping one environment for both resolves exactly as it did before."""
    found = application_candidates("/opt/reachy/venv/bin/python")

    assert found[-1].path == "/opt/reachy/venv/bin/python"
    assert found[-1].source == "the daemon's own environment"


def test_an_interpreter_outside_a_bin_directory_derives_no_sibling() -> None:
    """There is no environment to be a sibling of, so nothing is invented."""
    found = application_candidates("/usr/local/python3.12")

    assert [candidate.path for candidate in found] == ["/usr/local/python3.12"]


def test_the_gate_still_holds_when_the_daemon_s_own_path_is_not_one() -> None:
    """The caller passes a proven interpreter, and the gate does not take that on trust.

    Every path this function offers is executed with `-V` by its caller, so the
    same rule applies here as everywhere else: a name CPython never gives an
    interpreter is not offered, whoever produced it.
    """
    found = application_candidates("/venvs/mini_daemon/bin/launcher.sh")

    assert "/venvs/mini_daemon/bin/launcher.sh" not in [
        candidate.path for candidate in found
    ]
