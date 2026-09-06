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

from reachyctl.interpreters import candidates, names_an_interpreter

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
        "/opt/reachy/venv/bin/python3.12.1",
    ],
)
def test_a_name_cpython_gives_an_interpreter_is_one(path: str) -> None:
    """These are the names an interpreter is installed under.

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
        "",
    ],
)
def test_anything_else_is_not_run_however_much_it_looks_like_one(path: str) -> None:
    """The gate is deliberately narrow, and this is the list it has to refuse.

    A real interpreter under an unusual name costs an operator one `--python`.
    A wrapper admitted by a loose pattern costs them a second daemon.

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


def test_the_one_path_an_operator_may_not_name_is_the_unit_s_own_launcher() -> None:
    """The name gate outranks the order, including over `--python`.

    Every other path is theirs to name. This one is the second daemon arriving
    by the single route an operator can open by mistake.
    """
    found = candidates(configured=LAUNCHER, exec_start=LAUNCHER, environment={})

    assert [candidate.path for candidate in found] == [INTERPRETER]


def test_an_operator_may_name_an_interpreter_under_any_other_name() -> None:
    """The gate is about the unit's start program, not about naming conventions."""
    found = candidates(
        configured="/usr/local/bin/py312",
        exec_start=LAUNCHER,
        environment={},
    )

    assert found[0].path == "/usr/local/bin/py312"


def test_a_console_script_offers_the_bin_directory_it_sits_in() -> None:
    """A unit starting an entry point still names the environment holding it."""
    found = candidates(
        configured=None,
        exec_start="/venvs/mini_daemon/bin/reachy-mini-daemon",
        environment={},
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
