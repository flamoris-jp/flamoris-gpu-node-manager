"""Bounded MCP adapter over the shared runtime authority."""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Protocol

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from flamoris_gpu_node_manager.application.manager import RuntimeManager
from flamoris_gpu_node_manager.domain.errors import NodeManagerError, UnknownRuntimeError
from flamoris_gpu_node_manager.domain.models import RuntimeProfile, SystemStatus
from flamoris_gpu_node_manager.identity import DEFAULT_IDENTITY, NodeIdentity
from flamoris_gpu_node_manager.serialization import profile_to_dict, system_to_dict

_LOG = logging.getLogger(__name__)


class TelemetryPort(Protocol):
    def read(self) -> Mapping[str, Any]: ...


class McpRuntimeAdapter:
    """Coordinates MCP calls without owning runtime transition semantics."""

    def __init__(self, manager: RuntimeManager, telemetry: TelemetryPort | None = None) -> None:
        self.manager = manager
        self.telemetry = telemetry
        self._operation_gate = threading.Lock()
        self._active_mutations = 0

    def snapshot(self) -> SystemStatus:
        """Refresh from real adapters unless this process is mutating the manager."""
        with self._operation_gate:
            if self._active_mutations == 0:
                self.manager.reconstruct()
            return self.manager.system_status()

    @contextmanager
    def mutation(self) -> Iterator[None]:
        with self._operation_gate:
            self._active_mutations += 1
        try:
            yield
        finally:
            with self._operation_gate:
                self._active_mutations -= 1

    def profile(self, runtime_id: str) -> RuntimeProfile:
        for profile in self.manager.list_runtimes():
            if profile.id == runtime_id:
                return profile
        raise UnknownRuntimeError(f"unknown runtime id: {runtime_id}")

    def telemetry_snapshot(self) -> Mapping[str, Any] | None:
        if self.telemetry is None:
            return None
        try:
            return self.telemetry.read()
        except Exception as exc:  # cosmetic telemetry never becomes runtime truth
            _LOG.warning("MCP telemetry read failed: %s", exc)
            return {
                "available": False,
                "error": str(exc) or type(exc).__name__,
            }


def create_mcp_server(
    manager: RuntimeManager,
    telemetry: TelemetryPort | None = None,
    *,
    identity: NodeIdentity = DEFAULT_IDENTITY,
) -> MCPServer:
    """Create the local MCP surface; registration is intentionally allow-listed."""
    adapter = McpRuntimeAdapter(manager, telemetry)
    server = MCPServer(
        identity.manager_display_name,
        instructions=(
            "Inspect and switch only configured GPU runtimes. "
            "This server cannot run shell commands or control arbitrary services."
        ),
    )

    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    mutation = ToolAnnotations(
        destructive_hint=True,
        idempotent_hint=True,
        open_world_hint=False,
    )

    @server.tool(name="runtime.list", annotations=read_only)
    def runtime_list() -> list[dict[str, Any]]:
        """List configured runtimes and their current authoritative status."""
        try:
            adapter.snapshot()
            return [
                profile_to_dict(profile, manager.runtime_status(profile.id))
                for profile in manager.list_runtimes()
            ]
        except NodeManagerError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(name="runtime.status", annotations=read_only)
    def runtime_status(runtime_id: str) -> dict[str, Any]:
        """Inspect one configured runtime by its registry ID."""
        try:
            adapter.snapshot()
            profile = adapter.profile(runtime_id)
            return profile_to_dict(profile, manager.runtime_status(runtime_id))
        except NodeManagerError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(name="runtime.activate", annotations=mutation)
    def runtime_activate(runtime_id: str) -> dict[str, Any]:
        """Activate one enabled configured runtime through RuntimeManager."""
        try:
            with adapter.mutation():
                return manager.activate(runtime_id).to_dict()
        except NodeManagerError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(name="runtime.stop", annotations=mutation)
    def runtime_stop(runtime_id: str) -> list[dict[str, Any]]:
        """Stop one configured runtime through RuntimeManager."""
        try:
            with adapter.mutation():
                return [status.to_dict() for status in manager.stop(runtime_id)]
        except NodeManagerError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(name="system.status", annotations=read_only)
    def system_status() -> dict[str, Any]:
        """Inspect current ownership, transitions, anomalies, and optional telemetry."""
        try:
            return {
                **system_to_dict(adapter.snapshot(), adapter.telemetry_snapshot()),
                **identity.to_dict(),
            }
        except NodeManagerError as exc:
            raise ToolError(str(exc)) from exc

    return server


def serve_mcp(
    manager: RuntimeManager,
    telemetry: TelemetryPort | None = None,
    *,
    identity: NodeIdentity = DEFAULT_IDENTITY,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8766,
    mcp_path: str = "/mcp",
) -> None:
    """Run the same bounded tool set over the selected local transport."""
    server = create_mcp_server(manager, telemetry, identity=identity)
    if transport == "stdio":
        server.run()
    elif transport == "streamable-http":
        server.run(
            transport="streamable-http",
            host=host,
            port=port,
            streamable_http_path=mcp_path,
        )
    else:
        raise ValueError(f"unsupported MCP transport: {transport}")
