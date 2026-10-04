"""The single authority for node runtime state and transitions."""

from __future__ import annotations

import threading
from collections.abc import Iterable, Sequence
from contextlib import AbstractContextManager, nullcontext

from flamoris_gpu_node_manager.domain.errors import TransitionError
from flamoris_gpu_node_manager.domain.models import (
    RuntimeProfile,
    RuntimeState,
    RuntimeStatus,
    ServiceState,
    SystemStatus,
    TransitionStatus,
)
from flamoris_gpu_node_manager.domain.registry import RuntimeRegistry

from .ports import (
    EvidenceInvalidationPort,
    HealthPort,
    ResourcePort,
    SystemdPort,
    TransitionLockPort,
)

_RUNNING_SERVICE_STATES = {
    ServiceState.ACTIVE,
    ServiceState.ACTIVATING,
    ServiceState.DEACTIVATING,
}


class RuntimeManager:
    """Serializes every runtime mutation and reports truthful structured state."""

    def __init__(
        self,
        registry: RuntimeRegistry,
        systemd: SystemdPort,
        resources: ResourcePort,
        health: HealthPort,
        transition_lock: TransitionLockPort,
        *,
        lock_timeout: float = 5.0,
        evidence_invalidator: EvidenceInvalidationPort | None = None,
    ) -> None:
        self._registry = registry
        self._systemd = systemd
        self._resources = resources
        self._health = health
        self._transition_lock = transition_lock
        self._lock_timeout = lock_timeout
        self._evidence_invalidator = evidence_invalidator
        self._status_lock = threading.RLock()
        self._statuses: dict[str, RuntimeStatus] = {}
        self._transition: TransitionStatus | None = None
        self._anomalies: tuple[str, ...] = ()

    def list_runtimes(self) -> tuple[RuntimeProfile, ...]:
        return tuple(self._registry)

    def reconstruct(self) -> SystemStatus:
        """Rebuild status from adapters; no saved manager state is authoritative."""
        statuses: dict[str, RuntimeStatus] = {}
        anomalies: list[str] = []
        running_by_class: dict[str, list[RuntimeProfile]] = {}

        for profile in self._registry:
            service_state = self._safe_service_state(profile)
            released = self._safe_is_released(profile)
            if service_state in _RUNNING_SERVICE_STATES:
                running_by_class.setdefault(profile.resource_class, []).append(profile)

            if service_state == ServiceState.ACTIVE:
                healthy = self._safe_health_check(profile)
                state = RuntimeState.READY if healthy else RuntimeState.FAILED
                reason = None if healthy else "service is active but health check failed"
            elif service_state == ServiceState.ACTIVATING:
                healthy, state, reason = None, RuntimeState.STARTING, None
            elif service_state == ServiceState.DEACTIVATING:
                healthy, state, reason = None, RuntimeState.STOPPING, None
            elif service_state == ServiceState.INACTIVE and released:
                healthy, state, reason = False, RuntimeState.OFF, None
            elif service_state == ServiceState.INACTIVE:
                healthy, state = False, RuntimeState.FAILED
                reason = "service is inactive but runtime-owned resources remain"
                anomalies.append(f"{profile.id}: {reason}")
            elif service_state == ServiceState.NOT_FOUND and released and not profile.enabled:
                healthy, state, reason = False, RuntimeState.OFF, None
            elif service_state == ServiceState.NOT_FOUND and released:
                healthy, state = False, RuntimeState.FAILED
                reason = "configured systemd unit is not installed"
                anomalies.append(f"{profile.id}: {reason}")
            elif service_state == ServiceState.NOT_FOUND:
                healthy, state = False, RuntimeState.FAILED
                reason = "systemd unit is not installed but runtime-owned resources remain"
                anomalies.append(f"{profile.id}: {reason}")
            elif service_state == ServiceState.FAILED:
                healthy, state, reason = False, RuntimeState.FAILED, "systemd unit is failed"
            else:
                healthy, state, reason = None, RuntimeState.FAILED, "systemd state is unknown"

            statuses[profile.id] = RuntimeStatus(
                runtime=profile.id,
                state=state,
                service=profile.service,
                healthy=healthy,
                failure_reason=reason,
            )

        for resource_class, profiles in running_by_class.items():
            if len(profiles) < 2:
                continue
            ids = ", ".join(profile.id for profile in profiles)
            reason = f"multiple runtimes own resource class {resource_class}: {ids}"
            anomalies.append(reason)
            for profile in profiles:
                old = statuses[profile.id]
                statuses[profile.id] = RuntimeStatus(
                    runtime=old.runtime,
                    state=RuntimeState.FAILED,
                    service=old.service,
                    healthy=old.healthy,
                    failure_reason=reason,
                )

        with self._status_lock:
            self._statuses = statuses
            self._anomalies = tuple(anomalies)
        return self.system_status()

    def runtime_status(self, runtime_id: str) -> RuntimeStatus:
        self._registry.get(runtime_id)
        with self._status_lock:
            status = self._statuses.get(runtime_id)
        if status is None:
            self.reconstruct()
            with self._status_lock:
                status = self._statuses[runtime_id]
        return status

    def system_status(self) -> SystemStatus:
        with self._status_lock:
            statuses = tuple(
                self._statuses.get(
                    profile.id,
                    RuntimeStatus(
                        runtime=profile.id,
                        state=RuntimeState.FAILED,
                        service=profile.service,
                        healthy=None,
                        failure_reason="runtime status has not been reconstructed",
                    ),
                )
                for profile in self._registry
            )
            ready = [status.runtime for status in statuses if status.state == RuntimeState.READY]
            return SystemStatus(
                runtimes=statuses,
                current_runtime=ready[0] if len(ready) == 1 else None,
                transition=self._transition,
                anomalies=self._anomalies,
            )

    def activate(self, runtime_id: str) -> RuntimeStatus:
        target = self._registry.get(runtime_id, require_enabled=True)
        with self._transition_lock.hold(self._lock_timeout):
            self.reconstruct()
            try:
                self._ensure_inspectable(target)
            except Exception as exc:
                reason = str(exc) or type(exc).__name__
                failed = TransitionStatus(
                    from_runtime=None,
                    target_runtime=target.id,
                    step="inspect",
                    failure_reason=reason,
                )
                self._set_transition(failed)
                self._set_runtime(
                    target,
                    RuntimeState.FAILED,
                    healthy=False,
                    transition=failed,
                    failure_reason=reason,
                )
                if isinstance(exc, TransitionError):
                    raise
                raise TransitionError(reason) from exc
            current = self.runtime_status(target.id)
            if current.state == RuntimeState.READY:
                self._set_transition(None)
                return current

            conflicts = self._active_conflicts(target)
            from_runtime = conflicts[0].id if conflicts else None
            transition = TransitionStatus(
                from_runtime=from_runtime,
                target_runtime=target.id,
                step="inspect",
            )
            self._set_transition(transition)
            try:
                with self._invalidate_evidence((target, *conflicts), from_runtime, target.id):
                    for conflict in conflicts:
                        self._stop_profile(conflict, target.id)
                    target_service_state = self._systemd.get_state(target.service)
                    if (
                        target_service_state not in _RUNNING_SERVICE_STATES
                        and not self._resources.is_released(target)
                    ):
                        self._stop_profile(target, target.id)
                    self._start_profile(target, from_runtime)
            except Exception as exc:
                reason = str(exc) or type(exc).__name__
                failed = TransitionStatus(
                    from_runtime=from_runtime,
                    target_runtime=target.id,
                    step=self._current_step(),
                    failure_reason=reason,
                )
                self._set_transition(failed)
                self._set_runtime(
                    target,
                    RuntimeState.FAILED,
                    healthy=False,
                    transition=failed,
                    failure_reason=reason,
                )
                if failed.from_runtime and failed.from_runtime != target.id:
                    source = self._registry.get(failed.from_runtime)
                    self._set_runtime(
                        source,
                        RuntimeState.FAILED,
                        healthy=False,
                        transition=failed,
                        failure_reason=reason,
                    )
                if isinstance(exc, TransitionError):
                    raise
                raise TransitionError(reason) from exc

            self._set_transition(None)
            return self.runtime_status(target.id)

    def stop(self, runtime_id: str | None = None) -> tuple[RuntimeStatus, ...]:
        requested: tuple[RuntimeProfile, ...] = (
            (self._registry.get(runtime_id),) if runtime_id is not None else ()
        )
        with self._transition_lock.hold(self._lock_timeout):
            self.reconstruct()
            targets = requested or self._running_profiles()
            if not targets:
                self._set_transition(None)
                return self.system_status().runtimes
            if runtime_id is None and len(targets) > 1:
                raise TransitionError(
                    "multiple runtimes are active; specify a runtime id for recovery"
                )
            profile = targets[0]
            transition = TransitionStatus(from_runtime=profile.id, target_runtime=None, step="stop")
            self._set_transition(transition)
            try:
                with self._invalidate_evidence((profile,), profile.id, None):
                    self._stop_profile(profile, None)
            except Exception as exc:
                reason = str(exc) or type(exc).__name__
                failed = TransitionStatus(
                    from_runtime=profile.id,
                    target_runtime=None,
                    step=self._current_step(),
                    failure_reason=reason,
                )
                self._set_transition(failed)
                self._set_runtime(
                    profile,
                    RuntimeState.FAILED,
                    healthy=False,
                    transition=failed,
                    failure_reason=reason,
                )
                if isinstance(exc, TransitionError):
                    raise
                raise TransitionError(reason) from exc
            self._set_transition(None)
            return (self.runtime_status(profile.id),)

    def _invalidate_evidence(
        self,
        profiles: Sequence[RuntimeProfile],
        from_runtime: str | None,
        target_runtime: str | None,
    ) -> AbstractContextManager[None]:
        if not any(profile.evidence_record is not None for profile in profiles):
            return nullcontext()
        self._set_transition(
            TransitionStatus(
                from_runtime=from_runtime,
                target_runtime=target_runtime,
                step="evidence-invalidation",
            )
        )
        if self._evidence_invalidator is None:
            raise TransitionError("configured runtime evidence invalidator is unavailable")
        return self._evidence_invalidator.hold(profiles, self._lock_timeout)

    def _active_conflicts(self, target: RuntimeProfile) -> tuple[RuntimeProfile, ...]:
        return tuple(
            profile
            for profile in self._registry.conflicts_for(target)
            if self._safe_service_state(profile) in _RUNNING_SERVICE_STATES
            or not self._safe_is_released(profile)
        )

    def _ensure_inspectable(self, target: RuntimeProfile) -> None:
        for profile in (target, *self._registry.conflicts_for(target)):
            try:
                state = self._systemd.get_state(profile.service)
            except Exception as exc:
                raise TransitionError(
                    f"cannot inspect systemd state for {profile.service}: {exc}"
                ) from exc
            if state == ServiceState.UNKNOWN:
                raise TransitionError(f"systemd state is unknown for {profile.service}")
            try:
                released = self._resources.is_released(profile)
            except Exception as exc:
                raise TransitionError(
                    f"cannot inspect runtime-owned resources for {profile.id}: {exc}"
                ) from exc
            if state == ServiceState.NOT_FOUND:
                if profile.id == target.id:
                    raise TransitionError(
                        f"configured systemd unit is not installed: {profile.service}"
                    )
                if not released:
                    raise TransitionError(
                        f"{profile.id} owns resources but its systemd unit is not installed"
                    )

    def _running_profiles(self) -> tuple[RuntimeProfile, ...]:
        return tuple(
            profile
            for profile in self._registry
            if self._safe_service_state(profile) in _RUNNING_SERVICE_STATES
            or not self._safe_is_released(profile)
        )

    def _stop_profile(self, profile: RuntimeProfile, target_runtime: str | None) -> None:
        transition = TransitionStatus(
            from_runtime=profile.id, target_runtime=target_runtime, step="stop"
        )
        self._set_transition(transition)
        self._set_runtime(profile, RuntimeState.STOPPING, healthy=None, transition=transition)
        if self._safe_service_state(profile) != ServiceState.INACTIVE:
            self._systemd.stop(profile.service)
        if not self._systemd.wait_inactive(profile.service, profile.timeouts.stop_seconds):
            raise TransitionError(f"timed out waiting for {profile.service} to become inactive")

        transition = TransitionStatus(
            from_runtime=profile.id, target_runtime=target_runtime, step="resource-release"
        )
        self._set_transition(transition)
        self._set_runtime(
            profile, RuntimeState.WAITING_FOR_GPU, healthy=False, transition=transition
        )
        if not self._resources.wait_released(profile, profile.timeouts.release_seconds):
            raise TransitionError(f"timed out waiting for {profile.id} resources to release")
        self._set_runtime(profile, RuntimeState.OFF, healthy=False)

    def _start_profile(self, profile: RuntimeProfile, from_runtime: str | None) -> None:
        transition = TransitionStatus(
            from_runtime=from_runtime, target_runtime=profile.id, step="start"
        )
        self._set_transition(transition)
        self._set_runtime(profile, RuntimeState.STARTING, healthy=None, transition=transition)
        self._systemd.start(profile.service)
        if not self._systemd.wait_active(profile.service, profile.timeouts.start_seconds):
            raise TransitionError(f"timed out waiting for {profile.service} to become active")

        transition = TransitionStatus(
            from_runtime=from_runtime, target_runtime=profile.id, step="health-check"
        )
        self._set_transition(transition)
        self._set_runtime(profile, RuntimeState.STARTING, healthy=None, transition=transition)
        if not self._health.wait_healthy(profile, profile.timeouts.health_seconds):
            raise TransitionError(f"health check failed for {profile.id}")
        self._set_runtime(profile, RuntimeState.READY, healthy=True)

    def _set_transition(self, transition: TransitionStatus | None) -> None:
        with self._status_lock:
            self._transition = transition

    def _current_step(self) -> str:
        with self._status_lock:
            return self._transition.step if self._transition else "unknown"

    def _set_runtime(
        self,
        profile: RuntimeProfile,
        state: RuntimeState,
        *,
        healthy: bool | None,
        transition: TransitionStatus | None = None,
        failure_reason: str | None = None,
    ) -> None:
        with self._status_lock:
            self._statuses[profile.id] = RuntimeStatus(
                runtime=profile.id,
                state=state,
                service=profile.service,
                healthy=healthy,
                transition=transition,
                failure_reason=failure_reason,
            )

    def _safe_service_state(self, profile: RuntimeProfile) -> ServiceState:
        try:
            return self._systemd.get_state(profile.service)
        except Exception:
            return ServiceState.UNKNOWN

    def _safe_is_released(self, profile: RuntimeProfile) -> bool:
        try:
            return self._resources.is_released(profile)
        except Exception:
            return False

    def _safe_health_check(self, profile: RuntimeProfile) -> bool:
        try:
            return self._health.check(profile)
        except Exception:
            return False

    def statuses_for(self, profiles: Iterable[RuntimeProfile]) -> tuple[RuntimeStatus, ...]:
        return tuple(self.runtime_status(profile.id) for profile in profiles)
