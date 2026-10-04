from __future__ import annotations

import fcntl
import json
import os
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from flamoris_gpu_node_manager.application.manager import RuntimeManager
from flamoris_gpu_node_manager.domain.errors import TransitionError
from flamoris_gpu_node_manager.domain.models import RuntimeState, ServiceState
from flamoris_gpu_node_manager.domain.registry import RuntimeRegistry
from flamoris_gpu_node_manager.infrastructure.runtime_evidence import (
    FileEvidenceInvalidator,
    provision_evidence_slot,
)
from tests.helpers import FakeHealth, FakeResources, FakeSystemd, RecordingLock, profile


def slot(tmp_path: Path, name: str = "alpha"):
    directory = tmp_path / name
    directory.mkdir(mode=0o700)
    record = directory / "runtime.json"
    provision_evidence_slot(record)
    return replace(profile(name), evidence_record=record)


def epoch(record: Path):
    return json.loads(Path(str(record) + ".epoch.json").read_text())


def make_manager(profiles, *, active: str | None = None, timeout=0.1):
    events = []
    systemd = FakeSystemd({f"{active}.service": ServiceState.ACTIVE} if active else {}, events)
    resources = FakeResources({active: False} if active else {}, events)
    health = FakeHealth({}, events)
    lock = RecordingLock()
    manager = RuntimeManager(
        RuntimeRegistry(profiles),
        systemd,
        resources,
        health,
        lock,
        lock_timeout=timeout,
        evidence_invalidator=FileEvidenceInvalidator(profiles),
    )
    return manager, systemd, events, lock


def test_provision_creates_no_manifest_and_never_recreates_lock(tmp_path):
    p = slot(tmp_path)
    record = p.evidence_record
    lock = Path(str(record) + ".lock")
    inode = lock.stat().st_ino
    assert not record.exists()
    assert epoch(record)["counter"] == 0
    with pytest.raises(FileExistsError):
        provision_evidence_slot(record)
    assert lock.stat().st_ino == inode


def test_withdraws_both_runtimes_before_stop_and_keeps_locks_through_health(tmp_path):
    alpha, beta = slot(tmp_path, "alpha"), slot(tmp_path, "beta")
    for p in (alpha, beta):
        p.evidence_record.write_text("synthetic expired manifest")
    manager, systemd, events, lock = make_manager((alpha, beta), active="alpha")
    original_start, original_stop = systemd.start, systemd.stop

    def assert_protected():
        assert lock.active == 1
        for p in (alpha, beta):
            assert not p.evidence_record.exists()
            assert epoch(p.evidence_record)["counter"] == 1
            with (
                Path(str(p.evidence_record) + ".lock").open("rb") as reader,
                pytest.raises(BlockingIOError),
            ):
                fcntl.flock(reader, fcntl.LOCK_SH | fcntl.LOCK_NB)

    def stop(service):
        assert_protected()
        original_stop(service)

    def start(service):
        assert_protected()
        original_start(service)

    def health(p, timeout):
        assert_protected()
        return True

    systemd.stop, systemd.start = stop, start
    manager._health.wait_healthy = health
    assert manager.activate("beta").state == RuntimeState.READY
    assert events[0] == "stop:alpha.service"
    assert "start:beta.service" in events
    assert not beta.evidence_record.exists()  # READY health is not qualification.


@pytest.mark.parametrize("target", ["stop", "switch"])
def test_generation_shared_guard_blocks_mutation_without_withdrawing(tmp_path, target):
    alpha = slot(tmp_path)
    alpha.evidence_record.write_text("synthetic existing manifest")
    manager, _, events, _ = make_manager((alpha, profile("beta")), active="alpha")
    with Path(str(alpha.evidence_record) + ".lock").open("rb") as reader:
        fcntl.flock(reader, fcntl.LOCK_SH | fcntl.LOCK_NB)
        with pytest.raises(TransitionError, match="in use"):
            manager.stop("alpha") if target == "stop" else manager.activate("beta")
    assert events == []
    assert alpha.evidence_record.exists()
    assert epoch(alpha.evidence_record)["counter"] == 0
    status = manager.runtime_status("alpha" if target == "stop" else "beta")
    assert status.state == RuntimeState.FAILED
    assert status.transition.step == "evidence-invalidation"


