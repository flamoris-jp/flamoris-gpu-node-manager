"""Bounded local UNIX observation channel with exact kernel peer credentials.

No network listener, request inventory, pickle response or manifest upload API.
"""

from __future__ import annotations

import json
import math
import os
import socket
import stat
import struct
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .comfyui_capture import ComfyUIBindings, ModelFolders, ModuleBinding, NodeBinding
from .comfyui_measurement import (
    ComfyUIClosure,
    ComfyUIMeasurementError,
    InitializedObservation,
    InitializedRuntimePort,
    _Budget,
    _json_copy,
)

_MAX_OBSERVATION = 16 * 1024**2


def _encode(observation: InitializedObservation) -> bytes:
    b, c = observation.bindings, observation.closure
    value = {
        "version": 1,
        "pid": b.pid,
        "uid": b.uid,
        "gid": b.gid,
        "cwd": str(b.cwd),
        "executable": str(b.executable),
        "search_paths": [str(p) for p in b.search_paths],
        "modules": {
            name: {
                "kind": m.kind,
                "file": str(m.file) if m.file else None,
                "namespace_paths": (
                    [str(p) for p in m.namespace_paths] if m.namespace_paths is not None else None
                ),
            }
            for name, m in b.modules.items()
        },
        "nodes": {
            name: {"module": n.module, "qualname": n.qualname, "file": str(n.file)}
            for name, n in b.nodes.items()
        },
        "model_folders": {
            name: {"paths": [str(p) for p in f.paths], "extensions": list(f.extensions)}
            for name, f in b.model_folders.items()
        },
        "unresolved_modules": list(b.unresolved_modules),
        "node_interfaces": dict(observation.node_interfaces),
        "groups": {g: {name: str(p) for name, p in roots.items()} for g, roots in c.groups.items()},
        "native_libraries": [str(p) for p in c.native_libraries],
        "checkpoints": {name: str(p) for name, p in c.checkpoints.items()},
        "complete": c.complete,
        "mutation_coverage_complete": c.mutation_coverage_complete,
        "initialized": observation.initialized,
        "schema_contract": observation.schema_contract,
        "snapshot_token": observation.snapshot_token,
    }
    data = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
    if len(data) > _MAX_OBSERVATION:
        raise ComfyUIMeasurementError()
    return data


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate observation key")
        result[key] = value
    return result


def _decode(data: bytes, *, deadline: float) -> InitializedObservation:
    if not data or len(data) > _MAX_OBSERVATION:
        raise ComfyUIMeasurementError()
    budget = _Budget(deadline)
    value: Any = json.loads(data, object_pairs_hook=_unique)
    _json_copy(value, budget)
    if (
        type(value) is not dict
        or set(value)
        != {
            "version",
            "pid",
            "uid",
            "gid",
            "cwd",
            "executable",
            "search_paths",
            "modules",
            "nodes",
            "model_folders",
            "unresolved_modules",
            "node_interfaces",
            "groups",
            "native_libraries",
            "checkpoints",
            "complete",
            "mutation_coverage_complete",
            "initialized",
            "schema_contract",
            "snapshot_token",
        }
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise ComfyUIMeasurementError()

    if any(type(value[key]) is not int for key in ("pid", "uid", "gid")) or any(
        type(value[key]) is not bool
        for key in ("initialized", "complete", "mutation_coverage_complete")
    ):
        raise ComfyUIMeasurementError()

    def path(raw: Any) -> Path:
        return budget.path(Path(budget.text(raw)))

    def mapping(raw: Any) -> Mapping[str, Any]:
        if type(raw) is not dict:
            raise ValueError("unsupported observation mapping")
        return raw

    def sequence(raw: Any) -> list[Any]:
        if type(raw) is not list:
            raise ValueError("unsupported observation sequence")
        return raw

    def fields(raw: Any, keys: set[str]) -> Mapping[str, Any]:
        result = mapping(raw)
        if set(result) != keys:
            raise ValueError("unsupported observation fields")
        return result

    modules = {}
    for name, raw in mapping(value["modules"]).items():
        m = fields(raw, {"kind", "file", "namespace_paths"})
        modules[name] = ModuleBinding(
            m["kind"],
            path(m["file"]) if m["file"] is not None else None,
            tuple(path(p) for p in sequence(m["namespace_paths"]))
            if m["namespace_paths"] is not None
            else None,
        )
    nodes = {}
    for name, raw in mapping(value["nodes"]).items():
        n = fields(raw, {"module", "qualname", "file"})
        nodes[name] = NodeBinding(
            budget.text(n["module"]), budget.text(n["qualname"]), path(n["file"])
        )
    folders = {}
    for name, raw in mapping(value["model_folders"]).items():
        f = fields(raw, {"paths", "extensions"})
        folders[name] = ModelFolders(
            tuple(path(p) for p in sequence(f["paths"])),
            tuple(budget.text(e) for e in sequence(f["extensions"])),
        )
    bindings = ComfyUIBindings(
        value["pid"],
        value["uid"],
        value["gid"],
        path(value["cwd"]),
        path(value["executable"]),
        tuple(path(p) for p in sequence(value["search_paths"])),
        MappingProxyType(modules),
        MappingProxyType(nodes),
        MappingProxyType(folders),
        tuple(budget.text(n) for n in sequence(value["unresolved_modules"])),
    )
    closure = ComfyUIClosure(
        {
            g: {n: path(p) for n, p in mapping(roots).items()}
            for g, roots in mapping(value["groups"]).items()
        },
        tuple(path(p) for p in sequence(value["native_libraries"])),
        {n: path(p) for n, p in mapping(value["checkpoints"]).items()},
        value["complete"],
        value["mutation_coverage_complete"],
    )
    return InitializedObservation(
        bindings,
        mapping(value["node_interfaces"]),
        closure,
        value["initialized"],
        budget.text(value["schema_contract"]),
        budget.text(value["snapshot_token"]),
    )


def _peer(connection: socket.socket) -> tuple[int, int, int]:
    return struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))


