from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import replace
from importlib.machinery import ModuleSpec
from types import MappingProxyType, ModuleType
from unittest.mock import patch

import pytest

from flamoris_gpu_node_manager.infrastructure import comfyui_capture as capture
from flamoris_gpu_node_manager.infrastructure import comfyui_measurement as cm


def module(name, path):
    value = ModuleType(name)
    value.__file__ = str(path)
    value.__spec__ = ModuleSpec(name, None, origin=str(path))
    return value


@pytest.fixture
def initialized(tmp_path):
    core = tmp_path / "core"
    core.mkdir()
    dependencies = tmp_path / "dependencies"
    dependencies.mkdir()
    namespace = dependencies / "namespace"
    namespace.mkdir()
    first_models = tmp_path / "first-models"
    first_models.mkdir()
    models = tmp_path / "models"
    models.mkdir()
    embeddings = tmp_path / "embeddings"
    embeddings.mkdir()
    settings = tmp_path / "settings.json"
    settings.write_bytes(b'{"option":1}')
    for path, data in (
        (core / "nodes.py", b"node-implementation-v1"),
        (core / "folder_paths.py", b"folder-lookup-v1"),
        (dependencies / "python", b"interpreter-v1"),
        (dependencies / "library.so", b"native-library-v1"),
        (models / "example.bin", b"checkpoint-v1"),
        (embeddings / "example.pt", b"embedding-v1"),
    ):
        path.write_bytes(data)
    nodes = module("nodes", core / "nodes.py")
    folders = module("folder_paths", core / "folder_paths.py")

    class SyntheticNode:
        @classmethod
        def INPUT_TYPES(cls):
            raise AssertionError("source adapter must not invoke node interfaces")

    SyntheticNode.__module__ = "nodes"
    nodes.NODE_CLASS_MAPPINGS = {"Image": SyntheticNode}
    folders.folder_names_and_paths = {
        "checkpoints": ([str(first_models), str(models)], {".bin"}),
        "embeddings": ([str(embeddings)], {".pt"}),
    }
    builtin = ModuleType("builtin")
    builtin.__spec__ = ModuleSpec("builtin", None, origin="built-in")
    frozen = ModuleType("frozen")
    frozen.__spec__ = ModuleSpec("frozen", None, origin="frozen")
    package = ModuleType("namespace")
    package.__path__ = [str(namespace)]
    package.__spec__ = ModuleSpec("namespace", None)
    runtime_uid = 10001 if os.geteuid() != 10001 else 10002
    with (
        patch.object(
            sys,
            "modules",
            {
                "nodes": nodes,
                "folder_paths": folders,
                "builtin": builtin,
                "frozen": frozen,
                "namespace": package,
                "blocked": None,
            },
        ),
        patch.object(sys, "path", [str(core), str(dependencies)]),
        patch.object(sys, "executable", str(dependencies / "python")),
        patch.object(os, "getcwd", return_value=str(core)),
        patch.object(os, "geteuid", return_value=runtime_uid),
    ):
        bindings = capture.capture_comfyui_bindings()
    return cm.InitializedObservation(
        bindings=bindings,
        node_interfaces={
            "Image": {
                "inputs": {"画像": {"kind": "IMAGE", "optional": False}},
                "output_types": ["IMAGE"],
                "output_node": False,
            }
        },
        closure=cm.ComfyUIClosure(
            groups={
                "core": {"runtime": core},
                "dependencies": {"environment": dependencies},
                "config": {
                    "settings": settings,
                    "first-checkpoint-lookup": first_models,
                    "checkpoint-lookup": models,
                    "embedding-lookup": embeddings,
                },
            },
            native_libraries=(dependencies / "library.so",),
            checkpoints={"example.bin": models / "example.bin"},
            complete=True,
            mutation_coverage_complete=True,
        ),
        initialized=True,
        schema_contract=cm.SCHEMA_CONTRACT,
        snapshot_token="retained-object-bindings-v1",
    )


class Port:
    def __init__(self, observation, after=None):
        self.observation = observation
        self.after = observation if after is None else after
        self.calls = []

    def observe(self, *, deadline):
        self.calls.append(deadline)
        return self.observation if len(self.calls) == 1 else self.after