def test_waits_for_existing_reader_then_mutates(tmp_path):
    alpha = slot(tmp_path)
    manager, _, events, _ = make_manager((alpha,), active="alpha", timeout=2)
    started = threading.Event()
    errors = []

    def mutation():
        started.set()
        try:
            manager.stop("alpha")
        except Exception as exc:
            errors.append(exc)

    with Path(str(alpha.evidence_record) + ".lock").open("rb") as reader:
        fcntl.flock(reader, fcntl.LOCK_SH)
        worker = threading.Thread(target=mutation)
        worker.start()
        assert started.wait(1)
        assert not events
    worker.join(3)
    assert not worker.is_alive()
    assert not errors
    assert events[0] == "stop:alpha.service"


@pytest.mark.parametrize(
    "kind",
    [
        "lock-missing",
        "lock-replaced",
        "directory-replaced",
        "epoch-missing",
        "epoch-invalid",
        "epoch-exhausted",
        "record-symlink",
        "record-hardlink",
        "lock-hardlink",
        "record-writable",
        "anchor-missing",
    ],
)
def test_unsafe_storage_blocks_all_supervisor_calls(tmp_path, kind):
    alpha = slot(tmp_path)
    record = alpha.evidence_record
    record.write_text("synthetic existing manifest")
    manager, _, events, _ = make_manager((alpha,), active="alpha")
    lock = Path(str(record) + ".lock")
    counter = Path(str(record) + ".epoch.json")
    if kind == "lock-missing":
        lock.unlink()
    elif kind == "lock-replaced":
        lock.rename(lock.with_suffix(".old"))
        lock.touch(mode=0o600)
    elif kind == "directory-replaced":
        record.parent.rename(record.parent.with_suffix(".old"))
        record.parent.mkdir(mode=0o700)
    elif kind == "epoch-missing":
        counter.unlink()
    elif kind == "epoch-invalid":
        counter.write_text("{}")
    elif kind == "epoch-exhausted":
        value = epoch(record)
        value["counter"] = 2**64 - 1
        value["provider_epoch"] = value["authority_id"] + "_ffffffffffffffff"
        counter.write_text(json.dumps(value))
    elif kind == "record-symlink":
        record.unlink()
        record.symlink_to(tmp_path / "external.json")
    elif kind == "record-hardlink":
        os.link(record, tmp_path / "alias.json")
    elif kind == "lock-hardlink":
        os.link(lock, tmp_path / "alias.lock")
    elif kind == "record-writable":
        record.chmod(0o666)
    else:
        Path(str(record) + ".identity.json").unlink()
    with pytest.raises(TransitionError):
        manager.stop("alpha")
    assert not events


def test_new_process_rejects_replaced_lock_against_persistent_anchor(tmp_path):
    alpha = slot(tmp_path)
    lock = Path(str(alpha.evidence_record) + ".lock")
    lock.rename(lock.with_suffix(".old"))
    lock.touch(mode=0o600)
    with pytest.raises(OSError, match="identity changed"):
        FileEvidenceInvalidator((alpha,))


def test_missing_epoch_never_resets_counter_after_restart(tmp_path):
    alpha = slot(tmp_path)
    manager, _, _, _ = make_manager((alpha,), active="alpha")
    manager.stop("alpha")
    previous = epoch(alpha.evidence_record)["provider_epoch"]
    Path(str(alpha.evidence_record) + ".epoch.json").unlink()
    restarted, _, events, _ = make_manager((alpha,), active="alpha")
    with pytest.raises(TransitionError):
        restarted.stop("alpha")
    assert not events
    assert not alpha.evidence_record.exists()
    assert previous


