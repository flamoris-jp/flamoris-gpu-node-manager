"""Explicit in-process observation after the actual ComfyUI initialization hook."""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from types import MappingProxyType, ModuleType
from typing import Any

from .comfyui_capture import _Budget, _collect, _namespace
from .comfyui_measurement import (
    SCHEMA_CONTRACT,
    ComfyUIClosure,
    ComfyUIMeasurementError,
    InitializedObservation,
)
from .comfyui_schema import read_legacy_node_schema


class LocalInitializedRuntimePort:
    """Runtime-owned observations, never an inferred initialization certificate.

    Construct and mark_initialized from a reviewed hook in the provider. Closure
    and schema callbacks are trusted code, not client request parameters. The
    source still independently checks coverage, protection and byte contents.
    """

    def __init__(
        self,
        closure: Callable[[], ComfyUIClosure],
        *,
        schema_reader: Callable[..., object] = read_legacy_node_schema,
    ) -> None:
        self._closure = closure
        self._schema_reader = schema_reader
        self._initialized = False
        self._lock = threading.Lock()
        self._references: dict[object, object] = {}
        self._token = secrets.token_hex(32)

    def mark_initialized(self) -> None:
        """Only the reviewed completion hook may call this after awaited init."""
        with self._lock:
            if self._initialized:
                raise ComfyUIMeasurementError()
            self._initialized = True

    def invalidate(self) -> None:
        """Call before every dynamic reinitialization/mutation; no auto re-enable."""
        with self._lock:
            self._initialized = False
            self._references = {}
            self._token = secrets.token_hex(32)

    def observe(self, *, deadline: float) -> InitializedObservation:
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._lock.acquire(timeout=remaining):
                raise ComfyUIMeasurementError()
            try:
                if not self._initialized:
                    raise ComfyUIMeasurementError()
                budget = _Budget()
                budget.deadline = min(budget.deadline, deadline)
                first, raw_references = _collect(budget)
                references: dict[object, object] = {
                    key: value for key, value in raw_references.items()
                }
                self._retain_module_attributes(references, budget)
                self._retain_class_attributes(references, first.nodes, budget)
                interfaces: dict[str, object] = {}
                for name, binding in first.nodes.items():
                    cls: Any = references["node:" + name]
                    interfaces[name] = self._schema_reader(cls, binding, deadline=deadline)
                    budget.check()
                closure = self._closure()
                second, raw_recheck = _collect(budget)
                recheck: dict[object, object] = {key: value for key, value in raw_recheck.items()}
                self._retain_module_attributes(recheck, budget)
                self._retain_class_attributes(recheck, second.nodes, budget)
                if (
                    first != second
                    or references.keys() != recheck.keys()
                    or any(value is not references.get(key) for key, value in recheck.items())
                ):
                    raise ComfyUIMeasurementError()
                if self._references.keys() != references.keys() or any(
                    value is not self._references.get(key) for key, value in references.items()
                ):
                    self._token = secrets.token_hex(32)
                self._references = references
                budget.check()
                return InitializedObservation(
                    first, MappingProxyType(interfaces), closure, True, SCHEMA_CONTRACT, self._token
                )
            finally:
                self._lock.release()
        except Exception as error:
            raise ComfyUIMeasurementError() from error

    @staticmethod
    def _retain_module_attributes(references: dict[object, object], budget: _Budget) -> None:
        for label, module in tuple(references.items()):
            if (
                type(label) is str
                and label.startswith("module:")
                and issubclass(type(module), ModuleType)
            ):
                for key, value in _namespace(module).copy().items():
                    budget.check(1)
                    budget.text(key)
                    references[("module-attr", label, key)] = value

    @staticmethod
    def _retain_class_attributes(
        references: dict[object, object], nodes: MappingProxyType[str, Any], budget: _Budget
    ) -> None:
        # Retain inherited methods/descriptors too; don't call metaclass getters.
        for name in nodes:
            cls = references["node:" + name]
            for index, base in enumerate(type.__getattribute__(cls, "__mro__")):
                budget.check(1)
                references[("base", name, index)] = base
                for key, value in type.__getattribute__(base, "__dict__").items():
                    budget.check(1)
                    budget.text(key)
                    references[("attr", name, index, key)] = value
