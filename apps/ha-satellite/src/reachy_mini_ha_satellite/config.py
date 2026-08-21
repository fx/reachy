"""Settings, read once by a function the entry point calls, and never behind it.

The predecessor to this application had a configuration reader that nothing ever
called, so every environment override was silently a dataclass default and the
mistake stayed quiet for months. Everything here is shaped by that, in the same
way `reachy_groundstation.config` is: `load_settings` is the only way a `Settings`
comes into existence, it is a pure function of the mappings it is handed, and an
unrecognised variable under this application's prefix is fatal and names itself.

Two functions do read the environment without going through it, and both are the
bootstrap rather than an exception to the rule. `overrides_path` reads
`STATE_DIR` to find the file the overrides layer lives in — it has to, since that
file cannot say where it is — and `daemon_app._settings_port` reads `WEB_PORT` to
build the URL the daemon's dashboard links to, before any settings exist to read.
Each reads one variable, neither produces a `Settings`, and both are covered by
`BOOTSTRAP_SETTINGS` below, which is what keeps the two answers from diverging
from the resolved one.

Three things are different here, and each has a reason.

**The announced identity has no default at all.** Home Assistant keys an ESPHome
device on the identity it announces; change it and Home Assistant does not update
the existing device, it registers a new one. Every entity acquires a suffixed
identifier, history detaches, and every automation and dashboard card referencing
the old identifiers silently stops matching anything. A default derived from the
package name would be correct on a fresh installation and silently destructive on
the upgrade from the predecessor — which is the case that actually exists, since
that application was a different distribution. Refusing to start is what makes
the hazard visible at configuration time rather than after it has happened.

**There is a third layer under the environment.** ha-satellite REQ-049 requires
every operator-facing setting to be changeable from the application's own web
interface, and a layer that the environment overrode would make that false for
any setting an operator had ever put in the environment. So the precedence runs
defaults, then environment, then the overrides the web interface writes — and
`Resolution.sources` records which layer each value came from, so the precedence
is visible on the settings page rather than surprising.

**Which settings are secret is declared in exactly one place** — `SECRET_SETTINGS`,
derived from the field types — and every surface reads it: the boot log, the
settings page, the configuration endpoint and the overrides file's own summary.
Redaction happens *before* rendering, never after, so there is no escaped or
truncated spelling of a credential for a redactor to have failed to recognise:
`resolved_configuration` turns a secret into `<set>` or `<unset>` and every
surface renders that.

A secret's raw value does still travel, and it is worth saying where rather than
claiming it never leaves this module. Three paths carry it and none of them is a
rendering: `canonical_string` hands it back unchanged so a submission can be
*compared* against it, `OverrideStore.save` writes it to a file owner-only, and
`main.build_perception_source` reveals it once into a `Credential`, which is the
type that will not print itself. Everywhere else it is read only to be tested for
emptiness and discarded — `resolved_configuration` choosing between `<set>` and
`<unset>`, and the coherence check refusing a session with no credential.
`test_satellite_config.py` and `test_satellite_web_settings.py` assert that a
credential carrying a tab, a newline and a backslash appears in no rendering of
any surface — raw, escaped, or `repr`'d.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

# TID253 bans importing pydantic at module level outside the contracts package,
# so that no consumer declares a second copy of a wire type. This model is
# configuration and never crosses the wire; `SecretStr` is what marks a setting
# secret, and `Field` is what constrains the scalars. The ban is suppressed for
# this one import, as the root AGENTS.md describes.
from pydantic import (  # noqa: TID253  # configuration, not a wire type: `SecretStr` is what marks a setting secret, `Field` is what bounds the scalars, and `TypeAdapter` is what reads one field's raw string the way the model would — none of it crosses the link
    Field,
    SecretStr,
    TypeAdapter,
    ValidationError,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from reachy_contracts.settings import ROBOT_SETTINGS
from reachy_mini_ha_satellite.ports import SourceSelection
from reachy_session_client import validate_session_url

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "BOOTSTRAP_SETTINGS",
    "ENV_PREFIX",
    "IDENTITY_SETTING",
    "LIVE_SETTINGS",
    "OVERRIDES_FILENAME",
    "REDACTED_SET",
    "REDACTED_UNSET",
    "SECRET_SETTINGS",
    "ConfigurationError",
    "OverrideStore",
    "Resolution",
    "SettingSource",
    "Settings",
    "as_configured_string",
    "canonical_string",
    "configuration_report",
    "declared_but_unread",
    "declared_elsewhere",
    "load_settings",
    "log_resolved_configuration",
    "overrides_path",
    "resolved_configuration",
    "setting_names",
    "state_directory",
    "unrecognised_variables",
    "variable_for",
]

_LOGGER: Final = logging.getLogger(__name__)

ENV_PREFIX: Final = "REACHY_SATELLITE_"

# The one setting with no default, named here so the message that explains why
# and the model that declares it cannot drift apart.
IDENTITY_SETTING: Final = "device_name"

# What a secret looks like on every surface. The settings page is reachable by
# anything that can reach the robot, so the question it answers is "is it set?"
# and never "what is it?".
REDACTED_SET: Final = "<set>"
REDACTED_UNSET: Final = "<unset>"

# What the overrides the settings interface writes are kept in, inside the
# application's state directory.
OVERRIDES_FILENAME: Final = "settings.json"

# Settings the overrides layer cannot supply, because they decide whether that
# layer can be reached at all.
#
# The overrides file sits *above* the environment, so an override can only be
# undone by writing a different one. That is fine for every setting except the
# ones the settings page itself depends on: write one of those wrongly and the
# page is gone, the environment cannot override it back, and the only way out is
# a shell — which is precisely what ha-satellite REQ-049 exists to avoid. The
# page would be a control that can disable itself and nothing else.
#
# Two kinds, and the second is the one a reviewer found:
#
# * `state_dir` names the directory the overrides file lives in, so an override
#   for it would be a file saying where to look for itself. Honouring one splits
#   the settings across two directories — startup keeps reading the old location
#   while the page writes to the new one — and the first ordinary save after
#   that drops the credential, because the page submits a secret's field blank
#   meaning "keep" and there is nothing to keep in a store never written.
# * `web_enabled`, `web_host` and `web_port` decide whether the interface is
#   served, on which address and on which port. Saving `web_enabled=false`, or a
#   host the robot has not got, leaves no interface to change it back with.
#
# All four are read from the environment and the defaults only. They stay
# **readable** on the settings page, which is what REQ-049 asks of a setting it
# does not mark secret; what they are not is writable there, and the page says
# why rather than offering a control that is a trap. Moving the state directory
# means moving the files in it, and moving the interface means knowing where it
# went — neither is something a web form can finish anyway.
BOOTSTRAP_SETTINGS: Final[frozenset[str]] = frozenset(
    {"state_dir", "web_enabled", "web_host", "web_port"}
)

# Settings the running application can adopt without being restarted. Everything
# else is read while something is being built — a socket is bound, a session is
# opened, an identity is announced — so changing it takes effect at the next
# start, and the settings page says so rather than implying otherwise.
#
# A literal rather than something derived, because "can this be swapped into a
# running object?" is a property of the code that consumes it and not of the
# field's type. `test_satellite_config.py` pins every name here to a real field,
# so a setting renamed without updating this set is a red run.
LIVE_SETTINGS: Final[frozenset[str]] = frozenset(
    {
        "log_level",
        "behaviour_tick_seconds",
        "gaze_deadzone",
        "gaze_smoothing",
        "idle_seconds",
    }
)

# ⚠️ `face_tracking_enabled` is deliberately NOT in that set, and the reason is
# worth stating because the behaviour layer can adopt it in isolation and looks
# as though it should. Switching it on means building a detector — opening a
# robot-link session, or loading a model onto the robot's own cores — and
# switching it off means shutting one down. Neither is something the behaviour
# layer owns; both happen in `main.build_application`, once, at startup. A page
# that said "applies at once" would be telling an operator that tracking is now
# on while nothing was ever built to do it.


# The selection that runs the detector on the robot and opens no session. Bound
# once rather than spelled at each site, because this repository's leak scanner
# reads the dotted form as an mDNS hostname suffix — the same reason
# `adapters/perception_source.py` binds it, and one exempted line is better than
# several.
_ROBOT_ONLY: Final = SourceSelection.LOCAL  # leak-scan:allow


class ConfigurationError(RuntimeError):
    """Startup cannot proceed because the configuration is not usable."""


class SettingSource(StrEnum):
    """Which layer a value in effect came from.

    Attributes:
        DEFAULT: Nothing set it; this is the model's own default.
        ENVIRONMENT: A prefixed environment variable set it.
        OVERRIDE: The settings interface wrote it, which is the top layer.
    """

    DEFAULT = "default"
    ENVIRONMENT = "environment"
    OVERRIDE = "override"


#:= docs/specs/ha-satellite/index.md#req-040-the-announced-device-identity-is-configuration
#:% The identity the satellite announces to Home Assistant MUST be read from
#:% configuration rather than derived from the package name, the host name, or any
#:% other value that changes when the software is repackaged.
class Settings(BaseSettings):
    """Everything this application reads from its environment.

    Every field but `device_name` has a default, so the resolved configuration
    is complete whether or not an operator set anything — which is what makes
    the boot dump and the settings page worth reading.

    Attributes:
        device_name: The identity announced to Home Assistant, which keys the
            device on it. **No default, deliberately**: see this module's
            docstring, and `_identity_is_unset_message` for what an operator is
            told when it is missing.
        friendly_name: The display name Home Assistant shows. Blank means the
            announced identity is used for both. Unlike the identity it is safe
            to change: Home Assistant renames the device rather than replacing
            it.
        mac_address: The hardware address announced alongside the name, which
            Home Assistant also keys the device on. Blank means it is read from
            the network interface at startup and reported in the resolved
            configuration, so an operator can see what was announced and pin it.
        network_interface: Which interface the address and the mDNS
            advertisement are taken from. Blank means the one the default route
            uses.
        api_host: The address the ESPHome native API binds.
        api_port: The port it binds. 6053 is the port Home Assistant looks for.
        advertise: Whether to advertise over mDNS, which is how Home Assistant
            discovers the satellite without being told where it is.
        web_enabled: Whether the settings interface is served at all.
        web_host: The address the settings interface binds.
        web_port: The port it binds.
        state_dir: Where preferences, downloaded media and the overrides the
            settings interface writes are kept. Outside the wheel, so
            reinstalling the application does not discard them.
        active_wake_word: Which shipped wake word is listening. Home Assistant
            can add more at run time; this is what the satellite starts with.
        samples_per_chunk: How many samples per channel one capture chunk
            carries.
        face_tracking_enabled: Whether the head follows a face at all. Off means
            no detector is built and no session is opened, which is the
            configuration for a robot with neither a groundstation nor cores to
            spare.
        detection_source: Which detector answers — see ha-satellite REQ-047.
        groundstation_url: Where the groundstation serves its session endpoint.
            Required by every selection but `local`.
        groundstation_credential: The shared secret presented to open a session.
        frame_interval_seconds: How long between frames submitted to the
            groundstation.
        staleness_seconds: How long a detection stays worth acting on. Past it
            the head returns to neutral — ha-satellite REQ-048.
        local_model_path: The face-detection weights the robot's own detector
            loads. Required by every selection but `remote`, and empty by
            default because the weights are not shipped in the wheel: they are
            somebody else's model under somebody else's terms, and this
            repository's asset registry only carries what it can defend
            redistributing.
        local_score_threshold: The confidence a locally-detected face must reach.
        local_nms_threshold: How much two local face boxes may overlap before
            the lower-scoring one is suppressed.
        local_detection_interval_seconds: How long between local detection
            passes.
        camera_horizontal_fov_degrees: How much of the scene the camera sees
            across, which is what turns a normalised position into an angle.
        camera_vertical_fov_degrees: The same, vertically.
        behaviour_tick_seconds: How often the behaviour layer is asked what the
            robot should be doing.
        gaze_deadzone: How far a face must move, in normalised image
            coordinates, before the head is re-commanded. Zero would put a
            command on the motor bus every tick for a face that has not moved.
        gaze_smoothing: How much of the way towards a new target one tick
            moves. One follows instantly and jitters; zero never arrives.
        idle_seconds: How long without a visible face before the robot settles
            into its idle behaviour.
        log_level: The lowest severity emitted.
    """

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        extra="forbid",
        frozen=True,
        validate_default=True,
    )

    device_name: str = Field(min_length=1, max_length=64)
    friendly_name: str = Field(default="", max_length=64)
    mac_address: str = Field(default="", max_length=32)
    network_interface: str = Field(default="", max_length=32)

    # The satellite is useless on the loopback interface: Home Assistant runs on
    # another host and has to reach it. S104 objects to binding every interface,
    # which is exactly what a device announcing itself over mDNS has to do. The
    # vendored code is exempted from the same rule for the same reason, but by
    # `per-file-ignores` in the root `pyproject.toml` rather than inline —
    # annotating a derived file would be an unlisted edit to it. These two are
    # this repository's own, so they are suppressed where they are written.
    api_host: str = Field(default="0.0.0.0", min_length=1)  # noqa: S104  # a voice satellite is reached from another host; binding loopback would make the default configuration one that cannot work
    api_port: int = Field(default=6053, ge=1, le=65535)
    advertise: bool = True

    #:= docs/specs/ha-satellite/index.md#req-049-settings-are-changeable-without-a-shell
    #:% Every operator-facing setting MUST be changeable through the application's own
    #:% web interface, and MUST be readable there except where the setting is marked
    #:% secret, which is reported as set or unset without its value.
    web_enabled: bool = True
    web_host: str = Field(default="0.0.0.0", min_length=1)  # noqa: S104  # the settings interface is opened from an operator's laptop, not from the robot's own console
    web_port: int = Field(default=8088, ge=1, le=65535)

    state_dir: str = Field(
        default="~/.local/state/reachy-mini-ha-satellite",
        min_length=1,
    )
    active_wake_word: str = Field(default="okay_nabu", min_length=1)
    samples_per_chunk: int = Field(default=160, ge=16, le=16000)

    face_tracking_enabled: bool = True

    #:= docs/specs/ha-satellite/index.md#req-047-detection-source-is-selectable
    #:% The source of face detections MUST be selectable between the groundstation, the
    #:% robot's own detector, and the groundstation with local fallback.
    detection_source: SourceSelection = SourceSelection.REMOTE
    groundstation_url: str = Field(default="", max_length=512)
    groundstation_credential: SecretStr = SecretStr("")
    frame_interval_seconds: float = Field(default=0.1, gt=0.0, le=60.0)

    #:= docs/specs/ha-satellite/index.md#req-048-the-head-returns-to-neutral-when-tracking-data-goes-stale
    #:% When results stop arriving within the staleness window, the application MUST
    #:% return the head to its neutral position rather than holding its last commanded
    #:% pose.
    staleness_seconds: float = Field(default=2.0, gt=0.0, le=600.0)

    local_model_path: str = Field(default="", max_length=512)
    local_score_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    local_nms_threshold: float = Field(default=0.3, ge=0.0, le=1.0)
    local_detection_interval_seconds: float = Field(default=0.2, gt=0.0, le=60.0)

    camera_horizontal_fov_degrees: float = Field(default=87.0, gt=0.0, lt=180.0)
    camera_vertical_fov_degrees: float = Field(default=67.0, gt=0.0, lt=180.0)

    behaviour_tick_seconds: float = Field(default=0.05, gt=0.0, le=5.0)
    gaze_deadzone: float = Field(default=0.02, ge=0.0, le=1.0)
    gaze_smoothing: float = Field(default=0.35, gt=0.0, le=1.0)
    idle_seconds: float = Field(default=6.0, gt=0.0, le=3600.0)

    log_level: Literal["debug", "info", "warning", "error"] = "info"

    @property
    def announced_friendly_name(self) -> str:
        """What Home Assistant shows for this device.

        Returns:
            The configured display name, or the announced identity when none
            was configured.
        """
        return self.friendly_name or self.device_name


def as_configured_string(value: object) -> str:
    """Render a value the way the configuration layer reads it back.

    The inverse of parsing, and the one definition of it: the settings page
    renders a field with this, and `canonical_string` renders the layer beneath
    with it, so "did the operator change anything?" is a comparison of two
    strings produced the same way rather than of one produced two ways.

    Args:
        value: A value in effect.

    Returns:
        A string that, fed back through the settings model, produces the same
        value.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, StrEnum):
        return value.value
    return str(value)


