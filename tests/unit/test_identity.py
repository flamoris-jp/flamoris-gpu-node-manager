from __future__ import annotations

import json
import threading
import urllib.request
from html import escape
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client

from flamoris_gpu_node_manager.cli import run
from flamoris_gpu_node_manager.domain.errors import ProfileValidationError
from flamoris_gpu_node_manager.http_server import create_server
from flamoris_gpu_node_manager.identity import NodeIdentity, load_identity
from flamoris_gpu_node_manager.mcp_server import create_mcp_server
from tests.unit.test_mcp_server import manager_fixture, result_value


def test_identity_defaults_and_partial_config(tmp_path: Path) -> None:
    assert load_identity() == NodeIdentity()
    path = tmp_path / "identity.yaml"
    path.write_text("manager:\n  display_name: Render Manager\n")
    assert load_identity(path) == NodeIdentity(manager_display_name="Render Manager")


@pytest.mark.parametrize(
    "value",
    [
        "theme: anything",
        "node: null",
        "node: {id: MixedCase}",
        "node: {id: true}",
        "manager: {display_name: 42}",
        'manager: {display_name: ""}',
        "node: {id: a, id: b}",
        'manager: {display_name: "bad\\nname"}',
        "[]",
        "node: {1: value}",
        "node: {unknown: value}",
        "manager: {display_name: [",
        'manager: {display_name: "' + "x" * 121 + '"}',
    ],
)
def test_invalid_identity_fails_before_authority_construction(tmp_path: Path, value: str) -> None:
    path = tmp_path / "identity.yaml"
    path.write_text(value)
    with pytest.raises(ProfileValidationError):
        load_identity(path)
    assert (
        run(
            ["--identity-config", str(path), "serve"],
            manager_factory=lambda *a, **k: pytest.fail("manager constructed"),
        )
        == 2
    )


def test_explicit_missing_identity_is_not_silently_defaulted(tmp_path: Path) -> None:
    with pytest.raises(ProfileValidationError):
        load_identity(tmp_path / "missing.yaml")


def test_http_identity_is_escaped_and_does_not_change_registry() -> None:
    identity = NodeIdentity("render", "<Render>", "</title><script>alert(1)</script>{{node_name}}")
    manager, _, _, _, _ = manager_fixture()
    server = create_server(manager, port=0, identity=identity)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(base, timeout=2) as response:
            html = response.read().decode()
        with urllib.request.urlopen(base + "/api/status", timeout=2) as response:
            status = json.load(response)
        assert f"<title>{escape(identity.manager_display_name)}</title>" in html
        assert "<script>alert" not in html
        assert status["node"] == identity.to_dict()["node"]
        assert status["manager"] == identity.to_dict()["manager"]
        assert {item["runtime"] for item in status["runtimes"]} == {"alpha", "beta"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_mcp_configured_identity_and_bounded_tools() -> None:
    manager, _, _, _, _ = manager_fixture()
    identity = NodeIdentity("render", "Render Node", "Render Manager")
    server = create_mcp_server(manager, identity=identity)
    assert server.name == "Render Manager"

    async def scenario() -> None:
        async with Client(server, raise_exceptions=True) as client:
            status = result_value(await client.call_tool("system.status"))
            assert status["node"]["id"] == "render"
            assert status["manager"]["display_name"] == "Render Manager"
            assert {tool.name for tool in (await client.list_tools()).tools} == {
                "runtime.list",
                "runtime.status",
                "runtime.activate",
                "runtime.stop",
                "system.status",
            }

    anyio.run(scenario)


@pytest.mark.parametrize("surface", ["serve", "mcp"])
def test_cli_passes_identity_to_surface(tmp_path: Path, surface: str) -> None:
    manager, _, _, _, _ = manager_fixture()
    path = tmp_path / "identity.yaml"
    path.write_text(
        "node: {id: render, display_name: Render Node}\nmanager: {display_name: Render Manager}\n"
    )
    captured: list[NodeIdentity] = []

    def runner(received: object, **kwargs: Any) -> None:
        assert received is manager
        captured.append(kwargs["identity"])

    assert (
        run(
            ["--identity-config", str(path), surface],
            manager_factory=lambda *a, **k: manager,
            server_runner=runner,
            mcp_runner=runner,
        )
        == 0
    )
    assert captured == [load_identity(path)]


def test_cli_system_status_contains_configured_identity(tmp_path: Path, capsys: Any) -> None:
    manager, _, _, _, _ = manager_fixture()
    path = tmp_path / "identity.yaml"
    path.write_text("node: {id: render}\n")
    assert (
        run(
            ["--identity-config", str(path), "system", "status"],
            manager_factory=lambda *a, **k: manager,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["node"]["id"] == "render"
