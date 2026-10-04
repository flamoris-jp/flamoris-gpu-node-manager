from __future__ import annotations

import fcntl
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from flamoris_gpu_node_manager.application.manager import RuntimeManager
from flamoris_gpu_node_manager.domain.errors import TransitionError
from flamoris_gpu_node_manager.domain.models import RuntimeState, ServiceState
from flamoris_gpu_node_manager.domain.registry import RuntimeRegistry
from flamoris_gpu_node_manager.infrastructure.evidence_lifecycle import (
    EvidencePublicationBinding,
    RuntimeEvidenceLifecycle,
)
from flamoris_gpu_node_manager.infrastructure.evidence_publication import (
    FileEvidencePublisher,
    MeasuredManifest,
    MeasuredNode,
)
from flamoris_gpu_node_manager.infrastructure.runtime_evidence import provision_evidence_slot
from tests.helpers import FakeHealth, FakeResources, FakeSystemd, RecordingLock, profile


def digest(text):
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


class Source:
    def __init__(self):
        self.fail = False
        self.calls = 0

    def measure(self, *, deadline):
        self.calls += 1
        if self.fail:
            raise ValueError("private measurement detail")
        return MeasuredManifest(
            digest("core"),
            digest("dependencies"),
            digest("config"),
            {"TestNode": MeasuredNode(digest("schema"), digest("implementation"))},
            {"checkpoint:test.safetensors": digest("model")},
        )


class Lifetime:
    def observe(self):
        return "offline-boot-instance"


def setup(tmp_path):
    profiles, publishers, sources = [], {}, {}
    for name in ("alpha", "beta"):
        directory = tmp_path / name
        directory.mkdir(mode=0o700)
        record = directory / "runtime.json"
        provision_evidence_slot(record)
        p = replace(profile(name), evidence_record=record)
        source = Source()
        publisher = FileEvidencePublisher(record, "http://127.0.0.1:8188", source, Lifetime())
        profiles.append(p)
        publishers[name], sources[name] = publisher, source
    events = []
    systemd = FakeSystemd({"alpha.service": ServiceState.ACTIVE}, events)
    health = FakeHealth({}, events)
    bindings = [
        EvidencePublicationBinding(
            p, publishers[p.id], lambda p=p: systemd.get_state(p.service) == ServiceState.ACTIVE
        )
        for p in profiles
    ]
    lifecycle = RuntimeEvidenceLifecycle(bindings)
    manager = RuntimeManager(
        RuntimeRegistry(profiles),
        systemd,
        FakeResources({"alpha": False}, events),
        health,
        RecordingLock(),
        lock_timeout=0.03,
        evidence_invalidator=lifecycle,
    )
    return manager, lifecycle, profiles, publishers, sources, systemd, health, events


def test_switch_publishes_only_target_under_existing_guard_after_health(tmp_path):
    manager, _, profiles, publishers, _, systemd, health, _ = setup(tmp_path)
    for publisher in publishers.values():
        publisher.issue()
    original = health.wait_healthy

    def checked(p, timeout):
        assert systemd.get_state(p.service) == ServiceState.ACTIVE
        for selected in profiles:
            assert not selected.evidence_record.exists()
            with (
                Path(str(selected.evidence_record) + ".lock").open("rb") as reader,
                pytest.raises(BlockingIOError),
            ):
                fcntl.flock(reader, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return original(p, timeout)

    health.wait_healthy = checked
    assert manager.activate("beta").state == RuntimeState.READY
    assert not profiles[0].evidence_record.exists()
    assert profiles[1].evidence_record.exists()


def test_all_slots_lock_before_any_epoch_or_withdrawal(tmp_path):
    manager, _, profiles, publishers, _, _, _, events = setup(tmp_path)
    for publisher in publishers.values():
        publisher.issue()
    before = [p.evidence_record.read_bytes() for p in profiles]
    with Path(str(profiles[1].evidence_record) + ".lock").open("rb") as reader:
        fcntl.flock(reader, fcntl.LOCK_SH | fcntl.LOCK_NB)
        with pytest.raises(TransitionError, match="lifecycle unavailable"):
            manager.activate("beta")
    assert events == []
    assert [p.evidence_record.read_bytes() for p in profiles] == before


def test_health_failure_never_publishes(tmp_path):
    manager, _, profiles, publishers, sources, _, health, _ = setup(tmp_path)
    publishers["alpha"].issue()
    health.wait_result = False
    with pytest.raises(TransitionError):
        manager.activate("beta")
    assert all(not p.evidence_record.exists() for p in profiles)
    assert sources["beta"].calls == 0


def test_stop_does_not_measure_or_restore_record(tmp_path):
    manager, _, profiles, publishers, sources, _, _, _ = setup(tmp_path)
    publishers["alpha"].issue()
    before = sources["alpha"].calls
    assert manager.stop("alpha")[0].state == RuntimeState.OFF
    assert not profiles[0].evidence_record.exists()
    assert sources["alpha"].calls == before


def test_ready_activation_noop_preserves_epoch(tmp_path):
    manager, _, profiles, publishers, sources, _, _, _ = setup(tmp_path)
    publishers["alpha"].issue()
    before = profiles[0].evidence_record.read_bytes()
    assert manager.activate("alpha").state == RuntimeState.READY
    assert profiles[0].evidence_record.read_bytes() == before
    assert sources["alpha"].calls == 1


def test_failed_later_publication_revokes_earlier_slot(tmp_path):
    _, _, profiles, publishers, sources, _, _, _ = setup(tmp_path)
    sources["beta"].fail = True
    lifecycle = RuntimeEvidenceLifecycle(
        [EvidencePublicationBinding(p, publishers[p.id], lambda: True) for p in profiles]
    )
    with (
        pytest.raises(TransitionError, match="lifecycle unavailable"),
        lifecycle.hold(profiles, 0.1),
    ):
        pass
    assert all(not p.evidence_record.exists() for p in profiles)


def test_unknown_profile_blocks_before_any_service_mutation(tmp_path):
    manager, lifecycle, profiles, _, _, _, _, _ = setup(tmp_path)
    mismatched = replace(profiles[0], evidence_record=tmp_path / "elsewhere.json")
    with pytest.raises(TransitionError), lifecycle.hold((mismatched,), 0.1):
        pytest.fail("unknown binding admitted")
    assert manager.runtime_status("alpha").runtime == "alpha"


def test_invalid_eligibility_is_unavailable(tmp_path):
    _, _, profiles, publishers, _, _, _, _ = setup(tmp_path)
    lifecycle = RuntimeEvidenceLifecycle(
        [EvidencePublicationBinding(profiles[0], publishers["alpha"], lambda: None)]
    )
    with (
        pytest.raises(TransitionError, match="lifecycle unavailable"),
        lifecycle.hold((profiles[0],), 0.1),
    ):
        pass
    assert not profiles[0].evidence_record.exists()
    epoch = json.loads(Path(str(profiles[0].evidence_record) + ".epoch.json").read_text())
    assert epoch["counter"] > 0