def test_epoch_advances_across_new_manager_process_and_failure(tmp_path):
    alpha = slot(tmp_path)
    manager, systemd, _, _ = make_manager((alpha,))
    initial = epoch(alpha.evidence_record)["provider_epoch"]
    systemd.start_error = RuntimeError("synthetic failed start")
    with pytest.raises(TransitionError):
        manager.activate("alpha")
    failed = epoch(alpha.evidence_record)["provider_epoch"]
    restarted, _, _, _ = make_manager((alpha,))
    restarted.activate("alpha")
    next_epoch = epoch(alpha.evidence_record)["provider_epoch"]
    assert len({initial, failed, next_epoch}) == 3
    assert epoch(alpha.evidence_record)["counter"] == 2
    assert not alpha.evidence_record.exists()


def test_persistence_failure_does_not_start_provider_or_restore_manifest(tmp_path, monkeypatch):
    alpha = slot(tmp_path)
    alpha.evidence_record.write_text("synthetic manifest")
    manager, _, events, _ = make_manager((alpha,))

    def fail(*args, **kwargs):
        raise OSError("synthetic storage failure")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(TransitionError):
        manager.activate("alpha")
    assert not events
    assert not alpha.evidence_record.exists()
    assert epoch(alpha.evidence_record)["counter"] == 0
    assert not list(alpha.evidence_record.parent.glob("*.tmp-*"))


def test_ready_no_op_does_not_withdraw_evidence_or_advance_epoch(tmp_path):
    alpha = slot(tmp_path)
    alpha.evidence_record.write_text("synthetic manifest")
    manager, _, events, _ = make_manager((alpha,), active="alpha")
    assert manager.activate("alpha").state == RuntimeState.READY
    assert not events
    assert alpha.evidence_record.exists()
    assert epoch(alpha.evidence_record)["counter"] == 0


def test_configured_profile_cannot_bypass_required_invalidator(tmp_path):
    alpha = slot(tmp_path)
    events = []
    manager = RuntimeManager(
        RuntimeRegistry((alpha,)),
        FakeSystemd({}, events),
        FakeResources({}, events),
        FakeHealth({}, events),
        RecordingLock(),
    )
    with pytest.raises(TransitionError, match="invalidator is unavailable"):
        manager.activate("alpha")
    assert not events


def test_overlapping_slots_and_writable_parent_are_rejected(tmp_path):
    alpha = slot(tmp_path)
    with pytest.raises(ValueError, match="overlap"):
        FileEvidenceInvalidator(
            (alpha, replace(profile("beta"), evidence_record=alpha.evidence_record))
        )
    alpha.evidence_record.parent.chmod(0o777)
    with pytest.raises(OSError, match="ancestry"):
        FileEvidenceInvalidator((alpha,))


def test_second_slot_contention_does_not_withdraw_first_slot(tmp_path):
    alpha, beta = slot(tmp_path, "alpha"), slot(tmp_path, "beta")
    for p in (alpha, beta):
        p.evidence_record.write_text("synthetic manifest")
    manager, _, events, _ = make_manager((alpha, beta), active="alpha")
    with Path(str(beta.evidence_record) + ".lock").open("rb") as reader:
        fcntl.flock(reader, fcntl.LOCK_SH)
        with pytest.raises(TransitionError, match="in use"):
            manager.activate("beta")
    assert not events
    for p in (alpha, beta):
        assert p.evidence_record.exists()
        assert epoch(p.evidence_record)["counter"] == 0


def test_fsync_failure_before_epoch_change_blocks_service_mutation(tmp_path, monkeypatch):
    alpha = slot(tmp_path)
    alpha.evidence_record.write_text("synthetic manifest")
    manager, _, events, _ = make_manager((alpha,))

    def fail(fd):
        raise OSError("synthetic fsync failure")

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(TransitionError):
        manager.activate("alpha")
    assert not events
    assert not alpha.evidence_record.exists()
    assert epoch(alpha.evidence_record)["counter"] == 0
