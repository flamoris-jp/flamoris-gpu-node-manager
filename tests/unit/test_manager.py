from __future__ import annotations

import threading

import pytest

from flamoris_gpu_node_manager.application.manager import RuntimeManager
from flamoris_gpu_node_manager.domain.errors import (
    RuntimeUnavailableError,
    TransitionError,
    UnknownRuntimeError,
)
from flamoris_gpu_node_manager.domain.models import RuntimeState, ServiceState
from flamoris_gpu_node_manager.domain.registry import RuntimeRegistry
from tests.helpers import FakeHealth, FakeResources, FakeSystemd, RecordingLock, profile


def manager_fixture(
    *,
    states: dict[str, ServiceState] | None = None,
    released: dict[str, bool] | None = None,
    healthy: dict[str, bool] | None = None,
    profiles: tuple | None = None,
) -> tuple[RuntimeManager, FakeSystemd, FakeResources, FakeHealth, RecordingLock, list[str]]:
    events: list[str] = []
    configured = profiles or (profile("alpha"), profile("beta"))
    systemd = FakeSystemd(states or {}, events)
    resources = FakeResources(released or {}, events)
    health = FakeHealth(healthy or {}, events)
    lock = RecordingLock()
    manager = RuntimeManager(
        RuntimeRegistry(configured), systemd, resources, health, lock, lock_timeout=1
    )
    return manager, systemd, resources, health, lock, events


def test_no_active_runtime_starts_target() -> None:
    manager, _, _, _, _, events = manager_fixture()

    result = manager.activate("alpha")

    assert result.state == RuntimeState.READY
    assert events == ["start:alpha.service", "wait-active:alpha.service", "health:alpha"]


def test_target_already_ready_is_safe_no_op() -> None:
    manager, _, _, _, _, events = manager_fixture(
        states={"alpha.service": ServiceState.ACTIVE}, healthy={"alpha": True}
    )

    result = manager.activate("alpha")

    assert result.state == RuntimeState.READY
    assert events == []


def test_switch_stops_waits_for_release_then_starts() -> None:
    manager, _, _, _, _, events = manager_fixture(
        states={"alpha.service": ServiceState.ACTIVE},
        released={"alpha": False},
        healthy={"alpha": True},
    )

    result = manager.activate("beta")

    assert result.state == RuntimeState.READY
    assert events == [
        "stop:alpha.service",
        "wait-inactive:alpha.service",
        "release:alpha",
        "start:beta.service",
        "wait-active:beta.service",
        "health:beta",
    ]


def test_stop_timeout_fails_without_starting_target() -> None:
    manager, systemd, _, _, _, events = manager_fixture(
        states={"alpha.service": ServiceState.ACTIVE}, released={"alpha": False}
    )
    systemd.stop_wait_result = False

    with pytest.raises(TransitionError, match="inactive"):
        manager.activate("beta")

    assert not any(event.startswith("start:") for event in events)
    assert manager.runtime_status("beta").state == RuntimeState.FAILED


def test_resource_release_timeout_fails_without_starting_target() -> None:
    manager, _, resources, _, _, events = manager_fixture(
        states={"alpha.service": ServiceState.ACTIVE}, released={"alpha": False}
    )
    resources.wait_result = False

    with pytest.raises(TransitionError, match="resources"):
        manager.activate("beta")

    assert not any(event.startswith("start:") for event in events)
    assert manager.runtime_status("beta").state == RuntimeState.FAILED
    assert manager.runtime_status("alpha").state == RuntimeState.FAILED


def test_inactive_target_with_owned_process_must_release_before_start() -> None:
    manager, _, _, _, _, events = manager_fixture(released={"alpha": False})

    result = manager.activate("alpha")

    assert result.state == RuntimeState.READY
    assert events == [
        "wait-inactive:alpha.service",
        "release:alpha",
        "start:alpha.service",
        "wait-active:alpha.service",
        "health:alpha",
    ]


def test_inspection_failure_blocks_target_start() -> None:
    manager, systemd, _, _, _, events = manager_fixture()
    systemd.inspect_error_for = "beta.service"

    with pytest.raises(TransitionError, match="cannot inspect"):
        manager.activate("alpha")

    assert not any(event.startswith("start:") for event in events)
    status = manager.runtime_status("alpha")
    assert status.state == RuntimeState.FAILED
    assert status.transition is not None
    assert status.transition.step == "inspect"


