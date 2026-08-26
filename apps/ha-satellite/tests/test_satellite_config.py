"""Configuration: what it refuses, where each value came from, and what it hides.

Four things are under test here and they are not the same thing.

**Refusing.** A variable under this application's prefix that names no setting
is fatal and says which one it was — architecture REQ-009, and the direct remedy
for the predecessor bug where every override was inert because the function
reading them was never called. The announced identity being unset is fatal too,
and its message has to explain *why* rather than merely that it is missing.

**Layering.** Defaults, then the environment, then the overrides the settings
interface writes — in that order, so REQ-049 holds for a setting somebody has
also exported.

**Hiding.** A secret is reported as set or unset on every surface and by value on
none, including when its value contains characters that survive escaping.

**Coherence.** A configuration that parses and then produces a robot which
silently never tracks anything is refused at startup instead.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from satellite_support import address_of_length

from reachy_contracts.settings import SESSION_URL_MAX_LENGTH
from reachy_mini_ha_satellite.config import (
    BOOTSTRAP_SETTINGS,
    COMPATIBILITY_SETTINGS,
    ENV_PREFIX,
    GROUNDSTATION_URL_MAX_LENGTH,
    GROUNDSTATION_URL_SETTING,
    IDENTITY_SETTING,
    LIVE_SETTINGS,
    OVERRIDES_FILENAME,
    SECRET_SETTINGS,
    ConfigurationError,
    OverrideStore,
    Settings,
    SettingSource,
    apply_settings_change,
    canonical_string,
    configuration_report,
    declared_elsewhere,
    load_settings,
    log_resolved_configuration,
    overrides_path,
    resolved_configuration,
    setting_names,
    state_directory,
    unrecognised_variables,
    validate_groundstation_url_length,
    variable_for,
)
from reachy_mini_ha_satellite.timing import MIN_BEHAVIOUR_TICK_SECONDS

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pyfakefs.fake_filesystem import FakeFilesystem

# A placeholder credential carrying every character that changes shape when
# something escapes it: a tab, a newline and a backslash. Never anybody's — see
# the root AGENTS.md on what may enter a tracked file in a public repository.
AWKWARD_CREDENTIAL = "ex\tam\nple\\credential"

# The smallest environment that resolves. Face tracking is off, so no
# groundstation address and no local model are needed — the coherence rules that
# demand them have tests of their own below.
MINIMAL = {
    f"{ENV_PREFIX}DEVICE_NAME": "reachy-mini-1",
    f"{ENV_PREFIX}FACE_TRACKING_ENABLED": "false",
}


class TestRefusingAnUnusableEnvironment:
    """Architecture REQ-009's first half: a typo is a startup failure."""

    def test_a_misspelled_variable_is_fatal_and_names_itself(self) -> None:
        """The whole point: an override nobody read is not a thing that happens."""
        environ = {**MINIMAL, f"{ENV_PREFIX}WEB_PROT": "9000"}

        with pytest.raises(ConfigurationError) as raised:
            load_settings(environ, {})

        assert f"{ENV_PREFIX}WEB_PROT" in str(raised.value)

    def test_the_refusal_lists_what_it_would_have_accepted(self) -> None:
        """So the fix is in the message rather than in the source."""
        with pytest.raises(ConfigurationError) as raised:
            load_settings({**MINIMAL, f"{ENV_PREFIX}NONSENSE": "1"}, {})

        assert variable_for("web_port") in str(raised.value)

    def test_a_variable_under_another_prefix_is_left_alone(self) -> None:
        """This application does not police the groundstation's environment."""
        resolution = load_settings(
            {**MINIMAL, "REACHY_GROUNDSTATION_CREDENTIAL": "not ours"},
            {},
        )

        assert resolution.settings.device_name == "reachy-mini-1"

    def test_unrecognised_variables_are_reported_in_a_stable_order(self) -> None:
        """A message that reordered itself would be a message nobody diffs."""
        found = unrecognised_variables(
            {f"{ENV_PREFIX}ZEBRA": "1", f"{ENV_PREFIX}ALPHA": "1"},
        )

        assert found == (f"{ENV_PREFIX}ALPHA", f"{ENV_PREFIX}ZEBRA")

    def test_a_value_that_does_not_parse_names_its_variable(self) -> None:
        """And nothing else: a pydantic error also carries the rejected input."""
        with pytest.raises(ConfigurationError) as raised:
            load_settings({**MINIMAL, f"{ENV_PREFIX}WEB_PORT": "not a port"}, {})

        assert variable_for("web_port") in str(raised.value)

    def test_behaviour_tick_has_one_supported_runtime_minimum(self) -> None:
        """Live cadence and fixed history capacity depend on the same floor."""
        tick = variable_for("behaviour_tick_seconds")
        accepted = load_settings(
            {**MINIMAL, tick: str(MIN_BEHAVIOUR_TICK_SECONDS)},
            {},
        )

        assert accepted.settings.behaviour_tick_seconds == MIN_BEHAVIOUR_TICK_SECONDS
        with pytest.raises(ConfigurationError):
            load_settings(
                {**MINIMAL, tick: str(MIN_BEHAVIOUR_TICK_SECONDS / 2.0)},
                {},
            )


