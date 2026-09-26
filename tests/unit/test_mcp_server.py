from __future__ import annotations

import asyncio
from typing import Any

import anyio
from mcp import Client

from flamoris_gpu_node_manager.application.manager import RuntimeManager
from flamoris_gpu_node_manager.domain.models import RuntimeState, ServiceState
from flamoris_gpu_node_manager.domain.registry import RuntimeRegistry
from flamoris_gpu_node_manager.mcp_server import create_mcp_server, serve_mcp
from tests.helpers import FakeHealth, FakeResources, FakeSystemd, RecordingLock, profile


def manager_fixture(
    *,
    profiles: tuple | None = None,
) -> tuple[RuntimeManager, FakeSystemd, FakeHealth, RecordingLock, list[str]]:
    events: list[str] = []
    systemd = FakeSystemd({}, events)
    health = FakeHealth({}, events)
    lock = RecordingLock()
    manager = RuntimeManager(
        RuntimeRegistry(profiles or (profile("alpha"), profile("beta"))),
        systemd,
        FakeResources({}, events),
        health,
        lock,
    )
    manager.reconstruct()
    return manager, systemd, health, lock, events


def result_value(result: Any) -> Any:
    assert result.is_error is False
    assert result.structured_content is not None
    structured = result.structured_content
    return structured["result"] if set(structured) == {"result"} else structured


def test_transport_selection_uses_one_server_factory(monkeypatch: Any) -> None:
    manager, _, _, _, _ = manager_fixture()
    observed: list[dict[str, Any]] = []

    class FakeServer:
        def run(self, **kwargs: Any) -> None:
            observed.append(kwargs)

    monkeypatch.setattr(
        "flamoris_gpu_node_manager.mcp_server.create_mcp_server",
        lambda *args, **kwargs: FakeServer(),
    )
    serve_mcp(manager)
    serve_mcp(manager, transport="streamable-http", host="localhost", port=9876, mcp_path="/api")
    assert observed == [
        {},
        {
            "transport": "streamable-http",
            "host": "localhost",
            "port": 9876,
            "streamable_http_path": "/api",
        },
    ]


def test_tool_discovery_is_bounded_and_registry_driven() -> None:
    manager, _, _, _, _ = manager_fixture(
        profiles=(profile("future-runtime"), profile("offline", enabled=False))
    )
    server = create_mcp_server(manager)

    async def scenario() -> None:
        async with Client(server, raise_exceptions=True) as client:
            tools = await client.list_tools()
            listed = await client.call_tool("runtime.list")

        assert {tool.name for tool in tools.tools} == {
            "runtime.list",
            "runtime.status",
            "runtime.activate",
            "runtime.stop",
            "system.status",
        }
        assert {item["id"] for item in result_value(listed)} == {
            "future-runtime",
            "offline",
        }

    anyio.run(scenario)


def test_status_activate_and_stop_delegate_to_runtime_manager() -> None:
    manager, systemd, _, _, events = manager_fixture()
    server = create_mcp_server(manager)

    async def scenario() -> None:
        async with Client(server, raise_exceptions=True) as client:
            status = await client.call_tool("runtime.status", {"runtime_id": "alpha"})
            activated = await client.call_tool("runtime.activate", {"runtime_id": "alpha"})
            stopped = await client.call_tool("runtime.stop", {"runtime_id": "alpha"})

        assert result_value(status)["status"]["state"] == RuntimeState.OFF
        assert result_value(activated)["state"] == RuntimeState.READY
        assert result_value(stopped)[0]["state"] == RuntimeState.OFF

    anyio.run(scenario)
    assert events == [
        "start:alpha.service",
        "wait-active:alpha.service",
        "health:alpha",
        "stop:alpha.service",
        "wait-inactive:alpha.service",
        "release:alpha",
    ]
    assert systemd.states["alpha.service"] == ServiceState.INACTIVE


