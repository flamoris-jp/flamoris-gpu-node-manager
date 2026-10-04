from __future__ import annotations

import builtins
import os
import sys
from dataclasses import FrozenInstanceError
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

from flamoris_gpu_node_manager.infrastructure import comfyui_capture as cc


def source_module(name, file):
    module = ModuleType(name)
    module.__file__ = str(file)
    module.__spec__ = ModuleSpec(name, None, origin=str(file))
    return module


@pytest.fixture
def runtime(tmp_path):
    nodes = source_module("nodes", tmp_path / "core/nodes.py")
    folders = source_module("folder_paths", tmp_path / "core/folder_paths.py")
    plugin = source_module("custom.plugin", tmp_path / "external/plugin.py")

    class ImageNode:
        @classmethod
        def INPUT_TYPES(cls):
            raise AssertionError("node interface must not execute")

        @classmethod
        def GET_SCHEMA(cls):
            raise AssertionError("modern interface must not execute")

    ImageNode.__module__ = "nodes"
    nodes.NODE_CLASS_MAPPINGS = {"Image": ImageNode}
    folders.folder_names_and_paths = {
        "checkpoints": ([str(tmp_path / "models"), "relative-models"], {".bin", ".ckpt"}),
        "custom_nodes": ([str(tmp_path / "external")], set()),
    }
    builtin = ModuleType("synthetic_builtin")
    builtin.__spec__ = ModuleSpec("synthetic_builtin", None, origin="built-in")
    frozen = ModuleType("synthetic_frozen")
    frozen.__file__ = str(tmp_path / "stdlib/frozen.py")
    frozen.__spec__ = ModuleSpec("synthetic_frozen", None, origin="frozen")
    modules = {
        "nodes": nodes,
        "folder_paths": folders,
        "custom.plugin": plugin,
        "synthetic_builtin": builtin,
        "synthetic_frozen": frozen,
        "blocked": None,
    }
    return modules, nodes, folders, tmp_path


def capture(runtime, *, modules=None):
    original, _, _, root = runtime
    with (
        patch.object(sys, "modules", original if modules is None else modules),
        patch.object(sys, "path", ["", str(root / "core")]),
        patch.object(sys, "executable", str(root / "python")),
    ):
        return cc.capture_comfyui_bindings()


def test_actual_registrations_all_origins_order_and_immutable_result(runtime):
    modules, _, folders, root = runtime
    observed = capture(runtime)
    assert observed.pid == os.getpid()
    assert observed.uid == os.geteuid()
    assert observed.gid == os.getegid()
    assert observed.executable == root / "python"
    assert observed.cwd == Path.cwd()
    assert observed.search_paths == (Path.cwd(), root / "core")
    assert observed.nodes["Image"].file == root / "core/nodes.py"
    assert observed.nodes["Image"].module == "nodes"
    assert "custom.plugin" in observed.modules
    assert observed.modules["synthetic_builtin"].kind == "builtin"
    assert observed.modules["synthetic_frozen"].kind == "frozen"
    assert observed.modules["blocked"].kind == "blocked"
    assert observed.model_folders["checkpoints"].paths == (
        root / "models",
        Path.cwd() / "relative-models",
    )
    assert observed.model_folders["checkpoints"].extensions == (".bin", ".ckpt")
    with pytest.raises(TypeError):
        observed.nodes["other"] = observed.nodes["Image"]
    with pytest.raises(FrozenInstanceError):
        observed.nodes["Image"].module = "other"
    modules.clear()
    folders.folder_names_and_paths["checkpoints"][0].append("changed")
    assert "nodes" in observed.modules
    assert len(observed.model_folders["checkpoints"].paths) == 2
    assert not hasattr(observed, "provider_epoch")
    assert not hasattr(observed, "expires_at")
    assert not hasattr(observed, "manifest")


def test_no_import_interface_loader_file_read_or_dynamic_getter(runtime):
    modules, _, _, _ = runtime

    class GuardedModule(ModuleType):
        @property
        def __dict__(self):
            raise AssertionError("dynamic module dict getter")

        def __getattr__(self, name):
            raise AssertionError("dynamic module attribute getter")

    modules["guarded"] = GuardedModule("guarded")
    with (
        patch.object(builtins, "__import__", side_effect=AssertionError("import")),
        patch.object(builtins, "open", side_effect=AssertionError("file read")),
        patch.object(os, "open", side_effect=AssertionError("fd open")),
    ):
        observed = capture(runtime)
    assert observed.modules["guarded"].kind == "unknown"


