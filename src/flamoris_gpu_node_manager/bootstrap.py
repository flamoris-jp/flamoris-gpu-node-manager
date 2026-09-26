"""Composition root used by local adapters."""

from __future__ import annotations

from pathlib import Path

from flamoris_gpu_node_manager.application.manager import RuntimeManager
from flamoris_gpu_node_manager.infrastructure.config import load_registry
from flamoris_gpu_node_manager.infrastructure.health import HealthAdapter
from flamoris_gpu_node_manager.infrastructure.locking import (
    DEFAULT_TRANSITION_LOCK_PATH,
    FileTransitionLock,
)
from flamoris_gpu_node_manager.infrastructure.resource import ProcessResourceAdapter
from flamoris_gpu_node_manager.infrastructure.systemd import SystemdAdapter


def build_manager(
    config_directory: Path,
    *,
    lock_path: Path = DEFAULT_TRANSITION_LOCK_PATH,
    lock_timeout: float = 5.0,
) -> RuntimeManager:
    registry = load_registry(config_directory)
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
    )
