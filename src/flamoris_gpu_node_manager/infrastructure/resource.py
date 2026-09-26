"""Runtime-owned process inspection and bounded release waiting."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from flamoris_gpu_node_manager.domain.models import ProcessMatcher, RuntimeProfile


@dataclass(frozen=True)
class ProcessSnapshot:
    pid: int
    name: str
    command_line: str


class ResourceInspectionError(RuntimeError):
    """Runtime ownership could not be inspected safely."""


class ProcfsProcessSource:
    def __init__(self, root: Path = Path("/proc")) -> None:
        self._root = root

    def snapshots(self) -> Iterable[ProcessSnapshot]:
        try:
            entries = tuple(self._root.iterdir())
        except OSError as exc:
            raise ResourceInspectionError(f"cannot inspect {self._root}: {exc}") from exc
        snapshots: list[ProcessSnapshot] = []
        for entry in entries:
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                name = (entry / "comm").read_text(encoding="utf-8").strip()
                raw = (entry / "cmdline").read_bytes()
            except (FileNotFoundError, ProcessLookupError):
                continue
            except (OSError, UnicodeError) as exc:
                raise ResourceInspectionError(
                    f"cannot inspect process {entry.name}: {exc}"
                ) from exc
            command_line = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
            snapshots.append(
                ProcessSnapshot(pid=int(entry.name), name=name, command_line=command_line)
            )
        return snapshots


class ProcessResourceAdapter:
    def __init__(
        self,
        source: ProcfsProcessSource | None = None,
        *,
        poll_interval: float = 0.25,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._source = source or ProcfsProcessSource()
        self._poll_interval = poll_interval
        self._monotonic = monotonic
        self._sleep = sleep

    @staticmethod
    def _matches(process: ProcessSnapshot, matcher: ProcessMatcher) -> bool:
        return process.name == matcher.name and all(
            token in process.command_line for token in matcher.cmdline_contains
        )

    def has_process_name(self, name: str) -> bool:
        return any(process.name == name for process in self._source.snapshots())

    def owned_processes(self, profile: RuntimeProfile) -> tuple[ProcessSnapshot, ...]:
        if profile.release.type == "none":
            return ()
        return tuple(
            process
            for process in self._source.snapshots()
            if any(self._matches(process, matcher) for matcher in profile.release.processes)
        )

    def is_released(self, profile: RuntimeProfile) -> bool:
        return not self.owned_processes(profile)

    def wait_released(self, profile: RuntimeProfile, timeout: float) -> bool:
        deadline = self._monotonic() + timeout
        while True:
            if self.is_released(profile):
                return True
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return False
            self._sleep(min(self._poll_interval, remaining))
