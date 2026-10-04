"""Trusted closure plan plus actual runtime checkpoint/native-library lookup."""

from __future__ import annotations

import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .comfyui_capture import _namespace
from .comfyui_measurement import ComfyUIClosure, _Budget


def mapped_native_libraries(*, deadline: float) -> tuple[Path, ...]:
    """Observe executable file mappings; unsupported anonymous/deleted code fails.

    Kernel vdso/vsyscall mappings belong to the boot lifetime. This is not proof
    of all data/configuration/future-import roots; the reviewed plan supplies those.
    """
    budget = _Budget(deadline)
    with open("/proc/self/maps", "rb") as stream:
        raw = stream.read(8 * 1024**2 + 1)
    if len(raw) > 8 * 1024**2:
        raise ValueError("native mapping inventory exceeds bound")
    result = set()
    for line in raw.decode("utf-8", errors="strict").splitlines():
        budget.check()
        parts = line.split(maxsplit=5)
        if len(parts) < 5:
            raise ValueError("unsupported native mapping")
        if "x" not in parts[1]:
            continue
        if len(parts) == 6 and parts[5] in {"[vdso]", "[vsyscall]"}:
            continue
        if (
            len(parts) != 6
            or not parts[5].startswith("/")
            or "\\" in parts[5]
            or parts[5].endswith(" (deleted)")
        ):
            raise ValueError("unmeasurable executable mapping")
        result.add(budget.path(Path(parts[5])))
    if not result:
        raise ValueError("native executable mappings unavailable")
    return tuple(sorted(result))


@dataclass(frozen=True)
class ReviewedRuntimeClosure:
    """Local policy, not a client-authored list or discovery completeness claim.

    Defaults deliberately leave qualification unavailable. Real deployments must
    review the entire effective closure and writer/lifecycle policy before certifying.
    """

    groups: Mapping[str, Mapping[str, Path]]
    checkpoint_names: tuple[str, ...]
    complete: bool = False
    mutation_coverage_complete: bool = False

    def __post_init__(self) -> None:
        if (
            type(self.checkpoint_names) is not tuple
            or not self.checkpoint_names
            or len(self.checkpoint_names) > 20_000
            or type(self.complete) is not bool
            or type(self.mutation_coverage_complete) is not bool
        ):
            raise ValueError("unsupported reviewed closure plan")
        copied = {}
        budget = _Budget(time.monotonic() + 5.0)
        for group, roots in self.groups.items():
            copied[budget.text(group)] = MappingProxyType(
                {budget.text(name): budget.path(path) for name, path in roots.items()}
            )
        if set(copied) != {"core", "dependencies", "config"} or not all(copied.values()):
            raise ValueError("incomplete reviewed closure groups")
        for name in self.checkpoint_names:
            budget.text(name)
            if (
                Path(name).is_absolute()
                or "\\" in name
                or any(part in {"", ".", ".."} for part in name.split("/"))
            ):
                raise ValueError("unsupported exact checkpoint name")
        object.__setattr__(self, "groups", MappingProxyType(copied))

    def __call__(self) -> ComfyUIClosure:
        # Invoked only inside the initialized runtime; no provider imports.
        namespace = _namespace(sys.modules.get("folder_paths"))
        lookup: Any = namespace.get("get_full_path")
        if not callable(lookup):
            raise ValueError("effective checkpoint lookup unavailable")
        budget = _Budget(time.monotonic() + 5.0)
        selected = {}
        for name in self.checkpoint_names:
            raw = lookup("checkpoints", name)
            selected[name] = budget.path(Path(budget.text(raw)))
        return ComfyUIClosure(
            self.groups,
            mapped_native_libraries(deadline=budget.deadline),
            MappingProxyType(selected),
            self.complete,
            self.mutation_coverage_complete,
        )