def test_unknown_and_disabled_runtime_are_tool_errors_without_mutation() -> None:
    manager, _, _, _, events = manager_fixture(
        profiles=(profile("alpha"), profile("offline", enabled=False))
    )
    server = create_mcp_server(manager)

    async def scenario() -> None:
        async with Client(server, raise_exceptions=True) as client:
            unknown = await client.call_tool("runtime.activate", {"runtime_id": "missing"})
            disabled = await client.call_tool("runtime.activate", {"runtime_id": "offline"})
            arbitrary_service = await client.call_tool(
                "runtime.activate", {"runtime_id": "alpha.service"}
            )

        assert unknown.is_error is True
        assert "unknown runtime id" in unknown.content[0].text  # type: ignore[union-attr]
        assert disabled.is_error is True
        assert "unavailable" in disabled.content[0].text  # type: ignore[union-attr]
        assert arbitrary_service.is_error is True

    anyio.run(scenario)
    assert events == []


def test_transition_failure_never_reports_ready() -> None:
    manager, systemd, _, _, _ = manager_fixture()
    systemd.start_error = RuntimeError("start exploded")
    server = create_mcp_server(manager)

    async def scenario() -> None:
        async with Client(server, raise_exceptions=True) as client:
            result = await client.call_tool("runtime.activate", {"runtime_id": "alpha"})

        assert result.is_error is True
        assert manager.runtime_status("alpha").state == RuntimeState.FAILED

    anyio.run(scenario)


def test_system_status_remains_queryable_during_mcp_transition() -> None:
    manager, _, health, _, _ = manager_fixture()
    health.delay = 0.25
    server = create_mcp_server(manager)

    async def scenario() -> None:
        async with (
            Client(server, raise_exceptions=True) as mutation_client,
            Client(server, raise_exceptions=True) as query_client,
        ):
            mutation = asyncio.create_task(
                mutation_client.call_tool("runtime.activate", {"runtime_id": "alpha"})
            )
            observed: dict[str, Any] | None = None
            for _ in range(50):
                candidate = result_value(await query_client.call_tool("system.status"))
                transition = candidate["transition"]
                if transition and transition["step"] == "health-check":
                    observed = candidate
                    break
                await asyncio.sleep(0.01)
            activated = await mutation

        assert observed is not None
        runtime = next(item for item in observed["runtimes"] if item["runtime"] == "alpha")
        assert runtime["state"] == RuntimeState.STARTING
        assert result_value(activated)["state"] == RuntimeState.READY

    anyio.run(scenario)


def test_mcp_mutations_share_runtime_manager_serialization() -> None:
    manager, _, health, lock, _ = manager_fixture()
    health.delay = 0.05
    server = create_mcp_server(manager)

    async def scenario() -> None:
        async with (
            Client(server, raise_exceptions=True) as first,
            Client(server, raise_exceptions=True) as second,
        ):
            results = await asyncio.gather(
                first.call_tool("runtime.activate", {"runtime_id": "alpha"}),
                second.call_tool("runtime.activate", {"runtime_id": "beta"}),
            )
        assert all(result.is_error is False for result in results)

    anyio.run(scenario)
    assert lock.maximum_active == 1


def test_new_mcp_process_reconstructs_real_state() -> None:
    first, systemd, _, _, _ = manager_fixture()
    first.activate("alpha")
    events: list[str] = []
    restarted = RuntimeManager(
        RuntimeRegistry((profile("alpha"), profile("beta"))),
        systemd,
        FakeResources({}, events),
        FakeHealth({"alpha": True}, events),
        RecordingLock(),
    )
    restarted.reconstruct()
    server = create_mcp_server(restarted)

    async def scenario() -> None:
        async with Client(server, raise_exceptions=True) as client:
            status = result_value(await client.call_tool("system.status"))
        assert status["current_runtime"] == "alpha"

    anyio.run(scenario)


def test_system_status_telemetry_is_best_effort() -> None:
    class ExplodingTelemetry:
        def read(self) -> dict[str, object]:
            raise OSError("sensor unavailable")

    manager, _, _, _, _ = manager_fixture()
    server = create_mcp_server(manager, ExplodingTelemetry())

    async def scenario() -> None:
        async with Client(server, raise_exceptions=True) as client:
            status = result_value(await client.call_tool("system.status"))
        assert status["online"] is True
        assert status["telemetry"]["available"] is False
        assert status["telemetry"]["error"] == "sensor unavailable"

    anyio.run(scenario)
