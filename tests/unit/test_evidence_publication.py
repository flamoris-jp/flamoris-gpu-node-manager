from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path

import pytest

from flamoris_gpu_node_manager.infrastructure import evidence_publication as publication
from flamoris_gpu_node_manager.infrastructure.evidence_publication import (
    EvidencePublicationError,
    FileEvidencePublisher,
    MeasuredManifest,
    MeasuredNode,
)
from flamoris_gpu_node_manager.infrastructure.runtime_evidence import provision_evidence_slot


def digest(character="a"):
    return "sha256:" + character * 64


def manifest():
    return MeasuredManifest(
        digest("a"),
        digest("b"),
        digest("c"),
        {"SyntheticNode": MeasuredNode(digest("d"), digest("e"))},
        {"checkpoint:synthetic.safetensors": digest("f")},
    )


class Source:
    def __init__(self):
        self.value = manifest()
        self.calls = 0
        self.action = None

    def measure(self, *, deadline):
        assert deadline > time.monotonic()
        self.calls += 1
        if self.action is not None:
            self.action()
        return self.value


class Lifetime:
    def __init__(self):
        self.token = "synthetic-boot-start-identity"
        self.calls = 0
        self.action = None

    def observe(self):
        self.calls += 1
        if self.action is not None:
            self.action()
        return self.token


def publisher(tmp_path: Path, *, name="slot", ttl=120, measurement_timeout=900, reader_gid=None):
    directory = tmp_path / name
    directory.mkdir(mode=0o700)
    record = directory / "runtime.json"
    provision_evidence_slot(record, reader_gid=reader_gid)
    source, lifetime = Source(), Lifetime()
    owner = FileEvidencePublisher(
        record,
        "http://127.0.0.1:8188",
        source,
        lifetime,
        ttl=ttl,
        measurement_timeout=measurement_timeout,
    )
    return owner, source, lifetime


def epoch(owner):
    return json.loads(Path(str(owner.record) + ".epoch.json").read_text())


def test_issue_uses_exact_schema_canonical_digest_and_never_reuses_epoch(tmp_path):
    owner, source, lifetime = publisher(tmp_path)
    assert not owner.record.exists()
    first = owner.issue()
    data = json.loads(owner.record.read_bytes())
    assert set(data) == {
        "schema_version",
        "continuity",
        "provider_url",
        "provider_epoch",
        "expires_at",
        "manifest",
    }
    assert data["schema_version"] == 1
    assert data["continuity"] == "exclusive-mutation-lock-v1"
    assert data["provider_url"] == "http://127.0.0.1:8188"
    assert data["manifest"] == source.value.value()
    assert data["provider_epoch"] == first.provider_epoch
    assert time.time() < data["expires_at"] <= time.time() + 120
    assert epoch(owner)["counter"] == 1
    assert lifetime.calls == 4
    restarted = FileEvidencePublisher(owner.record, data["provider_url"], source, lifetime)
    second = restarted.issue()
    assert second.provider_epoch != first.provider_epoch
    assert second.manifest_digest == first.manifest_digest
    assert epoch(owner)["counter"] == 2


def test_measured_manifest_copies_mappings_and_explicit_consumer_model_names():
    node = MeasuredNode(digest(), digest())
    nodes, models = {"Node": node}, {"checkpoint:folder/model.bin": digest()}
    measured = MeasuredManifest(digest(), digest(), digest(), nodes, models)
    nodes.clear()
    models.clear()
    assert measured.nodes == {"Node": node}
    assert measured.models == {"checkpoint:folder/model.bin": digest()}
    with pytest.raises(TypeError):
        measured.models["checkpoint:other"] = digest()
    with pytest.raises(ValueError, match="kind:name"):
        replace(measured, models={"checkpoint/model.bin": digest()})


@pytest.mark.parametrize("value", [True, 0, -1, float("inf"), float("nan"), 301])
def test_expiry_must_be_positive_finite_and_at_most_300(tmp_path, value):
    with pytest.raises(ValueError):
        publisher(tmp_path, ttl=value)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8188/",
        "http://user:secret@localhost:8188",
        "file:///tmp/test",
        "http://localhost/?q=1",
        "http://localhost/#fragment",
        "http://localhost/\nsecret",
        "http://localhost:",
        "http://localhost:bad",
        "http://localhost:65536",
        "http://localhost:0",
        "http://localhost\\secret",
        "http://localhost/path%broken",
    ],
)
def test_provider_url_is_exact_and_has_no_credential_or_normalization_fallback(tmp_path, url):
    owner, source, lifetime = publisher(tmp_path)
    with pytest.raises(ValueError):
        FileEvidencePublisher(owner.record, url, source, lifetime)


