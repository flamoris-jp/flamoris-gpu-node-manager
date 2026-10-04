"""Generic, profile-selected readiness checks."""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from flamoris_gpu_node_manager.application.ports import SystemdPort
from flamoris_gpu_node_manager.domain.models import RuntimeProfile, ServiceState

from .resource import ProcessResourceAdapter

_MAX_HEALTH_BODY_BYTES = 64 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


class HealthAdapter:
    def __init__(
        self,
        systemd: SystemdPort,
        processes: ProcessResourceAdapter,
        *,
        poll_interval: float = 0.25,
        probe_timeout: float = 1.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._systemd = systemd
        self._processes = processes
        self._poll_interval = poll_interval
        self._probe_timeout = probe_timeout
        self._monotonic = monotonic
        self._sleep = sleep
        # Never send loopback probes via an environment proxy or a redirect target.
        self._http = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def check(self, profile: RuntimeProfile) -> bool:
        config = profile.health
        if config.type == "none":
            return True
        if config.type == "systemd-active":
            return self._systemd.get_state(profile.service) == ServiceState.ACTIVE
        if config.type == "process":
            assert config.process_name is not None
            return self._processes.has_process_name(config.process_name)
        if config.type == "tcp":
            assert config.host is not None and config.port is not None
            try:
                with socket.create_connection(
                    (config.host, config.port), timeout=self._probe_timeout
                ):
                    return True
            except OSError:
                return False
        if config.type in {"http", "http-json"}:
            assert config.url is not None
            try:
                with self._http.open(config.url, timeout=self._probe_timeout) as response:
                    if not 200 <= int(response.status) < 300:
                        return False
                    if config.type == "http":
                        return True
                    length = response.headers.get("Content-Length")
                    if length is not None and int(length) > _MAX_HEALTH_BODY_BYTES:
                        return False
                    body = response.read(_MAX_HEALTH_BODY_BYTES + 1)
                    if len(body) > _MAX_HEALTH_BODY_BYTES:
                        return False
                    assert config.json_pointer is not None
                    observed = _resolve_json_pointer(json.loads(body), config.json_pointer)
                    return type(observed) is type(config.equals) and observed == config.equals
            except (
                OSError,
                ValueError,
                KeyError,
                IndexError,
                UnicodeError,
                json.JSONDecodeError,
                urllib.error.URLError,
            ):
                return False
        raise RuntimeError(f"validated health type has no adapter: {config.type}")

    def wait_healthy(self, profile: RuntimeProfile, timeout: float) -> bool:
        deadline = self._monotonic() + timeout
        while True:
            if self.check(profile):
                return True
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return False
            self._sleep(min(self._poll_interval, remaining))


def _resolve_json_pointer(document: object, pointer: str) -> object:
    current = document
    for raw_token in pointer.split("/")[1:]:
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit():
                raise KeyError(token)
            current = current[int(token)]
        else:
            raise KeyError(token)
    return current