def canonical_string(name: str, raw: str) -> str:
    """Render a raw configured string the way the model would read it back.

    **This is what stops the settings page pinning an override nobody asked
    for.** The page renders each field from the *parsed* settings, so a browser
    submits `true` for a variable somebody wrote as `TRUE`, `9000` for `09000`,
    and `0.1` for `0.10`. Comparing a submission against the raw environment
    string would then see a difference in every such setting and write an
    override for it — after which the environment can no longer change that
    setting at all, because an override sits above it. The operator would have
    changed one thing and pinned four.

    The parsing is pydantic's own, on the field's own annotation, rather than a
    second implementation of its coercion rules that would be free to drift from
    the one that actually resolves the configuration.

    Args:
        name: Which setting.
        raw: What the environment or the overrides file says.

    Returns:
        The canonical spelling, or the raw string when it does not parse at all
        — in which case `load_settings` is about to refuse it, with a message
        that names the variable, and guessing here would only obscure that.
    """
    if name in SECRET_SETTINGS:
        return raw
    annotation = Settings.model_fields[name].annotation
    if annotation is None:
        return raw
    try:
        value = TypeAdapter(annotation).validate_python(raw)
    except ValidationError:
        return raw
    return as_configured_string(value)


def _secret_settings() -> frozenset[str]:
    """Work out which settings are secret from how they are declared.

    Deriving the set from the field type rather than repeating the names is what
    makes "mark a secret in exactly one place" true by construction: a setting
    added as a `SecretStr` is redacted on every surface without anybody
    remembering to add it to a list.

    Returns:
        The names of the settings whose values are never reported.
    """
    return frozenset(
        name
        for name, field in Settings.model_fields.items()
        if isinstance(field.annotation, type)
        and issubclass(field.annotation, SecretStr)
    )


