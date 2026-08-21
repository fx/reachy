"""The deployment files, checked against the code they deploy.

Every test here reads a tracked file and compares it with something derived from
source, and each one exists because the two would otherwise be free to drift in
the direction nobody notices: a setting added to `config.py` and not to
`.env.example`, a port changed in one place and scraped in another, a workspace
member added and not copied into the build, an interpreter pinned twice.

They are contract tests rather than unit tests and each says so with
`@pytest.mark.filesystem`, for the reason the root `AGENTS.md` gives: the bytes
on disk are the thing under test. A fake would pin whatever the fake was told to
return, which is precisely the failure mode — "the deployment documentation
matches the deployment documentation" — these exist to rule out.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from reachy_groundstation.config import (
    ENV_PREFIX,
    Settings,
    load_settings,
    unrecognised_variables,
)

_REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[3]
_SERVICE_ROOT: Final = Path(__file__).resolve().parents[1]
_DEPLOY: Final = _SERVICE_ROOT / "deploy"

_DOCKERFILE: Final = _SERVICE_ROOT / "Dockerfile"
_COMPOSE: Final = _DEPLOY / "compose.yaml"
_ENV_EXAMPLE: Final = _DEPLOY / ".env.example"
_PROMETHEUS: Final = _DEPLOY / "prometheus.yml"

# The compose service the scrape configuration must target, and the variables
# compose reads that are compose's rather than the service's. They deliberately
# carry no `REACHY_GROUNDSTATION_` prefix: the service refuses to start on an
# unrecognised variable under its own prefix, and `env_file` hands it everything
# in the file.
_SERVICE_NAME: Final = "groundstation"
_COMPOSE_ONLY_VARIABLES: Final[frozenset[str]] = frozenset(
    {
        "GROUNDSTATION_IMAGE",
        "GROUNDSTATION_PUBLISH",
        "PROMETHEUS_IMAGE",
        "PROMETHEUS_PUBLISH",
    },
)

# `${NAME}`, `${NAME:-default}` and `$NAME` alike, which is every form compose
# interpolates.
_INTERPOLATION: Final = re.compile(r"\$\{?([A-Z][A-Z0-9_]*)")


def _read_env_file(path: Path) -> dict[str, str]:
    """Parse an environment file into the variables it sets.

    Deliberately small: this understands `KEY=value`, `#` comments and blank
    lines, which is what the file uses and what compose's own `env_file` reader
    accepts without quoting rules entering the picture.

    Args:
        path: The file to read.

    Returns:
        Variable name to value, in the order the file declares them.
    """
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        values[name.strip()] = value.strip()
    return values


def _load_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML document.

    Args:
        path: The file to read.

    Returns:
        The document, which every file this is used on is a mapping.
    """
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{path} is not a YAML mapping"
    return loaded


def _settings_variables() -> frozenset[str]:
    """Name the environment variable behind every setting.

    Returns:
        One prefixed, upper-cased variable name per field on `Settings`.
    """
    return frozenset(f"{ENV_PREFIX}{name.upper()}" for name in Settings.model_fields)


#:= docs/specs/architecture/index.md#req-009-configuration-is-validated-and-self-reporting
#:% Every component that reads configuration from its environment MUST fail to start
#:% when it encounters a variable matching its own prefix that it does not
#:% recognise, and MUST emit its fully resolved configuration at startup with every
#:% value marked secret replaced by a redacted placeholder.
@pytest.mark.filesystem  # the example file on disk is the thing under test
def test_the_example_environment_documents_every_setting_and_no_others() -> None:
    """`.env.example` and the settings model cannot disagree about what exists."""
    documented = {
        name for name in _read_env_file(_ENV_EXAMPLE) if name.startswith(ENV_PREFIX)
    }
    assert documented == _settings_variables()


@pytest.mark.filesystem  # the example file on disk is the thing under test
def test_the_example_environment_is_one_the_service_would_start_on() -> None:
    """Every documented value parses, and none of them is unrecognised."""
    declared = _read_env_file(_ENV_EXAMPLE)
    assert unrecognised_variables(declared) == ()
    settings = load_settings(declared)
    assert settings.credential.get_secret_value() != ""


