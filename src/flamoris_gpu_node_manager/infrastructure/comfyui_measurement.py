"""Bind initialized ComfyUI observations to protected content measurements.

This adapter is intentionally not a production capture hook. Only a reviewed
local runtime/overlay port can establish initialization, effective closure,
retained binding identity and coverage of every privileged mutation path.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, cast

from ..domain.errors import NodeManagerError
from .comfyui_capture import ComfyUIBindings, ModelFolders, ModuleBinding, NodeBinding
from .content_measurement import MeasurementLimits, ProtectedContentMeasurer
from .evidence_publication import MeasuredManifest, MeasuredNode

SCHEMA_CONTRACT = "comfyui-structural-schema-v1"


class ComfyUIMeasurementError(NodeManagerError):
    def __init__(self) -> None:
        super().__init__("initialized runtime measurement unavailable")


@dataclass(frozen=True)
class ComfyUIClosure:
    """Reviewed effective closure, never an inventory authored by a client.

    ``checkpoints`` maps exact loader names to the effective path observed from
    the initialized runtime's checkpoint lookup, rather than a supplied digest.
    Completeness flags are certifications made by the trusted port, not facts
    that can be inferred from a nonempty list of roots.
    """

    groups: Mapping[str, Mapping[str, Path]]
    native_libraries: tuple[Path, ...]
    checkpoints: Mapping[str, Path]
    complete: bool
    mutation_coverage_complete: bool


@dataclass(frozen=True)
class InitializedObservation:
    bindings: ComfyUIBindings
    node_interfaces: Mapping[str, object]
    closure: ComfyUIClosure
    initialized: bool
    schema_contract: str
    snapshot_token: str


class InitializedRuntimePort(Protocol):
    """Local trusted instrumentation, invoked after actual initialization.

    Capture actual bindings inside the selected process. Retain object references
    across observations and change ``snapshot_token`` on registry/class/module
    replacement, including replacements whose observable values are identical.
    Unknown initialization/closure/mutation coverage must return false or raise.
    Schema normalization is the reviewed runtime port's responsibility; remote
    object_info responses and handwritten schemas are not admissible sources.
    """

    def observe(self, *, deadline: float) -> InitializedObservation: ...


class _Budget:
    def __init__(self, deadline: float) -> None:
        self.deadline = deadline
        self.entries = 0
        self.text_bytes = 0

    def check(self, entries: int = 1) -> None:
        self.entries += entries
        if self.entries > 100_000 or time.monotonic() >= self.deadline:
            raise ValueError("runtime observation exceeds bounds")

    def text(self, value: object) -> str:
        self.check()
        if (
            type(value) is not str
            or not value
            or len(value) > 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError("invalid runtime observation text")
        self.text_bytes += len(value.encode("utf-8"))
        if self.text_bytes > 8 * 1024**2:
            raise ValueError("runtime observation text exceeds bound")
        return value

    def path(self, value: Path) -> Path:
        self.text(str(value))
        # Never resolve a path or normalize away '..' from captured origins.
        if (
            not isinstance(value, Path)
            or not value.is_absolute()
            or ".." in value.parts
            or str(value) != os.path.normpath(value)
        ):
            raise ValueError("unsupported runtime path spelling")
        return value


def _mapping(value: Mapping[str, object]) -> dict[str, object]:
    # Only ordinary captured mappings are supported; do not invoke custom
    # iterator/getter implementations from an unsupported observation.
    if type(value) not in (dict, MappingProxyType):
        raise ValueError("unsupported observation mapping")
    return dict(value)


def _json_copy(value: object, budget: _Budget, *, depth: int = 0) -> object:
    budget.check()
    if depth > 64:
        raise ValueError("runtime schema nesting exceeds bound")
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("runtime schema contains nonfinite value")
        return value
    if type(value) is str:
        if value:
            return budget.text(value)
        return ""
    if type(value) in (tuple, list):
        sequence = cast(tuple[object, ...] | list[object], value)
        return [_json_copy(item, budget, depth=depth + 1) for item in sequence]
    if type(value) in (dict, MappingProxyType):
        mapping = cast(Mapping[str, object], value)
        return {
            budget.text(key): _json_copy(item, budget, depth=depth + 1)
            for key, item in mapping.items()
        }
    raise ValueError("runtime schema contains unsupported value")


def _fingerprint(value: object) -> str:
    # This is Generation's canonical JSON SHA-256, including ensure_ascii=True.
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class _Snapshot:
    observation: InitializedObservation
    groups: Mapping[str, Mapping[str, Path]]
    interfaces: Mapping[str, object]
    checkpoints: Mapping[str, Path]
    binding_document: object


def _snapshot(observation: InitializedObservation, budget: _Budget) -> _Snapshot:
    if (
        type(observation) is not InitializedObservation
        or type(observation.bindings) is not ComfyUIBindings
        or type(observation.closure) is not ComfyUIClosure
        or observation.initialized is not True
        or observation.closure.complete is not True
        or observation.closure.mutation_coverage_complete is not True
        or observation.schema_contract != SCHEMA_CONTRACT
    ):
        raise ValueError("initialized complete runtime closure is unproven")
    budget.text(observation.snapshot_token)
    bindings = observation.bindings
    for mapping in (
        bindings.modules,
        bindings.nodes,
        bindings.model_folders,
        observation.closure.groups,
        observation.closure.checkpoints,
    ):
        if type(mapping) not in (dict, MappingProxyType):
            raise ValueError("unsupported runtime closure mapping")
    for sequence in (bindings.search_paths, observation.closure.native_libraries):
        if type(sequence) is not tuple:
            raise ValueError("unsupported runtime closure sequence")
    for labels in observation.closure.groups.values():
        if type(labels) not in (dict, MappingProxyType):
            raise ValueError("unsupported runtime content mapping")
    if (
        type(bindings.pid) is not int
        or bindings.pid <= 0
        or type(bindings.uid) is not int
        or type(bindings.gid) is not int
        or not 0 <= bindings.gid < 2**32 - 1
        or bindings.unresolved_modules
    ):
        raise ValueError("runtime binding identity or module closure is unavailable")
    budget.path(bindings.cwd)
    groups = {
        budget.text(group): MappingProxyType(
            {budget.text(label): budget.path(path) for label, path in roots.items()}
        )
        for group, roots in observation.closure.groups.items()
    }
    if set(groups) != {"core", "dependencies", "config"} or not all(groups.values()):
        raise ValueError("effective closure groups are incomplete")
    roots = tuple(path for labels in groups.values() for path in labels.values())

    def covered(path: Path, *, directory: bool = False) -> str:
        budget.path(path)
        if not any(path == root or path.is_relative_to(root) for root in roots):
            raise ValueError("observed origin is outside measured effective closure")
        mode = path.lstat().st_mode
        if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
            raise ValueError("observed origin is not a supported installed file or directory")
        return str(path)

    executable = covered(bindings.executable)
    search_paths = [covered(path, directory=True) for path in bindings.search_paths]
    if not search_paths:
        raise ValueError("runtime import search closure is empty")
    modules: dict[str, object] = {}
    for name, binding in bindings.modules.items():
        budget.text(name)
        if type(binding) is not ModuleBinding:
            raise ValueError("unsupported runtime module binding")
        if binding.kind not in {"file", "builtin", "frozen", "namespace", "blocked"}:
            raise ValueError("runtime module origin is unresolved")
        if binding.namespace_paths is None:
            raise ValueError("runtime namespace origin is unresolved")
        file = covered(binding.file) if binding.file is not None else None
        namespace = [covered(path, directory=True) for path in binding.namespace_paths]
        if (binding.kind == "file" and file is None) or (
            binding.kind == "namespace" and not namespace
        ):
            raise ValueError("runtime module origin is incomplete")
        modules[name] = {"kind": binding.kind, "file": file, "namespace_paths": namespace}
    if not modules or not bindings.nodes:
        raise ValueError("initialized runtime registry is empty")
    nodes: dict[str, object] = {}
    for name, node_binding in bindings.nodes.items():
        budget.text(name)
        if type(node_binding) is not NodeBinding:
            raise ValueError("unsupported runtime node binding")
        budget.text(node_binding.module)
        budget.text(node_binding.qualname)
        module = bindings.modules.get(node_binding.module)
        if module is None or module.kind != "file" or module.file != node_binding.file:
            raise ValueError("runtime node origin disagrees with module registry")
        nodes[name] = {
            "module": node_binding.module,
            "qualname": node_binding.qualname,
            "file": covered(node_binding.file),
        }
    interfaces = {
        budget.text(name): _json_copy(schema, budget)
        for name, schema in _mapping(observation.node_interfaces).items()
    }
    if set(interfaces) != set(nodes) or any(
        not isinstance(schema, dict) or not schema for schema in interfaces.values()
    ):
        raise ValueError("initialized runtime structural interfaces are incomplete")
    native = [covered(path) for path in observation.closure.native_libraries]
    checkpoints = {
        budget.text(name): budget.path(path)
        for name, path in observation.closure.checkpoints.items()
    }
    if not checkpoints:
        raise ValueError("initialized runtime has no selected checkpoint lookup")
    folders: dict[str, object] = {}
    for category, folder_binding in bindings.model_folders.items():
        budget.text(category)
        if type(folder_binding) is not ModelFolders:
            raise ValueError("unsupported runtime model-folder binding")
        if not folder_binding.paths:
            raise ValueError("runtime model-folder closure is unavailable")
        # Other effective categories can influence execution too, e.g. embedding
        # files read by text nodes. All captured lookup roots must be measured,
        # not only the category supplying the selected checkpoint.
        paths = [covered(path, directory=True) for path in folder_binding.paths]
        extensions = [budget.text(extension) for extension in folder_binding.extensions]
        folders[category] = {"paths": paths, "extensions": extensions}
    lookup = bindings.model_folders.get("checkpoints")
    if lookup is None or not lookup.paths or not lookup.extensions:
        raise ValueError("initialized checkpoint lookup is unavailable")
    # Hash every consulted lookup directory too, including earlier shadowing
    # roots; a protected selected model alone cannot prove lookup continuity.
    for path in lookup.paths:
        covered(path, directory=True)
    for name, selected in checkpoints.items():
        relative = Path(name)
        if (
            relative.is_absolute()
            or str(relative) != name
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or "\\" in name
            or relative.suffix.lower() not in lookup.extensions
        ):
            raise ValueError("unsupported exact checkpoint loader name")
        effective = None
        for root in lookup.paths:
            candidate = root / relative
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode):
                effective = candidate
                break
            raise ValueError("checkpoint lookup contains unsupported target")
        if effective is None or effective != selected:
            raise ValueError("runtime checkpoint selection disagrees with effective lookup")
    document = {
        "executable": executable,
        "search_paths": search_paths,
        "modules": modules,
        "nodes": nodes,
        "model_folders": folders,
        "native_libraries": native,
        "groups": {
            group: {label: str(path) for label, path in labels.items()}
            for group, labels in groups.items()
        },
        "checkpoints": {name: str(path) for name, path in checkpoints.items()},
    }
    return _Snapshot(
        observation,
        MappingProxyType(groups),
        MappingProxyType(interfaces),
        MappingProxyType(checkpoints),
        document,
    )


class ComfyUIManifestSource:
    """Implement ManifestSource with trusted observations and actual byte hashes.

    The publisher owns the exclusive evidence lock and provider lifetime fence.
    A hard subprocess watchdog remains an overlay requirement for blocking OS or
    runtime-port calls; this adapter's absolute deadline is cooperative.
    """

    def __init__(
        self, port: InitializedRuntimePort, *, limits: MeasurementLimits | None = None
    ) -> None:
        self._port = port
        self._limits = limits or MeasurementLimits()

    def measure(self, *, deadline: float) -> MeasuredManifest:
        try:
            if type(deadline) not in (int, float) or not math.isfinite(deadline):
                raise ValueError("measurement deadline must be finite")
            budget = _Budget(deadline)
            budget.check()
            first = _snapshot(self._port.observe(deadline=deadline), budget)
            remaining = deadline - time.monotonic()
            limits = replace(self._limits, timeout=min(self._limits.timeout, remaining))
            measured = ProtectedContentMeasurer(
                runtime_uid=first.observation.bindings.uid, limits=limits
            ).measure(
                first.groups,
                {"checkpoints/" + name: path for name, path in first.checkpoints.items()},
            )
            second = _snapshot(self._port.observe(deadline=deadline), budget)
            if (
                first.observation.bindings != second.observation.bindings
                or first.observation.snapshot_token != second.observation.snapshot_token
                or first.binding_document != second.binding_document
                or first.interfaces != second.interfaces
            ):
                raise ValueError("initialized runtime changed during measurement")
            binding_identity = _fingerprint(first.binding_document)
            nodes: dict[str, MeasuredNode] = {}
            for name, schema in first.interfaces.items():
                budget.check()
                node_binding = first.observation.bindings.nodes[name]
                nodes[name] = MeasuredNode(
                    interface=_fingerprint(schema),
                    implementation=_fingerprint(
                        {
                            "contract": "comfyui-measured-node-v1",
                            "content": dict(measured.groups),
                            "runtime_bindings": binding_identity,
                            "node": {
                                "name": name,
                                "module": node_binding.module,
                                "qualname": node_binding.qualname,
                                "file": str(node_binding.file),
                            },
                        }
                    ),
                )
            budget.check()
            return MeasuredManifest(
                core_identity=measured.groups["core"],
                dependency_identity=measured.groups["dependencies"],
                config_identity=measured.groups["config"],
                nodes=MappingProxyType(nodes),
                models=MappingProxyType(
                    {
                        "checkpoint:" + key.removeprefix("checkpoints/"): digest
                        for key, digest in measured.models.items()
                    }
                ),
            )
        except Exception as error:
            if isinstance(error, ComfyUIMeasurementError):
                raise
            raise ComfyUIMeasurementError() from error