SECRET_SETTINGS: Final[frozenset[str]] = _secret_settings()


def setting_names() -> tuple[str, ...]:
    """Every setting this application has, in declaration order.

    Returns:
        The names, which is also the order the settings page lists them in.
    """
    return tuple(Settings.model_fields)


def variable_for(field_name: str) -> str:
    """Name the environment variable a setting is read from.

    Args:
        field_name: The setting's name on the model.

    Returns:
        The variable name, prefixed and upper-cased.
    """
    return f"{ENV_PREFIX}{field_name.upper()}"


def declared_elsewhere() -> frozenset[str]:
    """Variables the robot's shared vocabulary declares that this does not read.

    `reachy_contracts.settings.ROBOT_SETTINGS` is the one declaration of what the
    robot's daemon environment carries: `reachyctl config` validates against it,
    the Ansible role writes it, and some of its names fall under this
    application's prefix without being settings this application consumes.

    **They must not be fatal**, and the reason is what architecture REQ-009
    actually says: a variable the component "does not recognise". A name the
    repository's own vocabulary declares is recognised — it is simply not
    consumed here yet. Refusing it would mean an operator running
    `reachyctl config apply` with the documented vocabulary got a robot that
    would not start, which is a worse outcome than the typo the requirement
    exists to catch. A typo is still fatal, because a typo is not in
    `ROBOT_SETTINGS`.

    They are reported rather than ignored: the boot log and the settings
    interface both name them, so "why is this variable having no effect?" has an
    answer that is written down.

    Returns:
        The declared names under this application's prefix that it does not
        read.
    """
    consumed = {variable_for(name) for name in Settings.model_fields}
    return frozenset(
        setting.name
        for setting in ROBOT_SETTINGS
        if setting.name.startswith(ENV_PREFIX) and setting.name not in consumed
    )


