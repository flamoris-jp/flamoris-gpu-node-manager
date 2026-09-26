from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from flamoris_gpu_node_manager.application.manager import RuntimeManager
from flamoris_gpu_node_manager.domain.errors import TransitionError
from flamoris_gpu_node_manager.domain.models import RuntimeState, ServiceState
from flamoris_gpu_node_manager.domain.registry import RuntimeRegistry
from flamoris_gpu_node_manager.http_server import create_server
from tests.helpers import FakeHealth, FakeResources, FakeSystemd, RecordingLock, profile


class Telemetry:
    def read(self) -> dict[str, Any]:
        return {"available": True, "gpu_utilization_percent": 12.5}


def manager_fixture(
    *, disabled: bool = False
) -> tuple[RuntimeManager, FakeSystemd, FakeHealth, RecordingLock]:
    events: list[str] = []
    systemd = FakeSystemd({}, events)
    health = FakeHealth({}, events)
    lock = RecordingLock()
    manager = RuntimeManager(
        RuntimeRegistry((profile("alpha"), profile("unavailable", enabled=not disabled))),
        systemd,
        FakeResources({}, events),
        health,
        lock,
    )
    manager.reconstruct()
    return manager, systemd, health, lock


@contextmanager
def running_server(manager: RuntimeManager, telemetry: object | None = None) -> Iterator[str]:
    server = create_server(
        manager,
        host="127.0.0.1",
        port=0,
        telemetry=telemetry or Telemetry(),  # type: ignore[arg-type]
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def request_json(url: str, *, method: str = "GET") -> tuple[int, object]:
    headers = {"X-GPU-Node-Manager-Intent": "runtime-mutation"} if method == "POST" else {}
    request = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def test_read_endpoints_use_dynamic_manager_registry() -> None:
    manager, _, _, _ = manager_fixture()

    with running_server(manager) as base:
        status_code, status = request_json(f"{base}/api/status")
        runtimes_code, runtimes = request_json(f"{base}/api/runtimes")
        detail_code, detail = request_json(f"{base}/api/runtimes/alpha")

    assert status_code == 200
    assert status["online"] is True  # type: ignore[index]
    assert status["telemetry"]["gpu_utilization_percent"] == 12.5  # type: ignore[index]
    assert runtimes_code == 200
    assert {item["id"] for item in runtimes} == {"alpha", "unavailable"}  # type: ignore[union-attr]
    assert detail_code == 200
    assert detail["service"] == "alpha.service"  # type: ignore[index]


def test_activate_and_stop_delegate_to_manager_authority() -> None:
    manager, systemd, _, _ = manager_fixture()

    with running_server(manager) as base:
        activate_code, activated = request_json(
            f"{base}/api/runtimes/alpha/activate", method="POST"
        )
        stop_code, stopped = request_json(f"{base}/api/runtimes/alpha/stop", method="POST")

    assert activate_code == 200
    assert activated["state"] == RuntimeState.READY  # type: ignore[index]
    assert stop_code == 200
    assert stopped[0]["state"] == RuntimeState.OFF  # type: ignore[index]
    assert systemd.states["alpha.service"] == ServiceState.INACTIVE


def test_unknown_and_disabled_runtime_activation_are_structured_errors() -> None:
    manager, _, _, _ = manager_fixture(disabled=True)

    with running_server(manager) as base:
        missing_code, missing = request_json(f"{base}/api/runtimes/missing/activate", method="POST")
        disabled_code, disabled = request_json(
            f"{base}/api/runtimes/unavailable/activate", method="POST"
        )

    assert missing_code == 404
    assert missing["error"]["code"] == "UnknownRuntimeError"  # type: ignore[index]
    assert disabled_code == 409
    assert disabled["error"]["code"] == "RuntimeUnavailableError"  # type: ignore[index]


def test_arbitrary_service_and_shell_routes_are_not_exposed() -> None:
    manager, systemd, _, _ = manager_fixture()

    with running_server(manager) as base:
        service_code, _ = request_json(f"{base}/api/runtimes/alpha.service/activate", method="POST")
        shell_code, _ = request_json(f"{base}/api/systemctl", method="POST")

    assert service_code == 404
    assert shell_code == 404
    assert systemd.events == []


def test_transition_failure_is_returned_without_false_ready_state() -> None:
    manager, systemd, _, _ = manager_fixture()
    systemd.start_error = RuntimeError("start exploded")

    with running_server(manager) as base:
        code, response = request_json(f"{base}/api/runtimes/alpha/activate", method="POST")

    assert code == 503
    assert response["error"]["code"] == TransitionError.__name__  # type: ignore[index]
    runtime = next(  # type: ignore[union-attr]
        item for item in response["status"]["runtimes"] if item["runtime"] == "alpha"
    )
    assert runtime["state"] == RuntimeState.FAILED


def test_mutation_request_body_is_rejected() -> None:
    manager, systemd, _, _ = manager_fixture()

    with running_server(manager) as base:
        request = urllib.request.Request(
            f"{base}/api/runtimes/alpha/activate",
            data=b"{}",
            method="POST",
            headers={"X-GPU-Node-Manager-Intent": "runtime-mutation"},
        )
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=2)

    assert caught.value.code == 400
    assert systemd.events == []


