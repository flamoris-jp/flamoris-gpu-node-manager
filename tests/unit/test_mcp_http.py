"""Exercise the actual Streamable HTTP transport without systemd or a tunnel."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
import httpx
import uvicorn
from mcp import Client

from flamoris_gpu_node_manager.domain.models import RuntimeState
from flamoris_gpu_node_manager.mcp_server import create_mcp_server
from tests.unit.test_mcp_server import manager_fixture, result_value


@asynccontextmanager
async def local_http_server(path: str) -> AsyncIterator[str]:
    manager, _, _, _, _ = manager_fixture()
    server = create_mcp_server(manager)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        app = server.streamable_http_app(host="127.0.0.1", streamable_http_path=path)
        runner = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_level="warning",
                lifespan="on",
                timeout_graceful_shutdown=2,
            )
        )
        task = asyncio.create_task(runner.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not runner.started:
                    if task.done():
                        await task
                        raise AssertionError("HTTP MCP server exited before startup")
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{port}"
        finally:
            runner.should_exit = True
            await asyncio.wait_for(task, timeout=5)


def test_http_tool_parity_shared_authority_and_host_check(monkeypatch: object) -> None:
    monkeypatch.setenv("NO_PROXY", "*")  # type: ignore[attr-defined]
    monkeypatch.setenv("no_proxy", "*")  # type: ignore[attr-defined]

    async def scenario() -> None:
        async with local_http_server("/api/node") as base:
            async with httpx.AsyncClient(trust_env=False) as http:
                assert (await http.post(base + "/mcp", json={})).status_code == 404
                response = await http.post(
                    base + "/api/node", json={}, headers={"Host": "untrusted.example"}
                )
                assert response.status_code == 421
            async with Client(base + "/api/node", raise_exceptions=True) as first:
                assert {tool.name for tool in (await first.list_tools()).tools} == {
                    "runtime.list",
                    "runtime.status",
                    "runtime.activate",
                    "runtime.stop",
                    "system.status",
                }
                assert {
                    item["id"] for item in result_value(await first.call_tool("runtime.list"))
                } == {"alpha", "beta"}
                assert (
                    result_value(
                        await first.call_tool("runtime.activate", {"runtime_id": "alpha"})
                    )["state"]
                    == RuntimeState.READY
                )
                async with Client(base + "/api/node", raise_exceptions=True) as second:
                    assert (
                        result_value(
                            await second.call_tool("runtime.status", {"runtime_id": "alpha"})
                        )["status"]["state"]
                        == RuntimeState.READY
                    )
                    unknown = await second.call_tool("runtime.activate", {"runtime_id": "unknown"})
                    assert unknown.is_error
                    assert (
                        result_value(
                            await second.call_tool("runtime.stop", {"runtime_id": "alpha"})
                        )[0]["state"]
                        == RuntimeState.OFF
                    )
                assert (
                    result_value(await first.call_tool("system.status"))["current_runtime"] is None
                )

    anyio.run(scenario)
