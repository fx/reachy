"""The declarations themselves: what is registered, and what it promises.

The identifiers and the remediation strings are a published interface — the
troubleshooting runbook is keyed to them and quotes them rather than restating
them — so the properties asserted here are the ones a consumer relies on
without being able to see this file: names are unique and stable in shape,
every check can say how to fix itself, and nothing in the text belongs to
anybody's environment.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import re
from typing import Final, TypeAliasType, get_type_hints

import pytest

import reachy_checks
from reachy_checks import (
    APPLICATION_INSTALLED,
    APPLICATION_RUNNING,
    CHECKS,
    CONFIGURATION_EFFECTIVE,
    DAEMON_REACHABLE,
    GROUNDSTATION_CAPABILITIES,
    GROUNDSTATION_ROUND_TRIP,
    GROUNDSTATION_SESSION,
    HOME_ASSISTANT_IDENTITY,
    MODEL_FILES,
    Check,
    Remediation,
    check_by_identifier,
    identifiers,
)

# Dotted segments of lowercase words, hyphenated inside a segment. Asserted
# rather than merely intended, because the runbook greps for these.
_SHAPE: Final = re.compile(r"[a-z]+(-[a-z]+)*(\.[a-z]+(-[a-z]+)*)+")

# The chain, in the order it is walked. Written out rather than derived from
# `CHECKS`, so that adding, removing or reordering a check is a visible edit
# here and a reviewer is asked whether the runbook moved with it.
_EXPECTED: Final = (
    DAEMON_REACHABLE,
    APPLICATION_INSTALLED,
    APPLICATION_RUNNING,
    GROUNDSTATION_SESSION,
    GROUNDSTATION_CAPABILITIES,
    GROUNDSTATION_ROUND_TRIP,
    MODEL_FILES,
    CONFIGURATION_EFFECTIVE,
    HOME_ASSISTANT_IDENTITY,
)


def test_the_registry_is_the_chain_in_order() -> None:
    """A check added, removed or moved is a deliberate edit, not a surprise."""
    assert identifiers() == _EXPECTED


def test_every_identifier_is_unique() -> None:
    """Two checks under one name would make the runbook ambiguous."""
    assert len(set(identifiers())) == len(_EXPECTED)


def test_every_identifier_keeps_the_published_shape() -> None:
    """Greppable means predictable: lowercase, dotted, hyphens inside a segment."""
    for identifier in identifiers():
        assert _SHAPE.fullmatch(identifier), identifier


def test_every_check_can_say_how_to_fix_itself() -> None:
    """Every failing check reports a remediation, which is reachyctl REQ-055."""
    for check in CHECKS:
        assert check.remediation.explanation.strip()
        assert check.description.strip()
        assert check.requires


def test_the_checks_that_have_a_command_name_a_real_one() -> None:
    """A runnable command wherever one exists, and nothing invented where none does."""
    with_commands = {
        check.identifier: check.remediation.command
        for check in CHECKS
        if check.remediation.command
    }

    assert with_commands == {
        APPLICATION_INSTALLED: "reachyctl deploy",
        APPLICATION_RUNNING: "reachyctl app start",
        MODEL_FILES: (
            "python -m reachy_groundstation.models.fetch "
            '"$REACHY_GROUNDSTATION_MODELS_DIR"'
        ),
        CONFIGURATION_EFFECTIVE: "reachyctl config apply",
        HOME_ASSISTANT_IDENTITY: "reachyctl config apply",
    }


def test_a_check_without_a_command_still_says_what_to_do() -> None:
    """Saying there is no command beats naming one that does not exist."""
    for check in CHECKS:
        if check.remediation.command:
            continue
        assert len(check.remediation.explanation) > 40, check.identifier


def test_nothing_in_the_registry_names_anybody_s_environment() -> None:
    """This repository is public; see the root AGENTS.md."""
    text = " ".join(
        f"{check.identifier} {check.description} {check.remediation.render()}"
        for check in CHECKS
    )

    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text)
    assert "@" not in text
    assert "http://" not in text
    assert "https://" not in text


def test_a_check_is_found_by_the_name_the_runbook_quotes() -> None:
    """The runbook holds identifiers, so looking one up has to work."""
    assert check_by_identifier(DAEMON_REACHABLE).identifier == DAEMON_REACHABLE


def test_an_unknown_name_says_what_is_registered() -> None:
    """The likeliest cause is a runbook quoting a name that was renamed."""
    with pytest.raises(KeyError, match=DAEMON_REACHABLE):
        check_by_identifier("daemon.reachible")


def test_a_remediation_with_a_command_renders_both_halves() -> None:
    """An operator reading one line gets the reason and the thing to type."""
    remediation = Remediation(explanation="Start it.", command="reachyctl app start")

    assert remediation.render() == "Start it. Run: reachyctl app start"


def test_a_remediation_without_a_command_renders_only_the_explanation() -> None:
    """No trailing "Run:" with nothing after it."""
    assert Remediation(explanation="Check the cable.").render() == "Check the cable."


def test_every_exported_type_alias_can_be_evaluated() -> None:
    """A PEP 695 alias is lazy, and a public one that raises is a trap for a consumer.

    The right-hand side of a `type` statement is evaluated on first access, so
    an alias mentioning a name imported only under `TYPE_CHECKING` raises
    `NameError` the moment anything looks at it — `__value__`, or any tool that
    introspects the module. `Probe` is what a consumer writes a check against
    and this package is imported as a module by the provisioning verification
    role, so it has to survive being looked at.

    Written over `__all__` rather than naming `Probe`, so an alias added later
    is covered without anybody remembering this rule.
    """
    aliases = {
        name: exported
        for name in reachy_checks.__all__
        if isinstance(exported := getattr(reachy_checks, name), TypeAliasType)
    }

    # Without this the loop below would pass over nothing on the day someone
    # removes the last alias, and the guard would quietly stop guarding.
    assert aliases, "no exported type alias found; this guard is checking nothing"
    for name, alias in aliases.items():
        assert alias.__value__ is not None, name


def test_the_check_declaration_can_be_introspected() -> None:
    """A consumer reading the type of a check gets types rather than a NameError."""
    hints = get_type_hints(Check)

    assert set(hints) == {
        "identifier",
        "description",
        "requires",
        "probe",
        "remediation",
    }