def test_mutation_requires_browser_csrf_intent_header() -> None:
    manager, systemd, _, _ = manager_fixture()

    with running_server(manager) as base:
        request = urllib.request.Request(f"{base}/api/runtimes/alpha/activate", method="POST")
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=2)

    assert caught.value.code == 403
    assert systemd.events == []


def test_api_exposes_real_transition_before_ready() -> None:
    manager, _, health, _ = manager_fixture()
    health.delay = 0.25
    result: list[tuple[int, object]] = []

    with running_server(manager) as base:
        worker = threading.Thread(
            target=lambda: result.append(
                request_json(f"{base}/api/runtimes/alpha/activate", method="POST")
            )
        )
        worker.start()
        observed: object | None = None
        for _ in range(50):
            _, candidate = request_json(f"{base}/api/status")
            if candidate["transition"] and candidate["transition"]["step"] == "health-check":  # type: ignore[index]
                observed = candidate
                break
            time.sleep(0.01)
        worker.join()

    assert observed is not None
    runtime = next(  # type: ignore[union-attr]
        item for item in observed["runtimes"] if item["runtime"] == "alpha"
    )
    assert runtime["state"] == RuntimeState.STARTING
    assert result[0][1]["state"] == RuntimeState.READY  # type: ignore[index]


def test_concurrent_http_mutations_use_existing_manager_serialization() -> None:
    manager, _, health, lock = manager_fixture()
    health.delay = 0.05
    barrier = threading.Barrier(3)
    results: list[tuple[int, object]] = []

    with running_server(manager) as base:

        def activate(runtime_id: str) -> None:
            barrier.wait()
            results.append(
                request_json(f"{base}/api/runtimes/{runtime_id}/activate", method="POST")
            )

        workers = [
            threading.Thread(target=activate, args=("alpha",)),
            threading.Thread(target=activate, args=("unavailable",)),
        ]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join()

    assert [code for code, _ in results] == [200, 200]
    assert lock.maximum_active == 1


def test_new_server_manager_reconstructs_real_state() -> None:
    first, systemd, _, _ = manager_fixture()
    first.activate("alpha")
    events: list[str] = []
    restarted = RuntimeManager(
        RuntimeRegistry((profile("alpha"), profile("unavailable"))),
        systemd,
        FakeResources({}, events),
        FakeHealth({"alpha": True}, events),
        RecordingLock(),
    )
    restarted.reconstruct()

    with running_server(restarted) as base:
        _, status = request_json(f"{base}/api/status")

    assert status["current_runtime"] == "alpha"  # type: ignore[index]


def test_idle_server_refreshes_state_changed_by_another_surface() -> None:
    manager, systemd, _, _ = manager_fixture()

    with running_server(manager) as base:
        _, before = request_json(f"{base}/api/status")
        systemd.states["alpha.service"] = ServiceState.ACTIVE
        time.sleep(0.26)
        _, after = request_json(f"{base}/api/status")

    assert before["current_runtime"] is None  # type: ignore[index]
    assert after["current_runtime"] == "alpha"  # type: ignore[index]


