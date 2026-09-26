"""Controlled systemd integration for allow-listed runtime services."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Sequence

from flamoris_gpu_node_manager.domain.models import ServiceState


class SystemdError(RuntimeError):
    """A controlled systemd command failed."""


class SystemdAdapter:
    def __init__(
        self,
        *,
        poll_interval: float = 0.25,
        command_timeout: float = 10,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._poll_interval = poll_interval
        self._command_timeout = command_timeout
        self._runner = runner
        self._monotonic = monotonic
        self._sleep = sleep

    def _run(self, arguments: Sequence[str]) -> str:
        result = self._execute(arguments)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown systemctl error"
            raise SystemdError(detail)
        return result.stdout.strip()

    def _execute(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        try:
            result = self._runner(
                ["systemctl", *arguments],
                check=False,
                capture_output=True,
                text=True,
                timeout=self._command_timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SystemdError(f"cannot execute systemctl: {exc}") from exc
        return result

    def get_state(self, service: str) -> ServiceState:
        result = self._execute(["show", "--property=LoadState", "--property=ActiveState", service])
        properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if properties.get("LoadState") == "not-found":
            return ServiceState.NOT_FOUND
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown systemctl error"
            raise SystemdError(detail)
        value = properties.get("ActiveState", "")
        try:
            return ServiceState(value)
        except ValueError:
            return ServiceState.UNKNOWN

    def start(self, service: str) -> None:
        self._run(["--no-block", "start", service])

    def stop(self, service: str) -> None:
        self._run(["--no-block", "stop", service])

    def wait_active(self, service: str, timeout: float) -> bool:
        return self._wait_for(service, {ServiceState.ACTIVE}, timeout)

    def wait_inactive(self, service: str, timeout: float) -> bool:
        return self._wait_for(
            service,
            {ServiceState.INACTIVE, ServiceState.FAILED, ServiceState.NOT_FOUND},
            timeout,
        )

    def _wait_for(self, service: str, expected: set[ServiceState], timeout: float) -> bool:
        deadline = self._monotonic() + timeout
        while True:
            if self.get_state(service) in expected:
                return True
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return False
            self._sleep(min(self._poll_interval, remaining))