def unrecognised_variables(environ: Mapping[str, str]) -> tuple[str, ...]:
    """List the prefixed variables nothing in this repository declares.

    Args:
        environ: The environment to inspect.

    Returns:
        The offending variable names, sorted, so the message is stable.
    """
    known = {
        variable_for(name) for name in Settings.model_fields
    } | declared_elsewhere()
    return tuple(
        sorted(
            name
            for name in environ
            if name.startswith(ENV_PREFIX) and name not in known
        ),
    )


def declared_but_unread(environ: Mapping[str, str]) -> tuple[str, ...]:
    """List the shared-vocabulary variables that are set and have no effect here.

    Args:
        environ: The environment to inspect.

    Returns:
        The names, sorted.
    """
    return tuple(sorted(name for name in environ if name in declared_elsewhere()))


@dataclass(frozen=True, slots=True)
class Resolution:
    """The settings in effect, and where each of them came from.

    Attributes:
        settings: What the application runs on.
        sources: Which layer supplied each setting.
        declared_but_unread: Variables the robot's shared vocabulary declares,
            set in this environment, that this application does not read — see
            `declared_elsewhere`. Reported rather than refused, and reported
            rather than ignored.
        ignored_overrides: Names in the overrides file that were not applied:
            settings that no longer exist, and the bootstrap settings the
            overrides layer cannot supply. Dropped rather than fatal — the file
            is written by this application rather than typed by an operator, and
            a stale key left by an upgrade must not be the thing that stops a
            robot booting — but reported here, logged at startup and shown on
            the settings page, so it is visible rather than silent.
    """

    settings: Settings
    sources: Mapping[str, SettingSource]
    ignored_overrides: tuple[str, ...] = ()
    declared_but_unread: tuple[str, ...] = ()


