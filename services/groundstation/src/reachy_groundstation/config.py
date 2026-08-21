"""Settings, read from the environment once by a function the entry point calls.

The predecessor to this service had a configuration reader that nothing ever
called, so every environment override was silently a dataclass default and the
mistake stayed quiet for months. Two things here are shaped by that.

`load_settings` is the only way settings come into existence, and it is a pure
function of the mapping it is given — `os.environ` in production, a dictionary in
a test. There is no second path that reads the environment behind its back, so
"is this variable actually read?" is answered by one function with tests on it
rather than by hoping.

And an unrecognised variable under this component's prefix is fatal, naming
itself, which is what architecture REQ-009 requires. A typo is reported at
startup instead of leaving the operator looking at a value they believe they
set.

Which settings are secret is declared in exactly one place — `SECRET_SETTINGS`,
derived from the field types — and both self-reporting surfaces read it. The boot
log and the run-time configuration endpoint call the same
`resolved_configuration`, so a secret cannot be redacted in one and forgotten in
the other when a setting is added.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final, Literal

# TID253 bans importing pydantic at module level outside the contracts package,
# so that no consumer declares a second copy of a wire type. This model is
# configuration and never crosses the wire; `SecretStr` is what marks a setting
# secret, and `Field` is what constrains the scalars. The ban is suppressed for
# this one import, as the root AGENTS.md describes.
from pydantic import (  # noqa: TID253  # configuration, not a wire type: `SecretStr` is what marks a setting secret and `Field` is what bounds the scalars, and none of it crosses the link
    Field,
    SecretStr,
    ValidationError,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "ENV_PREFIX",
    "REDACTED_SET",
    "REDACTED_UNSET",
    "SECRET_SETTINGS",
    "ConfigurationError",
    "Settings",
    "load_settings",
    "resolved_configuration",
    "unrecognised_variables",
]

ENV_PREFIX: Final = "REACHY_GROUNDSTATION_"

# What a secret looks like in a log line and in the configuration endpoint's
# answer. The endpoint is reachable by anything that can reach the service, so
# the question it answers is "is it set?" and never "what is it?".
REDACTED_SET: Final = "<set>"
REDACTED_UNSET: Final = "<unset>"


class ConfigurationError(RuntimeError):
    """Startup cannot proceed because the environment is not usable."""


class Settings(BaseSettings):
    """Everything this service reads from its environment.

    Every field is a scalar and every field but the credential has a default, so
    the resolved configuration is complete whether or not an operator set
    anything — which is what makes the boot dump worth reading.

    Attributes:
        credential: The shared secret a client presents to open a session. It
            has no default: a groundstation that authenticated nothing because
            nobody configured it would be a worse failure than one that refuses
            to start.
        host: The address the server binds. The default is the loopback
            interface; the container image binds every interface instead,
            because that is a deployment decision and not a default.
        port: The port the server binds.
        queue_bound: How many frames one session may hold before the oldest is
            dropped. Deliberately small — see the change document.
        capability_timeout_seconds: How long one capability may spend on one
            frame before that capability's answer is abandoned.
        handshake_timeout_seconds: How long a connected client has to present
            its offer before the session is closed.
        warm_up_timeout_seconds: How long one capability may spend warming up
            before it is recorded as unhealthy.
        max_message_bytes: The largest message of either kind the session will
            accept, counted in the bytes the client sent — a control message
            arrives already decoded, and UTF-8 spends up to four bytes on each
            of its characters.
        log_level: The lowest severity emitted.
        log_format: `json` for machines, `console` for a terminal.
        service_name: What this process calls itself in traces and metrics.
        models_dir: The directory model files are read from. They are put there
            when the artifact is built and never fetched at run time, which is
            what groundstation REQ-023 requires; the default is where the
            container image bakes them.
        inference_intra_op_threads: How many threads one model runtime may use
            within a single operator. This is the number groundstation REQ-027
            is about — left to the runtime, it sizes itself against the host and
            takes every core it finds. The default of four is where the
            predecessor's measured curve flattened: 93 ms at one thread, 51 ms
            at four and 55 ms at six, so the sixth thread cost time rather than
            saving it. The curve is re-measured by change 0014 on this
            repository's own hardware; until then four is a starting point that
            was measured somewhere, not a constant that was guessed.
        inference_inter_op_threads: How many operators one model runtime may run
            at once. One, because these are single-branch detection graphs with
            almost nothing to overlap, and a second scheduler thread spends more
            on coordination than the overlap returns.
        inference_providers: The execution providers to try, best first, as a
            comma-separated list. Anything this build has not got is skipped and
            the CPU provider is always the last resort, so the accelerated image
            variant sets this and nothing else changes.
        face_enabled: Whether the face detector is built and offered.
        face_score_threshold: The confidence a face detection must reach to be
            reported. Perception REQ-039's whole point: settable here rather
            than rebuilt into the artifact.
        face_nms_threshold: How much two face boxes may overlap before the
            lower-scoring one is suppressed, as intersection over union.
        gesture_enabled: Whether the gesture detector is built and offered. Off
            by default — the perception spec defers the model choice, and this
            build wires no gesture model at all, so switching it on offers a
            capability that answers every frame with no gestures.
        gesture_score_threshold: The confidence a gesture must reach to be
            reported.
        gesture_sample_interval: How many frames apart the gesture classifier
            runs. The predecessor sampled every fourth frame to bound the cost;
            here that rate is configuration, because the right value depends on
            the classifier eventually chosen. One means every frame.
    """

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        extra="forbid",
        frozen=True,
        validate_default=True,
    )

    credential: SecretStr = Field(min_length=1)
    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=8080, ge=1, le=65535)
    queue_bound: int = Field(default=2, ge=1, le=1024)
    capability_timeout_seconds: float = Field(default=5.0, gt=0.0, le=600.0)
    handshake_timeout_seconds: float = Field(default=10.0, gt=0.0, le=600.0)
    warm_up_timeout_seconds: float = Field(default=60.0, gt=0.0, le=3600.0)
    max_message_bytes: int = Field(
        default=4 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024
    )
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    log_format: Literal["json", "console"] = "json"
    service_name: str = Field(default="reachy-groundstation", min_length=1)

    #:= docs/specs/groundstation/index.md#req-023-model-files-are-present-in-the-image
    #:% The service MUST load every model from a file already present in its deployed
    #:% artifact, and MUST NOT fetch model weights over the network at run time.
    models_dir: str = Field(default="/opt/reachy/models", min_length=1)

    #:= docs/specs/groundstation/index.md#req-027-inference-parallelism-is-bounded-by-configuration
    #:% The service MUST constrain each model runtime to a configured number of threads
    #:% rather than letting it size itself against the host.
    inference_intra_op_threads: int = Field(default=4, ge=1, le=256)
    inference_inter_op_threads: int = Field(default=1, ge=1, le=256)
    inference_providers: str = Field(default="CPUExecutionProvider", min_length=1)

    #:= docs/specs/perception/index.md#req-039-detection-thresholds-are-configuration
    #:% The confidence threshold for each detector MUST be settable without rebuilding
    #:% the artifact.
    face_enabled: bool = True
    face_score_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    face_nms_threshold: float = Field(default=0.3, ge=0.0, le=1.0)
    gesture_enabled: bool = False
    gesture_score_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    gesture_sample_interval: int = Field(default=4, ge=1, le=1024)


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


#:= docs/specs/architecture/index.md#req-009-configuration-is-validated-and-self-reporting
#:% Every component that reads configuration from its environment MUST fail to start
#:% when it encounters a variable matching its own prefix that it does not
#:% recognise, and MUST emit its fully resolved configuration at startup with every
#:% value marked secret replaced by a redacted placeholder.
SECRET_SETTINGS: Final[frozenset[str]] = _secret_settings()


def _variable_for(field_name: str) -> str:
    """Name the environment variable a setting is read from.

    Args:
        field_name: The setting's name on the model.

    Returns:
        The variable name, prefixed and upper-cased.
    """
    return f"{ENV_PREFIX}{field_name.upper()}"


def unrecognised_variables(environ: Mapping[str, str]) -> tuple[str, ...]:
    """List the prefixed variables this service does not know what to do with.

    Args:
        environ: The environment to inspect.

    Returns:
        The offending variable names, sorted, so the message is stable.
    """
    known = {_variable_for(name) for name in Settings.model_fields}
    return tuple(
        sorted(
            name
            for name in environ
            if name.startswith(ENV_PREFIX) and name not in known
        ),
    )


def _declared_values(environ: Mapping[str, str]) -> dict[str, str]:
    """Pick out the variables that correspond to a setting.

    Args:
        environ: The environment to read.

    Returns:
        Setting name to raw string value, for the settings that were set.
    """
    return {
        name: environ[variable]
        for name in Settings.model_fields
        if (variable := _variable_for(name)) in environ
    }


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Resolve the settings, or refuse to start.

    Args:
        environ: The environment to read. Defaults to the process environment;
            a test passes a mapping instead, which is what keeps this function
            free of input and output.

    Returns:
        The resolved settings.

    Raises:
        ConfigurationError: If a prefixed variable is not recognised, or if a
            recognised one does not parse. Both name the variable.
    """
    source = os.environ if environ is None else environ

    unknown = unrecognised_variables(source)
    if unknown:
        message = (
            f"unrecognised configuration variable(s): {', '.join(unknown)}. "
            f"Every {ENV_PREFIX}* variable must name a known setting; "
            f"the known ones are "
            f"{', '.join(sorted(_variable_for(name) for name in Settings.model_fields))}."
        )
        raise ConfigurationError(message)

    try:
        return Settings.model_validate(_declared_values(source))
    except ValidationError as error:
        faults = "; ".join(
            f"{_variable_for(str(fault['loc'][0])) if fault['loc'] else '(none)'}: "
            f"{fault['msg']}"
            for fault in error.errors()
        )
        message = f"configuration is not usable: {faults}"
        raise ConfigurationError(message) from error


def resolved_configuration(settings: Settings) -> dict[str, object]:
    """Render every setting in effect, with the secrets reported as set or not.

    This is the one renderer. The boot log and the configuration endpoint both
    call it, so a setting added later is redacted on both surfaces or on
    neither, and there is no second copy to forget.

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
        else:
            rendered[name] = value
    return rendered