@pytest.mark.filesystem  # the example file on disk is the thing under test
def test_the_example_environment_carries_the_defaults_the_service_carries() -> None:
    """A re-defaulted setting is a failing test, not stale documentation.

    The credential is excluded because it has no default: a groundstation that
    authenticated nothing because nobody configured it would be a worse failure
    than one that refuses to start.

    The host is excluded because the image overrides it deliberately — the
    loopback default is right for a process started by hand and wrong for a
    container, where nothing outside the network namespace could reach it.
    """
    documented = load_settings(_read_env_file(_ENV_EXAMPLE))
    reference = Settings.model_validate(
        # S104 is bandit's "binding to all interfaces". That is exactly what the
        # image does and what this asserts the example documents: a container
        # binding the loopback interface could be reached by nothing outside its
        # own network namespace.
        {"credential": "placeholder", "host": "0.0.0.0"},  # noqa: S104  # the container's deliberate bind address, which is the value under test
    )
    for name in Settings.model_fields:
        if name == "credential":
            continue
        assert getattr(documented, name) == getattr(reference, name), name


@pytest.mark.filesystem  # the compose file on disk is the thing under test
def test_the_compose_file_reads_only_variables_the_example_documents() -> None:
    """Nothing compose interpolates is missing from the file operators copy."""
    interpolated = set(_INTERPOLATION.findall(_COMPOSE.read_text(encoding="utf-8")))
    documented = set(_read_env_file(_ENV_EXAMPLE))
    assert interpolated <= documented
    assert interpolated == _COMPOSE_ONLY_VARIABLES


@pytest.mark.filesystem  # the compose file on disk is the thing under test
def test_the_compose_only_variables_cannot_reach_the_service_as_settings() -> None:
    """A compose variable under the service's prefix would stop it starting."""
    for name in _COMPOSE_ONLY_VARIABLES:
        assert not name.startswith(ENV_PREFIX)
    assert unrecognised_variables(dict.fromkeys(_COMPOSE_ONLY_VARIABLES, "")) == ()


@pytest.mark.filesystem  # the compose file on disk is the thing under test
def test_the_compose_file_hands_the_service_the_example_file_s_real_sibling() -> None:
    """The file operators are told to copy is the file compose reads."""
    compose = _load_yaml(_COMPOSE)
    service = compose["services"][_SERVICE_NAME]
    assert service["env_file"] == [".env"]
    assert _ENV_EXAMPLE.name == ".env.example"
    assert _ENV_EXAMPLE.parent == _COMPOSE.parent


#:= docs/specs/groundstation/index.md#req-029-per-stage-timings-are-measured-and-exposed
#:% The service MUST record the duration of each pipeline stage separately and
#:% expose those durations as metrics.
@pytest.mark.filesystem  # the scrape configuration on disk is the thing under test
def test_the_scrape_configuration_points_at_the_port_the_service_binds() -> None:
    """The predecessor exposed metrics nothing collected; this is why it cannot."""
    scrape = _load_yaml(_PROMETHEUS)
    jobs = scrape["scrape_configs"]
    assert len(jobs) == 1
    targets = jobs[0]["static_configs"][0]["targets"]
    port = _read_env_file(_ENV_EXAMPLE)[f"{ENV_PREFIX}PORT"]
    assert targets == [f"{_SERVICE_NAME}:{port}"]
    assert jobs[0]["metrics_path"] == "/metrics"


@pytest.mark.filesystem  # the compose file on disk is the thing under test
def test_the_collector_scrapes_the_service_compose_actually_runs() -> None:
    """The scrape target is a compose service name, so it has to be one."""
    compose = _load_yaml(_COMPOSE)
    assert _SERVICE_NAME in compose["services"]
    scrape = _load_yaml(_PROMETHEUS)
    mounted = compose["services"]["prometheus"]["volumes"][0]
    assert mounted.startswith(f"./{_PROMETHEUS.name}:")
    assert scrape["scrape_configs"][0]["static_configs"][0]["targets"]