def test_same_identity_refresh_changes_only_expiry(tmp_path, monkeypatch):
    owner, source, _ = publisher(tmp_path)
    first = owner.issue()
    old = json.loads(owner.record.read_text())
    wall = time.time()
    monkeypatch.setattr(publication.time, "time", lambda: wall + 1)
    refreshed = owner.refresh()
    new = json.loads(owner.record.read_text())
    assert new.pop("expires_at") > old.pop("expires_at")
    assert new == old
    assert refreshed.provider_epoch == first.provider_epoch
    assert refreshed.manifest_digest == first.manifest_digest
    assert epoch(owner)["counter"] == 1
    assert source.calls == 2


@pytest.mark.parametrize("operation", ["issue", "refresh", "mutation"])
def test_shared_generation_guard_blocks_all_writers_without_withdrawing(tmp_path, operation):
    owner, _, _ = publisher(tmp_path)
    owner.issue()
    old = owner.record.read_bytes()
    before = epoch(owner)
    with Path(str(owner.record) + ".lock").open("rb") as reader:
        fcntl.flock(reader, fcntl.LOCK_SH)
        with pytest.raises(EvidencePublicationError):
            if operation == "mutation":
                with owner.mutation(timeout=0.01):
                    pytest.fail("contended lease entered")
            else:
                getattr(owner, operation)(timeout=0.01)
    assert owner.record.read_bytes() == old
    assert epoch(owner) == before


def test_mutation_withdraws_before_body_and_publish_stays_exclusive(tmp_path):
    owner, _, _ = publisher(tmp_path)
    first = owner.issue()
    with owner.mutation() as lease:
        assert not owner.record.exists()
        assert epoch(owner)["counter"] == 2
        with (
            Path(str(owner.record) + ".lock").open("rb") as reader,
            pytest.raises(BlockingIOError),
        ):
            fcntl.flock(reader, fcntl.LOCK_SH | fcntl.LOCK_NB)
        second = lease.publish()
        assert owner.record.exists()
        assert second.provider_epoch != first.provider_epoch
        with pytest.raises(EvidencePublicationError):
            lease.publish()
    with pytest.raises(EvidencePublicationError):
        lease.publish()


def test_locked_lease_is_inert_until_all_slots_acquired(tmp_path):
    first, _, _ = publisher(tmp_path, name="first")
    second, _, _ = publisher(tmp_path, name="second")
    first.issue()
    old, before = first.record.read_bytes(), epoch(first)
    with Path(str(second.record) + ".lock").open("rb") as reader:
        fcntl.flock(reader, fcntl.LOCK_SH)
        with pytest.raises(EvidencePublicationError), ExitStack() as stack:
            stack.enter_context(first.locked())
            stack.enter_context(second.locked(timeout=0.01))
    assert first.record.read_bytes() == old
    assert epoch(first) == before


def test_locked_lease_rejects_publication_before_begin_and_cross_thread(tmp_path):
    owner, _, _ = publisher(tmp_path)
    errors = []
    with owner.locked() as lease:
        with pytest.raises(EvidencePublicationError):
            lease.publish()
        lease.begin_mutation()
        with pytest.raises(EvidencePublicationError):
            lease.begin_mutation()

        def other_thread():
            try:
                lease.publish()
            except EvidencePublicationError as error:
                errors.append(error)

        worker = threading.Thread(target=other_thread)
        worker.start()
        worker.join(1)
        assert errors
    assert not owner.record.exists()


def test_mutation_exception_removes_even_a_published_result(tmp_path):
    owner, _, _ = publisher(tmp_path)
    with (
        pytest.raises(RuntimeError, match="secret runtime information"),
        owner.mutation() as lease,
    ):
        lease.publish()
        raise RuntimeError("secret runtime information")
    assert not owner.record.exists()


