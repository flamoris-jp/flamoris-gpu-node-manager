from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path

import pytest

from flamoris_gpu_node_manager.infrastructure import content_measurement as cm


@pytest.fixture
def installation(tmp_path: Path) -> tuple[dict[str, dict[str, Path]], dict[str, Path]]:
    core = tmp_path / "core"
    core.mkdir()
    (core / "main.py").write_bytes(b"core-v1")
    (core / "empty").mkdir()
    dependencies = tmp_path / "dependencies"
    dependencies.mkdir()
    (dependencies / "library.so").write_bytes(b"dependency-v1")
    config = tmp_path / "config.json"
    config.write_bytes(b'{"option":1}')
    model = tmp_path / "model.bin"
    model.write_bytes(b"model-v1")
    return (
        {
            "core": {"runtime": core},
            "dependencies": {"environment": dependencies},
            "config": {"settings": config},
        },
        {"checkpoints/example.bin": model},
    )


def measurer(*, limits: cm.MeasurementLimits | None = None) -> cm.ProtectedContentMeasurer:
    runtime_uid = 10001 if os.geteuid() != 10001 else 10002
    return cm.ProtectedContentMeasurer(runtime_uid=runtime_uid, limits=limits)


def test_real_bytes_deterministic_order_and_immutable_result(installation):
    groups, models = installation
    first = measurer().measure(groups, models)
    second = measurer().measure(dict(reversed(list(groups.items()))), models)
    assert first == second
    assert first.entries == 7
    assert first.bytes_read == len(b"core-v1dependency-v1model-v1") + len(b'{"option":1}')
    assert (
        first.models["checkpoints/example.bin"]
        == "sha256:" + hashlib.sha256(b"model-v1").hexdigest()
    )
    assert all(value.startswith("sha256:") and len(value) == 71 for value in first.groups.values())
    with pytest.raises(TypeError):
        first.models["checkpoints/example.bin"] = "changed"
    assert not hasattr(first, "provider_epoch")
    assert not hasattr(first, "nodes")


@pytest.mark.parametrize("changed", ["core", "dependencies", "config", "model"])
def test_same_name_content_changes_identity_even_with_restored_mtime(installation, changed):
    groups, models = installation
    authority = measurer()
    first = authority.measure(groups, models)
    path = {
        "core": groups["core"]["runtime"] / "main.py",
        "dependencies": groups["dependencies"]["environment"] / "library.so",
        "config": groups["config"]["settings"],
        "model": models["checkpoints/example.bin"],
    }[changed]
    old = path.stat()
    # Same filename, size and mtime: the measured byte digest still changes.
    path.write_bytes(b"X" * old.st_size)
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
    second = authority.measure(groups, models)
    if changed == "model":
        assert second.models != first.models
        assert second.groups == first.groups
    else:
        assert second.groups[changed] != first.groups[changed]


def test_directory_topology_and_no_file_exclusions(installation):
    groups, models = installation
    first = measurer().measure(groups, models)
    core = groups["core"]["runtime"]
    (core / "main.py").rename(core / "renamed.py")
    second = measurer().measure(groups, models)
    assert second.groups["core"] != first.groups["core"]
    (core / "empty").rmdir()
    third = measurer().measure(groups, models)
    assert third.groups["core"] != second.groups["core"]
    (core / ".hidden").write_bytes(b"included")
    fourth = measurer().measure(groups, models)
    assert fourth.groups["core"] != third.groups["core"]


@pytest.mark.parametrize("mode", [0o666, 0o664, 0o622])
def test_writable_content_rejected(installation, mode):
    groups, models = installation
    models["checkpoints/example.bin"].chmod(mode)
    with pytest.raises(cm.ContentMeasurementError):
        measurer().measure(groups, models)


def test_writable_ancestor_rejected_even_when_model_is_protected(installation, tmp_path):
    groups, models = installation
    parent = tmp_path / "writable-parent"
    parent.mkdir()
    parent.chmod(0o2775)
    model = parent / "model.bin"
    model.write_bytes(b"protected-model")
    model.chmod(0o600)
    models["checkpoints/example.bin"] = model
    with pytest.raises(cm.ContentMeasurementError):
        measurer().measure(groups, models)


