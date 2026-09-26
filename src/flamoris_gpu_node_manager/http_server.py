"""Loopback-first HTTP adapter over the shared runtime authority."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

from flamoris_gpu_node_manager.application.manager import RuntimeManager
from flamoris_gpu_node_manager.domain.errors import (
    NodeManagerError,
    RuntimeUnavailableError,
    TransitionBusyError,
    TransitionError,
    UnknownRuntimeError,
)
from flamoris_gpu_node_manager.domain.models import SystemStatus
from flamoris_gpu_node_manager.identity import DEFAULT_IDENTITY, NodeIdentity
from flamoris_gpu_node_manager.serialization import profile_to_dict, system_to_dict

_LOG = logging.getLogger(__name__)


class TelemetryPort(Protocol):
    def read(self) -> Mapping[str, Any]: ...


class _UnavailableTelemetry:
    def read(self) -> dict[str, Any]:
        return {
            "available": False,
            "gpu_utilization_percent": None,
            "vram_used_bytes": None,
            "vram_total_bytes": None,
            "temperature_celsius": None,
            "power_watts": None,
            "error": "telemetry adapter is not configured",
        }


class NodeHttpServer(ThreadingHTTPServer):
    """Threaded server so status polling remains available during transitions."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        manager: RuntimeManager,
        telemetry: TelemetryPort | None = None,
    ) -> None:
        self.identity = NodeIdentity()
        self.manager = manager
        self.telemetry = telemetry or _UnavailableTelemetry()
        self._operation_gate = threading.Lock()
        self._active_mutations = 0
        self._last_refresh = 0.0
        super().__init__(address, NodeRequestHandler)
        # Exact authorities, including the actual port (also for ephemeral test ports).
        # Reverse proxies must rewrite Host after enforcing their own access policy.
        hosts = {"127.0.0.1", "localhost"}
        if address[0] not in {"0.0.0.0", "::", ""}:
            hosts.add(address[0].lower())
        port = self.server_address[1]
        self.trusted_authorities = {f"{host}:{port}" for host in hosts}
        if port == 80:
            self.trusted_authorities.update(hosts)

    def snapshot(self) -> SystemStatus:
        """Refresh real state when idle without erasing an in-flight transition."""
        with self._operation_gate:
            now = time.monotonic()
            if self._active_mutations == 0 and now - self._last_refresh >= 0.25:
                self.manager.reconstruct()
                self._last_refresh = now
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
                self._last_refresh = 0.0


