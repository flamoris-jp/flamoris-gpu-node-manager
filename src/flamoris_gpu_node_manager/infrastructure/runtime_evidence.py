"""Withdraw qualification evidence before managed mutations; never publish it.

The deployment must provision a protected, persistent slot while readers and
writers are stopped. Its identity marker anchors the directory and lock inode.
This is a necessary invalidation primitive, not complete runtime continuity.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import secrets
import stat
import time
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flamoris_gpu_node_manager.domain.errors import TransitionError
from flamoris_gpu_node_manager.domain.models import RuntimeProfile

_MAX_COUNTER = 2**64 - 1
_MAX_RECORD = 4096


def _validate_record(record: Path) -> None:
    if (
        not record.is_absolute()
        or str(record) != os.path.normpath(record)
        or len(str(record)) > 4096
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.json", record.name)
        or any(ord(c) < 32 for c in str(record))
    ):
        raise ValueError("evidence record must be an absolute normalized JSON path")


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _check_file(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise OSError("evidence files must be singly linked, caller-owned and write-protected")


@contextmanager
def _parent(path: Path) -> Iterator[int]:
    # Reject symlink components and untrusted rename/write access. A root-owned
    # sticky ancestor (e.g. /tmp in tests) protects caller-owned descendants;
    # the actual slot directory must always be protected without that exception.
    for directory in (path.parent, *path.parent.parents):
        info = directory.lstat()
        sticky_ancestor = (
            directory != path.parent and info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        )
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid not in {0, os.geteuid()}
            or (info.st_mode & 0o022 and not sticky_ancestor)
        ):
            raise OSError("evidence directory ancestry is not protected")
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_uid != os.geteuid():
            raise OSError("evidence directory must be owned by caller")
        yield fd
    finally:
        os.close(fd)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate evidence authority field")
        result[key] = value
    return result


def _read_json(directory: int, name: str) -> dict[str, Any]:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        _check_file(os.fstat(fd))
        raw = os.read(fd, _MAX_RECORD + 1)
    finally:
        os.close(fd)
    if len(raw) > _MAX_RECORD:
        raise ValueError("evidence authority record exceeds bound")
    value = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise ValueError("invalid evidence authority record")
    return value


def _create_json(directory: int, name: str, value: dict[str, Any]) -> None:
    fd = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
    )
    with os.fdopen(fd, "wb") as stream:
        stream.write(json.dumps(value, sort_keys=True, allow_nan=False).encode())
        stream.flush()
        os.fsync(stream.fileno())


def _epoch(authority_id: str, counter: int) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "authority_id": authority_id,
        "counter": counter,
        "provider_epoch": f"{authority_id}_{counter:016x}",
    }


def provision_evidence_slot(record: Path, *, reader_gid: int | None = None) -> None:
    """Cold-provision lock/anchor/epoch only, never a qualification manifest.

    All participants must be stopped. Refuse any existing slot component rather
    than repair/recreate a lock. Interrupted provisioning requires offline review.
    The overlay owns parent creation, group access and persistent backups.
    """
    _validate_record(record)
    with _parent(record) as directory:
        names = [
            record.name,
            record.name + ".lock",
            record.name + ".identity.json",
            record.name + ".epoch.json",
        ]
        for name in names:
            try:
                os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise FileExistsError("evidence slot already exists; never replace its lock")
        lock_fd = os.open(
            names[1], os.O_RDONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
        )
        try:
            if reader_gid is not None:
                os.fchown(lock_fd, -1, reader_gid)
                os.fchmod(lock_fd, 0o640)
            lock_identity = _identity(os.fstat(lock_fd))
            os.fsync(lock_fd)
        finally:
            os.close(lock_fd)
        authority_id = secrets.token_hex(16)
        _create_json(
            directory,
            names[2],
            {
                "schema_version": 1,
                "record_name": record.name,
                "authority_id": authority_id,
                "directory": list(_identity(os.fstat(directory))),
                "lock": list(lock_identity),
            },
        )
        _create_json(directory, names[3], _epoch(authority_id, 0))
        os.fsync(directory)


@dataclass(frozen=True)
class _Slot:
    record: Path
    directory_identity: tuple[int, int]
    lock_identity: tuple[int, int]
    authority_id: str


def _pair(value: object) -> tuple[int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(type(item) is not int or item < 0 for item in value)
    ):
        raise ValueError("invalid evidence storage identity")
    return value[0], value[1]


def _load_slot(record: Path) -> _Slot:
    _validate_record(record)
    with _parent(record) as directory:
        data = _read_json(directory, record.name + ".identity.json")
        if (
            set(data) != {"schema_version", "record_name", "authority_id", "directory", "lock"}
            or type(data.get("schema_version")) is not int
            or data["schema_version"] != 1
            or data["record_name"] != record.name
            or not isinstance(data["authority_id"], str)
            or len(data["authority_id"]) != 32
            or any(c not in "0123456789abcdef" for c in data["authority_id"])
        ):
            raise ValueError("invalid evidence slot identity")
        slot = _Slot(record, _pair(data["directory"]), _pair(data["lock"]), data["authority_id"])
        if _identity(os.fstat(directory)) != slot.directory_identity:
            raise OSError("evidence directory identity changed")
        lock = os.stat(record.name + ".lock", dir_fd=directory, follow_symlinks=False)
        _check_file(lock)
        if _identity(lock) != slot.lock_identity:
            raise OSError("evidence lock identity changed")
        return slot


class FileEvidenceInvalidator:
    """All affected slots are locked before any withdrawal or systemd call."""

    def __init__(self, profiles: Sequence[RuntimeProfile]) -> None:
        records = [p.evidence_record for p in profiles if p.evidence_record is not None]
        reserved: set[Path] = set()
        for record in records:
            names = {
                record,
                Path(str(record) + ".lock"),
                Path(str(record) + ".identity.json"),
                Path(str(record) + ".epoch.json"),
            }
            if reserved & names:
                raise ValueError("runtime evidence slots overlap")
            reserved.update(names)
        self._slots = {record: _load_slot(record) for record in records}

    @contextmanager
    def hold(self, profiles: Sequence[RuntimeProfile], timeout: float) -> Iterator[None]:
        if not math.isfinite(timeout) or timeout <= 0:
            raise TransitionError("evidence lock timeout must be finite and positive")
        records = sorted({p.evidence_record for p in profiles if p.evidence_record is not None})
        deadline = time.monotonic() + timeout
        with ExitStack() as stack:
            opened: list[tuple[_Slot, int]] = []
            try:
                for record in records:
                    slot = self._slots[record]
                    directory = stack.enter_context(_parent(record))
                    if _identity(os.fstat(directory)) != slot.directory_identity:
                        raise OSError("evidence directory identity changed")
                    fd = os.open(
                        record.name + ".lock",
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=directory,
                    )
                    stack.callback(os.close, fd)
                    _check_file(os.fstat(fd))
                    if _identity(os.fstat(fd)) != slot.lock_identity:
                        raise OSError("evidence lock identity changed")
                    while True:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise TransitionError("runtime evidence is in use") from None
                            time.sleep(min(0.05, remaining))
                    current = os.stat(
                        record.name + ".lock", dir_fd=directory, follow_symlinks=False
                    )
                    if _identity(current) != slot.lock_identity:
                        raise OSError("evidence lock identity changed")
                    opened.append((slot, directory))
                for slot, _directory in opened:
                    if _load_slot(slot.record) != slot:
                        raise OSError("evidence slot identity changed")
                for slot, directory in opened:
                    self._withdraw(slot, directory)
            except (OSError, ValueError, KeyError) as exc:
                raise TransitionError("runtime evidence invalidation failed") from exc
            yield

    @staticmethod
    def _withdraw(slot: _Slot, directory: int) -> None:
        name = slot.record.name
        try:
            _check_file(os.stat(name, dir_fd=directory, follow_symlinks=False))
            os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass
        # Persist removal before touching epoch state or invoking the supervisor.
        os.fsync(directory)
        old = _read_json(directory, name + ".epoch.json")
        counter = old.get("counter")
        if (
            type(old.get("schema_version")) is not int
            or type(counter) is not int
            or not 0 <= counter < _MAX_COUNTER
            or old != _epoch(slot.authority_id, counter)
        ):
            raise ValueError("invalid or exhausted evidence epoch")
        temporary = name + ".epoch.tmp-" + secrets.token_hex(16)
        try:
            _create_json(directory, temporary, _epoch(slot.authority_id, counter + 1))
            os.replace(temporary, name + ".epoch.json", src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory)