def _identity_is_unset_message() -> str:
    """Explain why the announced identity has to be pinned, not just that it is.

    Returns:
        The refusal an operator reads, which is the only warning they get
        before the hazard it describes has already happened.
    """
    variable = variable_for(IDENTITY_SETTING)
    return (
        f"{variable} is not set, and there is deliberately no default.\n"  # noqa: S608  # prose, not a query: the rule matches on the words "set" and "from" appearing in one f-string
        f"\n"
        f"Home Assistant keys an ESPHome device on the identity it announces. "
        f"If that identity changes, Home Assistant does not update the existing "
        f"device — it registers a new one. Every entity acquires a suffixed "
        f"identifier, history detaches from the old entity, and every "
        f"automation, script and dashboard card referencing the old identifiers "
        f"silently stops matching anything.\n"
        f"\n"
        f"There is no default derived from the package name, the host name or "
        f"anything else that changes when this software is repackaged, because "
        f"such a default would be correct on a fresh installation and silently "
        f"destructive on an upgrade from an application with a different "
        f"package name — which is exactly the upgrade this application exists "
        f"to be.\n"
        f"\n"
        f"Upgrading an existing installation: set it to the name the previous "
        f"application announced. Home Assistant shows it on the device page, "
        f"and it is the prefix of every entity identifier belonging to the "
        f"device.\n"
        f"\n"
        f"A new robot: choose a name now and never change it, for example "
        f"{variable}=reachy-mini-1."
    )