def test_telemetry_failure_does_not_break_status_or_mutation() -> None:
    class ExplodingTelemetry:
        def read(self) -> dict[str, object]:
            raise OSError("sensor unavailable")

    manager, _, _, _ = manager_fixture()

    with running_server(manager, ExplodingTelemetry()) as base:
        status_code, status = request_json(f"{base}/api/status")
        activate_code, activated = request_json(
            f"{base}/api/runtimes/alpha/activate", method="POST"
        )

    assert status_code == 200
    assert status["telemetry"]["available"] is False  # type: ignore[index]
    assert activate_code == 200
    assert activated["state"] == RuntimeState.READY  # type: ignore[index]


def test_web_ui_assets_render_runtime_cards_from_api() -> None:
    manager, _, _, _ = manager_fixture()

    with running_server(manager) as base:
        with urllib.request.urlopen(f"{base}/", timeout=2) as response:
            page = response.read().decode()
            assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
            assert response.headers["X-Frame-Options"] == "DENY"
        with urllib.request.urlopen(f"{base}/app.js", timeout=2) as response:
            script = response.read().decode()

    assert 'id="runtimes"' in page
    assert 'href="./styles.css"' in page
    assert 'src="./app.js"' in page
    assert "new URL(`./api/${path}`, document.baseURI)" in script
    assert all(
        root_absolute not in page + script
        for root_absolute in ('"/api/', "`/api/", '"/app.js"', '"/styles.css"')
    )
    assert "runtime.id" in script
    assert "comfyui" not in script.lower()
    assert "yue2" not in script.lower()


@pytest.mark.parametrize(
    "method,path", [("GET", "/api/status"), ("POST", "/api/runtimes/alpha/activate")]
)
@pytest.mark.parametrize(
    "host",
    [None, "evil.example", "127.0.0.1.evil.example", "localhost:1", "127.0.0.1:8090@evil.example"],
)
def test_untrusted_host_rejected_before_observation_or_mutation(method, path, host) -> None:
    manager, systemd, _, _ = manager_fixture()
    with running_server(manager) as base:
        connection = http.client.HTTPConnection(base.removeprefix("http://"), timeout=2)
        connection.putrequest(method, path, skip_host=True)
        if host is not None:
            connection.putheader("Host", host)
        connection.putheader("X-GPU-Node-Manager-Intent", "runtime-mutation")
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 421
        response.read()
        connection.close()
    assert not systemd.events


def test_duplicate_host_and_absolute_request_targets_rejected() -> None:
    manager, systemd, _, _ = manager_fixture()
    with running_server(manager) as base:
        authority = base.removeprefix("http://")
        for target, duplicate, status in [
            ("/api/status", True, 421),
            (base + "/api/status", False, 400),
        ]:
            connection = http.client.HTTPConnection(authority, timeout=2)
            connection.putrequest("GET", target, skip_host=True)
            connection.putheader("Host", authority)
            if duplicate:
                connection.putheader("Host", authority)
            connection.endheaders()
            response = connection.getresponse()
            assert response.status == status
            response.read()
            connection.close()
        request = urllib.request.Request(
            base + "/api/status", headers={"Host": "localhost:" + authority.rsplit(":", 1)[1]}
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            assert response.status == 200
    assert not systemd.events


@pytest.mark.parametrize(
    ("page_url", "api_path", "expected_api_url"),
    [
        (
            "http://127.0.0.1:8090/",
            "status",
            "http://127.0.0.1:8090/api/status",
        ),
        (
            "http://node.example.test/node-manager/",
            "status",
            "http://node.example.test/node-manager/api/status",
        ),
        (
            "http://node.example.test/node-manager/",
            "runtimes/alpha/activate",
            "http://node.example.test/node-manager/api/runtimes/alpha/activate",
        ),
        (
            "http://node.example.test/node-manager/",
            "runtimes/alpha/stop",
            "http://node.example.test/node-manager/api/runtimes/alpha/stop",
        ),
    ],
)
def test_browser_api_paths_resolve_relative_to_page_base(
    page_url: str, api_path: str, expected_api_url: str
) -> None:
    from urllib.parse import urljoin

    assert urljoin(page_url, f"./api/{api_path}") == expected_api_url
