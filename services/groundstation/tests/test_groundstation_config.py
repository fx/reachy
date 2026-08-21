"""Configuration: what it refuses, and what it says about itself.

The predecessor's configuration reader was never called, so every override was
silently a default. These tests are written against that: they assert that a
misspelled variable is fatal and names itself, not that a parser exists.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`. Nothing here touches a socket, a clock or a file.
"""

from __future__ import annotations

import pytest

from reachy_groundstation.config import (
    ENV_PREFIX,
    REDACTED_SET,
    SECRET_SETTINGS,
    ConfigurationError,
    Settings,
    load_settings,
    resolved_configuration,
    unrecognised_variables,
)

CREDENTIAL = "example-credential"
MINIMAL = {f"{ENV_PREFIX}CREDENTIAL": CREDENTIAL}


def test_the_credential_is_required() -> None:
    """A groundstation that authenticates nothing must not start."""
    with pytest.raises(ConfigurationError, match="CREDENTIAL"):
        load_settings({})


def test_defaults_apply_when_nothing_is_set() -> None:
    """Everything but the credential has a default, so the dump is complete."""
    settings = load_settings(MINIMAL)
    assert settings.port == 8080
    assert settings.queue_bound == 2
    assert settings.log_format == "json"


def test_a_recognised_variable_takes_effect() -> None:
    """The reader is the only path, so a set value is a value in effect."""
    settings = load_settings({**MINIMAL, f"{ENV_PREFIX}PORT": "9443"})
    assert settings.port == 9443


#:= docs/specs/architecture/index.md#req-009-configuration-is-validated-and-self-reporting
#:% Every component that reads configuration from its environment MUST fail to start
#:% when it encounters a variable matching its own prefix that it does not
#:% recognise, and MUST emit its fully resolved configuration at startup with every
#:% value marked secret replaced by a redacted placeholder.
def test_a_misspelled_variable_is_fatal_and_names_itself() -> None:
    """Startup fails on the typo rather than running on the default."""
    typo = f"{ENV_PREFIX}PROT"
    with pytest.raises(ConfigurationError) as raised:
        load_settings({**MINIMAL, typo: "9443"})
    assert typo in str(raised.value)


def test_the_failure_lists_every_unrecognised_variable() -> None:
    """Two typos are two findings, so a second run is not needed to see them."""
    environ = {**MINIMAL, f"{ENV_PREFIX}PROT": "1", f"{ENV_PREFIX}HSOT": "2"}
    with pytest.raises(ConfigurationError) as raised:
        load_settings(environ)
    message = str(raised.value)
    assert f"{ENV_PREFIX}HSOT" in message
    assert f"{ENV_PREFIX}PROT" in message


def test_a_variable_belonging_to_something_else_is_left_alone() -> None:
    """The prefix is what scopes the check; other components own their own."""
    assert load_settings({**MINIMAL, "REACHY_SATELLITE_PORT": "1"}).port == 8080


def test_unrecognised_variables_are_reported_sorted() -> None:
    """A stable message is one a runbook can quote."""
    environ = {f"{ENV_PREFIX}ZED": "1", f"{ENV_PREFIX}ALPHA": "2", **MINIMAL}
    assert unrecognised_variables(environ) == (
        f"{ENV_PREFIX}ALPHA",
        f"{ENV_PREFIX}ZED",
    )


def test_a_recognised_variable_that_does_not_parse_names_itself() -> None:
    """A port of "eighty" is a configuration error, not a stack trace."""
    with pytest.raises(ConfigurationError) as raised:
        load_settings({**MINIMAL, f"{ENV_PREFIX}PORT": "eighty"})
    assert f"{ENV_PREFIX}PORT" in str(raised.value)


def test_a_value_outside_its_bounds_is_refused() -> None:
    """The constraints are part of the contract, not documentation."""
    with pytest.raises(ConfigurationError, match="QUEUE_BOUND"):
        load_settings({**MINIMAL, f"{ENV_PREFIX}QUEUE_BOUND": "0"})


def test_the_secret_settings_are_derived_from_their_type() -> None:
    """Marking a secret is declaring it as one; there is no second list."""
    assert frozenset({"credential"}) == SECRET_SETTINGS


def test_every_secret_field_is_marked() -> None:
    """A setting added as a secret is redacted without anybody remembering."""
    declared = {
        name
        for name, field in Settings.model_fields.items()
        if "SecretStr" in str(field.annotation)
    }
    assert declared == SECRET_SETTINGS


def test_the_resolved_configuration_reports_the_credential_as_set() -> None:
    """The question answered is whether it is set, never what it is."""
    rendered = resolved_configuration(load_settings(MINIMAL))
    assert rendered["credential"] == REDACTED_SET
    assert CREDENTIAL not in str(rendered)


def test_the_resolved_configuration_includes_the_defaults() -> None:
    """An operator reading it can tell what is in effect, not what was set."""
    rendered = resolved_configuration(load_settings(MINIMAL))
    assert set(rendered) == set(Settings.model_fields)
    assert rendered["host"] == "127.0.0.1"


def test_settings_are_frozen() -> None:
    """Configuration resolved once stays resolved."""
    settings = load_settings(MINIMAL)
    with pytest.raises(ValueError, match="frozen"):
        settings.port = 1  # type: ignore[misc]  # the point of the test
