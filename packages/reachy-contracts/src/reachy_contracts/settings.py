"""The robot's configuration vocabulary, and what each setting will accept.

reachyctl REQ-053 requires a configuration value the receiving component would
refuse to be rejected *before* it reaches the robot. That is only possible if
something on this side of the link knows what the robot accepts, and this module
is that something: one declaration of every setting the robot's daemon
environment carries, with the constraint each one imposes.

It lives in the contracts package for the same reason the wire types do. The
tool validates against it, the provisioning declaration is written in the same
vocabulary, and the application reads the same names — so a constraint declared
here is one constraint rather than three that are free to drift. A second copy
inside `reachyctl` would be a tool that accepts what the robot refuses, which is
the round trip REQ-053 exists to avoid.

**No constraint message ever quotes a value.** A setting is exactly where a
credential ends up, and `reachy_checks.probes` already holds the line that a
check reports which keys differ and never what they hold. The messages here name
the setting and state the constraint, which is what an operator needs and what
reachyctl REQ-059 permits. `Setting.secret` marks the settings whose value must
additionally never be rendered at all, even by a consumer that is only echoing
what the robot reported.

Nothing here is a wire type, so nothing here is a pydantic model: these are
plain declarations a validator walks, and adding one is a line in `ROBOT_SETTINGS`
rather than a schema to publish.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

__all__ = [
    "ROBOT_SETTINGS",
    "Setting",
    "SettingError",
    "SettingKind",
    "UnknownSettingError",
    "setting_for",
    "setting_names",
    "validate_setting",
    "validate_settings",
]


class SettingKind(StrEnum):
    """What kind of value a setting holds.

    Attributes:
        TEXT: Free text, optionally constrained by a pattern.
        INTEGER: A whole number, optionally bounded.
        NUMBER: A real number, optionally bounded.
        BOOLEAN: `true` or `false`.
        CHOICE: One of a fixed set of names.
    """

    TEXT = "text"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    CHOICE = "choice"


class SettingError(ValueError):
    """A configuration value is not one the robot would accept.

    Raised locally, before anything is sent. The message states the constraint
    and never the value.
    """


class UnknownSettingError(SettingError):
    """A setting name nothing declares.

    Its own type rather than a message, because a caller reconciling a
    declaration against this vocabulary wants to distinguish "this name is not
    ours" from "this value is out of range" — the first is a typo or a setting
    that has been withdrawn, the second is a number to change.
    """


# Every value is written into a systemd `Environment=` line, and a line break
# there ends the directive. Rejecting the whole control range rather than the
# newline alone is deliberate: a carriage return, a form feed and a NUL are all
# values that survive being typed into a declaration and none of them survives
# the round trip intact.
_CONTROL_CHARACTERS: Final = re.compile(r"[\x00-\x1f\x7f]")

_TRUE: Final = frozenset({"true", "yes", "on", "1"})
_FALSE: Final = frozenset({"false", "no", "off", "0"})


@dataclass(frozen=True, slots=True, kw_only=True)
class Setting:
    """One setting the robot understands, and what it will accept.

    Attributes:
        name: The setting's name, which is also the environment variable the
            robot reads it from.
        kind: What kind of value it holds.
        description: One line for an operator reading `config` output.
        pattern: For `TEXT`, a regular expression the whole value must match.
            Empty when any text will do.
        minimum: For `INTEGER` and `NUMBER`, the lowest acceptable value.
        maximum: For `INTEGER` and `NUMBER`, the highest acceptable value.
        choices: For `CHOICE`, the acceptable names, in the order to show them.
        secret: Whether the value must never be rendered. A consumer reports
            such a setting as set or unset and never by value — see the module
            documentation.
    """

    name: str
    kind: SettingKind
    description: str
    pattern: str = ""
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    secret: bool = False

    def constraint(self) -> str:
        """State in one line what this setting will accept.

        Returns:
            The constraint, which is what a rejection reports instead of the
            value that was rejected.
        """
        if self.kind is SettingKind.CHOICE:
            return f"one of {', '.join(self.choices)}"
        if self.kind is SettingKind.BOOLEAN:
            return f"{', '.join(sorted(_TRUE))} or {', '.join(sorted(_FALSE))}"
        if self.kind is SettingKind.TEXT:
            return (
                f"text matching {self.pattern}" if self.pattern else "any text"
            ) + " with no control characters"
        bounds = _bounds(self.minimum, self.maximum)
        noun = "a whole number" if self.kind is SettingKind.INTEGER else "a number"
        return f"{noun}{bounds}"

    def validate(self, value: str) -> str:
        """Check a value against this setting and normalise it.

        Args:
            value: What the operator asked for.

        Returns:
            The value as it will be written to the robot. Only `BOOLEAN` is
            rewritten, to the spelling the robot reads; everything else is
            returned unchanged, because normalising further would mean the
            configuration in force did not match what was declared and the
            effective-configuration check would report a difference on every
            run.

        Raises:
            SettingError: If the value is not one the robot would accept. The
                message names this setting and states the constraint; it never
                quotes the value.
        """
        if _CONTROL_CHARACTERS.search(value):
            message = (
                f"{self.name} carries a control character, which a systemd "
                f"environment line cannot hold; {self.constraint()}"
            )
            raise SettingError(message)
        if self.kind is SettingKind.TEXT:
            return self._validate_text(value)
        if self.kind is SettingKind.CHOICE:
            if value not in self.choices:
                raise SettingError(self._refusal())
            return value
        if self.kind is SettingKind.BOOLEAN:
            folded = value.casefold()
            if folded in _TRUE:
                return "true"
            if folded in _FALSE:
                return "false"
            raise SettingError(self._refusal())
        return self._validate_number(value)

    def _validate_text(self, value: str) -> str:
        """Check a text value against this setting's pattern.

        Args:
            value: What the operator asked for.

        Returns:
            The same value.

        Raises:
            SettingError: If a pattern is declared and the value does not match
                the whole of it.
        """
        if self.pattern and not re.fullmatch(self.pattern, value):
            raise SettingError(self._refusal())
        return value

    def _validate_number(self, value: str) -> str:
        """Check a numeric value against this setting's bounds.

        Args:
            value: What the operator asked for.

        Returns:
            The same value.

        Raises:
            SettingError: If it does not parse as this setting's kind, or falls
                outside the declared bounds.
        """
        # A whole number is compared as one. Converting it to a float first
        # would raise `OverflowError` on an integer larger than a float can
        # hold — a refusal, arriving as a crash — and would silently round one
        # merely large enough to lose its last digits.
        number: float | int
        if self.kind is SettingKind.INTEGER:
            try:
                number = int(value, 10)
            except ValueError as error:
                raise SettingError(self._refusal()) from error
        else:
            try:
                number = float(value)
            except ValueError as error:
                raise SettingError(self._refusal()) from error
            # `float("nan")` parses, and every comparison with a NaN is false —
            # so a bounded setting would accept it while reporting a range it is
            # not in. Infinity parses too, and is outside every bound there is.
            if not math.isfinite(number):
                raise SettingError(self._refusal())
        if self.minimum is not None and number < self.minimum:
            raise SettingError(self._refusal())
        if self.maximum is not None and number > self.maximum:
            raise SettingError(self._refusal())
        return value

    def _refusal(self) -> str:
        """Say that a value was refused, and what would be accepted instead.

        Returns:
            The message. The setting's name is a name and is safe to print; the
            value it was given is not, and is deliberately absent.
        """
        return f"{self.name} does not accept that value; it takes {self.constraint()}"


def _bounds(minimum: float | None, maximum: float | None) -> str:
    """Render a numeric setting's bounds for a constraint line.

    Args:
        minimum: The lowest acceptable value, or `None`.
        maximum: The highest acceptable value, or `None`.

    Returns:
        The bounds as a phrase, empty when the setting is unbounded.
    """
    if minimum is not None and maximum is not None:
        return f" from {_number(minimum)} to {_number(maximum)}"
    if minimum is not None:
        return f" of at least {_number(minimum)}"
    if maximum is not None:
        return f" of at most {_number(maximum)}"
    return ""


def _number(value: float) -> str:
    """Render a bound without a trailing `.0` on a whole number.

    Args:
        value: The bound.

    Returns:
        Its text form, so that an integer bound reads as an integer.
    """
    return str(int(value)) if value == int(value) else str(value)


#:= docs/specs/reachyctl/index.md#req-053-configuration-values-are-validated-before-they-are-sent
#:% The tool MUST reject a configuration value that the receiving component would
#:% not accept, before applying it to the robot.
ROBOT_SETTINGS: Final[tuple[Setting, ...]] = (
    Setting(
        name="REACHY_GROUNDSTATION_URL",
        kind=SettingKind.TEXT,
        description="The groundstation's session endpoint the robot opens.",
        pattern=r"wss?://\S+",
    ),
    Setting(
        name="REACHY_GROUNDSTATION_CREDENTIAL",
        kind=SettingKind.TEXT,
        description="The shared secret the robot presents to the groundstation.",
        pattern=r"\S+",
        secret=True,
    ),
    Setting(
        name="REACHY_HOME_ASSISTANT_IDENTITY",
        kind=SettingKind.TEXT,
        description=(
            "The device identity the satellite announces to Home Assistant. "
            "Changing it makes Home Assistant register a second device."
        ),
        pattern=r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}",
    ),
    Setting(
        name="REACHY_SATELLITE_LOG_LEVEL",
        kind=SettingKind.CHOICE,
        description="How much the satellite writes to the robot's journal.",
        choices=("debug", "info", "warning", "error", "critical"),
    ),
    Setting(
        name="REACHY_SATELLITE_FRAME_INTERVAL_MS",
        kind=SettingKind.INTEGER,
        description="How long the satellite waits between camera frames.",
        minimum=20,
        maximum=1000,
    ),
    Setting(
        name="REACHY_SATELLITE_JPEG_QUALITY",
        kind=SettingKind.INTEGER,
        description="How hard the satellite compresses a frame before sending it.",
        minimum=1,
        maximum=100,
    ),
    Setting(
        name="REACHY_SATELLITE_RESULT_STALENESS_SECONDS",
        kind=SettingKind.NUMBER,
        description="How old a perception result may be before it is ignored.",
        minimum=0.1,
        maximum=10.0,
    ),
)

_BY_NAME: Final[Mapping[str, Setting]] = {
    setting.name: setting for setting in ROBOT_SETTINGS
}


def setting_names() -> tuple[str, ...]:
    """List every declared setting, in the order they are shown.

    Returns:
        The names.
    """
    return tuple(setting.name for setting in ROBOT_SETTINGS)


def setting_for(name: str) -> Setting:
    """Look one setting up by name.

    Args:
        name: What the operator wrote.

    Returns:
        The declaration.

    Raises:
        UnknownSettingError: If nothing declares that name. The message lists
            what is declared, because the likeliest cause is a typo or a
            setting that has been withdrawn.
    """
    try:
        return _BY_NAME[name]
    except KeyError as error:
        message = (
            f"no setting named {name!r} is declared; "
            f"declared: {', '.join(setting_names())}"
        )
        raise UnknownSettingError(message) from error


def validate_setting(name: str, value: str) -> str:
    """Check one named setting's value, before anything is sent.

    Args:
        name: The setting's name.
        value: What the operator asked for.

    Returns:
        The value as it will be written to the robot.

    Raises:
        SettingError: If the name is not declared or the value is not one the
            robot would accept.
    """
    return setting_for(name).validate(value)


def validate_settings(declared: Mapping[str, str]) -> dict[str, str]:
    """Check a whole declaration at once, and report every problem in it.

    Every setting is checked rather than stopping at the first, because a
    declaration is applied as one change: reporting one problem, being
    corrected, and then reporting the next costs a round trip per mistake on a
    link this one is deliberately not using yet.

    Args:
        declared: The settings by name.

    Returns:
        The declaration as it will be written to the robot, in name order.

    Raises:
        SettingError: If anything in it is not acceptable. The message names
            every offending setting and states each constraint, and quotes no
            value.
    """
    accepted: dict[str, str] = {}
    problems: list[str] = []
    for name in sorted(declared):
        try:
            accepted[name] = validate_setting(name, declared[name])
        except SettingError as error:
            problems.append(str(error))
    if problems:
        raise SettingError(_joined(problems))
    return accepted


def _joined(problems: Iterable[str]) -> str:
    """Turn several refusals into one message.

    Args:
        problems: One line per offending setting.

    Returns:
        The lines joined, numbered when there is more than one so that a
        message listing several is readable rather than one long sentence.
    """
    lines = list(problems)
    if len(lines) == 1:
        return lines[0]
    listed = "; ".join(f"({index}) {line}" for index, line in enumerate(lines, 1))
    return f"{len(lines)} settings were refused: {listed}"