def _declared_values(
    environ: Mapping[str, str],
    overrides: Mapping[str, str],
) -> tuple[dict[str, str], dict[str, SettingSource], tuple[str, ...]]:
    """Fold the two layers over the defaults, recording where each value came from.

    Args:
        environ: The environment to read.
        overrides: What the settings interface wrote, by setting name.

    Returns:
        The raw string values to validate, the layer each came from, and the
        override keys that name nothing.
    """
    applicable = set(Settings.model_fields) - BOOTSTRAP_SETTINGS
    values: dict[str, str] = {}
    sources: dict[str, SettingSource] = dict.fromkeys(
        Settings.model_fields,
        SettingSource.DEFAULT,
    )

    for name in Settings.model_fields:
        variable = variable_for(name)
        if variable in environ:
            values[name] = environ[variable]
            sources[name] = SettingSource.ENVIRONMENT

    for name, value in overrides.items():
        if name not in applicable:
            continue
        values[name] = value
        sources[name] = SettingSource.OVERRIDE

    ignored = tuple(sorted(name for name in overrides if name not in applicable))
    return values, sources, ignored


def load_settings(
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, str] | None = None,
) -> Resolution:
    """Resolve the settings, or refuse to start.

    Args:
        environ: The environment to read. Defaults to the process environment;
            a test passes a mapping instead, which is what keeps this function
            free of input and output.
        overrides: What the settings interface wrote, by setting name. The top
            layer, so a setting an operator changed from the interface takes
            effect even where the same setting is also in the environment —
            without that, REQ-049 would be false for any setting anybody had
            ever exported.

    Returns:
        The settings in effect and where each of them came from.

    Raises:
        ConfigurationError: If a prefixed variable is not recognised, if the
            announced identity is unset, or if a recognised value does not
            parse. Every message names the variable.
    """
    source = os.environ if environ is None else environ
    written = {} if overrides is None else overrides

    unknown = unrecognised_variables(source)
    if unknown:
        message = (
            f"unrecognised configuration variable(s): {', '.join(unknown)}. "
            f"Every {ENV_PREFIX}* variable must name a known setting; "
            f"the known ones are "
            f"{', '.join(sorted(variable_for(name) for name in Settings.model_fields))}."
        )
        raise ConfigurationError(message)

    values, sources, ignored = _declared_values(source, written)

    if not values.get(IDENTITY_SETTING, "").strip():
        raise ConfigurationError(_identity_is_unset_message())

    try:
        settings = Settings.model_validate(values)
    except ValidationError as error:
        # `loc` and `msg` only. A pydantic error also carries the input that
        # failed, and including it would print a rejected credential into the
        # one place an operator is certain to read and paste.
        faults = "; ".join(
            f"{variable_for(str(fault['loc'][0])) if fault['loc'] else '(none)'}: "
            f"{fault['msg']}"
            for fault in error.errors()
        )
        message = f"configuration is not usable: {faults}"
        raise ConfigurationError(message) from error

    _check_coherence(settings)
    return Resolution(
        settings=settings,
        sources=sources,
        ignored_overrides=ignored,
        declared_but_unread=declared_but_unread(source),
    )


def _check_coherence(settings: Settings) -> None:
    """Refuse a configuration whose parts contradict each other.

    Each of these is a combination that parses and then produces a robot that
    silently never tracks anything, which is the least debuggable way to be told
    about a mistake.

    Args:
        settings: The parsed settings.

    Raises:
        ConfigurationError: If face tracking is asked for without the source it
            would need, or — via `_check_session_url`, which this calls once the
            address is known to be non-empty — if the groundstation address is
            not one a session can be opened on.
    """
    if not settings.face_tracking_enabled:
        return

    needs_groundstation = settings.detection_source is not _ROBOT_ONLY
    if needs_groundstation:
        if not settings.groundstation_url.strip():
            message = (
                f"{variable_for('detection_source')}="
                f"{settings.detection_source.value} needs a groundstation, but "
                f"{variable_for('groundstation_url')} is empty. Set it, or select "
                f"{_ROBOT_ONLY.value} to run the detector on the robot, "
                f"or set {variable_for('face_tracking_enabled')}=false to switch "
                f"face tracking off."
            )
            raise ConfigurationError(message)
        _check_session_url(settings.groundstation_url.strip())
        if not settings.groundstation_credential.get_secret_value():
            message = (
                f"{variable_for('detection_source')}="
                f"{settings.detection_source.value} opens a session with the "
                f"groundstation, and {variable_for('groundstation_credential')} "
                f"is empty. A session with no credential is refused by the "
                f"groundstation, so this is caught here rather than as a robot "
                f"that connects to nothing."
            )
            raise ConfigurationError(message)

    needs_local_model = settings.detection_source is not SourceSelection.REMOTE
    if needs_local_model and not settings.local_model_path.strip():
        message = (
            f"{variable_for('detection_source')}="
            f"{settings.detection_source.value} runs a detector on the robot, "
            f"but {variable_for('local_model_path')} is empty. The weights are "
            f"not shipped in this wheel; point this at the model file on the "
            f"robot, or select {SourceSelection.REMOTE.value} to leave "
            f"detection to the groundstation."
        )
        raise ConfigurationError(message)


