"""Host-wide transition locking for concurrent CLI processes and threads."""

from __future__ import annotations

import fcntl
import os
import stat
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from flamoris_gpu_node_manager.domain.errors import TransitionBusyError

DEFAULT_TRANSITION_LOCK_PATH = Path("/run/flamoris-gpu-node-manager/transition.lock")


class FileTransitionLock:
    def __init__(
        self,
        path: Path,
        *,
        poll_interval: float = 0.05,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._path = path
        self._thread_lock = threading.Lock()
        self._poll_interval = poll_interval
        self._monotonic = monotonic
        self._sleep = sleep

    @contextmanager
    def hold(self, timeout: float) -> Iterator[None]:
        if not self._thread_lock.acquire(timeout=timeout):
            raise TransitionBusyError("another runtime transition is in progress")
        handle = None
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(
                self._path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
            )
            handle = os.fdopen(descriptor, "a+", encoding="utf-8")
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise OSError(
                    "transition lock must be a singly linked regular file owned by caller"
                )
            # Tighten legacy permissions in place; never replace the shared inode.
            os.fchmod(handle.fileno(), 0o600)
            deadline = self._monotonic() + timeout
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    remaining = deadline - self._monotonic()
                    if remaining <= 0:
                        raise TransitionBusyError(
                            "another runtime transition is in progress"
                        ) from exc
                    self._sleep(min(self._poll_interval, remaining))
            yield
        finally:
            if handle is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()
            self._thread_lock.release()