@pytest.mark.parametrize("attribute", ["system.posix_acl_access", "system.posix_acl_default"])
def test_extended_acl_rejected_without_printing_its_value(installation, monkeypatch, attribute):
    groups, models = installation
    target = models["checkpoints/example.bin"].stat().st_ino
    original = os.getxattr

    def acl(fd, key):
        if os.fstat(fd).st_ino == target and key == attribute:
            return b"private-acl-value"
        return original(fd, key)

    monkeypatch.setattr(os, "getxattr", acl)
    with pytest.raises(cm.ContentMeasurementError) as failure:
        measurer().measure(groups, models)
    assert "private-acl-value" not in str(failure.value)


def test_acl_inspection_failure_is_not_treated_as_absence(installation, monkeypatch):
    groups, models = installation
    original = os.getxattr
    target = models["checkpoints/example.bin"].stat().st_ino

    def inaccessible(fd, key):
        if os.fstat(fd).st_ino == target:
            raise OSError(errno.ENOTSUP, "unknown ACL support")
        return original(fd, key)

    monkeypatch.setattr(os, "getxattr", inaccessible)
    with pytest.raises(cm.ContentMeasurementError):
        measurer().measure(groups, models)


def test_foreign_owner_rejected(installation, monkeypatch):
    groups, models = installation
    original = os.fstat
    target = models["checkpoints/example.bin"].stat().st_ino

    def foreign(fd):
        info = original(fd)
        if info.st_ino == target:
            fields = list(info)
            fields[4] = 10001 if os.geteuid() != 10001 else 10002
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(os, "fstat", foreign)
    with pytest.raises(cm.ContentMeasurementError):
        measurer().measure(groups, models)


@pytest.mark.parametrize("kind", ["file-link", "parent-link", "hard-link", "fifo"])
def test_links_and_special_files_fail_without_blocking(installation, tmp_path, kind):
    groups, models = installation
    original = models["checkpoints/example.bin"]
    substitute = tmp_path / "unsupported"
    if kind == "file-link":
        substitute.symlink_to(original)
    elif kind == "parent-link":
        substitute.symlink_to(groups["core"]["runtime"], target_is_directory=True)
        substitute /= "main.py"
    elif kind == "hard-link":
        os.link(original, substitute)
    else:
        os.mkfifo(substitute)
    models["checkpoints/example.bin"] = substitute
    with pytest.raises(cm.ContentMeasurementError):
        measurer().measure(groups, models)


def test_symlink_inside_tree_is_not_skipped(installation):
    groups, models = installation
    (groups["core"]["runtime"] / "link").symlink_to(models["checkpoints/example.bin"])
    with pytest.raises(cm.ContentMeasurementError):
        measurer().measure(groups, models)


def test_content_change_during_stream_rejected(installation, monkeypatch):
    groups, models = installation
    path = models["checkpoints/example.bin"]
    inode = path.stat().st_ino
    original = os.read
    changed = False

    def mutate(fd, bound):
        nonlocal changed
        chunk = original(fd, bound)
        if chunk and os.fstat(fd).st_ino == inode and not changed:
            changed = True
            path.write_bytes(b"mutated!")
        return chunk

    monkeypatch.setattr(os, "read", mutate)
    with pytest.raises(cm.ContentMeasurementError):
        measurer().measure(groups, models)
    assert changed


def test_large_model_streams_in_bounded_chunks(installation, monkeypatch):
    groups, models = installation
    data = b"x" * (1024**2 + 17)
    models["checkpoints/example.bin"].write_bytes(data)
    original = os.read
    bounds = []

    def read(fd, bound):
        bounds.append(bound)
        return original(fd, bound)

    monkeypatch.setattr(os, "read", read)
    result = measurer().measure(groups, models)
    assert result.models["checkpoints/example.bin"] == "sha256:" + hashlib.sha256(data).hexdigest()
    assert bounds and max(bounds) <= 1024**2


