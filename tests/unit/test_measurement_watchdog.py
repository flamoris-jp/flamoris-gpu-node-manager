from __future__ import annotations

import fcntl
import multiprocessing
import os
import struct
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from flamoris_gpu_node_manager.infrastructure import measurement_watchdog as watchdog
from flamoris_gpu_node_manager.infrastructure.comfyui_measurement import ComfyUIManifestSource
from flamoris_gpu_node_manager.infrastructure.evidence_publication import EvidencePublicationError
from flamoris_gpu_node_manager.infrastructure.process_lifetime import (
    LinuxProcessLifetimeFence,
    ProcessExitEvidenceInvalidator,
)
from tests.unit.test_comfyui_measurement import initialized as observation_fixture
from tests.unit.test_evidence_publication import manifest, publisher

observation_fixture = observation_fixture


class ReadySource:
    def measure(self, *, deadline):
        return manifest()


class StalledSource:
    def __init__(self, pid_path):
        self.pid_path = pid_path

    def measure(self, *, deadline):
        self.pid_path.write_text(str(os.getpid()))
        time.sleep(60)
        return manifest()


class FailedSource:
    def measure(self, *, deadline):
        raise ValueError("private filesystem metadata")


class BadSource:
    def measure(self, *, deadline):
        return {"fake": "manifest"}


def test_spawned_success_and_return_never_unpickles_worker_objects():
    observed = watchdog.SpawnedManifestSource(ReadySource()).measure(deadline=time.monotonic() + 5)
    assert observed == manifest()
    assert not multiprocessing.active_children()


@pytest.mark.parametrize("source", [FailedSource(), BadSource()])
def test_failed_worker_is_redacted_and_reaped(source):
    with pytest.raises(EvidencePublicationError, match="^runtime measurement unavailable$"):
        watchdog.SpawnedManifestSource(source).measure(deadline=time.monotonic() + 5)
    assert not multiprocessing.active_children()


def test_stalled_measurement_is_killed_evidence_withdrawn_and_lock_reusable(tmp_path):
    owner, _, _ = publisher(tmp_path, measurement_timeout=1.5)
    owner.issue()
    assert owner.record.exists()
    worker_pid = tmp_path / "worker-pid"
    owner._source = watchdog.SpawnedManifestSource(StalledSource(worker_pid))
    started = time.monotonic()
    with pytest.raises(EvidencePublicationError):
        owner.issue()
    assert 1.4 < time.monotonic() - started < 3.0
    assert not owner.record.exists()
    assert worker_pid.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int(worker_pid.read_text()), 0)
    assert not multiprocessing.active_children()
    owner._source = watchdog.SpawnedManifestSource(ReadySource())
    owner.issue()
    assert owner.record.exists()


def test_partial_pipe_body_cannot_bypass_deadline():
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, struct.pack("!I", 10) + b"part")
        deadline = time.monotonic() + 0.03
        size = struct.unpack("!I", watchdog._read_exact(read_fd, 4, deadline))[0]
        with pytest.raises(TimeoutError):
            watchdog._read_exact(read_fd, size, deadline)
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.parametrize("offset", [-1, 3601, float("nan"), float("inf")])
def test_invalid_watchdog_deadlines_start_no_worker(offset):
    with pytest.raises(EvidencePublicationError):
        watchdog.SpawnedManifestSource(ReadySource()).measure(deadline=time.monotonic() + offset)


def _require_matching_procfs():
    try:
        actual = int(Path("/proc/self/stat").read_text().split(" ", 1)[0])
        Path(f"/proc/{os.getpid()}/exe").stat()
    except (OSError, ValueError):
        actual = None
    if actual != os.getpid():
        pytest.skip("execution sandbox uses incompatible procfs namespace; exercised on Linux CI")


def test_actual_lifetime_fence_exit_and_credential_mismatch():
    _require_matching_procfs()
    with subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    ) as child:
        with LinuxProcessLifetimeFence(child.pid, os.geteuid()) as fence:
            first = fence.observe()
            assert fence.observe() == first
            with pytest.raises(EvidencePublicationError):
                LinuxProcessLifetimeFence(child.pid, os.geteuid() + 1)
            child.terminate()
            child.wait(timeout=5)
            with pytest.raises(EvidencePublicationError):
                fence.observe()
        with pytest.raises(EvidencePublicationError):
            fence.observe()


def test_lifetime_identity_drift_never_accepts_same_pid():
    _require_matching_procfs()
    with LinuxProcessLifetimeFence(os.getpid(), os.geteuid()) as fence:
        changed = (*fence._identity[:-1], fence._identity[-1] + 1)
        with (
            patch.object(fence, "_read", return_value=changed),
            pytest.raises(EvidencePublicationError),
        ):
            fence.observe()


def test_retained_object_revision_changes_implementation_even_when_values_match(
    observation_fixture,
):
    class Port:
        def __init__(self, value):
            self.value = value

        def observe(self, *, deadline):
            return self.value

    port = Port(observation_fixture)
    source = ComfyUIManifestSource(port)
    first = source.measure(deadline=time.monotonic() + 5)
    port.value = replace(observation_fixture, snapshot_token="same-values-new-object-revision")
    second = source.measure(deadline=time.monotonic() + 5)
    assert first.core_identity == second.core_identity
    assert first.nodes["Image"].interface == second.nodes["Image"].interface
    assert first.nodes["Image"].implementation != second.nodes["Image"].implementation


def test_exit_monitor_withdraws_after_shared_guard_releases(tmp_path):
    owner, _, _ = publisher(tmp_path)
    owner.issue()
    read_fd, write_fd = os.pipe()
    fence = object.__new__(LinuxProcessLifetimeFence)
    fence._fd = read_fd
    lock_fd = os.open(str(owner.record) + ".lock", os.O_RDONLY)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
        with (
            patch.object(fence, "observe", return_value="synthetic"),
            ProcessExitEvidenceInvalidator(fence, owner, lock_timeout=0.03),
        ):
            os.write(write_fd, b"exit")
            time.sleep(0.08)
            assert owner.record.exists()
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            deadline = time.monotonic() + 2
            while owner.record.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert not owner.record.exists()
    finally:
        os.close(lock_fd)
        os.close(read_fd)
        os.close(write_fd)


def test_exit_monitor_start_failure_withdraws_old_evidence(tmp_path):
    owner, _, _ = publisher(tmp_path)
    owner.issue()
    read_fd, write_fd = os.pipe()
    fence = object.__new__(LinuxProcessLifetimeFence)
    fence._fd = read_fd
    try:
        with (
            patch.object(fence, "observe", return_value="synthetic"),
            patch("threading.Thread.start", side_effect=RuntimeError("thread unavailable")),
            pytest.raises(RuntimeError),
            ProcessExitEvidenceInvalidator(fence, owner),
        ):
            pytest.fail("failed monitor must not enter")
        assert not owner.record.exists()
    finally:
        os.close(read_fd)
        os.close(write_fd)