def test_disabled_uninstalled_placeholders_do_not_block_enabled_target() -> None:
    configured = (
        profile("alpha"),
        profile("llm", enabled=False),
        profile("disabled-a", enabled=False),
        profile("disabled-b", enabled=False),
    )
    manager, _, _, _, _, events = manager_fixture(
        profiles=configured,
        states={
            "llm.service": ServiceState.NOT_FOUND,
            "disabled-a.service": ServiceState.NOT_FOUND,
            "disabled-b.service": ServiceState.NOT_FOUND,
        },
    )

    result = manager.activate("alpha")

    assert result.state == RuntimeState.READY
    assert events == ["start:alpha.service", "wait-active:alpha.service", "health:alpha"]


def test_uninstalled_placeholder_with_owned_resources_blocks_activation() -> None:
    configured = (profile("alpha"), profile("llm", enabled=False))
    manager, _, _, _, _, events = manager_fixture(
        profiles=configured,
        states={"llm.service": ServiceState.NOT_FOUND},
        released={"llm": False},
    )

    with pytest.raises(TransitionError, match="owns resources"):
        manager.activate("alpha")

    assert not any(event.startswith("start:") for event in events)


def test_target_start_failure_is_truthful() -> None:
    manager, systemd, _, _, _, _ = manager_fixture()
    systemd.start_error = RuntimeError("start exploded")

    with pytest.raises(TransitionError, match="start exploded"):
        manager.activate("alpha")

    failed = manager.runtime_status("alpha")
    assert failed.state == RuntimeState.FAILED
    assert failed.transition is not None
    assert failed.transition.step == "start"


def test_target_active_timeout_is_truthful() -> None:
    manager, systemd, _, _, _, _ = manager_fixture()
    systemd.start_wait_result = False

    with pytest.raises(TransitionError, match="active"):
        manager.activate("alpha")

    assert manager.runtime_status("alpha").state == RuntimeState.FAILED


def test_health_failure_never_reports_ready() -> None:
    manager, _, _, health, _, _ = manager_fixture()
    health.wait_result = False

    with pytest.raises(TransitionError, match="health"):
        manager.activate("alpha")

    failed = manager.runtime_status("alpha")
    assert failed.state == RuntimeState.FAILED
    assert failed.healthy is False


def test_concurrent_activations_are_serialized() -> None:
    manager, _, _, health, lock, _ = manager_fixture()
    health.delay = 0.05
    barrier = threading.Barrier(3)
    errors: list[BaseException] = []

    def activate(runtime_id: str) -> None:
        try:
            barrier.wait()
            manager.activate(runtime_id)
        except BaseException as exc:  # captured for assertion in the parent thread
            errors.append(exc)

    threads = [
        threading.Thread(target=activate, args=("alpha",)),
        threading.Thread(target=activate, args=("beta",)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert errors == []
    assert lock.maximum_active == 1


def test_startup_reconstructs_from_adapters_and_reports_conflict() -> None:
    manager, systemd, _, _, _, events = manager_fixture(
        states={
            "alpha.service": ServiceState.ACTIVE,
            "beta.service": ServiceState.ACTIVE,
        }
    )

    status = manager.reconstruct()

    assert status.current_runtime is None
    assert status.anomalies
    assert all(runtime.state == RuntimeState.FAILED for runtime in status.runtimes)
    assert not any(event.startswith("stop:") for event in events)
    assert systemd.states["alpha.service"] == ServiceState.ACTIVE


def test_startup_reports_inactive_service_with_owned_process() -> None:
    manager, _, _, _, _, _ = manager_fixture(released={"alpha": False})

    status = manager.reconstruct()

    alpha = next(item for item in status.runtimes if item.runtime == "alpha")
    assert alpha.state == RuntimeState.FAILED
    assert "resources remain" in (alpha.failure_reason or "")


def test_unknown_and_disabled_runtime_are_rejected_before_mutation() -> None:
    manager, _, _, _, _, events = manager_fixture(profiles=(profile("alpha", enabled=False),))

    with pytest.raises(UnknownRuntimeError):
        manager.activate("missing")
    with pytest.raises(RuntimeUnavailableError):
        manager.activate("alpha")
    assert events == []


def test_stop_without_id_refuses_ambiguous_recovery() -> None:
    manager, _, _, _, _, _ = manager_fixture(
        states={
            "alpha.service": ServiceState.ACTIVE,
            "beta.service": ServiceState.ACTIVE,
        }
    )

    with pytest.raises(TransitionError, match="specify"):
        manager.stop()
