"""Composition root used by local adapters."""

from __future__ import annotations

from pathlib import Path

from flamoris_gpu_node_manager.application.manager import RuntimeManager
from flamoris_gpu_node_manager.application.ports import EvidenceInvalidationPort
from flamoris_gpu_node_manager.domain.errors import ProfileValidationError
from flamoris_gpu_node_manager.infrastructure.config import load_registry
from flamoris_gpu_node_manager.infrastructure.health import HealthAdapter
from flamoris_gpu_node_manager.infrastructure.locking import (
    DEFAULT_TRANSITION_LOCK_PATH,
    FileTransitionLock,
)
from flamoris_gpu_node_manager.infrastructure.resource import ProcessResourceAdapter
from flamoris_gpu_node_manager.infrastructure.runtime_evidence import FileEvidenceInvalidator
from flamoris_gpu_node_manager.infrastructure.systemd import SystemdAdapter


def build_manager(
    config_directory: Path,
    *,
    lock_path: Path = DEFAULT_TRANSITION_LOCK_PATH,
    lock_timeout: float = 5.0,
    evidence_lifecycle: EvidenceInvalidationPort | None = None,
) -> RuntimeManager:
    registry = load_registry(config_directory)
    if any(
        profile.evidence_record is not None
        and lock_path
        in {
            profile.evidence_record,
            Path(str(profile.evidence_record) + ".lock"),
            Path(str(profile.evidence_record) + ".identity.json"),
            Path(str(profile.evidence_record) + ".epoch.json"),
        }
        for profile in registry
    ):
        raise ProfileValidationError("transition lock and runtime evidence slot overlap")
    try:
        invalidator = FileEvidenceInvalidator(tuple(registry))
    except (ValueError, OSError) as exc:
        raise ProfileValidationError("runtime evidence slot is not safely provisioned") from exc
    systemd = SystemdAdapter()
    resources = ProcessResourceAdapter()
    health = HealthAdapter(systemd, resources)
    return RuntimeManager(
        registry,
        systemd,
        resources,
        health,
        FileTransitionLock(lock_path),
        lock_timeout=lock_timeout,
        evidence_invalidator=invalidator if evidence_lifecycle is None else evidence_lifecycle,
    )
