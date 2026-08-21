"""What the robot's configuration vocabulary accepts, and what it refuses.

reachyctl REQ-053 is a promise about a rejection happening *here* rather than on
the robot, so what is tested is both halves: that a value the robot would refuse
is refused, and that the refusal says what would be accepted instead without
quoting what was not.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import pytest

from reachy_contracts import (
    ROBOT_SETTINGS,
    Setting,
    SettingError,
    SettingKind,
    UnknownSettingError,
    setting_for,
    setting_names,
    validate_setting,
    validate_settings,
)

INTERVAL = "REACHY_SATELLITE_FRAME_INTERVAL_MS"
LEVEL = "REACHY_SATELLITE_LOG_LEVEL"
CREDENTIAL = "REACHY_GROUNDSTATION_CREDENTIAL"
URL = "REACHY_GROUNDSTATION_URL"
STALENESS = "REACHY_SATELLITE_RESULT_STALENESS_SECONDS"


def test_every_declared_setting_has_a_unique_name() -> None:
    """The names are what a declaration is keyed on, so a duplicate is a defect."""
    names = setting_names()

    assert len(names) == len(set(names))
    assert len(names) == len(ROBOT_SETTINGS)


def test_every_declared_setting_states_a_constraint_and_a_purpose() -> None:
    """A setting nobody can be told the shape of is one nobody can set."""
    for setting in ROBOT_SETTINGS:
        assert setting.constraint()
        assert setting.description


def test_a_value_inside_a_declared_range_is_accepted() -> None:
    """The ordinary case, so the refusals below are refusals of something."""
    assert validate_setting(INTERVAL, "100") == "100"


def test_a_value_outside_a_declared_range_is_refused_with_the_constraint() -> None:
    """REQ-053's scenario, at the layer that knows what the range is."""
    with pytest.raises(SettingError) as raised:
        validate_setting(INTERVAL, "12000")

    message = str(raised.value)
    assert INTERVAL in message
    assert "from 20 to 1000" in message


def test_a_refusal_never_quotes_the_value_it_refused() -> None:
    """A setting is exactly where a credential ends up; the name is safe, the value is not."""
    with pytest.raises(SettingError) as raised:
        validate_setting(CREDENTIAL, "")

    assert "example-not-a-real-secret" not in str(raised.value)
    with pytest.raises(SettingError) as second:
        validate_setting(CREDENTIAL, " ")

    assert str(second.value) == str(raised.value)


def test_a_value_that_is_not_a_number_is_refused() -> None:
    """`--frame-interval later` is a typo, not a number the robot would take."""
    with pytest.raises(SettingError, match="whole number"):
        validate_setting(INTERVAL, "soon")


def test_a_real_number_setting_takes_a_fraction_and_bounds_it() -> None:
    """The staleness window is the one setting a whole number would be wrong for."""
    assert validate_setting(STALENESS, "0.5") == "0.5"
    with pytest.raises(SettingError, match=r"from 0\.1 to 10"):
        validate_setting(STALENESS, "60")


def test_a_choice_takes_only_what_it_declares() -> None:
    """And the refusal lists the alternatives, which is the whole remedy."""
    assert validate_setting(LEVEL, "debug") == "debug"
    with pytest.raises(SettingError) as raised:
        validate_setting(LEVEL, "verbose")

    assert "debug, info, warning, error, critical" in str(raised.value)


def test_text_is_checked_against_the_whole_pattern_rather_than_a_prefix() -> None:
    """A URL that merely starts with something plausible is not a session endpoint."""
    assert validate_setting(URL, "ws://192.0.2.10:8000/v1/session")
    with pytest.raises(SettingError, match=URL):
        validate_setting(URL, "http://192.0.2.10:8000/v1/session")


def test_a_value_carrying_a_control_character_is_refused() -> None:
    """A newline would end the systemd directive the value is written into."""
    with pytest.raises(SettingError, match="control character"):
        validate_setting(URL, "ws://192.0.2.10/v1/session\nEnvironment=SNEAK=yes")


