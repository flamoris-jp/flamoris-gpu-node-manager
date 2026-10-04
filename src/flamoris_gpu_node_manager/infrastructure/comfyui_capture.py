"""Observe installed bindings inside ComfyUI; never import or grant readiness."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Literal, cast

from ..domain.errors import NodeManagerError


class RuntimeCaptureError(NodeManagerError):
    def __init__(self) -> None:
        super().__init__("runtime binding capture unavailable")


@dataclass(frozen=True)
class ModuleBinding:
    kind: Literal["file", "builtin", "frozen", "namespace", "unknown", "blocked"]
    file: Path | None
    namespace_paths: tuple[Path, ...] | None


@dataclass(frozen=True)
class NodeBinding:
    module: str
    qualname: str
    file: Path


@dataclass(frozen=True)
class ModelFolders:
    paths: tuple[Path, ...]
    extensions: tuple[str, ...]


@dataclass(frozen=True)
class ComfyUIBindings:
    pid: int
    uid: int
    gid: int
    cwd: Path
    executable: Path
    search_paths: tuple[Path, ...]
    modules: MappingProxyType[str, ModuleBinding]
    nodes: MappingProxyType[str, NodeBinding]
    model_folders: MappingProxyType[str, ModelFolders]
    unresolved_modules: tuple[str, ...]


class _Budget:
    def __init__(self) -> None:
        self.deadline = time.monotonic() + 5.0
        self.entries = 0
        self.text_bytes = 0

    def check(self, count: int = 0) -> None:
        self.entries += count
        if self.entries > 100_000 or time.monotonic() >= self.deadline:
            raise RuntimeCaptureError()

    def text(self, value: object, *, empty: bool = False) -> str:
        self.check()
        if type(value) is not str or (not value and not empty) or len(value) > 4096:
            raise RuntimeCaptureError()
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise RuntimeCaptureError()
        self.text_bytes += len(value.encode("utf-8"))
        if self.text_bytes > 8 * 1024 * 1024:
            raise RuntimeCaptureError()
        return value

    def path(self, value: object, cwd: Path) -> Path:
        text = self.text(value, empty=True)
        # Anchor relative paths without collapsing '..': after a symlink it can
        # select a different parent than lexical normalization would record.
        # Protection/links/file contents are checked by the authority later.
        return cwd / text


def _dictionary(value: object, budget: _Budget) -> dict[object, object]:
    if type(value) is not dict:
        raise RuntimeCaptureError()
    budget.check(len(value))
    if len(value) > 20_000:
        raise RuntimeCaptureError()
    copy: dict[object, object] = value.copy()
    if len(copy) > 20_000:
        raise RuntimeCaptureError()
    return copy


def _sequence(value: object, budget: _Budget) -> tuple[object, ...]:
    if type(value) not in (list, tuple, set, frozenset):
        raise RuntimeCaptureError()
    items = cast(list[object] | tuple[object, ...] | set[object] | frozenset[object], value)
    budget.check(len(items))
    if len(items) > 20_000:
        raise RuntimeCaptureError()
    return tuple(items)


def _namespace(module: object) -> dict[str, object]:
    if not issubclass(type(module), ModuleType):
        raise RuntimeCaptureError()
    # Bypass module __getattr__/subclass __getattribute__. No plugin getters,
    # node INPUT_TYPES/GET_SCHEMA, import loaders or namespace iterators run.
    namespace: dict[str, object] = vars(ModuleType)["__dict__"].__get__(module, ModuleType)
    return namespace


def _module_binding(module: object, cwd: Path, budget: _Budget) -> ModuleBinding:
    if module is None:
        return ModuleBinding("blocked", None, ())
    if not issubclass(type(module), ModuleType):
        # Python registers compatibility aliases as classes (typing.io/re).
        # Preserve them as unknown without touching attributes or getters.
        return ModuleBinding("unknown", None, None)
    namespace = _namespace(module)
    raw_file = namespace.get("__file__")
    file = budget.path(raw_file, cwd) if raw_file is not None else None
    raw_paths = namespace.get("__path__", ())
    paths = (
        tuple(budget.path(value, cwd) for value in _sequence(raw_paths, budget))
        if type(raw_paths) in (list, tuple)
        else None
    )
    spec = namespace.get("__spec__")
    if spec is not None and type(spec) is not ModuleSpec:
        return ModuleBinding("unknown", file, paths)
    spec_dict = {} if spec is None else ModuleSpec.__dict__["__dict__"].__get__(spec, ModuleSpec)
    origin = spec_dict.get("origin")
    if type(origin) is str and origin in ("built-in", "frozen"):
        return ModuleBinding("builtin" if origin == "built-in" else "frozen", file, paths)
    if origin is not None:
        origin_path = budget.path(origin, cwd)
        if file is None or origin_path != file:
            return ModuleBinding("unknown", file, paths)
    if file is not None:
        return ModuleBinding("file", file, paths)
    return ModuleBinding("namespace" if "__path__" in namespace else "unknown", None, paths)


def _collect(budget: _Budget) -> tuple[ComfyUIBindings, dict[str, object]]:
    system = _namespace(sys)
    module_registry = system.get("modules")
    raw_modules = _dictionary(module_registry, budget)
    cwd = Path(budget.text(os.getcwd()))
    executable = budget.path(budget.text(system.get("executable")), cwd)
    search_paths = tuple(budget.path(value, cwd) for value in _sequence(system.get("path"), budget))
    tokens: dict[str, object] = {"module_registry": module_registry}
    modules: dict[str, ModuleBinding] = {}
    for raw_name, module in raw_modules.items():
        name = budget.text(raw_name)
        modules[name] = _module_binding(module, cwd, budget)
        tokens["module:" + name] = module
    node_module = raw_modules.get("nodes")
    folders_module = raw_modules.get("folder_paths")
    registry = _namespace(node_module).get("NODE_CLASS_MAPPINGS")
    folder_registry = _namespace(folders_module).get("folder_names_and_paths")
    raw_nodes = _dictionary(registry, budget)
    raw_folders = _dictionary(folder_registry, budget)
    if not raw_nodes or not raw_folders:
        raise RuntimeCaptureError()
    tokens["node_registry"] = registry
    tokens["folder_registry"] = folder_registry
    nodes: dict[str, NodeBinding] = {}
    for raw_name, cls in raw_nodes.items():
        name = budget.text(raw_name)
        if not issubclass(type(cls), type):
            raise RuntimeCaptureError()
        class_dict = type.__dict__["__dict__"].__get__(cls, type)
        module_name = budget.text(class_dict.get("__module__"))
        qualname = budget.text(type.__dict__["__qualname__"].__get__(cls, type))
        binding = modules.get(module_name)
        if binding is None or binding.kind != "file" or binding.file is None:
            raise RuntimeCaptureError()
        nodes[name] = NodeBinding(module_name, qualname, binding.file)
        tokens["node:" + name] = cls
    folders: dict[str, ModelFolders] = {}
    for raw_name, entry in raw_folders.items():
        name = budget.text(raw_name)
        if type(entry) is not tuple or len(entry) != 2:
            raise RuntimeCaptureError()
        paths = tuple(budget.path(value, cwd) for value in _sequence(entry[0], budget))
        extensions = tuple(sorted(budget.text(value) for value in _sequence(entry[1], budget)))
        folders[name] = ModelFolders(paths, extensions)
    unresolved = tuple(
        sorted(
            name
            for name, binding in modules.items()
            if binding.kind == "unknown" or binding.namespace_paths is None
        )
    )
    return (
        ComfyUIBindings(
            os.getpid(),
            os.geteuid(),
            os.getegid(),
            cwd,
            executable,
            search_paths,
            MappingProxyType(modules),
            MappingProxyType(nodes),
            MappingProxyType(folders),
            unresolved,
        ),
        tokens,
    )


def capture_comfyui_bindings() -> ComfyUIBindings:
    """Capture existing registrations in this process, after node initialization.

    No caller-supplied inventory, import, provider request, interface invocation,
    hashing or evidence publication occurs. A future trusted runtime adapter owns
    invocation timing and closure/protection/lifetime validation.
    """
    try:
        budget = _Budget()
        first, tokens = _collect(budget)
        second, recheck = _collect(budget)
        if first != second or tokens.keys() != recheck.keys():
            raise RuntimeCaptureError()
        if any(value is not recheck[key] for key, value in tokens.items()):
            raise RuntimeCaptureError()
        budget.check()
        return first
    except Exception as error:
        if issubclass(type(error), RuntimeCaptureError):
            raise
        raise RuntimeCaptureError() from error
