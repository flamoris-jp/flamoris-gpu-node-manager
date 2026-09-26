"""Generic, profile-selected readiness checks."""

from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from flamoris_gpu_node_manager.application.ports import SystemdPort
from flamoris_gpu_node_manager.domain.models import RuntimeProfile, ServiceState

from .resource import ProcessResourceAdapter


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
        if config.type == "http":
            assert config.url is not None
            try:
                with self._http.open(config.url, timeout=self._probe_timeout) as response:
                    return 200 <= int(response.status) < 300
            except (OSError, urllib.error.URLError):
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