@pytest.mark.filesystem  # the compose file on disk is the thing under test
def test_the_collector_image_is_pinned_by_digest() -> None:
    """A tag is a mutable ref, here as much as in a workflow."""
    compose = _load_yaml(_COMPOSE)
    assert "@sha256:" in compose["services"]["prometheus"]["image"]
    assert "@sha256:" in _read_env_file(_ENV_EXAMPLE)["PROMETHEUS_IMAGE"]


#:= docs/specs/groundstation/index.md#req-024-model-provenance-is-recorded-and-verified
#:% Every model file MUST be pinned by content hash, and the build MUST fail when a
#:% fetched file's hash does not match the pinned value.
@pytest.mark.filesystem  # the build file on disk is the thing under test
def test_the_build_fetches_models_through_the_module_just_models_runs() -> None:
    """One fetcher, one verification, called from two places.

    The Dockerfile cannot call `just models`: that recipe runs the fetch under
    `uv run --all-packages`, which would install the test and parity groups into
    an image that ships neither. What it can do is call the same entry point,
    and this is what stops the two becoming two implementations.
    """
    entry_point = "reachy_groundstation.models.fetch"
    assert entry_point in _DOCKERFILE.read_text(encoding="utf-8")
    justfile = (_REPOSITORY_ROOT / "Justfile").read_text(encoding="utf-8")
    recipe = justfile.split("\nmodels directory=", 1)[1].split("\n\n", 1)[0]
    assert entry_point in recipe


@pytest.mark.filesystem  # the build file on disk is the thing under test
def test_the_build_copies_a_manifest_for_every_workspace_member() -> None:
    """A member added without a `COPY` here is a red test, not a red build."""
    root = tomllib.loads(
        (_REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"),
    )
    members: list[str] = root["tool"]["uv"]["workspace"]["members"]
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    for member in members:
        assert f"COPY {member}/pyproject.toml {member}/" in dockerfile, member


@pytest.mark.filesystem  # the build file on disk is the thing under test
def test_the_image_runs_the_interpreter_the_toolchain_pins() -> None:
    """`mise.toml` is the only place a version of Python is chosen."""
    mise = tomllib.loads((_REPOSITORY_ROOT / "mise.toml").read_text(encoding="utf-8"))
    pinned: str = mise["tools"]["python"]
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    assert f"ARG PYTHON_VERSION={pinned}\n" in dockerfile


@pytest.mark.filesystem  # the build file on disk is the thing under test
def test_every_base_image_is_pinned_by_digest() -> None:
    """A rebuilt base must not change what ships with no diff to review."""
    references = re.findall(
        r"^ARG (?:UV_IMAGE|BUILDER_BASE|RUNTIME_BASE)=(\S+)$",
        _DOCKERFILE.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    assert len(references) == 3
    for reference in references:
        assert "@sha256:" in reference, reference

    # The accelerated variant's base is chosen by the recipe rather than by the
    # Dockerfile's default, so it is pinned there and checked here too.
    justfile = (_REPOSITORY_ROOT / "Justfile").read_text(encoding="utf-8")
    accelerated = re.findall(r"RUNTIME_BASE=(\S+?)'", justfile)
    assert accelerated
    for reference in accelerated:
        assert "@sha256:" in reference, reference


#:= docs/specs/groundstation/index.md#req-023-model-files-are-present-in-the-image
#:% The service MUST load every model from a file already present in its deployed
#:% artifact, and MUST NOT fetch model weights over the network at run time.
@pytest.mark.filesystem  # the build file on disk is the thing under test
def test_the_image_reads_models_from_where_the_build_put_them() -> None:
    """The baked directory and the setting's default are the same directory."""
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    baked = Settings.model_fields["models_dir"].default
    assert f"/opt/reachy/models {baked}" in dockerfile
    assert f"REACHY_GROUNDSTATION_MODELS_DIR={baked}" in dockerfile
