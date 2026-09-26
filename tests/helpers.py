from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

from flamoris_gpu_node_manager.domain.models import (
    HealthConfig,
    ProcessMatcher,
    ReleaseConfig,
    RuntimeProfile,
    ServiceState,
    TimeoutConfig,
)


def profile(runtime_id: str, *, enabled: bool = True) -> RuntimeProfile:
    return RuntimeProfile(
        id=runtime_id,
        display_name=runtime_id.upper(),
        service=f"{runtime_id}.service",
        resource_class="gpu-heavy",
        enabled=enabled,
        health=HealthConfig(type="none"),
        release=ReleaseConfig(
            type="process",
            processes=(ProcessMatcher(name=runtime_id),),
        ),
        timeouts=TimeoutConfig(
            stop_seconds=1,
            release_seconds=1,
            start_seconds=1,
            health_seconds=1,
        ),
    )


class FakeSystemd:
    def __init__(self, states: dict[str, ServiceState], events: list[str]) -> None:
        self.states = states
        self.events = events
        self.stop_wait_result = True
        self.start_wait_result = True
        self.start_error: Exception | None = None
        self.inspect_error_for: str | None = None

    def get_state(self, service: str) -> ServiceState:
        if self.inspect_error_for == service:
            raise RuntimeError("inspection failed")
        return self.states.get(service, ServiceState.INACTIVE)

    def start(self, service: str) -> None:
        self.events.append(f"start:{service}")
        if self.start_error:
            raise self.start_error
        self.states[service] = ServiceState.ACTIVE

    def stop(self, service: str) -> None:
        self.events.append(f"stop:{service}")
        self.states[service] = ServiceState.INACTIVE

    def wait_active(self, service: str, timeout: float) -> bool:
        self.events.append(f"wait-active:{service}")
        return self.start_wait_result

    def wait_inactive(self, service: str, timeout: float) -> bool:
        self.events.append(f"wait-inactive:{service}")
        return self.stop_wait_result


class FakeResources:
    def __init__(self, released: dict[str, bool], events: list[str]) -> None:
        self.released = released
        self.events = events
        self.wait_result = True

    def is_released(self, profile: RuntimeProfile) -> bool:
        return self.released.get(profile.id, True)

    def wait_released(self, profile: RuntimeProfile, timeout: float) -> bool:
        self.events.append(f"release:{profile.id}")
        if self.wait_result:
            self.released[profile.id] = True
        return self.wait_result


class FakeHealth:
    def __init__(self, healthy: dict[str, bool], events: list[str]) -> None:
        self.healthy = healthy
        self.events = events
        self.wait_result = True
        self.delay = 0.0

    def check(self, profile: RuntimeProfile) -> bool:
        return self.healthy.get(profile.id, True)

    def wait_healthy(self, profile: RuntimeProfile, timeout: float) -> bool:
        self.events.append(f"health:{profile.id}")
        if self.delay:
            time.sleep(self.delay)
        if self.wait_result:
            self.healthy[profile.id] = True
        return self.wait_result


class RecordingLock:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._guard = threading.Lock()
        self.active = 0
        self.maximum_active = 0

    @contextmanager
    def hold(self, timeout: float) -> Iterator[None]:
        if not self._lock.acquire(timeout=timeout):
            raise RuntimeError("test lock timeout")
        with self._guard:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        try:
            yield
        finally:
            with self._guard:
                self.active -= 1
            self._lock.release()