class TestTheSharedVocabularyIsNotATypo:
    """The other half of REQ-009: "does not recognise" is not "does not read".

    `reachy_contracts.settings.ROBOT_SETTINGS` is the one declaration of the
    robot's daemon environment, and some of its names fall under this
    application's prefix without being settings this application consumes. An
    operator running `reachyctl config apply` with the documented vocabulary
    gets those on the robot. Treating them as typos would mean the satellite
    refused to start on a correctly-configured robot, which is a worse failure
    than the one the requirement exists to catch.
    """

    def test_a_declared_name_this_application_does_not_read_is_not_fatal(
        self,
    ) -> None:
        """Otherwise the documented vocabulary would be a boot failure."""
        declared = sorted(declared_elsewhere())
        assert declared, "the fixture depends on there being at least one"

        resolution = load_settings({**MINIMAL, declared[0]: "40"}, {})

        assert resolution.settings.device_name == "reachy-mini-1"

    def test_it_is_reported_rather_than_silently_ignored(self) -> None:
        """A variable that quietly does nothing needs to say so somewhere."""
        declared = sorted(declared_elsewhere())

        resolution = load_settings({**MINIMAL, declared[0]: "40"}, {})

        assert resolution.declared_but_unread == (declared[0],)

    def test_a_declared_name_that_is_not_set_is_not_reported(self) -> None:
        """The report is about this environment, not about the vocabulary."""
        resolution = load_settings(MINIMAL, {})

        assert resolution.declared_but_unread == ()

    def test_a_typo_is_still_fatal(self) -> None:
        """The tolerance is for the vocabulary, not for anything prefixed."""
        with pytest.raises(ConfigurationError):
            load_settings({**MINIMAL, f"{ENV_PREFIX}WEB_PROT": "9000"}, {})

    def test_no_declared_name_collides_with_a_setting_this_reads(self) -> None:
        """A collision would mean one name with two meanings on one robot.

        `declared_elsewhere` subtracts what this application consumes, so a
        name added to `ROBOT_SETTINGS` that this application also reads would
        vanish from the report rather than announce the clash. This asserts the
        clash does not exist, which is what makes the subtraction safe.
        """
        consumed = {variable_for(name) for name in setting_names()}

        assert not (consumed & set(declared_elsewhere()))

    def test_the_boot_log_names_it(self, caplog: pytest.LogCaptureFixture) -> None:
        """REQ-009 asks the resolved configuration to be emitted, not just held."""
        declared = sorted(declared_elsewhere())
        resolution = load_settings({**MINIMAL, declared[0]: "40"}, {})

        with caplog.at_level(logging.WARNING):
            log_resolved_configuration(resolution)

        assert declared[0] in caplog.text


class TestTheAnnouncedIdentity:
    """ha-satellite REQ-040: no default, and a refusal that explains why."""

    def test_startup_fails_when_the_identity_is_unset(self) -> None:
        """There is no derived default, so there is nothing to fall back to."""
        with pytest.raises(ConfigurationError) as raised:
            load_settings({}, {})

        assert variable_for(IDENTITY_SETTING) in str(raised.value)

    def test_the_refusal_explains_the_hazard_rather_than_the_omission(self) -> None:
        """A message saying only "missing" would invite somebody to invent one."""
        with pytest.raises(ConfigurationError) as raised:
            load_settings({}, {})

        message = str(raised.value)
        assert "registers a new one" in message
        assert "history detaches" in message
        assert "repackaged" in message

    def test_a_blank_identity_is_refused_like_an_absent_one(self) -> None:
        """Because an operator who cleared it did not mean to disable the check."""
        with pytest.raises(ConfigurationError):
            load_settings({f"{ENV_PREFIX}DEVICE_NAME": "   "}, {})

    def test_the_model_declares_no_default_for_it(self) -> None:
        """Stated here so that adding one is a red run rather than a review miss."""
        assert Settings.model_fields[IDENTITY_SETTING].is_required()

    def test_the_display_name_falls_back_to_the_announced_one(self) -> None:
        """A display name is safe to change; the announced identity is not."""
        resolution = load_settings(MINIMAL, {})

        assert resolution.settings.announced_friendly_name == "reachy-mini-1"

    def test_a_configured_display_name_is_used(self) -> None:
        """Home Assistant renames the device rather than replacing it."""
        resolution = load_settings(
            {**MINIMAL, f"{ENV_PREFIX}FRIENDLY_NAME": "Desk robot"},
            {},
        )

        assert resolution.settings.announced_friendly_name == "Desk robot"


