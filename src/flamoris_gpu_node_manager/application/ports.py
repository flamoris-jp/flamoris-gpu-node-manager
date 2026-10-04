"""Narrow ports used by the runtime authority."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from typing import Protocol

from flamoris_gpu_node_manager.domain.models import RuntimeProfile, ServiceState


class SystemdPort(Protocol):
    def get_state(self, service: str) -> ServiceState: ...

    def start(self, service: str) -> None: ...

    def stop(self, service: str) -> None: ...

    def wait_active(self, service: str, timeout: float) -> bool: ...

    def wait_inactive(self, service: str, timeout: float) -> bool: ...


class ResourcePort(Protocol):
    def is_released(self, profile: RuntimeProfile) -> bool: ...

    def wait_released(self, profile: RuntimeProfile, timeout: float) -> bool: ...


class HealthPort(Protocol):
    def check(self, profile: RuntimeProfile) -> bool: ...

    def wait_healthy(self, profile: RuntimeProfile, timeout: float) -> bool: ...


class TransitionLockPort(Protocol):
    def hold(self, timeout: float) -> AbstractContextManager[None]: ...


class EvidenceInvalidationPort(Protocol):
    def hold(
        self, profiles: Sequence[RuntimeProfile], timeout: float
    ) -> AbstractContextManager[None]: ...
