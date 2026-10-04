"""Stable domain values shared by every manager surface."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class RuntimeState(StrEnum):
    OFF = "OFF"
    STOPPING = "STOPPING"
    WAITING_FOR_GPU = "WAITING_FOR_GPU"
    STARTING = "STARTING"
    READY = "READY"
    FAILED = "FAILED"


class ServiceState(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    ACTIVATING = "activating"
    DEACTIVATING = "deactivating"
    FAILED = "failed"
    NOT_FOUND = "not-found"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProcessMatcher:
    name: str
    cmdline_contains: tuple[str, ...] = ()


@dataclass(frozen=True)
class HealthConfig:
    type: str
    url: str | None = None
    host: str | None = None
    port: int | None = None
    process_name: str | None = None
    json_pointer: str | None = None
    equals: bool | int | float | str | None = None


@dataclass(frozen=True)
class ReleaseConfig:
    type: str
    processes: tuple[ProcessMatcher, ...] = ()


@dataclass(frozen=True)
class TimeoutConfig:
    stop_seconds: float
    release_seconds: float
    start_seconds: float
    health_seconds: float


@dataclass(frozen=True)
class RuntimeProfile:
    id: str
    display_name: str
    service: str
    resource_class: str
    enabled: bool
    health: HealthConfig
    release: ReleaseConfig
    timeouts: TimeoutConfig
    evidence_record: Path | None = None


@dataclass(frozen=True)
class TransitionStatus:
    from_runtime: str | None
    target_runtime: str | None
    step: str
    failure_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeStatus:
    runtime: str
    state: RuntimeState
    service: str
    healthy: bool | None
    transition: TransitionStatus | None = None
    failure_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["state"] = self.state.value
        return result


@dataclass(frozen=True)
class SystemStatus:
    runtimes: tuple[RuntimeStatus, ...]
    current_runtime: str | None
    transition: TransitionStatus | None
    anomalies: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_runtime": self.current_runtime,
            "transition": self.transition.to_dict() if self.transition else None,
            "anomalies": list(self.anomalies),
            "runtimes": [runtime.to_dict() for runtime in self.runtimes],
        }
