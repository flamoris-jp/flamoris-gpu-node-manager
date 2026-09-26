from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from flamoris_gpu_node_manager.domain.errors import TransitionBusyError
from flamoris_gpu_node_manager.domain.models import HealthConfig, RuntimeProfile, ServiceState
from flamoris_gpu_node_manager.infrastructure.health import HealthAdapter
from flamoris_gpu_node_manager.infrastructure.locking import FileTransitionLock
from flamoris_gpu_node_manager.infrastructure.resource import (
    ProcessResourceAdapter,
    ProcessSnapshot,
    ProcfsProcessSource,
    ResourceInspectionError,
)
from flamoris_gpu_node_manager.infrastructure.systemd import SystemdAdapter
from tests.helpers import profile


class ProcessSource:
    def __init__(self, snapshots: tuple[ProcessSnapshot, ...]) -> None:
        self.items = snapshots

    def snapshots(self) -> tuple[ProcessSnapshot, ...]:
        return self.items


def test_release_matches_name_and_all_declared_command_tokens() -> None:
    runtime = profile("alpha")
    source = ProcessSource(
        (
            ProcessSnapshot(10, "alpha", "/usr/bin/alpha --other"),
            ProcessSnapshot(11, "unrelated", "/opt/alpha/server"),
        )
    )
    adapter = ProcessResourceAdapter(source)  # type: ignore[arg-type]
    runtime = RuntimeProfile(
        **{
            **runtime.__dict__,
            "release": runtime.release.__class__(
                type="process",
                processes=(
                    runtime.release.processes[0].__class__(
                        name="alpha", cmdline_contains=("/opt/alpha/",)
                    ),
                ),
            ),
        }
    )

    assert adapter.is_released(runtime)
    source.items += (ProcessSnapshot(12, "alpha", "/opt/alpha/server --serve"),)
    assert not adapter.is_released(runtime)


def test_procfs_inspection_failure_is_not_treated_as_released(tmp_path: Path) -> None:
    missing = tmp_path / "missing-proc"
    adapter = ProcessResourceAdapter(ProcfsProcessSource(missing))

    with pytest.raises(ResourceInspectionError):
        adapter.is_released(profile("alpha"))


def test_systemd_adapter_uses_controlled_argument_vectors() -> None:
    calls: list[list[str]] = []

    def runner(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        stdout = "LoadState=loaded\nActiveState=active\n" if "show" in arguments else ""
        return subprocess.CompletedProcess(arguments, 0, stdout=stdout, stderr="")

    adapter = SystemdAdapter(runner=runner)
    assert adapter.get_state("alpha.service") == ServiceState.ACTIVE
    adapter.stop("alpha.service")
    adapter.start("alpha.service")

    assert calls == [
        [
            "systemctl",
            "show",
            "--property=LoadState",
            "--property=ActiveState",
            "alpha.service",
        ],
        ["systemctl", "--no-block", "stop", "alpha.service"],
        ["systemctl", "--no-block", "start", "alpha.service"],
    ]


def test_systemd_adapter_distinguishes_uninstalled_unit_from_inspection_failure() -> None:
    def runner(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            arguments,
            1,
            stdout="LoadState=not-found\nActiveState=inactive\n",
            stderr="Unit placeholder.service could not be found.",
        )

    adapter = SystemdAdapter(runner=runner)

    assert adapter.get_state("placeholder.service") == ServiceState.NOT_FOUND


def test_health_selection_is_profile_driven() -> None:
    events: list[str] = []

    class Systemd:
        def get_state(self, service: str) -> ServiceState:
            events.append(service)
            return ServiceState.ACTIVE

    processes = ProcessResourceAdapter(ProcessSource(()))  # type: ignore[arg-type]
    adapter = HealthAdapter(Systemd(), processes)  # type: ignore[arg-type]
    base = profile("alpha")
    runtime = RuntimeProfile(**{**base.__dict__, "health": HealthConfig(type="systemd-active")})

    assert adapter.check(runtime)
    assert events == ["alpha.service"]


def test_repository_profiles_validate() -> None:
    from flamoris_gpu_node_manager.infrastructure.config import load_registry

    root = Path(__file__).parents[2]
    registry = load_registry(root / "config" / "examples")
    assert {item.id for item in registry} == {"example-http-runtime", "example-tcp-runtime"}


def test_file_transition_lock_is_shared_between_authority_instances(tmp_path: Path) -> None:
    path = tmp_path / "transition.lock"
    first = FileTransitionLock(path)
    second = FileTransitionLock(path)

    with first.hold(0.1), pytest.raises(TransitionBusyError), second.hold(0.01):
        pass


def test_file_transition_lock_recreates_runtime_directory_after_boot(tmp_path: Path) -> None:
    path = tmp_path / "run" / "flamoris-gpu-node-manager" / "transition.lock"

    assert not path.parent.exists()
    with FileTransitionLock(path).hold(0.1):
        assert path.is_file()