@pytest.mark.parametrize("phase", ["before", "measurement", "after_write"])
def test_lifetime_unknown_or_changed_never_leaves_manifest(tmp_path, phase, monkeypatch):
    owner, source, lifetime = publisher(tmp_path)
    if phase == "before":
        lifetime.token = ""
    elif phase == "measurement":
        source.action = lambda: setattr(lifetime, "token", "different-provider-start")
    else:
        original = owner._write

        def changed_after_write(*args):
            original(*args)
            lifetime.token = "different-provider-start"

        monkeypatch.setattr(owner, "_write", changed_after_write)
    with pytest.raises(
        EvidencePublicationError, match="^runtime evidence publication unavailable$"
    ):
        owner.issue()
    assert not owner.record.exists()


@pytest.mark.parametrize("failure", ["source", "drift", "lifetime", "expiry", "corrupt_epoch"])
def test_refresh_failure_withdraws_and_does_not_reuse_previous_epoch(
    tmp_path, failure, monkeypatch
):
    owner, source, lifetime = publisher(tmp_path)
    first = owner.issue()
    if failure == "source":

        def fail():
            raise RuntimeError("secret from trusted source")

        source.action = fail
    elif failure == "drift":
        source.value = replace(source.value, core_identity=digest("f"))
    elif failure == "lifetime":
        lifetime.token = "different-provider-start"
    elif failure == "expiry":
        monkeypatch.setattr(publication.time, "time", lambda: first.expires_at + 1)
    else:
        Path(str(owner.record) + ".epoch.json").write_text("{}")
    with pytest.raises(EvidencePublicationError):
        owner.refresh()
    assert not owner.record.exists()
    if failure != "corrupt_epoch":
        assert epoch(owner)["provider_epoch"] != first.provider_epoch


def test_measurement_timeout_withdraws_before_unlock(tmp_path, monkeypatch):
    owner, source, _ = publisher(tmp_path, measurement_timeout=1)
    clock = [time.monotonic()]
    monkeypatch.setattr(publication.time, "monotonic", lambda: clock[0])
    source.action = lambda: clock.__setitem__(0, clock[0] + 2)
    with pytest.raises(EvidencePublicationError):
        owner.issue()
    assert not owner.record.exists()


def test_expired_during_refresh_cannot_be_extended(tmp_path, monkeypatch):
    owner, source, _ = publisher(tmp_path, ttl=2)
    owner.issue()
    clock = [time.monotonic()]
    monkeypatch.setattr(publication.time, "monotonic", lambda: clock[0])
    source.action = lambda: clock.__setitem__(0, clock[0] + 3)
    with pytest.raises(EvidencePublicationError):
        owner.refresh()
    assert not owner.record.exists()


@pytest.mark.parametrize("operation", ["refresh", "withdraw"])
def test_stale_owner_cannot_erase_new_publisher_epoch(tmp_path, operation):
    owner, source, lifetime = publisher(tmp_path)
    owner.issue()
    later = FileEvidencePublisher(owner.record, "http://127.0.0.1:8188", source, lifetime)
    new = later.issue()
    if operation == "refresh":
        with pytest.raises(EvidencePublicationError):
            owner.refresh()
    else:
        owner.withdraw()
    assert json.loads(owner.record.read_text())["provider_epoch"] == new.provider_epoch


@pytest.mark.parametrize("failure", ["replaced_lock", "writable_record", "record_symlink"])
def test_unsafe_storage_rejects_publication_without_using_replaced_authority(tmp_path, failure):
    owner, _, _ = publisher(tmp_path)
    owner.issue()
    if failure == "replaced_lock":
        lock = Path(str(owner.record) + ".lock")
        lock.rename(lock.with_suffix(".old"))
        lock.touch(mode=0o600)
    elif failure == "writable_record":
        owner.record.chmod(0o666)
    else:
        owner.record.unlink()
        owner.record.symlink_to(tmp_path / "unknown.json")
    with pytest.raises(EvidencePublicationError):
        owner.issue()


def test_atomic_write_failure_leaves_no_manifest_or_temporary_file(tmp_path, monkeypatch):
    owner, _, _ = publisher(tmp_path)
    original = os.replace

    def fail_manifest(source, destination, **kwargs):
        if destination == owner.record.name:
            raise OSError("secret path in storage failure")
        return original(source, destination, **kwargs)

    monkeypatch.setattr(os, "replace", fail_manifest)
    with pytest.raises(EvidencePublicationError):
        owner.issue()
    assert not owner.record.exists()
    assert not list(owner.record.parent.glob("*.tmp-*"))