def _receive(connection: socket.socket, count: int, deadline: float) -> bytes:
    chunks = bytearray()
    while len(chunks) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        connection.settimeout(remaining)
        block = connection.recv(min(count - len(chunks), 65536))
        if not block:
            raise ValueError("truncated runtime observation")
        chunks.extend(block)
    return bytes(chunks)


class UnixInitializedRuntimeClient:
    """Spawn-safe port bound to one actual PID/UID; no schema or closure requests."""

    def __init__(self, path: Path, *, pid: int, uid: int) -> None:
        if not path.is_absolute() or ".." in path.parts or type(pid) is not int or pid <= 0:
            raise ValueError("invalid runtime observation endpoint")
        if type(uid) is not int or not 0 <= uid < 2**32 - 1:
            raise ValueError("invalid runtime observation peer")
        self.path, self.pid, self.uid = path, pid, uid

    def observe(self, *, deadline: float) -> InitializedObservation:
        try:
            remaining = deadline - time.monotonic()
            if not math.isfinite(remaining) or remaining <= 0:
                raise TimeoutError()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(remaining)
                connection.connect(str(self.path))
                if _peer(connection)[:2] != (self.pid, self.uid):
                    raise ValueError("runtime peer differs")
                size = struct.unpack("!I", _receive(connection, 4, deadline))[0]
                if not 0 < size <= _MAX_OBSERVATION:
                    raise ValueError("runtime observation exceeds bound")
                observed = _decode(_receive(connection, size, deadline), deadline=deadline)
                if (observed.bindings.pid, observed.bindings.uid) != (self.pid, self.uid):
                    raise ValueError("runtime observation identity differs")
                return observed
        except Exception as error:
            raise ComfyUIMeasurementError() from error


class UnixRuntimeObservationServer:
    """Explicit provider-side read-only endpoint, restricted to one authority UID.

    Start only from a reviewed initialized provider hook. No endpoint replacement
    or parent provisioning is performed. A stalled schema method disables further
    observations; an external authority watchdog bounds publication attempts.
    """

    def __init__(self, path: Path, port: InitializedRuntimePort, *, authority_uid: int) -> None:
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("invalid runtime observation endpoint")
        if type(authority_uid) is not int or not 0 <= authority_uid < 2**32 - 1:
            raise ValueError("invalid observation authority")
        self.path, self.port, self.authority_uid = path, port, authority_uid
        self._socket: socket.socket | None = None
        self._identity: tuple[int, int] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._socket is not None or self._thread is not None:
            raise ComfyUIMeasurementError()
        # Walk every ancestor: a writable/linked parent permits endpoint exchange.
        for parent in (self.path.parent, *self.path.parent.parents):
            info = parent.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid not in (0, os.geteuid())
                or (info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX))
            ):
                raise ComfyUIMeasurementError()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.path))  # Existing file/socket is never unlinked.
            info = self.path.lstat()
            self._identity = (info.st_dev, info.st_ino)
            os.chmod(self.path, 0o600)
            listener.listen(1)
            listener.settimeout(0.1)
            self._socket = listener
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._serve, args=(listener,), daemon=True, name="runtime-observation"
            )
            self._thread.start()
        except BaseException:
            listener.close()
            self.close()
            raise

    def _serve(self, listener: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection:
                try:
                    if _peer(connection)[1] != self.authority_uid:
                        continue
                    deadline = time.monotonic() + 5.0
                    data = _encode(self.port.observe(deadline=deadline))
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or self._stop.is_set():
                        continue
                    connection.settimeout(remaining)
                    connection.sendall(struct.pack("!I", len(data)) + data)
                except Exception:
                    # No error/traceback metadata crosses the runtime channel.
                    continue

    def close(self) -> None:
        self._stop.set()
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        if (
            self._thread is not None
            and self._thread.is_alive()
            and self._thread is not threading.current_thread()
        ):
            self._thread.join(timeout=0.2)
        if self._identity is not None:
            try:
                info = self.path.lstat()
                if (info.st_dev, info.st_ino) == self._identity and stat.S_ISSOCK(info.st_mode):
                    self.path.unlink()
            except FileNotFoundError:
                pass
            self._identity = None

    def __enter__(self) -> UnixRuntimeObservationServer:
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