def measure(observation, *, after=None, deadline=None):
    port = Port(observation, after)
    manifest = cm.ComfyUIManifestSource(port).measure(
        deadline=time.monotonic() + 10 if deadline is None else deadline
    )
    return manifest, port


def changed_bindings(observation, **changes):
    return replace(observation, bindings=replace(observation.bindings, **changes))


def test_actual_capture_to_real_content_and_consumer_identity(initialized):
    first, port = measure(initialized)
    second, _ = measure(initialized)
    assert first == second
    assert len(port.calls) == 2 and port.calls[0] == port.calls[1]
    assert first.models == {
        "checkpoint:example.bin": "sha256:" + hashlib.sha256(b"checkpoint-v1").hexdigest()
    }
    raw = json.dumps(
        initialized.node_interfaces["Image"],
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    assert first.nodes["Image"].interface == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert first.nodes["Image"].implementation.startswith("sha256:")
    with pytest.raises(TypeError):
        first.nodes["Other"] = first.nodes["Image"]
    assert not hasattr(first, "provider_epoch")
    assert not hasattr(first, "expires_at")


@pytest.mark.parametrize("group", ["core", "dependencies", "config", "checkpoint"])
def test_same_name_content_changes_measured_identity(initialized, group):
    first, _ = measure(initialized)
    path = {
        "core": initialized.bindings.nodes["Image"].file,
        "dependencies": initialized.closure.native_libraries[0],
        "config": initialized.closure.groups["config"]["settings"],
        "checkpoint": initialized.closure.checkpoints["example.bin"],
    }[group]
    original = path.stat()
    path.write_bytes(b"X" * original.st_size)
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    second, _ = measure(initialized)
    assert first.nodes["Image"].interface == second.nodes["Image"].interface
    assert first.nodes["Image"].implementation != second.nodes["Image"].implementation
    if group == "checkpoint":
        assert first.models != second.models


@pytest.mark.parametrize("flag", ["initialized", "closure", "mutations", "schema"])
def test_trusted_port_must_certify_all_prerequisites(initialized, flag):
    if flag == "initialized":
        changed = replace(initialized, initialized=False)
    elif flag == "closure":
        changed = replace(initialized, closure=replace(initialized.closure, complete=False))
    elif flag == "mutations":
        changed = replace(
            initialized,
            closure=replace(initialized.closure, mutation_coverage_complete=False),
        )
    else:
        changed = replace(initialized, schema_contract="unreviewed-remote-object-info")
    with pytest.raises(cm.ComfyUIMeasurementError):
        measure(changed)


@pytest.mark.parametrize("kind", ["unknown", "lazy-namespace", "explicit-unresolved"])
def test_unknown_or_unresolved_module_never_grants_coverage(initialized, kind):
    if kind == "explicit-unresolved":
        changed = changed_bindings(initialized, unresolved_modules=("private-module",))
    else:
        modules = dict(initialized.bindings.modules)
        modules["private-module"] = capture.ModuleBinding(
            "unknown" if kind == "unknown" else "namespace",
            None,
            () if kind == "unknown" else None,
        )
        changed = changed_bindings(initialized, modules=MappingProxyType(modules))
    with pytest.raises(cm.ComfyUIMeasurementError) as failure:
        measure(changed)
    assert "private-module" not in str(failure.value)


@pytest.mark.parametrize("kind", ["executable", "search", "native", "module", "namespace"])
def test_every_observed_origin_must_be_in_measured_closure(initialized, tmp_path, kind):
    outside = tmp_path / "outside"
    outside.mkdir()
    file = outside / "private.py"
    file.write_bytes(b"uncovered")
    if kind == "executable":
        changed = changed_bindings(initialized, executable=file)
    elif kind == "search":
        changed = changed_bindings(
            initialized, search_paths=(*initialized.bindings.search_paths, outside)
        )
    elif kind == "native":
        changed = replace(
            initialized,
            closure=replace(initialized.closure, native_libraries=(file,)),
        )
    else:
        modules = dict(initialized.bindings.modules)
        modules["uncovered"] = capture.ModuleBinding(
            "file" if kind == "module" else "namespace",
            file if kind == "module" else None,
            () if kind == "module" else (outside,),
        )
        changed = changed_bindings(initialized, modules=MappingProxyType(modules))
    with pytest.raises(cm.ComfyUIMeasurementError):
        measure(changed)


@pytest.mark.parametrize("kind", ["module", "namespace", "native"])
def test_lexical_coverage_alone_cannot_prove_origin_exists(initialized, kind):
    missing = initialized.closure.groups["core"]["runtime"] / "missing"
    if kind == "native":
        changed = replace(
            initialized, closure=replace(initialized.closure, native_libraries=(missing,))
        )
    else:
        modules = dict(initialized.bindings.modules)
        modules["missing"] = capture.ModuleBinding(
            "file" if kind == "module" else "namespace",
            missing if kind == "module" else None,
            () if kind == "module" else (missing,),
        )
        changed = changed_bindings(initialized, modules=MappingProxyType(modules))
    with pytest.raises(cm.ComfyUIMeasurementError):
        measure(changed)


@pytest.mark.parametrize("target", ["code", "model", "earlier-lookup"])
def test_writable_runtime_or_lookup_content_fails_closed(initialized, target):
    path = {
        "code": initialized.bindings.nodes["Image"].file,
        "model": initialized.closure.checkpoints["example.bin"],
        "earlier-lookup": initialized.bindings.model_folders["checkpoints"].paths[0],
    }[target]
    path.chmod(0o777)
    with pytest.raises(cm.ComfyUIMeasurementError):
        measure(initialized)


def test_protected_selected_model_does_not_cover_unmeasured_earlier_lookup(initialized):
    groups = {name: dict(roots) for name, roots in initialized.closure.groups.items()}
    del groups["config"]["first-checkpoint-lookup"]
    changed = replace(initialized, closure=replace(initialized.closure, groups=groups))
    with pytest.raises(cm.ComfyUIMeasurementError):
        measure(changed)


def test_non_checkpoint_model_lookup_must_be_measured(initialized):
    groups = {name: dict(roots) for name, roots in initialized.closure.groups.items()}
    del groups["config"]["embedding-lookup"]
    changed = replace(initialized, closure=replace(initialized.closure, groups=groups))
    with pytest.raises(cm.ComfyUIMeasurementError):
        measure(changed)


def test_embedding_bytes_change_implementation_identity(initialized):
    first, _ = measure(initialized)
    embeddings = initialized.closure.groups["config"]["embedding-lookup"]
    model = embeddings / "example.pt"
    prior = model.stat()
    model.write_bytes(b"X" * prior.st_size)
    os.utime(model, ns=(prior.st_atime_ns, prior.st_mtime_ns))
    second, _ = measure(initialized)
    assert first.nodes["Image"].interface == second.nodes["Image"].interface
    assert first.nodes["Image"].implementation != second.nodes["Image"].implementation
    assert first.config_identity != second.config_identity


def test_missing_or_writable_embedding_lookup_is_unavailable(initialized):
    root = initialized.closure.groups["config"]["embedding-lookup"]
    (root / "example.pt").unlink()
    root.rmdir()
    with pytest.raises(cm.ComfyUIMeasurementError):
        measure(initialized)
    root.mkdir()
    root.chmod(0o777)
    with pytest.raises(cm.ComfyUIMeasurementError):
        measure(initialized)


def test_actual_first_lookup_shadows_same_name_model(initialized):
    folders = initialized.bindings.model_folders["checkpoints"]
    shadow = folders.paths[0] / "example.bin"
    shadow.write_bytes(b"different-checkpoint")
    with pytest.raises(cm.ComfyUIMeasurementError):
        measure(initialized)
    changed = replace(
        initialized, closure=replace(initialized.closure, checkpoints={"example.bin": shadow})
    )
    result, _ = measure(changed)
    assert (
        result.models["checkpoint:example.bin"]
        == "sha256:" + hashlib.sha256(b"different-checkpoint").hexdigest()
    )


@pytest.mark.parametrize(
    "name",
    [
        "../example.bin",
        "sub/../example.bin",
        "./example.bin",
        "/example.bin",
        "sub\\example.bin",
        "example.other",
    ],
)
def test_exact_checkpoint_loader_names_cannot_be_normalized(initialized, name):
    changed = replace(
        initialized,
        closure=replace(
            initialized.closure, checkpoints={name: initialized.closure.checkpoints["example.bin"]}
        ),
    )
    with pytest.raises(cm.ComfyUIMeasurementError):
        measure(changed)


def test_nested_checkpoint_identity_uses_explicit_kind_transform(initialized):
    model = initialized.closure.checkpoints["example.bin"]
    nested = model.parent / "sub"
    nested.mkdir()
    target = nested / model.name
    model.rename(target)
    changed = replace(
        initialized, closure=replace(initialized.closure, checkpoints={"sub/example.bin": target})
    )
    manifest, _ = measure(changed)
    assert set(manifest.models) == {"checkpoint:sub/example.bin"}


@pytest.mark.parametrize("kind", ["path-dotdot", "symlink", "model-symlink"])
def test_symlink_or_dotdot_origin_is_not_rewritten(initialized, tmp_path, kind):
    if kind == "path-dotdot":
        path = initialized.bindings.executable.parent / ".." / "dependencies" / "python"
        changed = changed_bindings(initialized, executable=path)
    elif kind == "symlink":
        path = initialized.bindings.executable
        backup = path.with_name("python-actual")
        path.rename(backup)
        path.symlink_to(backup)
        changed = initialized
    else:
        path = initialized.closure.checkpoints["example.bin"]
        outside = tmp_path / "other-model.bin"
        path.rename(outside)
        path.symlink_to(outside)
        changed = initialized
    with pytest.raises(cm.ComfyUIMeasurementError):
        measure(changed)


@pytest.mark.parametrize("kind", ["token", "pid", "schema", "search", "node", "lookup"])
def test_binding_changes_before_after_hashing_reject_manifest(initialized, kind):
    if kind == "token":
        changed = replace(initialized, snapshot_token="replacement-same-names-and-paths")
    elif kind == "pid":
        changed = changed_bindings(initialized, pid=initialized.bindings.pid + 1)
    elif kind == "schema":
        changed = replace(initialized, node_interfaces={"Image": {"new_input": "STRING"}})
    elif kind == "search":
        changed = changed_bindings(
            initialized, search_paths=tuple(reversed(initialized.bindings.search_paths))
        )
    elif kind == "node":
        binding = initialized.bindings.nodes["Image"]
        changed = changed_bindings(
            initialized,
            nodes=MappingProxyType({"Image": replace(binding, qualname="ReplacementNode")}),
        )
    else:
        folders = initialized.bindings.model_folders["checkpoints"]
        changed = changed_bindings(
            initialized,
            model_folders=MappingProxyType(
                {"checkpoints": replace(folders, paths=tuple(reversed(folders.paths)))}
            ),
        )
    with pytest.raises(cm.ComfyUIMeasurementError):
        measure(initialized, after=changed)


def test_port_failure_does_not_expose_private_debug_data(initialized):
    class Broken:
        def observe(self, *, deadline):
            raise RuntimeError("secret source path and private configuration")

    with pytest.raises(cm.ComfyUIMeasurementError) as failure:
        cm.ComfyUIManifestSource(Broken()).measure(deadline=time.monotonic() + 10)
    assert str(failure.value) == "initialized runtime measurement unavailable"


@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), True, -1.0])
def test_invalid_or_exhausted_absolute_deadline(initialized, deadline):
    port = Port(initialized)
    with pytest.raises(cm.ComfyUIMeasurementError):
        cm.ComfyUIManifestSource(port).measure(deadline=deadline)
    assert not port.calls


def test_incomplete_interface_and_node_origin_mismatch(initialized):
    with pytest.raises(cm.ComfyUIMeasurementError):
        measure(replace(initialized, node_interfaces={}))
    binding = initialized.bindings.nodes["Image"]
    changed = changed_bindings(
        initialized,
        nodes=MappingProxyType({"Image": replace(binding, module="not-an-installed-module")}),
    )
    with pytest.raises(cm.ComfyUIMeasurementError):
        measure(changed)
