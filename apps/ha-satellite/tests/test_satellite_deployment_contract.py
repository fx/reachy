"""Transactional and version-aware rollback contract in the deployment runbook."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[3]
RUNBOOK: Final = REPOSITORY_ROOT / "docs" / "ops" / "satellite-deployment.md"
CHANGE_0019: Final = (
    REPOSITORY_ROOT
    / "docs"
    / "changes"
    / "0019-predictive-gaze-and-coordinated-motion.md"
)
_STALE_COMPLETION_REFERENCES: Final = (
    (RUNBOOK, "pending actual coordinator execution"),
    (REPOSITORY_ROOT / "AGENTS.md", "canary outcome bookkeeping remains pending"),
    (
        REPOSITORY_ROOT / "apps" / "ha-satellite" / "AGENTS.md",
        "head/body canary outcome bookkeeping remains pending",
    ),
    (
        REPOSITORY_ROOT / ".duvet" / "config.toml",
        "live canary outcome bookkeeping remains pending",
    ),
    (REPOSITORY_ROOT / "docs" / "tasks.md", "these steps have never been run"),
    (
        REPOSITORY_ROOT / "docs" / "tasks.md",
        "the predictive head-only canary stays",
    ),
    (
        REPOSITORY_ROOT / "docs" / "tasks.md",
        "run the separately gated coordinated-body canary",
    ),
    (CHANGE_0019, "deferred to the canary outcome"),
)
_NUMBERED_STEP: Final = re.compile(r"(?ms)^\d+\. .+?(?=^\d+\. |\Z)")


def _section(text: str, heading: str, next_heading: str) -> str:
    """Return one exact runbook subsection between sibling headings."""
    return text.split(heading, 1)[1].split(next_heading, 1)[0]


def _steps(section: str) -> tuple[str, ...]:
    """Parse structural ordered-list items without depending on line wrapping."""
    return tuple(
        " ".join(match.group().split()) for match in _NUMBERED_STEP.finditer(section)
    )


@pytest.mark.filesystem
def test_completed_rollout_has_no_stale_pending_canary_references() -> None:
    """A completed change cannot leave its owned rollout references pending."""
    change = CHANGE_0019.read_text(encoding="utf-8")
    status = re.search(r"(?m)^\*\*Status:\*\* (.+)$", change)

    assert status is not None
    assert status.group(1) == "complete"
    for path, stale_phrase in _STALE_COMPLETION_REFERENCES:
        text = " ".join(path.read_text(encoding="utf-8").split()).casefold()
        relative_path = path.relative_to(REPOSITORY_ROOT)
        assert stale_phrase not in text, (
            f"stale completion reference in {relative_path}"
        )

    runbook = RUNBOOK.read_text(encoding="utf-8")
    assert "[change 0019 outcome]" in runbook
    assert (
        "../changes/0019-predictive-gaze-and-coordinated-motion.md#outcome" in runbook
    )


@pytest.mark.filesystem
def test_preflight_checksums_every_config_layer_before_candidate_install() -> None:
    """Both mutable precedence layers are hashed before candidate bytes can land."""
    text = RUNBOOK.read_text(encoding="utf-8")
    preflight = _steps(
        _section(
            text,
            "### Retain the rollback target first",
            "### Abort thresholds",
        )
    )
    staged = _steps(_section(text, "### Staged execution", "### Rollback"))

    managed = next(
        index
        for index, step in enumerate(preflight)
        if "managed daemon environment layer" in step and "SHA256" in step
    )
    override = next(
        index
        for index, step in enumerate(preflight)
        if "application overrides layer" in step and "SHA256" in step
    )
    seal = next(
        index
        for index, step in enumerate(preflight)
        if "each exact configuration-layer backup" in step and "original SHA256" in step
    )
    install = next(
        index
        for index, step in enumerate(staged)
        if "install the verified candidate" in step
    )

    assert text.index("### Retain the rollback target first") < text.index(
        "### Staged execution"
    )
    assert managed < seal
    assert override < seal
    assert install == 0


@pytest.mark.filesystem
def test_body_setting_uses_layer_specific_key_names() -> None:
    """Managed environment, override JSON and `/config` use distinct spellings."""
    text = RUNBOOK.read_text(encoding="utf-8")
    preflight = _steps(
        _section(
            text,
            "### Retain the rollback target first",
            "### Abort thresholds",
        )
    )
    staged = _steps(_section(text, "### Staged execution", "### Rollback"))
    rollback = _steps(_section(text, "### Rollback", "\n---"))
    rollout_steps = preflight + staged + rollback
    environment_name = "`REACHY_SATELLITE_BODY_MOTION_ENABLED`"
    setting_name = "`body_motion_enabled`"

    override_steps = tuple(
        step
        for step in rollout_steps
        if "application override" in step
        and (setting_name in step or environment_name in step)
    )
    managed_steps = tuple(
        step
        for step in rollout_steps
        if "managed environment" in step
        and (setting_name in step or environment_name in step)
    )
    config_steps = tuple(step for step in rollout_steps if "`/config`" in step)

    assert override_steps
    assert managed_steps
    assert config_steps
    assert all(
        setting_name in step and environment_name not in step for step in override_steps
    )
    assert all(
        environment_name in step and setting_name not in step for step in managed_steps
    )
    assert all(
        setting_name in step and environment_name not in step for step in config_steps
    )
    assert any('`"body_motion_enabled": "false"`' in step for step in override_steps)
    assert any(
        "`REACHY_SATELLITE_BODY_MOTION_ENABLED=false`" in step for step in managed_steps
    )

    normalized = " ".join(
        _section(text, "## Predictive gaze canary", "\n---\n\n## Stopping").split()
    )
    assert f"application override JSON key {environment_name}" not in normalized
    assert f"managed environment variable {setting_name}" not in normalized


@pytest.mark.filesystem
def test_body_setting_values_match_each_layer_representation() -> None:
    """Override JSON stores strings while environment and `/config` keep their forms."""
    text = RUNBOOK.read_text(encoding="utf-8")
    staged = _steps(_section(text, "### Staged execution", "### Rollback"))
    rollback = _steps(_section(text, "### Rollback", "\n---"))
    rollout_steps = staged + rollback

    assert any('`"body_motion_enabled": "true"`' in step for step in staged)
    assert any('`"body_motion_enabled": "false"`' in step for step in rollback)
    assert any("`REACHY_SATELLITE_BODY_MOTION_ENABLED=true`" in step for step in staged)
    assert any(
        "`REACHY_SATELLITE_BODY_MOTION_ENABLED=false`" in step for step in rollback
    )
    config_steps = tuple(step for step in rollout_steps if "`/config`" in step)
    assert any("JSON boolean `true`" in step for step in config_steps)
    assert any("JSON boolean `false`" in step for step in config_steps)
    rollout = " ".join(rollout_steps)
    assert '`"body_motion_enabled": true`' not in rollout
    assert '`"body_motion_enabled": false`' not in rollout


@pytest.mark.filesystem
def test_rollback_stops_and_releases_candidate_before_any_restore_write() -> None:
    """Artifact/config writes are forbidden until candidate motion ownership is gone."""
    text = RUNBOOK.read_text(encoding="utf-8")
    steps = _steps(_section(text, "### Rollback", "\n---"))

    stop = next(
        index
        for index, step in enumerate(steps)
        if "stop and release the candidate application" in step
    )
    first_restore = next(
        index
        for index, step in enumerate(steps)
        if step.startswith("3. restore the managed daemon environment layer")
    )
    artifact = next(
        index
        for index, step in enumerate(steps)
        if "install the retained checksum-verified released wheel" in step
    )
    restart = next(
        index
        for index, step in enumerate(steps)
        if "restart the retained application" in step
    )

    assert stop < first_restore < artifact < restart


@pytest.mark.filesystem
def test_rollback_restores_each_layer_then_forces_body_false_in_winner() -> None:
    """Ordered layer operations preserve bytes and make the precedence winner safe."""
    text = RUNBOOK.read_text(encoding="utf-8")
    steps = _steps(_section(text, "### Rollback", "\n---"))

    stop = next(
        index
        for index, step in enumerate(steps)
        if "stop and release the candidate application" in step
    )
    managed_restore = next(
        index
        for index, step in enumerate(steps)
        if "managed daemon environment layer" in step
        and "all non-body bytes exactly" in step
    )
    override_restore = next(
        index
        for index, step in enumerate(steps)
        if "application overrides layer" in step
        and "all non-body bytes exactly" in step
    )
    override_winner = next(
        index
        for index, step in enumerate(steps)
        if "highest-precedence effective body-setting layer" in step
        and "application override JSON key `body_motion_enabled`" in step
    )
    managed_winner = next(
        index
        for index, step in enumerate(steps)
        if "highest-precedence effective body-setting layer" in step
        and "managed environment variable `REACHY_SATELLITE_BODY_MOTION_ENABLED`"
        in step
    )
    override_false = next(
        index
        for index, step in enumerate(steps)
        if "application override JSON key `body_motion_enabled`" in step
        and "force" in step
        and '`"body_motion_enabled": "false"`' in step
    )
    managed_false = next(
        index
        for index, step in enumerate(steps)
        if "managed environment variable `REACHY_SATELLITE_BODY_MOTION_ENABLED`" in step
        and "force" in step
        and "`REACHY_SATELLITE_BODY_MOTION_ENABLED=false`" in step
    )
    checksums = next(
        index
        for index, step in enumerate(steps)
        if "original SHA256" in step
        and "safety-modified SHA256" in step
        and "for each layer" in step
    )
    resolved = next(
        index
        for index, step in enumerate(steps)
        if "resolved `/config`" in step
        and "`body_motion_enabled` is JSON boolean `false`" in step
    )
    wheel = next(
        index
        for index, step in enumerate(steps)
        if "install the retained checksum-verified released wheel" in step
    )
    restart = next(
        index
        for index, step in enumerate(steps)
        if "restart the retained application with motion ownership inhibited" in step
    )
    motion = next(
        index
        for index, step in enumerate(steps)
        if "permit any motion restart or canary" in step
    )

    assert stop < managed_restore < override_restore < override_winner
    assert override_winner < managed_winner < override_false < managed_false < checksums
    assert checksums < wheel < restart < resolved < motion


@pytest.mark.filesystem
def test_rollback_status_verification_is_retained_artifact_version_aware() -> None:
    """A healthy older release need not implement the candidate controller schema."""
    text = RUNBOOK.read_text(encoding="utf-8")
    rollback = " ".join(text.split("### Rollback", 1)[1].split("\n---", 1)[0].split())

    assert "always require the legacy status keys" in rollback
    for key in ("`running`", "`pipeline`", "`gaze`", "`tracking`", "`idle`"):
        assert key in rollback
    assert "absence of `controller` is valid for an older retained artifact" in rollback
    assert "only when `controller` is present" in rollback
    assert "fault `none` and safe hold `false`" in rollback
