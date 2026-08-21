"""One document saying what a robot is supposed to be, read by two commands.

`doctor --intent` asserts against it and `config --declaration` applies it, and
they read the same file because two documents describing one robot are two
documents that will disagree. The shape is deliberately small — the settings
that are supposed to be in force, and the identity the satellite is supposed to
announce — because provisioning holds the authoritative declaration and anything
richer here would be a second schema to reconcile with it.

The two keys overlap in exactly one place, and this module refuses the overlap
rather than picking a winner: `announced_identity` is what `doctor` compares
against what the satellite announces, and `REACHY_HOME_ASSISTANT_IDENTITY` is
the setting that makes it announce that. A document declaring both differently
describes a robot that cannot exist, and the failure it would otherwise produce
— an apply that succeeds, followed by a `doctor` that fails — is one an operator
would spend an afternoon on.

**A message may name a setting and never says what it holds.** A key is a name;
a value is exactly where a credential ends up. Every refusal below names the
offending key and reports its value's *type* at most.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

from reachy_checks import Intent
from reachyctl.credentials import ENV_PREFIX
from reachyctl.errors import ConfigurationError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

__all__ = [
    "DECLARATION_VARIABLE",
    "IDENTITY_SETTING",
    "INTENT_VARIABLE",
    "load_declaration",
    "load_intent",
]

# Where the declared intent is, when it is not given on the command line.
INTENT_VARIABLE: Final = f"{ENV_PREFIX}INTENT_FILE"

# The same document, named for what `config` does with it.
DECLARATION_VARIABLE: Final = f"{ENV_PREFIX}DECLARATION_FILE"

# The setting that makes the satellite announce an identity. Named here because
# this module is where the two ways of saying it are reconciled.
IDENTITY_SETTING: Final = "REACHY_HOME_ASSISTANT_IDENTITY"

_INTENT_KEYS: Final = frozenset({"configuration", "announced_identity"})


def _read(path: Path) -> str:
    """Read a declaration document.

    Nothing but the read, so that what a failure means stays `load_intent`'s to
    decide for whatever reader it was given.

    Args:
        path: Where the document is.

    Returns:
        Its contents.

    Raises:
        OSError: If the file cannot be read.
    """
    return path.read_text(encoding="utf-8")


def load_intent(
    path: Path,
    read: Callable[[Path], str] = _read,
) -> Intent:
    """Read what the robot is supposed to be from a declaration.

    Args:
        path: Where the document is.
        read: How to read it. Injected so the parsing rules are exercised
            without performing any input.

    Returns:
        The declared intent.

    Raises:
        ConfigurationError: If the file cannot be read, is not JSON, is not an
            object, carries a key this does not understand, holds a
            configuration that is not a mapping of strings to strings, or
            declares an announced identity that disagrees with the setting
            which produces one. A message may name a setting's key and never
            names what that setting holds.
    """
    try:
        content = read(path)
    except OSError as error:
        reason = error.strerror or type(error).__name__
        message = f"the intent document {path} could not be read: {reason}"
        raise ConfigurationError(message) from error
    try:
        document = json.loads(content)
    except ValueError as error:
        message = (
            f"the intent document {path} is not JSON: {type(error).__name__} "
            f"at position {getattr(error, 'pos', 'unknown')}"
        )
        raise ConfigurationError(message) from error
    if not isinstance(document, dict):
        message = (
            f"the intent document {path} is a "
            f"{type(document).__name__}; it must be an object with the keys "
            f"{sorted(_INTENT_KEYS)}"
        )
        raise ConfigurationError(message)
    unknown = sorted(set(document) - _INTENT_KEYS)
    if unknown:
        message = (
            f"the intent document {path} carries {unknown}, which this "
            f"command does not understand; it reads {sorted(_INTENT_KEYS)}"
        )
        raise ConfigurationError(message)
    configuration = _configuration(document.get("configuration", {}), path)
    identity = _identity(document.get("announced_identity"), path)
    _agree(configuration, identity, path)
    return Intent(configuration=configuration, announced_identity=identity)


def load_declaration(
    path: Path,
    read: Callable[[Path], str] = _read,
) -> Mapping[str, str]:
    """Read the settings a document says are supposed to be in force.

    The same document `--intent` reads, and the same parsing: `config` applies
    the settings and `doctor` asserts them, so a robot cannot be configured
    from one file and diagnosed against another.

    Args:
        path: Where the document is.
        read: How to read it.

    Returns:
        The settings by name. Whether the robot would accept each value is
        `reachy_contracts.validate_settings`' question, asked by the command
        before it contacts anything.

    Raises:
        ConfigurationError: For any of the reasons `load_intent` refuses a
            document.
    """
    return load_intent(path, read).configuration


def _agree(
    configuration: Mapping[str, str],
    identity: str | None,
    path: Path,
) -> None:
    """Refuse a document that says the identity twice, differently.

    Args:
        configuration: The declared settings.
        identity: The declared announced identity, or `None`.
        path: Where the document is, for the message.

    Raises:
        ConfigurationError: If both are declared and they differ. Both values
            are named here, and that is the one deliberate exception to the
            rule above: an identity is a device name whose whole purpose is to
            be recognisable, `reachy_checks.probes` already reports it
            verbatim for that reason, and a message about two identities that
            named neither would be unactionable.
    """
    declared = configuration.get(IDENTITY_SETTING)
    if identity is None or declared is None or declared == identity:
        return
    message = (
        f"the intent document {path} declares announced_identity "
        f"{identity!r} and {IDENTITY_SETTING} {declared!r}; the setting is "
        f"what makes the satellite announce an identity and the other is what "
        f"is asserted about it, so a robot matching both cannot exist"
    )
    raise ConfigurationError(message)


def _configuration(value: object, path: Path) -> Mapping[str, str]:
    """Read the declared settings out of an intent document.

    Args:
        value: What the document held under `configuration`.
        path: Where the document is, for the message.

    Returns:
        The settings by name.

    Raises:
        ConfigurationError: If it is not a mapping of strings to strings. The
            offending key is named and its value never is — a key is a
            setting's *name* and is safe to print, where a value is exactly
            where a credential ends up. A key that is not a string is reported
            by its type rather than by itself, because an object of any kind at
            all could be there and its `repr` is not something this message can
            vouch for.
    """
    if not isinstance(value, dict):
        message = (
            f"the intent document {path} declares a configuration that is a "
            f"{type(value).__name__}; it must be an object of setting names to "
            f"values"
        )
        raise ConfigurationError(message)
    settings: dict[str, str] = {}
    for name, setting in value.items():
        if not isinstance(name, str):
            message = (
                f"the intent document {path} declares a setting name of type "
                f"{type(name).__name__}; every setting name must be a string"
            )
            raise ConfigurationError(message)
        if not isinstance(setting, str):
            # The name is quoted and the value is not, and the asymmetry is the
            # point: naming which setting is wrong is what makes the message
            # actionable on a configuration of any size, and printing what it
            # holds is how a credential reaches the output.
            message = (
                f"the intent document {path} declares the setting {name!r} "
                f"with a value of type {type(setting).__name__}; every setting "
                f"value must be a string"
            )
            raise ConfigurationError(message)
        settings[name] = setting
    return settings


def _identity(value: object, path: Path) -> str | None:
    """Read the declared announced identity out of an intent document.

    Args:
        value: What the document held under `announced_identity`.
        path: Where the document is, for the message.

    Returns:
        The identity, or `None` when the document declares none.

    Raises:
        ConfigurationError: If it is present and is not a non-empty string.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        # The type rather than the value, for the same reason as a setting
        # name that is not a string: what is there could be any object at all.
        message = (
            f"the intent document {path} declares an announced identity that "
            f"is not a non-empty string (it is of type "
            f"{type(value).__name__})"
        )
        raise ConfigurationError(message)
    return value