def test_manifest_group_read_follows_preprovisioned_lock(tmp_path):
    owner, _, _ = publisher(tmp_path, reader_gid=os.getegid())
    owner.issue()
    assert owner.record.stat().st_mode & 0o777 == 0o640
    assert owner.record.stat().st_gid == os.getegid()


def test_arbitrary_json_is_not_a_measurement_source(tmp_path):
    owner, source, _ = publisher(tmp_path)
    source.value = manifest().value()
    with pytest.raises(EvidencePublicationError):
        owner.issue()
    assert not owner.record.exists()


def test_record_over_one_mib_is_rejected():
    with pytest.raises(ValueError, match="bound"):
        MeasuredManifest(
            digest(),
            digest(),
            digest(),
            {str(index) + "x" * 1000: MeasuredNode(digest(), digest()) for index in range(1100)},
            {"checkpoint:synthetic": digest()},
        )


def test_automatic_refresh_preserves_identity_and_close_withdraws(tmp_path):
    owner, source, _ = publisher(tmp_path, ttl=1)
    first = owner.issue()
    refreshed = threading.Event()
    source.action = refreshed.set
    with owner.automatic_refresh(interval=0.01, timeout=0.1) as worker:
        assert refreshed.wait(1)
        assert worker.failure is None
    assert not owner.record.exists()
    assert epoch(owner)["provider_epoch"] != first.provider_epoch


def test_automatic_failure_is_reported_and_withdrawn(tmp_path):
    owner, source, _ = publisher(tmp_path, ttl=1)
    owner.issue()
    failed = threading.Event()

    def fail():
        failed.set()
        raise ValueError("secret source detail")

    source.action = fail
    with (
        pytest.raises(EvidencePublicationError),
        owner.automatic_refresh(interval=0.01, timeout=0.1) as worker,
    ):
        assert failed.wait(1)
    assert worker.failure is not None
    assert not owner.record.exists()


def test_cancelled_blocked_refresh_cannot_publish_after_context_close(tmp_path):
    owner, source, _ = publisher(tmp_path, ttl=1)
    owner.issue()
    entered, release = threading.Event(), threading.Event()

    def blocked():
        entered.set()
        assert release.wait(1)

    source.action = blocked
    with (
        pytest.raises(EvidencePublicationError),
        owner.automatic_refresh(interval=0.01, timeout=0.01) as worker,
    ):
        assert entered.wait(1)
    release.set()
    worker._thread.join(1)
    assert not worker._thread.is_alive()
    assert not owner.record.exists()


def test_final_lifetime_fence_after_publish_body_failure_withdraws(tmp_path):
    owner, _, lifetime = publisher(tmp_path)
    with pytest.raises(EvidencePublicationError), owner.mutation() as lease:
        lease.publish()
        lifetime.token = "different-start-after-publish"
    assert not owner.record.exists()


def test_old_automatic_owner_close_cannot_withdraw_new_issue(tmp_path):
    owner, _, _ = publisher(tmp_path)
    first = owner.issue()
    with owner.automatic_refresh(interval=30, timeout=0.1):
        second = owner.issue()
        assert first.provider_epoch != second.provider_epoch
    assert json.loads(owner.record.read_text())["provider_epoch"] == second.provider_epoch
    assert owner.refresh().provider_epoch == second.provider_epoch


def test_duplicate_automatic_owner_is_rejected(tmp_path):
    owner, _, _ = publisher(tmp_path)
    owner.issue()
    with (
        owner.automatic_refresh(interval=30, timeout=0.1),
        pytest.raises(ValueError),
        owner.automatic_refresh(interval=30, timeout=0.1),
    ):
        pytest.fail("duplicate owner entered")


def test_automatic_tick_from_old_epoch_preserves_new_record(tmp_path):
    owner, _, _ = publisher(tmp_path, ttl=1)
    owner.issue()
    with (
        pytest.raises(EvidencePublicationError),
        owner.automatic_refresh(interval=0.01, timeout=0.1) as worker,
    ):
        second = owner.issue()
        worker._thread.join(1)
        assert worker.failure is not None
    assert json.loads(owner.record.read_text())["provider_epoch"] == second.provider_epoch