@pytest.mark.parametrize("change", ["file", "root", "permission", "removed"])
def test_late_change_rejected_after_earlier_root_was_hashed(installation, monkeypatch, change):
    groups, models = installation
    core = groups["core"]["runtime"]
    original = cm._MeasurementRun.model

    def mutate(run, path):
        result = original(run, path)
        if change == "file":
            replacement = core / "replacement"
            replacement.write_bytes(b"core-v1")
            replacement.replace(core / "main.py")
        elif change == "root":
            core.rename(core.with_name("previous"))
            core.mkdir()
            (core / "main.py").write_bytes(b"core-v1")
            (core / "empty").mkdir()
        elif change == "permission":
            core.chmod(0o777)
        else:
            (core / "main.py").unlink()
        return result

    monkeypatch.setattr(cm._MeasurementRun, "model", mutate)
    with pytest.raises(cm.ContentMeasurementError):
        measurer().measure(groups, models)


def test_exact_byte_budget_and_entry_budget(installation):
    groups, models = installation
    first = measurer().measure(groups, models)
    assert measurer(limits=cm.MeasurementLimits(max_bytes=first.bytes_read)).measure(groups, models)
    assert measurer(limits=cm.MeasurementLimits(max_entries=first.entries)).measure(groups, models)
    for limits in (
        cm.MeasurementLimits(max_bytes=first.bytes_read - 1),
        cm.MeasurementLimits(max_entries=first.entries - 1),
    ):
        with pytest.raises(cm.ContentMeasurementError):
            measurer(limits=limits).measure(groups, models)


def test_deadline_checked_during_measurement(installation, monkeypatch):
    groups, models = installation
    clock = iter([0.0, 0.0, 0.0, 0.0, 1.0])
    monkeypatch.setattr(cm.time, "monotonic", lambda: next(clock, 1.0))
    with pytest.raises(cm.ContentMeasurementError):
        measurer(limits=cm.MeasurementLimits(timeout=0.5)).measure(groups, models)


@pytest.mark.parametrize("empty", ["core", "dependencies", "config", "models", "model-file"])
def test_missing_content_cannot_yield_partial_success(installation, empty):
    groups, models = installation
    if empty == "models":
        models.clear()
    elif empty == "model-file":
        models["checkpoints/example.bin"].write_bytes(b"")
    else:
        groups[empty].clear()
    with pytest.raises(cm.ContentMeasurementError):
        measurer().measure(groups, models)


def test_missing_file_and_unsafe_names_are_unavailable(installation):
    groups, models = installation
    path = models["checkpoints/example.bin"]
    path.unlink()
    with pytest.raises(cm.ContentMeasurementError) as failure:
        measurer().measure(groups, models)
    assert str(path) not in str(failure.value)
    groups["core"]["runtime\nprivate"] = groups["core"].pop("runtime")
    with pytest.raises(cm.ContentMeasurementError):
        measurer().measure(groups, models)


def test_directory_depth_bound(installation):
    groups, models = installation
    path = groups["core"]["runtime"]
    for _ in range(66):
        path /= "nested"
        path.mkdir()
    with pytest.raises(cm.ContentMeasurementError):
        measurer().measure(groups, models)


def test_same_identity_and_root_runtime_are_not_supported():
    for uid in (os.geteuid(), 0, -1, True, 2**32):
        with pytest.raises(ValueError):
            cm.ProtectedContentMeasurer(runtime_uid=uid)


@pytest.mark.parametrize("key", ["checkpoint", "/model", "kind/../model", "kind//model"])
def test_model_identity_requires_kind_and_safe_name(installation, key):
    groups, models = installation
    path = models.pop("checkpoints/example.bin")
    models[key] = path
    with pytest.raises(cm.ContentMeasurementError):
        measurer().measure(groups, models)


@pytest.mark.parametrize(
    "values",
    [
        {"max_entries": 0},
        {"max_entries": True},
        {"max_bytes": 0},
        {"max_bytes": 1024**4 + 1},
        {"timeout": float("nan")},
        {"timeout": float("inf")},
        {"timeout": True},
        {"timeout": 0},
        {"timeout": 3601},
    ],
)
def test_invalid_limits_rejected(values):
    with pytest.raises(ValueError):
        cm.MeasurementLimits(**values)
