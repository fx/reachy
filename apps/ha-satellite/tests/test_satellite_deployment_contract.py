"""Transactional and version-aware rollback contract in the deployment runbook."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

RUNBOOK: Final = (
    Path(__file__).resolve().parents[3] / "docs" / "ops" / "satellite-deployment.md"
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
    winner = next(
        index
        for index, step in enumerate(steps)
        if "highest-precedence effective body-setting layer" in step
        and "override when present" in step
    )
    force_false = next(
        index
        for index, step in enumerate(steps)
        if "that winning mutable layer" in step
        and "force" in step
        and "`false`" in step
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
        if "resolved `/config`" in step and "body motion is `false`" in step
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

    assert stop < managed_restore < override_restore < winner < force_false < checksums
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