def test_lock_setup_past_deadline_cannot_enter_body(tmp_path, monkeypatch):
    owner, _, _ = publisher(tmp_path)
    owner.issue()
    before = owner.record.read_bytes()
    clock = [time.monotonic()]
    original = owner._check_anchor
    monkeypatch.setattr(publication.time, "monotonic", lambda: clock[0])

    def slow_anchor(directory):
        original(directory)
        clock[0] += 2

    monkeypatch.setattr(owner, "_check_anchor", slow_anchor)
    with pytest.raises(EvidencePublicationError), owner.mutation(timeout=1):
        pytest.fail("timed-out body entered")
    assert owner.record.read_bytes() == before


def test_publication_io_consuming_new_ttl_cannot_report_success(tmp_path, monkeypatch):
    owner, _, _ = publisher(tmp_path, ttl=1, measurement_timeout=900)
    owner.issue()
    clock = [time.monotonic()]
    original = owner._write
    monkeypatch.setattr(publication.time, "monotonic", lambda: clock[0])

    def slow_write(*args):
        original(*args)
        clock[0] += 2

    monkeypatch.setattr(owner, "_write", slow_write)
    with pytest.raises(EvidencePublicationError):
        owner.refresh()
    assert not owner.record.exists()


@pytest.mark.parametrize("operation", ["refresh", "withdraw"])
def test_transient_epoch_error_is_revoked_and_redacted(tmp_path, monkeypatch, operation):
    owner, _, _ = publisher(tmp_path)
    owner.issue()
    original = owner._current_epoch
    calls = [0]

    def transient(directory):
        calls[0] += 1
        if calls[0] == 1:
            raise OSError("secret epoch storage path")
        return original(directory)

    monkeypatch.setattr(owner, "_current_epoch", transient)
    with pytest.raises(
        EvidencePublicationError, match="^runtime evidence publication unavailable$"
    ):
        getattr(owner, operation)()
    assert not owner.record.exists()


def test_automatic_worker_start_failure_withdraws_releases_owner_and_allows_retry(
    tmp_path, monkeypatch
):
    owner, _, _ = publisher(tmp_path)
    initial = owner.issue()
    original = threading.Thread.start

    def fail_start(_thread):
        raise RuntimeError("secret worker startup detail")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    with (
        pytest.raises(EvidencePublicationError, match="^runtime evidence publication unavailable$"),
        owner.automatic_refresh(interval=30, timeout=0.1),
    ):
        pytest.fail("failed worker entered")
    assert not owner.record.exists()
    assert owner._refresh_owner is None
    assert epoch(owner)["provider_epoch"] != initial.provider_epoch
    owner.issue()
    monkeypatch.setattr(threading.Thread, "start", original)
    with owner.automatic_refresh(interval=30, timeout=0.1):
        pass
    assert not owner.record.exists()


def test_failed_worker_start_cannot_withdraw_intervening_new_epoch(tmp_path, monkeypatch):
    owner, _, _ = publisher(tmp_path)
    owner.issue()
    replacement = []

    def issue_then_fail(_thread):
        replacement.append(owner.issue())
        raise RuntimeError("secret worker startup detail")

    monkeypatch.setattr(threading.Thread, "start", issue_then_fail)
    with pytest.raises(EvidencePublicationError), owner.automatic_refresh(interval=30, timeout=0.1):
        pytest.fail("failed worker entered")
    assert owner._refresh_owner is None
    assert json.loads(owner.record.read_text())["provider_epoch"] == replacement[0].provider_epoch
    assert owner.refresh().provider_epoch == replacement[0].provider_epoch


@pytest.mark.parametrize("schema_version", [True, 1.0])
def test_epoch_schema_rejects_boolean_and_float_before_refresh(tmp_path, schema_version):
    owner, _, _ = publisher(tmp_path)
    owner.issue()
    corrupted = epoch(owner)
    corrupted["schema_version"] = schema_version
    Path(str(owner.record) + ".epoch.json").write_text(json.dumps(corrupted))
    with pytest.raises(EvidencePublicationError):
        owner.refresh()
    assert not owner.record.exists()