class TestTheLayers:
    """Defaults, then the environment, then what the settings page wrote."""

    def test_a_default_reports_itself_as_a_default(self) -> None:
        """Which is what makes the resolved dump worth reading."""
        resolution = load_settings(MINIMAL, {})

        assert resolution.sources["web_port"] is SettingSource.DEFAULT
        assert resolution.settings.web_port == Settings.model_fields["web_port"].default

    def test_the_environment_wins_over_the_default(self) -> None:
        """The ordinary case."""
        resolution = load_settings({**MINIMAL, f"{ENV_PREFIX}WEB_PORT": "9000"}, {})

        assert resolution.settings.web_port == 9000
        assert resolution.sources["web_port"] is SettingSource.ENVIRONMENT

    def test_an_override_wins_over_the_environment(self) -> None:
        """REQ-049 would otherwise be false for any exported setting."""
        resolution = load_settings(
            {**MINIMAL, f"{ENV_PREFIX}API_PORT": "9000"},
            {"api_port": "9100"},
        )

        assert resolution.settings.api_port == 9100
        assert resolution.sources["api_port"] is SettingSource.OVERRIDE

    def test_an_override_can_supply_the_identity_on_its_own(self) -> None:
        """A robot configured entirely from its own settings page still starts."""
        resolution = load_settings(
            {f"{ENV_PREFIX}FACE_TRACKING_ENABLED": "false"},
            {"device_name": "reachy-mini-2"},
        )

        assert resolution.settings.device_name == "reachy-mini-2"

    def test_an_override_naming_nothing_is_reported_rather_than_fatal(self) -> None:
        """The file is written by this application, not typed by an operator.

        A key left behind by an upgrade must not be the reason a robot stops
        booting — but it must not be silent either, so it is reported.
        """
        resolution = load_settings(MINIMAL, {"web_prot": "9000", "api_port": "9100"})

        assert resolution.ignored_overrides == ("web_prot",)
        assert resolution.settings.api_port == 9100

    def test_an_override_equal_to_the_environment_still_reports_as_an_override(
        self,
    ) -> None:
        """The layer that supplied it is a fact about the file, not about the value."""
        resolution = load_settings(
            {**MINIMAL, f"{ENV_PREFIX}API_PORT": "9000"},
            {"api_port": "9000"},
        )

        assert resolution.sources["api_port"] is SettingSource.OVERRIDE


