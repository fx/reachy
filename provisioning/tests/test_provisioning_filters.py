"""The filters the roles decide with: validation, the change report, and a wheel name.

Everything here is total. A filter that raised would reach an operator as a
templating traceback, and a role could not then report the problem in `--check`
without also stopping — so each of these answers with a record and the task that
called it decides whether that record is a failure.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

from typing import Final

import pytest

from reachy_app import wheel_release
from reachy_managed import settings_change, validated_settings

# RFC 5737 TEST-NET-1 — see the root AGENTS.md on what may enter a tracked file.
ENDPOINT: Final = "ws://192.0.2.10:8000/v1/session"


def test_a_declaration_the_robot_would_accept_comes_back_normalised() -> None:
    """Validating here is what stops a refused value costing a write and a restart."""
    accepted = validated_settings(
        {
            "REACHY_SATELLITE_LOG_LEVEL": "info",
            "REACHY_GROUNDSTATION_URL": ENDPOINT,
        },
    )

    assert accepted["ok"]
    assert accepted["settings"] == {
        "REACHY_GROUNDSTATION_URL": ENDPOINT,
        "REACHY_SATELLITE_LOG_LEVEL": "info",
    }
    assert not accepted["complaint"]


def test_a_value_the_robot_would_refuse_is_refused_without_being_quoted() -> None:
    """A setting is exactly where a credential ends up, so the constraint is reported."""
    refused = validated_settings({"REACHY_SATELLITE_JPEG_QUALITY": "9000"})

    assert not refused["ok"]
    assert refused["settings"] == {}
    assert "REACHY_SATELLITE_JPEG_QUALITY" in refused["complaint"]
    assert "9000" not in refused["complaint"]


def test_a_setting_nothing_declares_is_refused_by_name() -> None:
    """The vocabulary is one declaration; a name outside it is a typo or a withdrawal."""
    refused = validated_settings({"REACHY_NOT_A_SETTING": "1"})

    assert not refused["ok"]
    assert "REACHY_NOT_A_SETTING" in refused["complaint"]


def test_every_offending_setting_is_reported_at_once() -> None:
    """One correction per run against a robot is a run per mistake."""
    refused = validated_settings(
        {
            "REACHY_SATELLITE_JPEG_QUALITY": "9000",
            "REACHY_SATELLITE_LOG_LEVEL": "chatty",
        },
    )

    assert "REACHY_SATELLITE_JPEG_QUALITY" in refused["complaint"]
    assert "REACHY_SATELLITE_LOG_LEVEL" in refused["complaint"]


def test_an_empty_declaration_is_acceptable() -> None:
    """Withdrawing everything is a legitimate apply, and REQ-063 is what it is about."""
    accepted = validated_settings({})

    assert accepted["ok"]
    assert accepted["settings"] == {}


def test_a_withdrawn_setting_is_reported_as_a_removal() -> None:
    """Which is provisioning REQ-063 made visible before the write happens."""
    change = settings_change(
        {"A_SETTING": "1"},
        {"A_SETTING": "1", "B_SETTING": "2"},
    )

    assert change["changes"]
    assert change["removed"] == ["B_SETTING"]
    assert change["unchanged"] == ["A_SETTING"]
    assert change["added"] == []
    assert change["changed"] == []
    assert "1 to remove" in change["summary"]


def test_a_declaration_already_in_the_region_reports_no_change() -> None:
    """The second run of REQ-060, decided before anything is written."""
    change = settings_change({"A_SETTING": "1"}, {"A_SETTING": "1"})

    assert not change["changes"]
    assert "already declares" in change["summary"]


def test_the_change_report_names_settings_and_never_values() -> None:
    """`debug` on the two declarations would print a credential; this cannot."""
    change = settings_change(
        {"REACHY_GROUNDSTATION_CREDENTIAL": "new"},
        {"REACHY_GROUNDSTATION_CREDENTIAL": "old"},
    )

    assert change["changed"] == ["REACHY_GROUNDSTATION_CREDENTIAL"]
    rendered = repr(change)
    assert "new" not in rendered
    assert "old" not in rendered


@pytest.mark.parametrize(
    ("file_name", "distribution", "version"),
    [
        (
            "reachy_mini_ha_satellite-0.1.0-py3-none-any.whl",
            "reachy-mini-ha-satellite",
            "0.1.0",
        ),
        ("some_tool-1.2.3-1-py3-none-any.whl", "some-tool", "1.2.3"),
        ("Mixed_Case-2.0-cp312-cp312-linux_aarch64.whl", "mixed-case", "2.0"),
    ],
)
def test_a_wheel_name_says_what_installing_it_would_put_there(
    file_name: str,
    distribution: str,
    version: str,
) -> None:
    """Which is enough to decide whether to install, and never enough to decide it worked.

    Args:
        file_name: The wheel's file name.
        distribution: The normalised distribution name expected.
        version: The version expected.
    """
    release = wheel_release(file_name)

    assert release["ok"]
    assert release["distribution"] == distribution
    assert release["version"] == version


@pytest.mark.parametrize(
    "file_name",
    [
        "not-a-wheel.tar.gz",
        "missing_tags-1.0.whl",
        "",
        "reachy_mini_ha_satellite-0.1.0-py3-none-any.whl.asc",
    ],
)
def test_a_name_that_is_not_a_wheel_is_reported_rather_than_guessed(
    file_name: str,
) -> None:
    """A guess here would send an arbitrary file to a robot and call it an install.

    Args:
        file_name: The name to refuse.
    """
    release = wheel_release(file_name)

    assert not release["ok"]
    assert release["distribution"] == ""
    assert "wheel" in release["complaint"]