def _check_session_url(url: str) -> None:
    """Refuse a groundstation address that carries a credential in itself.

    **This is a redaction rule, not a syntax one, and it has to run before
    anything is reported.** `groundstation_url` is not a secret setting, so it
    is rendered by value — in the boot log, on the settings page and at
    `/config`. A URL such as `wss://someone:secret@host/v1/session` would
    therefore reach all three whole, and no redactor can remove a credential it
    was never given. So the address is refused at configuration time, before the
    first line of the resolved configuration is emitted.

    The rule itself lives in `reachy_session_client.validate_session_url` and is
    called rather than restated: it is the same rule `reachyctl probe` applies
    and the same one the client applies when it connects, and a second copy here
    would be free to drift from the one that actually holds.

    Args:
        url: The configured address.

    Raises:
        ConfigurationError: If it is not an address a session can be opened on,
            or if it carries user information, a query or a fragment. The
            message quotes nothing back: the value being refused is the one
            thing that must not be repeated.
    """
    try:
        validate_session_url(url)
    except ValueError as error:
        message = f"{variable_for('groundstation_url')} is not usable: {error}"
        raise ConfigurationError(message) from error


def resolved_configuration(settings: Settings) -> dict[str, object]:
    """Render every setting in effect, with the secrets reported as set or not.

    This is the one renderer. The boot log, the configuration endpoint and the
    settings page all call it, so a setting added later is redacted on every
    surface or on none, and there is no second copy to forget. A secret never
    leaves this function as a value, which is what stops an escaped or truncated
    rendering of one leaking a form no redactor would recognise.

    Args:
        settings: The settings in effect.

    Returns:
        Every setting name mapped to a value fit to publish, including the ones
        left at their defaults.
    """
    rendered: dict[str, object] = {}
    for name in Settings.model_fields:
        value = getattr(settings, name)
        if name in SECRET_SETTINGS:
            rendered[name] = (
                REDACTED_SET if value.get_secret_value() else REDACTED_UNSET
            )
        elif isinstance(value, StrEnum):
            rendered[name] = value.value
        else:
            rendered[name] = value
    return rendered


@dataclass(frozen=True, slots=True)
class SettingReport:
    """One setting, as an operator sees it.

    Attributes:
        name: The setting's name.
        variable: The environment variable that would set it.
        value: What is in effect, already redacted where the setting is secret.
        source: Which layer supplied it.
        secret: Whether the value is withheld.
        live: Whether changing it takes effect without a restart.
        writable: Whether the settings interface may change it. False for the
            bootstrap settings, which decide where the interface's own file
            lives and so cannot be supplied by it.
    """

    name: str
    variable: str
    value: object
    source: SettingSource
    secret: bool
    live: bool
    writable: bool


def configuration_report(resolution: Resolution) -> tuple[SettingReport, ...]:
    """Render the resolved configuration with its provenance, for the interface.

    Args:
        resolution: What `load_settings` produced.

    Returns:
        One row per setting, in declaration order. The values come from
        `resolved_configuration`, so this surface cannot report a secret the
        boot log withholds.
    """
    rendered = resolved_configuration(resolution.settings)
    return tuple(
        SettingReport(
            name=name,
            variable=variable_for(name),
            value=rendered[name],
            source=resolution.sources.get(name, SettingSource.DEFAULT),
            secret=name in SECRET_SETTINGS,
            live=name in LIVE_SETTINGS,
            writable=name not in BOOTSTRAP_SETTINGS,
        )
        for name in Settings.model_fields
    )


#:= docs/specs/architecture/index.md#req-009-configuration-is-validated-and-self-reporting
#:% Every component that reads configuration from its environment MUST fail to start
#:% when it encounters a variable matching its own prefix that it does not
#:% recognise, and MUST emit its fully resolved configuration at startup with every
#:% value marked secret replaced by a redacted placeholder.
def log_resolved_configuration(resolution: Resolution) -> None:
    """Say out loud what the application is actually running on.

    Args:
        resolution: What `load_settings` produced.
    """
    rendered = resolved_configuration(resolution.settings)
    for name, value in rendered.items():
        _LOGGER.info(
            "configuration.resolved %s=%s (%s)",
            name,
            value,
            resolution.sources.get(name, SettingSource.DEFAULT).value,
        )
    for name in resolution.declared_but_unread:
        _LOGGER.warning(
            "configuration.declared_but_unread %s is declared for this robot "
            "but this application does not read it, so setting it has no effect "
            "here",
            name,
        )
    for name in resolution.ignored_overrides:
        _LOGGER.warning(
            "configuration.override_ignored %s cannot be supplied by the "
            "overrides file; it names no setting, or it decides where that file "
            "lives. Delete it from the settings interface.",
            name,
        )


