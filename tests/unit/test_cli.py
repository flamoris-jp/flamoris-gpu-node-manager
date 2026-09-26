from __future__ import annotations

import json
from pathlib import Path

import pytest

from flamoris_gpu_node_manager.cli import run
from flamoris_gpu_node_manager.domain.models import RuntimeState, RuntimeStatus
from flamoris_gpu_node_manager.identity import NodeIdentity
from flamoris_gpu_node_manager.infrastructure.locking import DEFAULT_TRANSITION_LOCK_PATH
from tests.helpers import profile


class FakeManager:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.profile = profile("alpha")
        self.status = RuntimeStatus(
            runtime="alpha",
            state=RuntimeState.OFF,
            service="alpha.service",
            healthy=False,
        )

    def reconstruct(self) -> None:
        self.calls.append("reconstruct")

    def activate(self, runtime_id: str) -> RuntimeStatus:
        self.calls.append(("activate", runtime_id))
        return RuntimeStatus(
            runtime=runtime_id,
            state=RuntimeState.READY,
            service=f"{runtime_id}.service",
            healthy=True,
        )


def test_cli_calls_shared_manager_authority(capsys: object, tmp_path: Path) -> None:
    manager = FakeManager()
    factory_calls: list[tuple[Path, Path, float]] = []

    def factory(config_directory: Path, *, lock_path: Path, lock_timeout: float) -> FakeManager:
        factory_calls.append((config_directory, lock_path, lock_timeout))
        return manager

    result = run(
        [
            "--config-dir",
            str(tmp_path),
            "--lock-path",
            str(tmp_path / "transition.lock"),
            "runtime",
            "activate",
            "alpha",
        ],
        manager_factory=factory,
    )

    assert result == 0
    assert manager.calls == ["reconstruct", ("activate", "alpha")]
    assert factory_calls[0][0] == tmp_path
    output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert output["state"] == "READY"


def test_serve_defaults_to_loopback_and_shared_manager(tmp_path: Path) -> None:
    manager = FakeManager()
    calls: list[tuple[object, str, int, object]] = []
    telemetry = object()

    def factory(config_directory: Path, *, lock_path: Path, lock_timeout: float) -> FakeManager:
        return manager

    def server_runner(
        supplied_manager: object, *, host: str, port: int, telemetry: object, identity: NodeIdentity
    ) -> None:
        calls.append((supplied_manager, host, port, telemetry))

    result = run(
        ["--config-dir", str(tmp_path), "serve"],
        manager_factory=factory,
        server_runner=server_runner,
        telemetry_factory=lambda: telemetry,
    )

    assert result == 0
    assert manager.calls == ["reconstruct"]
    assert calls == [(manager, "127.0.0.1", 8090, telemetry)]


def test_mcp_command_uses_stdio_runner_and_shared_manager(tmp_path: Path) -> None:
    manager = FakeManager()
    calls: list[tuple[object, object]] = []
    telemetry = object()

    def factory(config_directory: Path, *, lock_path: Path, lock_timeout: float) -> FakeManager:
        return manager

    def mcp_runner(supplied_manager: object, *, telemetry: object, identity: NodeIdentity) -> None:
        calls.append((supplied_manager, telemetry))

    result = run(
        ["--config-dir", str(tmp_path), "mcp"],
        manager_factory=factory,
        mcp_runner=mcp_runner,
        telemetry_factory=lambda: telemetry,
    )

    assert result == 0
    assert manager.calls == ["reconstruct"]
    assert calls == [(manager, telemetry)]


def test_mcp_http_uses_same_manager_and_loopback_defaults(tmp_path: Path) -> None:
    manager = FakeManager()
    calls: list[tuple[object, dict[str, object]]] = []

    def runner(supplied_manager: object, **kwargs: object) -> None:
        calls.append((supplied_manager, kwargs))

    assert (
        run(
            ["--config-dir", str(tmp_path), "mcp", "--transport", "streamable-http"],
            manager_factory=lambda *args, **kwargs: manager,
            mcp_runner=runner,
            telemetry_factory=lambda: None,
        )
        == 0
    )
    assert manager.calls == ["reconstruct"]
    assert calls == [
        (
            manager,
            {
                "telemetry": None,
                "identity": NodeIdentity(),
                "transport": "streamable-http",
                "host": "127.0.0.1",
                "port": 8766,
                "mcp_path": "/mcp",
            },
        )
    ]


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--transport", "sse"),
        ("--host", "http://localhost"),
        ("--host", ""),
        ("--port", "0"),
        ("--port", "65536"),
        ("--port", "not-a-port"),
        ("--mcp-path", "mcp"),
        ("--mcp-path", "/../mcp"),
        ("--mcp-path", "/mcp/"),
        ("--mcp-path", "/mcp?key=x"),
        ("--mcp-path", "/{route}"),
        ("--mcp-path", "/%6dcp"),
    ],
)
def test_invalid_http_options_rejected_before_manager_build(
    tmp_path: Path, option: str, value: str
) -> None:
    with pytest.raises(SystemExit, match="2"):
        run(
            ["--config-dir", str(tmp_path), "mcp", "--transport", "streamable-http", option, value],
            manager_factory=lambda *args, **kwargs: pytest.fail("constructed manager"),
        )


def test_cli_defaults_to_host_wide_transition_lock(tmp_path: Path) -> None:
    manager = FakeManager()
    factory_calls: list[Path] = []

    def factory(config_directory: Path, *, lock_path: Path, lock_timeout: float) -> FakeManager:
        factory_calls.append(lock_path)
        return manager

    assert (
        run(
            ["--config-dir", str(tmp_path), "serve"],
            manager_factory=factory,
            server_runner=lambda *args, **kwargs: None,
        )
        == 0
    )
    assert factory_calls == [DEFAULT_TRANSITION_LOCK_PATH]