class TestSecretsAreNeverReported:
    """One place marks a setting secret, and every surface reads it."""

    def test_the_credential_is_the_only_secret(self) -> None:
        """Pinned so that adding a secret is a deliberate edit here as well."""
        assert frozenset({"groundstation_credential"}) == SECRET_SETTINGS

    def test_an_unset_secret_reports_as_unset(self) -> None:
        """Distinguishing "nothing configured" from "something configured"."""
        rendered = resolved_configuration(load_settings(MINIMAL, {}).settings)

        assert rendered["groundstation_credential"] == "<unset>"

    def test_a_set_secret_reports_as_set(self) -> None:
        """Which is what makes the page usable for rotation."""
        resolution = load_settings(
            {**MINIMAL, f"{ENV_PREFIX}GROUNDSTATION_CREDENTIAL": AWKWARD_CREDENTIAL},
            {},
        )

        rendered = resolved_configuration(resolution.settings)

        assert rendered["groundstation_credential"] == "<set>"

    def test_no_rendering_of_the_configuration_carries_the_credential(self) -> None:
        """Raw, escaped, or repr'd — the value never reaches a renderer at all.

        The three spellings matter because a value transformed before a
        redactor sees it no longer matches and leaks in its transformed form.
        Here the ordering is the other way round: redaction happens first, so
        there is no transformed spelling to miss.
        """
        resolution = load_settings(
            {**MINIMAL, f"{ENV_PREFIX}GROUNDSTATION_CREDENTIAL": AWKWARD_CREDENTIAL},
            {},
        )

        renderings = [
            repr(resolved_configuration(resolution.settings)),
            json.dumps(resolved_configuration(resolution.settings)),
            repr(configuration_report(resolution)),
            repr(resolution.settings),
        ]

        for rendering in renderings:
            for spelling in _spellings(AWKWARD_CREDENTIAL):
                assert spelling not in rendering

    def test_the_boot_log_reports_the_secret_as_set_and_not_by_value(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Architecture REQ-009's second half, on the surface an operator reads.

        Args:
            caplog: Captures what the boot log emitted.
        """
        resolution = load_settings(
            {**MINIMAL, f"{ENV_PREFIX}GROUNDSTATION_CREDENTIAL": AWKWARD_CREDENTIAL},
            {},
        )

        with caplog.at_level(logging.INFO):
            log_resolved_configuration(resolution)

        emitted = caplog.text
        assert "groundstation_credential=<set>" in emitted
        for spelling in _spellings(AWKWARD_CREDENTIAL):
            assert spelling not in emitted

    def test_the_boot_log_reports_every_setting_including_the_defaults(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A dump that omitted the defaults would not answer "what is it running on".

        Args:
            caplog: Captures what the boot log emitted.
        """
        with caplog.at_level(logging.INFO):
            log_resolved_configuration(load_settings(MINIMAL, {}))

        for name in setting_names():
            assert f"configuration.resolved {name}=" in caplog.text

    def test_a_stale_override_is_warned_about_at_boot(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Dropped, but not quietly.

        Args:
            caplog: Captures what the boot log emitted.
        """
        with caplog.at_level(logging.INFO):
            log_resolved_configuration(load_settings(MINIMAL, {"web_prot": "9000"}))

        assert "web_prot" in caplog.text


class TestCoherence:
    """Combinations that parse and then produce a robot that does nothing."""

    def test_remote_detection_without_an_address_is_refused(self) -> None:
        """Rather than a robot that silently never tracks anything."""
        with pytest.raises(ConfigurationError) as raised:
            load_settings({f"{ENV_PREFIX}DEVICE_NAME": "reachy-mini-1"}, {})

        assert variable_for("groundstation_url") in str(raised.value)

    def test_local_detection_without_a_model_is_refused(self) -> None:
        """The weights are not in the wheel, so the path has to be supplied."""
        environ = {
            f"{ENV_PREFIX}DEVICE_NAME": "reachy-mini-1",
            f"{ENV_PREFIX}DETECTION_SOURCE": "local",
        }

        with pytest.raises(ConfigurationError) as raised:
            load_settings(environ, {})

        assert variable_for("local_model_path") in str(raised.value)

    def test_the_fallback_selection_needs_both(self) -> None:
        """It is the selection that can use either source, so it needs either."""
        environ = {
            f"{ENV_PREFIX}DEVICE_NAME": "reachy-mini-1",
            f"{ENV_PREFIX}DETECTION_SOURCE": "remote_with_local_fallback",
            f"{ENV_PREFIX}GROUNDSTATION_URL": "ws://192.0.2.10:8080/v1/session",
            f"{ENV_PREFIX}GROUNDSTATION_CREDENTIAL": "example-credential",
        }

        with pytest.raises(ConfigurationError) as raised:
            load_settings(environ, {})

        assert variable_for("local_model_path") in str(raised.value)

    def test_switching_tracking_off_needs_neither(self) -> None:
        """Which is the configuration for a robot with no cores to spare."""
        resolution = load_settings(MINIMAL, {})

        assert not resolution.settings.face_tracking_enabled

    def test_a_configured_groundstation_resolves(self) -> None:
        """The ordinary deployment, with an address from the documentation range."""
        environ = {
            f"{ENV_PREFIX}DEVICE_NAME": "reachy-mini-1",
            f"{ENV_PREFIX}GROUNDSTATION_URL": "ws://192.0.2.10:8080/v1/session",
            f"{ENV_PREFIX}GROUNDSTATION_CREDENTIAL": "example-credential",
        }

        resolution = load_settings(environ, {})

        assert resolution.settings.groundstation_url.startswith("ws://192.0.2.10")


class TestWhatTheInterfaceCanChange:
    """`LIVE_SETTINGS` is a claim about the code, so it is pinned to the model."""

    def test_every_live_setting_is_a_real_setting(self) -> None:
        """A renamed field would otherwise make a live setting quietly dead."""
        assert set(setting_names()) >= LIVE_SETTINGS

    def test_no_secret_is_claimed_to_apply_at_once(self) -> None:
        """The credential is read when a session is opened, which is at startup."""
        assert not (LIVE_SETTINGS & SECRET_SETTINGS)

    def test_the_speaker_boost_applies_at_once(self) -> None:
        """Both outputs read it per pushed chunk, so it needs no restart.

        It is also what the Home Assistant control writes through, and a
        control that reported "applied" while nothing adopted the value would
        be worse than no control.
        """
        assert "speaker_boost_percent" in LIVE_SETTINGS

    def test_the_report_marks_each_setting_live_or_not(self) -> None:
        """Because the page tells an operator which changes need a restart."""
        report = {
            row.name: row for row in configuration_report(load_settings(MINIMAL, {}))
        }

        assert report["log_level"].live
        assert not report["api_port"].live

    def test_the_report_covers_every_setting(self) -> None:
        """REQ-049 says *every* operator-facing setting, so the page shows all."""
        report = configuration_report(load_settings(MINIMAL, {}))

        assert tuple(row.name for row in report) == setting_names()


class _RecordingStore(OverrideStore):
    """An overrides store that records what it was asked to write.

    A real store over a fake filesystem would work too, and would test the
    filesystem rather than the ordering these tests are about. Subclassed rather
    than faked because the ordering is the whole subject: what must be shown is
    that `save` was never *called*, not that no bytes reached a disk.
    """

    def __init__(self) -> None:
        """Start having been asked to write nothing."""
        OverrideStore.__init__(self, Path("/nowhere/settings.json"))
        self.saved: list[dict[str, str]] = []
        self.stored: dict[str, str] = {}

    def load(self) -> dict[str, str]:
        """Report what was last written.

        Returns:
            The overrides, which are empty until something saves.
        """
        return dict(self.stored)

    def save(self, overrides: Mapping[str, str]) -> None:
        """Record a write instead of performing one.

        Args:
            overrides: What was to be written.
        """
        self.saved.append(dict(overrides))
        self.stored = dict(overrides)


class TestApplyingASettingsChange:
    """One definition of it, so two surfaces cannot do it in two orders."""

    def test_it_resolves_before_it_writes(self) -> None:
        """A submission that would not start the robot must not become the file."""
        store = _RecordingStore()

        with pytest.raises(ConfigurationError):
            apply_settings_change(
                {"api_port": "not a port"},
                store=store,
                environ=MINIMAL,
            )

        assert store.saved == []

    def test_it_writes_and_then_adopts_what_can_be_adopted(self) -> None:
        """The order the settings page has always used, now in one place."""
        store = _RecordingStore()
        adopted: list[Settings] = []

        resolved = apply_settings_change(
            {"speaker_boost_percent": "620.0"},
            store=store,
            environ=MINIMAL,
            apply_live=adopted.append,
        )

        assert store.saved == [{"speaker_boost_percent": "620.0"}]
        assert resolved.settings.speaker_boost_percent == pytest.approx(620.0)
        assert [settings.speaker_boost_percent for settings in adopted] == [
            pytest.approx(620.0),
        ]

    def test_nothing_is_adopted_when_nothing_is_running(self) -> None:
        """The settings interface can be served before the application exists."""
        store = _RecordingStore()

        resolved = apply_settings_change({}, store=store, environ=MINIMAL)

        assert store.saved == [{}]
        assert resolved.settings.device_name == "reachy-mini-1"


class TestTheOverrideStore:
    """Where a change made from the settings page is kept."""

    @pytest.mark.usefixtures("fs")
    def test_a_missing_file_reads_as_no_overrides(self) -> None:
        """A robot that has never had its settings changed still starts."""
        assert OverrideStore(_path("settings.json")).load() == {}

    @pytest.mark.usefixtures("fs")
    def test_what_is_saved_is_what_is_read_back(self) -> None:
        """The whole contract."""
        store = OverrideStore(_path("state/settings.json"))

        store.save({"web_port": "9100"})

        assert store.load() == {"web_port": "9100"}

    @pytest.mark.usefixtures("fs")
    def test_the_file_is_readable_only_by_its_owner(self) -> None:
        """It holds whatever secrets an operator typed into the page."""
        store = OverrideStore(_path("state/settings.json"))

        store.save({"groundstation_credential": AWKWARD_CREDENTIAL})

        assert store.path.stat().st_mode & 0o777 == 0o600

    @pytest.mark.usefixtures("fs")
    def test_saving_nothing_records_that_rather_than_deleting_the_file(self) -> None:
        """Everything being back at its environment value is a state, not a silence."""
        store = OverrideStore(_path("state/settings.json"))
        store.save({"web_port": "9100"})

        store.save({})

        assert store.path.exists()
        assert store.load() == {}

    @pytest.mark.usefixtures("fs")
    def test_a_file_that_is_not_json_is_reported(self) -> None:
        """An operator who edited it by hand and broke it should be told."""
        path = _path("state/settings.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")

        with pytest.raises(ConfigurationError) as raised:
            OverrideStore(path).load()

        assert str(path) in str(raised.value)

    @pytest.mark.usefixtures("fs")
    def test_a_file_that_is_not_an_object_is_reported(self) -> None:
        """Same reason: silence would look like the settings never applying."""
        path = _path("state/settings.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[1, 2]", encoding="utf-8")

        with pytest.raises(ConfigurationError):
            OverrideStore(path).load()

    @pytest.mark.usefixtures("fs")
    def test_a_non_string_value_is_reported(self) -> None:
        """Values are strings because that is what a form and an environment are."""
        path = _path("state/settings.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"web_port": 9100}', encoding="utf-8")

        with pytest.raises(ConfigurationError):
            OverrideStore(path).load()

    def test_a_directory_that_cannot_be_written_is_reported(
        self,
        fs: FakeFilesystem,
    ) -> None:
        """A change that appears to have been accepted and was not is the worst case.

        Args:
            fs: An in-memory filesystem, with the parent made a file so the
                write cannot succeed.
        """
        blocked = _path("blocked")
        blocked.parent.mkdir(parents=True, exist_ok=True)
        blocked.write_text("not a directory", encoding="utf-8")
        del fs

        with pytest.raises(ConfigurationError):
            OverrideStore(blocked / "settings.json").save({"web_port": "9100"})


class TestWhereTheOverridesLive:
    """The one setting that has to be read before the settings exist."""

    def test_it_sits_under_the_configured_state_directory(self) -> None:
        """Two callers need this answer, so there is one function that gives it."""
        found = overrides_path(
            {f"{ENV_PREFIX}STATE_DIR": "/reachy-satellite-elsewhere"}
        )

        assert found == Path("/reachy-satellite-elsewhere/settings.json")

    def test_it_falls_back_to_the_default_state_directory(self) -> None:
        """A robot that was told nothing still knows where to look."""
        assert not str(overrides_path({})).startswith("~")


class TestTheStateDirectory:
    """Where everything that outlives the wheel is kept."""

    def test_a_leading_tilde_is_expanded(self) -> None:
        """Because the daemon starts this application with a home directory."""
        resolution = load_settings(
            {**MINIMAL, f"{ENV_PREFIX}STATE_DIR": "~/somewhere"},
            {},
        )

        assert not str(state_directory(resolution.settings)).startswith("~")


def _spellings(value: str) -> tuple[str, ...]:
    """Every form a leaked credential could take in a rendering.

    Args:
        value: The credential.

    Returns:
        The raw value, its JSON escaping and its Python `repr` — the three ways
        a string reaches a log line or a page.
    """
    return (value, json.dumps(value)[1:-1], repr(value)[1:-1])


def _path(relative: str) -> Path:
    """Build an absolute path inside the fake filesystem.

    Args:
        relative: Where, below the root the fake filesystem provides.

    Returns:
        The path.
    """
    return Path("/reachy-satellite-config") / relative


class TestAnUnreadableOverridesFile:
    """A file that exists and cannot be read is a different fact from no file."""

    @pytest.mark.usefixtures("fs")
    def test_a_directory_where_the_file_should_be_is_reported(self) -> None:
        """Silence here would look exactly like the settings never applying."""
        path = _path("state/settings.json")
        path.mkdir(parents=True, exist_ok=True)

        with pytest.raises(ConfigurationError) as raised:
            OverrideStore(path).load()

        assert str(path) in str(raised.value)


class TestTheOverridesAreWrittenSafely:
    """The file holds a credential and the robot will not boot without parsing it."""

    @pytest.mark.usefixtures("fs")
    def test_no_temporary_file_is_left_beside_it(self) -> None:
        """It is renamed into place, so the directory holds one file when done."""
        store = OverrideStore(_path("state/settings.json"))

        store.save({"web_port": "9100"})

        assert [entry.name for entry in store.path.parent.iterdir()] == [
            "settings.json",
        ]

    @pytest.mark.usefixtures("fs")
    def test_the_file_is_owner_only_from_the_moment_it_exists(self) -> None:
        """A `chmod` afterwards leaves a window the umask decides."""
        store = OverrideStore(_path("state/settings.json"))

        store.save({"groundstation_credential": AWKWARD_CREDENTIAL})

        assert store.path.stat().st_mode & 0o077 == 0

    @pytest.mark.usefixtures("fs")
    def test_a_failed_write_leaves_the_previous_settings_intact(self) -> None:
        """A settings change must not be able to produce a robot that will not boot."""
        store = OverrideStore(_path("state/settings.json"))
        store.save({"web_port": "9100"})
        # A directory where the temporary file has to go: the rename cannot
        # happen, so what was already there is what survives.
        (store.path.parent / f"{store.path.name}.new").mkdir()

        with pytest.raises(ConfigurationError):
            store.save({"web_port": "9200"})

        assert store.load() == {"web_port": "9100"}


class TestTheBootstrapSetting:
    """`state_dir` decides where the overrides file is, so it cannot come from it."""

    def test_the_set_is_pinned(self) -> None:
        """Each entry is a setting the page could otherwise disable itself with.

        So adding or removing one is a deliberate edit here as well.
        """
        assert (
            frozenset(
                {"state_dir", "web_enabled", "web_host", "web_port"},
            )
            == BOOTSTRAP_SETTINGS
        )

    @pytest.mark.parametrize("name", ["web_enabled", "web_host", "web_port"])
    def test_the_page_cannot_switch_itself_off(self, name: str) -> None:
        """An override sits above the environment, so nothing could undo it.

        A page that saved `web_enabled=false` would never be reachable again,
        and setting the variable would not help.

        Args:
            name: One of the settings the interface's own existence depends on.
        """
        resolution = load_settings(MINIMAL, {name: "false"})

        assert resolution.ignored_overrides == (name,)

    def test_an_override_for_it_is_ignored_and_reported(self) -> None:
        """Honouring one would split the settings across two directories."""
        resolution = load_settings(
            {**MINIMAL, f"{ENV_PREFIX}STATE_DIR": "/reachy-satellite-here"},
            {"state_dir": "/reachy-satellite-elsewhere"},
        )

        assert resolution.settings.state_dir == "/reachy-satellite-here"
        assert resolution.ignored_overrides == ("state_dir",)

    def test_the_environment_still_sets_it(self) -> None:
        """It is configuration; it is just not configuration the page writes."""
        resolution = load_settings(
            {**MINIMAL, f"{ENV_PREFIX}STATE_DIR": "/reachy-satellite-here"},
            {},
        )

        assert resolution.sources["state_dir"] is SettingSource.ENVIRONMENT

    def test_the_report_marks_bootstrap_unwritable_and_current_values_writable(
        self,
    ) -> None:
        """Compatibility rows are covered separately as ignored and read-only."""
        report = {
            row.name: row for row in configuration_report(load_settings(MINIMAL, {}))
        }

        assert not report["state_dir"].writable
        assert not report["web_enabled"].writable
        assert report["api_port"].writable

    def test_the_overrides_file_is_where_the_resolved_state_directory_says(
        self,
    ) -> None:
        """The invariant the whole rule exists for: one file, not two.

        Startup finds the overrides from the environment alone, before any
        setting exists; everything afterwards uses the resolved `state_dir`. The
        two agree only because the overrides layer cannot move it.
        """
        environ = {**MINIMAL, f"{ENV_PREFIX}STATE_DIR": "/reachy-satellite-here"}
        resolution = load_settings(
            environ, {"state_dir": "/reachy-satellite-elsewhere"}
        )

        assert state_directory(
            resolution.settings
        ) / OVERRIDES_FILENAME == overrides_path(environ)


class TestPredictiveGazeMigration:
    """Legacy gaze inputs remain valid but are explicitly ignored and unwritable."""

    def test_the_compatibility_set_is_exact(self) -> None:
        """Only released legacy gain and projection names receive migration handling."""
        assert (
            frozenset(
                {
                    "gaze_deadzone",
                    "gaze_smoothing",
                    "camera_horizontal_fov_degrees",
                    "camera_vertical_fov_degrees",
                }
            )
            == COMPATIBILITY_SETTINGS
        )

    @pytest.mark.parametrize("name", sorted(COMPATIBILITY_SETTINGS))
    def test_legacy_environment_values_are_accepted_validated_and_reported(
        self,
        name: str,
    ) -> None:
        """An existing daemon environment keeps starting during migration."""
        value = "0.2" if name.startswith("gaze_") else "90.0"

        resolution = load_settings(
            {**MINIMAL, variable_for(name): value},
            {},
        )
        row = {item.name: item for item in configuration_report(resolution)}[name]

        assert resolution.sources[name] is SettingSource.ENVIRONMENT
        assert row.compatibility
        assert not row.live
        assert not row.writable

    @pytest.mark.parametrize("name", sorted(COMPATIBILITY_SETTINGS))
    def test_stale_legacy_overrides_are_ignored_and_reported(self, name: str) -> None:
        """An old settings file cannot pin a value the new form no longer owns."""
        value = "0.2" if name.startswith("gaze_") else "90.0"

        resolution = load_settings(MINIMAL, {name: value})

        assert resolution.ignored_overrides == (name,)
        assert resolution.sources[name] is SettingSource.DEFAULT

    def test_body_motion_is_restart_bound_and_disabled_by_default(self) -> None:
        """Coordinated body output remains an explicit restart-only opt in."""
        default = load_settings(MINIMAL, {})
        enabled = load_settings(
            {**MINIMAL, variable_for("body_motion_enabled"): "true"},
            {},
        )

        assert not default.settings.body_motion_enabled
        assert enabled.settings.body_motion_enabled
        assert "body_motion_enabled" not in LIVE_SETTINGS

    def test_legacy_gains_are_not_live_settings(self) -> None:
        """Predictive control has no active smoothing or deadzone side loop."""
        assert not (COMPATIBILITY_SETTINGS & LIVE_SETTINGS)


class TestTheGroundstationAddressCarriesNoCredential:
    """`groundstation_url` is not a secret setting, so it is rendered by value."""

    @pytest.mark.parametrize(
        "url",
        [
            "ws://someone:secret@192.0.2.10:8080/v1/session",
            "ws://192.0.2.10:8080/v1/session?credential=secret",
            "ws://192.0.2.10:8080/v1/session#secret",
            "http://192.0.2.10:8080/v1/session",
        ],
    )
    def test_an_address_carrying_one_is_refused(self, url: str) -> None:
        """Refused before anything is reported.

        No redactor can remove what it was never given, and this value reaches
        the boot log, the settings page and `/config`.

        Args:
            url: An address that hides a credential in one of its parts, or
                names a scheme a session cannot be opened on.
        """
        environ = {
            f"{ENV_PREFIX}DEVICE_NAME": "reachy-mini-1",
            f"{ENV_PREFIX}GROUNDSTATION_URL": url,
            f"{ENV_PREFIX}GROUNDSTATION_CREDENTIAL": "example-credential",
        }

        with pytest.raises(ConfigurationError) as raised:
            load_settings(environ, {})

        assert variable_for("groundstation_url") in str(raised.value)

    def test_the_refusal_repeats_nothing_back(self) -> None:
        """The value being refused is the one thing that must not be echoed."""
        environ = {
            f"{ENV_PREFIX}DEVICE_NAME": "reachy-mini-1",
            f"{ENV_PREFIX}GROUNDSTATION_URL": (
                "ws://someone:hunter2@192.0.2.10:8080/v1/session"
            ),
            f"{ENV_PREFIX}GROUNDSTATION_CREDENTIAL": "example-credential",
        }

        with pytest.raises(ConfigurationError) as raised:
            load_settings(environ, {})

        assert "hunter2" not in str(raised.value)

    def test_a_session_source_with_no_credential_is_refused(self) -> None:
        """It would raise out of the client three layers down instead."""
        environ = {
            f"{ENV_PREFIX}DEVICE_NAME": "reachy-mini-1",
            f"{ENV_PREFIX}GROUNDSTATION_URL": "ws://192.0.2.10:8080/v1/session",
        }

        with pytest.raises(ConfigurationError) as raised:
            load_settings(environ, {})

        assert variable_for("groundstation_credential") in str(raised.value)

    def test_the_local_selection_needs_neither(self) -> None:
        """It opens no session, so there is nothing to authenticate."""
        environ = {
            f"{ENV_PREFIX}DEVICE_NAME": "reachy-mini-1",
            f"{ENV_PREFIX}DETECTION_SOURCE": "local",
            f"{ENV_PREFIX}LOCAL_MODEL_PATH": "/models/face.onnx",
        }

        resolution = load_settings(environ, {})

        assert resolution.settings.groundstation_url == ""


class TestWhatCannotBeSwappedIntoARunningApplication:
    """`face_tracking_enabled` builds a detector, so it is not a live setting."""

    def test_it_is_not_claimed_to_apply_at_once(self) -> None:
        """Switching it on means opening a session or loading a model.

        A page saying otherwise would report tracking that never started.
        """
        assert "face_tracking_enabled" not in LIVE_SETTINGS


class TestRenderingAValueBackToAString:
    """One definition of it, so a comparison is between two strings made alike."""

    @pytest.mark.parametrize(
        ("name", "written", "expected"),
        [
            ("advertise", "TRUE", "true"),
            ("advertise", "0", "false"),
            ("api_port", "09000", "9000"),
            ("frame_interval_seconds", "0.10", "0.1"),
            ("detection_source", "remote", "remote"),
            ("log_level", "info", "info"),
            ("device_name", "reachy-mini-1", "reachy-mini-1"),
        ],
    )
    def test_a_raw_value_is_rendered_the_way_the_model_reads_it_back(
        self,
        name: str,
        written: str,
        expected: str,
    ) -> None:
        """What stops the settings page pinning an override nobody asked for.

        Args:
            name: Which setting.
            written: How the environment spells it.
            expected: The canonical spelling the form renders.
        """
        assert canonical_string(name, written) == expected

    def test_a_value_that_does_not_parse_is_left_alone(self) -> None:
        """`load_settings` is about to refuse it with a message naming it.

        Guessing here would obscure that.
        """
        assert canonical_string("api_port", "not a port") == "not a port"

    def test_a_secret_is_never_re_rendered(self) -> None:
        """It is compared, never displayed.

        Transforming it first is how a redactor stops recognising it.
        """
        assert canonical_string("groundstation_credential", AWKWARD_CREDENTIAL) == (
            AWKWARD_CREDENTIAL
        )


# The smallest environment that resolves *and* opens a session, which is what
# makes the address below load-bearing rather than unread.
_TRACKING = {
    f"{ENV_PREFIX}DEVICE_NAME": "reachy-mini-1",
    f"{ENV_PREFIX}GROUNDSTATION_CREDENTIAL": "example-credential",
}


class TestTheGroundstationAddressBound:
    """One 255-character contract, refused rather than truncated."""

    def test_the_declared_maximum_is_the_shared_one(self) -> None:
        """Not a number of this application's own — see `config`'s own comment."""
        assert GROUNDSTATION_URL_MAX_LENGTH == SESSION_URL_MAX_LENGTH
        assert (
            Settings.model_fields[GROUNDSTATION_URL_SETTING].metadata[0].max_length
            == GROUNDSTATION_URL_MAX_LENGTH
        )

    def test_the_boundary_length_resolves(self) -> None:
        """255 is the limit, so 255 is a value that starts the application."""
        url = address_of_length(GROUNDSTATION_URL_MAX_LENGTH)

        resolution = load_settings({**_TRACKING, f"{ENV_PREFIX}GROUNDSTATION_URL": url})

        assert resolution.settings.groundstation_url == url

    @pytest.mark.parametrize("length", [256, 400, 512])
    def test_a_legacy_environment_value_refuses_startup(self, length: int) -> None:
        """REQ-095's legacy scenario, from the layer a deployment writes.

        Args:
            length: How long the released address is.
        """
        url = address_of_length(length)

        with pytest.raises(ConfigurationError) as raised:
            load_settings({**_TRACKING, f"{ENV_PREFIX}GROUNDSTATION_URL": url})

        message = str(raised.value)
        assert variable_for(GROUNDSTATION_URL_SETTING) in message
        assert str(GROUNDSTATION_URL_MAX_LENGTH) in message
        assert "environment" in message
        assert "start the application again" in message

    @pytest.mark.parametrize("length", [256, 512])
    def test_a_legacy_persisted_value_names_the_overrides_file(
        self,
        length: int,
    ) -> None:
        """The other layer, whose remedy is a different file and a different act.

        Args:
            length: How long the released address is.
        """
        environ = {**_TRACKING, f"{ENV_PREFIX}STATE_DIR": "/reachy-satellite-legacy"}

        with pytest.raises(ConfigurationError) as raised:
            load_settings(
                environ,
                {GROUNDSTATION_URL_SETTING: address_of_length(length)},
            )

        message = str(raised.value)
        assert "override" in message
        assert "/reachy-satellite-legacy/settings.json" in message
        assert str(GROUNDSTATION_URL_MAX_LENGTH) in message

    def test_the_refusal_never_repeats_the_address(self) -> None:
        """`groundstation_url` is rendered by value everywhere it is reported.

        A refusal that quoted it would put a value nothing can represent into
        the boot log, which is the one place an operator is certain to paste.
        """
        url = address_of_length(400)

        with pytest.raises(ConfigurationError) as raised:
            load_settings({**_TRACKING, f"{ENV_PREFIX}GROUNDSTATION_URL": url})

        message = str(raised.value)
        assert url not in message
        assert "192.0.2.10" not in message

    @pytest.mark.parametrize("length", [0, 40, GROUNDSTATION_URL_MAX_LENGTH])
    def test_a_runtime_submission_within_the_bound_is_accepted(
        self,
        length: int,
    ) -> None:
        """The check both paths reach through the owner, on the values it passes.

        Args:
            length: How long the submitted address is.
        """
        validate_groundstation_url_length("a" * length)

    @pytest.mark.parametrize("length", [256, 512])
    def test_a_runtime_submission_over_the_bound_is_refused(
        self,
        length: int,
    ) -> None:
        """Stated as the limit and the length, never as the address.

        Args:
            length: How long the submitted address is.
        """
        url = address_of_length(length)

        with pytest.raises(ConfigurationError) as raised:
            validate_groundstation_url_length(url)

        message = str(raised.value)
        assert str(GROUNDSTATION_URL_MAX_LENGTH) in message
        assert str(length) in message
        assert url not in message

    def test_the_address_is_a_live_setting(self) -> None:
        """What the set means to an operator is "no restart", which is now true.

        Its adoption is `groundstation_url.GroundstationUrlOwner`'s rather than
        `apply_live`'s, and that is the one entry in the set of which that is
        so — see the comment beside `LIVE_SETTINGS`.
        """
        assert GROUNDSTATION_URL_SETTING in LIVE_SETTINGS
