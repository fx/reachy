"""Transactional and version-aware rollback contract in the deployment runbook."""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

RUNBOOK: Final = (
    Path(__file__).resolve().parents[3] / "docs" / "ops" / "satellite-deployment.md"
)


@pytest.mark.filesystem
def test_rollback_stops_and_releases_candidate_before_any_restore_write() -> None:
    """Artifact/config writes are forbidden until candidate motion ownership is gone."""
    text = RUNBOOK.read_text(encoding="utf-8")
    rollback = " ".join(text.split("### Rollback", 1)[1].split("\n---", 1)[0].split())

    stop = rollback.index("stop and release the candidate application")
    configuration = rollback.index("restore all non-body configuration values")
    artifact = rollback.index("install the retained checksum-verified released wheel")
    restart = rollback.index("restart the retained application")

    assert stop < configuration < artifact < restart


@pytest.mark.filesystem
def test_rollback_preserves_exact_backup_but_forces_body_false_with_accounting() -> (
    None
):
    """The immutable backup and deliberately safer effective target are distinct."""
    text = RUNBOOK.read_text(encoding="utf-8")
    rollback = " ".join(text.split("### Rollback", 1)[1].split("\n---", 1)[0].split())

    assert "preserve the byte-for-byte configuration backup unchanged" in rollback
    assert "force only body motion to false as a safety override" in rollback
    assert "documented divergence from the byte backup" in rollback
    assert "record and verify the effective configuration checksum" in rollback


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