class OverrideStore:
    """The settings the interface wrote, kept beside the application's state.

    A JSON object of setting name to the string an operator typed, stored
    outside the wheel so that reinstalling the application does not discard it.
    Values are held as strings rather than parsed types because that is what a
    form submits and what an environment variable is, so one validation path
    covers both layers.

    **It holds secrets in plain text**, at the same trust level as the
    environment the daemon starts this application with — anything that can read
    the file can already read that environment. Two things follow from that and
    both are in `save`: the file is created owner-only rather than created and
    then narrowed, because a `chmod` after the write leaves a window in which the
    umask decided who could read it; and it is renamed into place rather than
    written in place, because a process stopped mid-write would otherwise leave
    truncated JSON that the next start refuses to parse — turning a settings
    change into a robot that will not boot.
    """

    def __init__(self, path: Path) -> None:
        """Say where the overrides are kept.

        Args:
            path: The file. Its parent is created on write rather than here, so
                constructing a store touches no disk.
        """
        self._path = path

    @property
    def path(self) -> Path:
        """Where the overrides are kept.

        Returns:
            The file's path, reported on the settings page so that an operator
            can find it.
        """
        return self._path

    def load(self) -> dict[str, str]:
        """Read what was written, tolerating a file that is not there.

        A file that is there and unreadable is a different matter and is
        reported: an operator who edited it by hand and broke it should be told
        so, not left wondering why their settings stopped applying.

        Returns:
            Setting name to the string value written for it.

        Raises:
            ConfigurationError: If the file exists and is not a JSON object of
                strings.
        """
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as error:
            message = f"the settings overrides at {self._path} cannot be read: {error}"
            raise ConfigurationError(message) from error

        try:
            parsed: Any = json.loads(raw)
        except json.JSONDecodeError as error:
            message = (
                f"the settings overrides at {self._path} are not valid JSON: {error}"
            )
            raise ConfigurationError(message) from error

        if not isinstance(parsed, dict):
            message = (
                f"the settings overrides at {self._path} must be a JSON object "
                f"of setting name to value"
            )
            raise ConfigurationError(message)

        written: dict[str, str] = {}
        for name, value in parsed.items():
            if not isinstance(name, str) or not isinstance(value, str):
                message = (
                    f"the settings overrides at {self._path} must map setting "
                    f"names to strings"
                )
                raise ConfigurationError(message)
            written[name] = value
        return written

    def save(self, overrides: Mapping[str, str]) -> None:
        """Replace the file with these overrides.

        Args:
            overrides: Setting name to the string value to write. An empty
                mapping leaves an empty object rather than deleting the file, so
                "everything is back at its environment value" is a state the
                file records rather than one it is silent about.

        Raises:
            ConfigurationError: If the file cannot be written, which is a thing
                an operator needs told rather than a change that appears to have
                been accepted.
        """
        payload = json.dumps(dict(sorted(overrides.items())), indent=2, sort_keys=True)
        temporary = self._path.with_name(f"{self._path.name}.new")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Owner-only from the moment the file exists, and complete before it
            # is visible under its own name. See the class docstring for why
            # both matter for a file that holds a credential.
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(f"{payload}\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self._path)
        except OSError as error:
            message = (
                f"the settings overrides at {self._path} cannot be written: {error}"
            )
            raise ConfigurationError(message) from error


def overrides_path(environ: Mapping[str, str] | None = None) -> Path:
    """Find the overrides file before the settings that name its directory exist.

    The state directory is itself a setting, so the file that overrides settings
    lives at a path one of those settings decides. That circularity is resolved
    the way every configuration layer resolves it: the environment is read
    first, on its own, and only for that one value.

    It lives here rather than in the composition root because it is one of the
    two bootstrap reads this module owns, and the pair has to agree. The other
    is `daemon_app._settings_port`. Both resolve a `BOOTSTRAP_SETTINGS` name
    from the environment and the model default alone, never from the overrides
    file, and keeping the rule next to `BOOTSTRAP_SETTINGS` is what stops one of
    them acquiring a third layer the other does not have. The composition root
    is this function's only caller today; that is not why it is here.

    Args:
        environ: The environment to read. Defaults to the process environment.

    Returns:
        Where the overrides are kept.
    """
    source = os.environ if environ is None else environ
    configured = source.get(variable_for("state_dir"), "")
    default = Settings.model_fields["state_dir"].default
    return Path(configured or str(default)).expanduser() / OVERRIDES_FILENAME


def state_directory(settings: Settings) -> Path:
    """Where this application keeps everything that outlives the wheel.

    Args:
        settings: The settings in effect.

    Returns:
        The directory, with a leading `~` expanded. Nothing is created here.
    """
    return Path(settings.state_dir).expanduser()