class NodeRequestHandler(BaseHTTPRequestHandler):
    server: NodeHttpServer
    server_version = "GpuNodeManagerHTTP/1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(10.0)

    def log_message(self, format: str, *args: object) -> None:
        _LOG.info("http %s - %s", self.address_string(), format % args)

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0].lower() not in self.server.trusted_authorities:
            self.close_connection = True
            self._send_error(HTTPStatus.MISDIRECTED_REQUEST, "untrusted_host", "untrusted Host")
            return False
        # This origin server does not accept proxy-style absolute-form targets.
        if not self.path.startswith("/") or self.path.startswith("//"):
            self.close_connection = True
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_request", "invalid request target")
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlsplit(self.path).path
        if path in {"/", "/index.html"}:
            self._send_asset("index.html", "text/html; charset=utf-8")
            return
        if path == "/app.js":
            self._send_asset("app.js", "text/javascript; charset=utf-8")
            return
        if path == "/styles.css":
            self._send_asset("styles.css", "text/css; charset=utf-8")
            return
        if path == "/api/status":
            try:
                status_payload = {
                    **system_to_dict(self.server.snapshot(), self._safe_telemetry()),
                    **self.server.identity.to_dict(),
                }
            except Exception as exc:
                self._internal_error(exc)
                return
            self._send_json(HTTPStatus.OK, status_payload)
            return
        if path == "/api/runtimes":
            try:
                self.server.snapshot()
                runtime_payload = self._runtime_list()
            except NodeManagerError as exc:
                self._manager_error(exc)
                return
            except Exception as exc:
                self._internal_error(exc)
                return
            self._send_json(HTTPStatus.OK, runtime_payload)
            return
        if path == "/api/telemetry":
            self._send_json(HTTPStatus.OK, self._safe_telemetry())
            return
        runtime_id = self._runtime_route(path)
        if runtime_id is not None:
            self._runtime_detail(runtime_id)
            return
        self._send_error(HTTPStatus.NOT_FOUND, "not_found", "route not found")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlsplit(self.path).path
        route = self._mutation_route(path)
        if route is None:
            self._send_error(HTTPStatus.NOT_FOUND, "not_found", "route not found")
            return
        if self.headers.get("X-GPU-Node-Manager-Intent") != "runtime-mutation":
            self._send_error(
                HTTPStatus.FORBIDDEN,
                "intent_header_required",
                "X-GPU-Node-Manager-Intent header is required for mutations",
            )
            return
        if not self._empty_body():
            return
        runtime_id, action = route
        try:
            with self.server.mutation():
                if action == "activate":
                    result: object = self.server.manager.activate(runtime_id).to_dict()
                else:
                    result = [item.to_dict() for item in self.server.manager.stop(runtime_id)]
        except NodeManagerError as exc:
            self._manager_error(exc)
            return
        except Exception as exc:
            self._internal_error(exc)
            return
        self._send_json(HTTPStatus.OK, result)

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_not_allowed()

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_not_allowed()

    def _runtime_list(self) -> list[dict[str, Any]]:
        manager = self.server.manager
        return [
            profile_to_dict(profile, manager.runtime_status(profile.id))
            for profile in manager.list_runtimes()
        ]

    def _runtime_detail(self, runtime_id: str) -> None:
        try:
            self.server.snapshot()
            profile = next(
                item for item in self.server.manager.list_runtimes() if item.id == runtime_id
            )
            status = self.server.manager.runtime_status(runtime_id)
        except StopIteration:
            self._manager_error(UnknownRuntimeError(f"unknown runtime id: {runtime_id}"))
            return
        except NodeManagerError as exc:
            self._manager_error(exc)
            return
        except Exception as exc:
            self._internal_error(exc)
            return
        self._send_json(HTTPStatus.OK, profile_to_dict(profile, status))

    def _safe_telemetry(self) -> dict[str, Any]:
        try:
            return dict(self.server.telemetry.read())
        except Exception as exc:  # telemetry is cosmetic and must not break the authority
            _LOG.warning("telemetry read failed: %s", exc)
            return {
                "available": False,
                "gpu_utilization_percent": None,
                "vram_used_bytes": None,
                "vram_total_bytes": None,
                "temperature_celsius": None,
                "power_watts": None,
                "error": str(exc) or type(exc).__name__,
            }

    @staticmethod
    def _runtime_route(path: str) -> str | None:
        prefix = "/api/runtimes/"
        if not path.startswith(prefix):
            return None
        runtime_id = unquote(path.removeprefix(prefix))
        return runtime_id if runtime_id and "/" not in runtime_id else None

    @staticmethod
    def _mutation_route(path: str) -> tuple[str, str] | None:
        prefix = "/api/runtimes/"
        if not path.startswith(prefix):
            return None
        remainder = path.removeprefix(prefix).split("/")
        if len(remainder) != 2 or remainder[1] not in {"activate", "stop"}:
            return None
        runtime_id = unquote(remainder[0])
        if not runtime_id or "/" in runtime_id:
            return None
        return runtime_id, remainder[1]

    def _empty_body(self) -> bool:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_request", "invalid Content-Length")
            return False
        if length != 0:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "mutation endpoints do not accept a request body",
            )
            return False
        return True

    def _manager_error(self, exc: NodeManagerError) -> None:
        if isinstance(exc, UnknownRuntimeError):
            status = HTTPStatus.NOT_FOUND
        elif isinstance(exc, (RuntimeUnavailableError, TransitionBusyError)):
            status = HTTPStatus.CONFLICT
        elif isinstance(exc, TransitionError):
            status = HTTPStatus.SERVICE_UNAVAILABLE
        else:
            status = HTTPStatus.BAD_REQUEST
        self._send_error(
            status,
            type(exc).__name__,
            str(exc),
            manager_status=self.server.manager.system_status().to_dict(),
        )

    def _internal_error(self, exc: Exception) -> None:
        _LOG.exception("unexpected HTTP adapter failure", exc_info=exc)
        self._send_error(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "internal_error",
            "unexpected manager failure",
            manager_status=self.server.manager.system_status().to_dict(),
        )

    def _method_not_allowed(self) -> None:
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET, POST")
        self._finish_json(
            {"error": {"code": "method_not_allowed", "message": "method not allowed"}}
        )

    def _send_asset(self, name: str, content_type: str) -> None:
        try:
            body = files("flamoris_gpu_node_manager.web").joinpath(name).read_bytes()
        except (FileNotFoundError, OSError):
            self._send_error(HTTPStatus.NOT_FOUND, "not_found", "asset not found")
            return
        if name == "index.html":
            identity = self.server.identity
            replacements = {
                "{{manager_name}}": escape(identity.manager_display_name),
                "{{node_name}}": escape(identity.node_display_name),
                "{{node_mark}}": escape(identity.node_display_name[0]),
            }
            # One pass: configured names are text, never templates or HTML.
            body = re.sub(
                r"{{(?:manager_name|node_name|node_mark)}}",
                lambda match: replacements[match.group()],
                body.decode("utf-8"),
            ).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; connect-src 'self'; frame-ancestors 'none'",
        )
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(
        self,
        status: HTTPStatus,
        code: str,
        message: str,
        *,
        manager_status: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"error": {"code": code, "message": message}}
        if manager_status is not None:
            payload["status"] = manager_status
        self._send_json(status, payload)

    def _send_json(self, status: HTTPStatus, payload: object) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self._finish_json(payload)

    def _finish_json(self, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def create_server(
    manager: RuntimeManager,
    *,
    host: str = "127.0.0.1",
    port: int = 8090,
    telemetry: TelemetryPort | None = None,
    identity: NodeIdentity = DEFAULT_IDENTITY,
    server_factory: Callable[
        [tuple[str, int], RuntimeManager, TelemetryPort | None], NodeHttpServer
    ] = NodeHttpServer,
) -> NodeHttpServer:
    server = server_factory((host, port), manager, telemetry)
    server.identity = identity
    return server


def serve(
    manager: RuntimeManager,
    *,
    host: str = "127.0.0.1",
    port: int = 8090,
    telemetry: TelemetryPort | None = None,
    identity: NodeIdentity = DEFAULT_IDENTITY,
) -> None:
    server = create_server(manager, host=host, port=port, telemetry=telemetry, identity=identity)
    _LOG.info("%s listening on http://%s:%d", identity.manager_display_name, host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _LOG.info("GPU Node Manager stopped by operator")
    finally:
        server.server_close()
