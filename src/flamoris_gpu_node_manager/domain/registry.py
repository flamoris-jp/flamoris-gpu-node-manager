"""Validated runtime registry."""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from .errors import RuntimeUnavailableError, UnknownRuntimeError
from .models import RuntimeProfile


class RuntimeRegistry:
    def __init__(self, profiles: Iterable[RuntimeProfile]) -> None:
        self._profiles: dict[str, RuntimeProfile] = {}
        for profile in profiles:
            if profile.id in self._profiles:
                raise ValueError(f"duplicate runtime id: {profile.id}")
            self._profiles[profile.id] = profile

    def __iter__(self) -> Iterator[RuntimeProfile]:
        return iter(self._profiles.values())

    def __len__(self) -> int:
        return len(self._profiles)

    def get(self, runtime_id: str, *, require_enabled: bool = False) -> RuntimeProfile:
        try:
            profile = self._profiles[runtime_id]
        except KeyError as exc:
            raise UnknownRuntimeError(f"unknown runtime id: {runtime_id}") from exc
        if require_enabled and not profile.enabled:
            raise RuntimeUnavailableError(f"runtime is configured but unavailable: {runtime_id}")
        return profile

    def conflicts_for(self, target: RuntimeProfile) -> tuple[RuntimeProfile, ...]:
        return tuple(
            profile
            for profile in self._profiles.values()
            if profile.id != target.id and profile.resource_class == target.resource_class
        )
