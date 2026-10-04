"""Linux pidfd plus boot/start/credential/executable identity; no polling claims."""

from __future__ import annotations

import hashlib
import json
import os
import select
import stat
import threading
from pathlib import Path

from .evidence_publication import EvidencePublicationError, FileEvidencePublisher


class LinuxProcessLifetimeFence:
    """Retain the exact live process handle; reject exit and observable identity drift.

    This observation fence does not withdraw a record continuously on crash.
    Supervisor crash invalidation remains mandatory for consumer schema v1.
    Same-binary exec can preserve this identity and needs the mutation protocol.
    """

    def __init__(self, pid: int, uid: int) -> None:
        if type(pid) is not int or pid <= 0 or type(uid) is not int or not 0 <= uid < 2**32 - 1:
            raise ValueError("invalid process lifetime binding")
        self.pid, self.uid = pid, uid
        self._fd = -1
        try:
            self._fd = os.pidfd_open(pid, 0)
            self._identity = self._read()
            self._check_alive()
            raw = json.dumps(self._identity, sort_keys=True, separators=(",", ":")).encode()
            self._token = hashlib.sha256(raw).hexdigest()
        except Exception as error:
            self.close()
            raise EvidencePublicationError("provider lifetime unavailable") from error

    def _check_alive(self) -> None:
        if self._fd < 0 or select.select([self._fd], [], [], 0)[0]:
            raise ValueError("provider exited or fence closed")

    def _read(self) -> tuple[object, ...]:
        self._check_alive()
        root = Path("/proc") / str(self.pid)
        with (root / "stat").open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536 or b") " not in raw:
            raise ValueError("invalid process stat")
        fields = raw[raw.rfind(b")") + 2 :].split()
        if len(fields) < 20 or fields[0] in (b"Z", b"X", b"x"):
            raise ValueError("provider is unavailable")
        with (root / "status").open("r") as stream:
            status = stream.read(65537)
        if len(status) > 65536:
            raise ValueError("invalid process status")
        credentials = next(line for line in status.splitlines() if line.startswith("Uid:"))
        if tuple(map(int, credentials.split()[1:])) != (self.uid,) * 4:
            raise ValueError("provider credentials differ")
        executable = (root / "exe").stat()
        if not stat.S_ISREG(executable.st_mode):
            raise ValueError("unsupported provider executable")
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if not boot or len(boot) != 36:
            raise ValueError("boot identity unavailable")
        identity = (
            boot,
            self.pid,
            int(fields[19]),
            self.uid,
            os.readlink(root / "exe"),
            executable.st_dev,
            executable.st_ino,
            executable.st_size,
            executable.st_mtime_ns,
            executable.st_ctime_ns,
        )
        self._check_alive()
        return identity

    def observe(self) -> str:
        try:
            if self._read() != self._identity:
                raise ValueError("provider lifetime changed")
            return self._token
        except Exception as error:
            raise EvidencePublicationError("provider lifetime unavailable") from error

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> LinuxProcessLifetimeFence:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class ProcessExitEvidenceInvalidator:
    """Owned pidfd monitor; withdraw this publisher's record on observed exit.

    Shared consumer guards can delay exclusive withdrawal. This monitor is a
    fail-closed backstop, not pre-crash synchronization or supervisor restart
    coverage. Every replacement start must still acquire the mutation lease.
    """

    def __init__(
        self,
        fence: LinuxProcessLifetimeFence,
        publisher: FileEvidencePublisher,
        *,
        lock_timeout: float = 1.0,
    ) -> None:
        if type(lock_timeout) not in (int, float) or not 0 < lock_timeout <= 5:
            raise ValueError("invalid exit invalidation timeout")
        self.fence, self.publisher, self.lock_timeout = fence, publisher, lock_timeout
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fd = -1

    def __enter__(self) -> ProcessExitEvidenceInvalidator:
        if self._thread is not None:
            raise EvidencePublicationError("provider exit monitoring unavailable")
        try:
            self.fence.observe()
            self._fd = os.dup(self.fence._fd)
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="runtime-evidence-exit"
            )
            self._thread.start()
            return self
        except BaseException:
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1
            self.publisher.withdraw(timeout=self.lock_timeout)
            raise

    def _run(self) -> None:
        exited = False
        while not self._stop.is_set():
            if not exited:
                try:
                    exited = bool(select.select([self._fd], [], [], 0.1)[0])
                except Exception:
                    exited = True
                if not exited:
                    continue
            try:
                self.publisher.withdraw(timeout=self.lock_timeout)
                return
            except Exception:
                # Lock/storage failure keeps qualification unavailable by TTL;
                # retry bounded withdrawals until the owner ends the context.
                self._stop.wait(0.1)

    def __exit__(self, *args: object) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=self.lock_timeout + 0.5)
            if self._thread.is_alive():
                raise EvidencePublicationError("provider exit monitoring unavailable")
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1
        self.publisher.withdraw(timeout=self.lock_timeout)