def test_a_boolean_is_normalised_to_the_spelling_the_robot_reads() -> None:
    """Declared through a setting built here, because the shipped vocabulary has none yet."""
    setting = Setting(
        name="REACHY_EXAMPLE_FLAG",
        kind=SettingKind.BOOLEAN,
        description="An example, declared by this test.",
    )

    assert setting.validate("Yes") == "true"
    assert setting.validate("OFF") == "false"
    with pytest.raises(SettingError, match="REACHY_EXAMPLE_FLAG"):
        setting.validate("maybe")


def test_an_unbounded_number_states_that_it_is_unbounded() -> None:
    """A constraint line with nothing in it would read as a missing constraint."""
    setting = Setting(
        name="REACHY_EXAMPLE_COUNT",
        kind=SettingKind.INTEGER,
        description="An example, declared by this test.",
    )

    assert setting.constraint() == "a whole number"
    assert setting.validate("-40000") == "-40000"


def test_a_half_bounded_number_names_the_bound_it_has() -> None:
    """Both halves, because each is a different branch of the same sentence."""
    lower = Setting(
        name="REACHY_EXAMPLE_FLOOR",
        kind=SettingKind.NUMBER,
        description="An example, declared by this test.",
        minimum=1.5,
    )
    upper = Setting(
        name="REACHY_EXAMPLE_CEILING",
        kind=SettingKind.NUMBER,
        description="An example, declared by this test.",
        maximum=9,
    )

    assert "at least 1.5" in lower.constraint()
    assert "at most 9" in upper.constraint()
    with pytest.raises(SettingError):
        lower.validate("1.0")
    with pytest.raises(SettingError):
        upper.validate("9.5")


def test_unconstrained_text_says_so() -> None:
    """Rather than reading as a pattern nobody wrote down."""
    setting = Setting(
        name="REACHY_EXAMPLE_NOTE",
        kind=SettingKind.TEXT,
        description="An example, declared by this test.",
    )

    assert setting.constraint().startswith("any text")
    assert setting.validate("anything at all") == "anything at all"


def test_a_name_nothing_declares_is_refused_and_the_message_lists_what_is() -> None:
    """The likeliest cause is a typo, and the remedy is the list."""
    with pytest.raises(UnknownSettingError) as raised:
        setting_for("REACHY_SATELLITE_FRAME_INTERVAL")

    assert INTERVAL in str(raised.value)


def test_a_whole_declaration_is_checked_and_every_problem_is_reported() -> None:
    """One round trip per mistake is what checking locally exists to avoid."""
    with pytest.raises(SettingError) as raised:
        validate_settings(
            {INTERVAL: "12000", LEVEL: "verbose", URL: "ws://192.0.2.10/v1/session"},
        )

    message = str(raised.value)
    assert "2 settings were refused" in message
    assert INTERVAL in message
    assert LEVEL in message


def test_a_declaration_that_is_acceptable_comes_back_normalised_and_ordered() -> None:
    """Name order is what makes two applies of one declaration identical."""
    accepted = validate_settings({LEVEL: "info", INTERVAL: "80"})

    assert list(accepted) == sorted([LEVEL, INTERVAL])
    assert accepted == {LEVEL: "info", INTERVAL: "80"}


def test_an_empty_declaration_is_acceptable() -> None:
    """It declares that nothing should be in force, which `apply` acts on."""
    assert validate_settings({}) == {}


@pytest.mark.parametrize("value", ["nan", "NaN", "inf", "-inf", "Infinity"])
def test_a_non_finite_number_is_refused_by_a_bounded_setting(value: str) -> None:
    """`float("nan")` parses, and every comparison with it is false.

    A bounded setting checking only its two comparisons would therefore accept
    it while reporting a range it is not in. Infinity parses too, and is outside
    every bound there is.

    Args:
        value: The non-finite spelling to refuse.
    """
    with pytest.raises(SettingError, match=STALENESS):
        validate_setting(STALENESS, value)
