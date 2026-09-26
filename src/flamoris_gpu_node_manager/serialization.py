"""Stable serializers shared by the CLI, HTTP, and MCP adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flamoris_gpu_node_manager.domain.models import RuntimeProfile, RuntimeStatus, SystemStatus


def profile_to_dict(profile: RuntimeProfile, status: RuntimeStatus) -> dict[str, Any]:
    return {
        "id": profile.id,
        "display_name": profile.display_name,
        "service": profile.service,
        "resource_class": profile.resource_class,
        "enabled": profile.enabled,
        "status": status.to_dict(),
    }


def system_to_dict(
    status: SystemStatus, telemetry: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = {"online": True, **status.to_dict()}
    if telemetry is not None:
        result["telemetry"] = dict(telemetry)
    return result