def test_metaclass_attribute_hooks_not_called(runtime):
    _, nodes, _, _ = runtime

    class Guarded(type):
        def __getattribute__(cls, name):
            raise AssertionError("metaclass attribute getter")

    class Node(metaclass=Guarded):
        pass

    type.__setattr__(Node, "__module__", "nodes")
    nodes.NODE_CLASS_MAPPINGS["Guarded"] = Node
    assert capture(runtime).nodes["Guarded"].module == "nodes"


def test_lazy_namespace_iterator_and_equality_not_called(runtime):
    modules, _, _, _ = runtime

    class Lazy:
        def __iter__(self):
            raise AssertionError("namespace resolver executed")

        def __eq__(self, other):
            raise AssertionError("namespace equality executed")

    namespace = ModuleType("namespace")
    namespace.__path__ = Lazy()
    namespace.__spec__ = ModuleSpec("namespace", None)
    modules["namespace"] = namespace
    observed = capture(runtime)
    assert observed.modules["namespace"].kind == "namespace"
    assert observed.modules["namespace"].namespace_paths is None
    assert "namespace" in observed.unresolved_modules


def test_normal_namespace_paths_and_unknown_spec_preserved(runtime):
    modules, _, _, root = runtime
    namespace = ModuleType("namespace")
    namespace.__path__ = [str(root / "one"), str(root / "two")]
    modules["namespace"] = namespace

    class UnknownSpec:
        @property
        def origin(self):
            raise AssertionError("spec getter executed")

    unknown = ModuleType("unknown")
    unknown.__spec__ = UnknownSpec()
    modules["unknown"] = unknown
    observed = capture(runtime)
    assert observed.modules["namespace"].namespace_paths == (root / "one", root / "two")
    assert observed.modules["unknown"].kind == "unknown"
    assert "unknown" in observed.unresolved_modules


def test_mismatched_file_origin_is_unknown(runtime):
    modules, _, _, root = runtime
    modules["custom.plugin"].__spec__.origin = str(root / "different.py")
    assert capture(runtime).modules["custom.plugin"].kind == "unknown"


def test_spec_origin_getter_and_untrusted_origin_object_not_called(runtime, monkeypatch):
    modules, _, _, _ = runtime
    spec = modules["custom.plugin"].__spec__
    monkeypatch.setattr(
        ModuleSpec,
        "origin",
        property(lambda self: (_ for _ in ()).throw(AssertionError("spec property getter"))),
        raising=False,
    )
    assert capture(runtime).modules["custom.plugin"].kind == "file"

    class Unknown:
        def __eq__(self, other):
            raise AssertionError("origin equality")

        def __fspath__(self):
            raise AssertionError("origin path conversion")

    spec.__dict__["origin"] = Unknown()
    with pytest.raises(cc.RuntimeCaptureError):
        capture(runtime)


def test_empty_executable_refused(runtime):
    modules, _, _, _ = runtime
    with (
        patch.object(sys, "modules", modules),
        patch.object(sys, "executable", ""),
        pytest.raises(cc.RuntimeCaptureError),
    ):
        cc.capture_comfyui_bindings()


@pytest.mark.parametrize(
    "change", ["node", "node_registry", "module", "module_registry", "folders", "paths"]
)
def test_registration_or_context_change_refused(runtime, monkeypatch, change):
    modules, nodes, folders, root = runtime
    original = cc._collect
    calls = 0

    def collect(budget):
        nonlocal calls
        result = original(budget)
        calls += 1
        if calls == 1:
            if change == "node":
                old = nodes.NODE_CLASS_MAPPINGS["Image"]
                replacement = type("Replacement", (), {"__module__": "nodes"})
                replacement.__qualname__ = old.__qualname__
                nodes.NODE_CLASS_MAPPINGS["Image"] = replacement
            elif change == "node_registry":
                nodes.NODE_CLASS_MAPPINGS = dict(nodes.NODE_CLASS_MAPPINGS)
            elif change == "module":
                modules["custom.plugin"] = source_module(
                    "custom.plugin", root / "external/plugin.py"
                )
            elif change == "module_registry":
                sys.modules = dict(sys.modules)
            elif change == "folders":
                folders.folder_names_and_paths["checkpoints"][0].reverse()
            else:
                sys.path.reverse()
        return result

    monkeypatch.setattr(cc, "_collect", collect)
    with pytest.raises(cc.RuntimeCaptureError, match="^runtime binding capture unavailable$"):
        capture(runtime)


