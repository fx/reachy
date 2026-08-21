# Documentation

## Specs

| Spec | Description | Status |
|------|-------------|--------|
| [architecture](specs/architecture/) | Workspace layout, tooling, versioning, CI gates, testing conventions, repository hygiene and documentation conventions | active |
| [robot-link](specs/robot-link/) | The wire contract between robot and groundstation — session, capability negotiation, framing, backpressure and reconnection | active |
| [groundstation](specs/groundstation/) | The off-robot service hosting heavy computation as pluggable capabilities | active |
| [perception](specs/perception/) | Face and gesture detection — model licensing, detection semantics and accuracy requirements | active |
| [ha-satellite](specs/ha-satellite/) | The robot-side ESPHome voice satellite for Home Assistant | active |
| [reachyctl](specs/reachyctl/) | The command-line tool for deploying, configuring and diagnosing a robot | active |
| [provisioning](specs/provisioning/) | Idempotent Ansible provisioning from a stock robot image to a configured state | active |
| [benchmarks](specs/benchmarks/) | The performance suite, its recorded baseline and the regression gates | active |

## Changes

| # | Change | Spec | Status | Depends On |
|---|--------|------|--------|------------|
| 0001 | [workspace-skeleton](changes/0001-workspace-skeleton.md) | [architecture](specs/architecture/) | complete | — |
| 0002 | [ci-and-hygiene-gates](changes/0002-ci-and-hygiene-gates.md) | [architecture](specs/architecture/) | complete | 0001 |
| 0003 | [contracts-package](changes/0003-contracts-package.md) | [robot-link](specs/robot-link/) | complete | 0001, 0002 |
| 0004 | [groundstation-session](changes/0004-groundstation-session.md) | [groundstation](specs/groundstation/) | complete | 0003 |
| 0005 | [perception-capability](changes/0005-perception-capability.md) | [perception](specs/perception/) | complete | 0004 |
| 0006 | [groundstation-images](changes/0006-groundstation-images.md) | [groundstation](specs/groundstation/) | complete | 0002, 0005 |
| 0007 | [reachyctl-probe](changes/0007-reachyctl-probe.md) | [reachyctl](specs/reachyctl/) | complete | 0003, 0004 |
| 0008 | [reachyctl-doctor](changes/0008-reachyctl-doctor.md) | [reachyctl](specs/reachyctl/) | draft | 0007 |
| 0009 | [reachyctl-deploy-and-config](changes/0009-reachyctl-deploy-and-config.md) | [reachyctl](specs/reachyctl/) | draft | 0002, 0008 |
| 0010 | [provisioning-ansible](changes/0010-provisioning-ansible.md) | [provisioning](specs/provisioning/) | draft | 0009 |
| 0011 | [satellite-esphome-vendoring](changes/0011-satellite-esphome-vendoring.md) | [ha-satellite](specs/ha-satellite/) | complete | 0001 |
| 0012 | [satellite-ports-and-adapters](changes/0012-satellite-ports-and-adapters.md) | [ha-satellite](specs/ha-satellite/) | draft | 0007, 0011 |
| 0013 | [satellite-behaviour-and-ui](changes/0013-satellite-behaviour-and-ui.md) | [ha-satellite](specs/ha-satellite/) | draft | 0002, 0012 |
| 0014 | [benchmarks-and-gates](changes/0014-benchmarks-and-gates.md) | [benchmarks](specs/benchmarks/) | draft | 0006, 0009, 0013 |
| 0015 | [docs-and-runbooks](changes/0015-docs-and-runbooks.md) | [architecture](specs/architecture/) | draft | 0013, 0014 |