@pytest.mark.parametrize(
    "change",
    [
        "missing_nodes",
        "missing_folders",
        "empty_nodes",
        "empty_folders",
        "bad_node",
        "unknown_node_module",
        "builtin_node_module",
        "bad_folder",
        "bad_module",
    ],
)
def test_missing_or_unsupported_bindings_refused(runtime, change):
    modules, nodes, folders, _ = runtime
    if change == "missing_nodes":
        del modules["nodes"]
    elif change == "missing_folders":
        del modules["folder_paths"]
    elif change == "empty_nodes":
        nodes.NODE_CLASS_MAPPINGS = {}
    elif change == "empty_folders":
        folders.folder_names_and_paths = {}
    elif change == "bad_node":
        nodes.NODE_CLASS_MAPPINGS["Image"] = object()
    elif change == "unknown_node_module":
        nodes.NODE_CLASS_MAPPINGS["Image"].__module__ = "missing"
    elif change == "builtin_node_module":
        nodes.NODE_CLASS_MAPPINGS["Image"].__module__ = "synthetic_builtin"
    elif change == "bad_folder":
        folders.folder_names_and_paths["checkpoints"] = [[], []]
    else:
        modules["bad"] = object()
    with pytest.raises(cc.RuntimeCaptureError, match="^runtime binding capture unavailable$"):
        capture(runtime)


@pytest.mark.parametrize("value", ["bad\nname", "bad\x7fname", "x" * 4097, "bad\ud800name", 123])
def test_bad_text_refused_without_echoing_value(runtime, value):
    _, nodes, _, _ = runtime
    cls = nodes.NODE_CLASS_MAPPINGS.pop("Image")
    nodes.NODE_CLASS_MAPPINGS[value] = cls
    with pytest.raises(cc.RuntimeCaptureError) as error:
        capture(runtime)
    assert str(error.value) == "runtime binding capture unavailable"


def test_collection_and_deadline_bounds(runtime, monkeypatch):
    modules, _, _, _ = runtime
    too_many = {**modules, **{f"extra{i}": None for i in range(20_001)}}
    with pytest.raises(cc.RuntimeCaptureError):
        capture(runtime, modules=too_many)
    ticks = iter([0.0, 5.0])
    monkeypatch.setattr(cc.time, "monotonic", lambda: next(ticks))
    with pytest.raises(cc.RuntimeCaptureError):
        capture(runtime)


def test_text_budget_and_mutable_container_subclasses_refused(runtime):
    _, nodes, folders, _ = runtime
    cls = nodes.NODE_CLASS_MAPPINGS["Image"]
    nodes.NODE_CLASS_MAPPINGS = {f"{i:04d}" + "x" * 4090: cls for i in range(2500)}
    with pytest.raises(cc.RuntimeCaptureError):
        capture(runtime)
    nodes.NODE_CLASS_MAPPINGS = {"Image": cls}

    class UntrustedList(list):
        def __iter__(self):
            raise AssertionError("custom iterable executed")

    folders.folder_names_and_paths["checkpoints"] = (UntrustedList(), set())
    with pytest.raises(cc.RuntimeCaptureError):
        capture(runtime)


def test_symlink_paths_remain_lexical_for_authority_validation(runtime):
    _, _, folders, root = runtime
    target = root / "actual"
    target.mkdir()
    link = root / "link"
    link.symlink_to(target)
    folders.folder_names_and_paths["checkpoints"] = ([str(link)], {".bin"})
    assert capture(runtime).model_folders["checkpoints"].paths == (link,)
