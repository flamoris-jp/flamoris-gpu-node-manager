"""Bounded byte measurement of protected installation trees, not an attestation.

This primitive does not discover a runtime's effective closure, cover lifecycle
changes, publish records, or grant Workflow readiness. Its caller must be a
separate trusted mutation identity and hold the existing exclusive evidence lock.
Every trusted administrator/writer must follow that same mutation protocol.
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import stat
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from flamoris_gpu_node_manager.domain.errors import NodeManagerError


class ContentMeasurementError(NodeManagerError):
    """No usable content measurement could be established."""


class _Digest(Protocol):
    def update(self, data: bytes) -> None: ...


@dataclass(frozen=True)
class MeasuredContent:
    """Partial content identities; intentionally has no nodes, epoch or expiry."""

    groups: Mapping[str, str]
    models: Mapping[str, str]
    entries: int
    bytes_read: int


@dataclass(frozen=True)
class MeasurementLimits:
    max_entries: int = 100_000
    max_bytes: int = 64 * 1024**3
    timeout: float = 900.0

    def __post_init__(self) -> None:
        if (
            type(self.max_entries) is not int
            or not 1 <= self.max_entries <= 1_000_000
            or type(self.max_bytes) is not int
            or not 1 <= self.max_bytes <= 1024**4
            or type(self.timeout) not in {int, float}
            or not 0 < self.timeout <= 3600
            or not math.isfinite(self.timeout)
        ):
            raise ValueError("invalid content measurement bounds")


def _name(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 4096
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError("invalid content identity name")


def _path(path: Path) -> None:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError("content paths must be absolute")
    _name(str(path))
    if str(path) != os.path.normpath(path) or ".." in path.parts:
        raise ValueError("content paths must be normalized")


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


class ProtectedContentMeasurer:
    """Measure all bytes under explicit roots without imports or exclusions.

    Paths and model identities come from trusted runtime/overlay instrumentation,
    never a client-authored assertion of closure. Permission checks are separate
    from that still-required completeness/lifecycle contract. Group/world write,
    extended ACLs, symlinks, hard-linked files and special files are unsupported.
    """

    def __init__(self, *, runtime_uid: int, limits: MeasurementLimits | None = None) -> None:
        self._authority_uid = os.geteuid()
        if (
            type(runtime_uid) is not int
            or not 0 <= runtime_uid < 2**32 - 1
            or runtime_uid in {0, self._authority_uid}
        ):
            raise ValueError("runtime and trusted measurement identities must be separate")
        self._limits = limits or MeasurementLimits()

    def measure(
        self, groups: Mapping[str, Mapping[str, Path]], models: Mapping[str, Path]
    ) -> MeasuredContent:
        """Return content only. Any unsafe/missing/changing input rejects all results.

        Exactly core/dependencies/config are required; each has nonempty labeled
        roots. A root may be a directory or one regular file. Model keys contain
        kind/name; models must be nonempty regular files. Limits include repeated
        roots across groups/models. Deadlines are cooperative; the caller needs a
        subprocess watchdog for a hard wall-clock bound on stalled storage.
        """
        try:
            if os.geteuid() != self._authority_uid:
                raise ValueError("measurement authority identity changed")
            if set(groups) != {"core", "dependencies", "config"} or not models:
                raise ValueError("content groups or model identities are incomplete")
            # Copy inputs before inspection; callers cannot change the plan while
            # a measurement is in progress. No externally supplied digests exist.
            plan = {group: dict(roots) for group, roots in groups.items()}
            selected_models = dict(models)
            for roots in plan.values():
                if not roots or len(roots) > self._limits.max_entries:
                    raise ValueError("empty content group")
                for label, path in roots.items():
                    _name(label)
                    _path(path)
            for label, path in selected_models.items():
                _name(label)
                if "/" not in label or any(part in {"", ".", ".."} for part in label.split("/")):
                    raise ValueError("model identity must contain kind/name")
                _path(path)
            if len(selected_models) > self._limits.max_entries:
                raise ValueError("too many model identities")
            run = _MeasurementRun(self._authority_uid, self._limits)
            identities = {group: run.group(roots) for group, roots in sorted(plan.items())}
            model_hashes = {
                label: run.model(path) for label, path in sorted(selected_models.items())
            }
            run.recheck()
            return MeasuredContent(
                MappingProxyType(identities),
                MappingProxyType(model_hashes),
                run.entries,
                run.bytes_read,
            )
        except (OSError, ValueError, UnicodeError, RuntimeError) as exc:
            # Paths, source, config and model bytes must not enter presentation.
            raise ContentMeasurementError("protected content measurement unavailable") from exc


class _MeasurementRun:
    def __init__(self, authority_uid: int, limits: MeasurementLimits) -> None:
        self.owners = {0, authority_uid}
        self.limits = limits
        self.deadline = time.monotonic() + limits.timeout
        self.entries = 0
        self.bytes_read = 0
        self.observed: dict[Path, tuple[int, ...]] = {}
        self.anchors: dict[Path, tuple[int, ...]] = {}

    def tick(self) -> None:
        if time.monotonic() >= self.deadline:
            raise ValueError("content measurement deadline exceeded")

    def count(self) -> None:
        self.tick()
        self.entries += 1
        if self.entries > self.limits.max_entries:
            raise ValueError("content inventory exceeds entry bound")

    def check(self, fd: int, *, sticky_ancestor: bool = False) -> os.stat_result:
        self.tick()
        info = os.fstat(fd)
        directory = stat.S_ISDIR(info.st_mode)
        if not directory and not stat.S_ISREG(info.st_mode):
            raise ValueError("unsupported content file type")
        if info.st_uid not in self.owners or (not directory and info.st_nlink != 1):
            raise ValueError("untrusted content ownership or hard link")
        sticky = sticky_ancestor and directory and info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        if info.st_mode & 0o022 and not sticky:
            raise ValueError("content or parent is writable by unmanaged principals")
        for attribute in ("system.posix_acl_access", "system.posix_acl_default"):
            try:
                os.getxattr(fd, attribute)
            except OSError as error:
                if error.errno != errno.ENODATA:
                    raise
            else:
                raise ValueError("extended content ACL requires explicit support")
        return info

    def remember(self, path: Path, info: os.stat_result) -> None:
        stamp = _stamp(info)
        prior = self.observed.setdefault(path, stamp)
        if prior != stamp:
            raise ValueError("content changed across measurement groups")

    def anchor(self, path: Path, info: os.stat_result) -> None:
        # An ancestor's unrelated directory entries/mtime are not runtime content.
        # Root-owned sticky ancestors still protect the caller-owned next component.
        stamp = (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)
        if self.anchors.setdefault(path, stamp) != stamp:
            raise ValueError("content ancestry identity or permissions changed")

    @contextmanager
    def open(self, path: Path) -> Iterator[int]:
        self.tick()
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        current = Path("/")
        try:
            self.check(fd, sticky_ancestor=path != current)
            self.anchor(current, os.fstat(fd))
            for index, part in enumerate(path.parts[1:]):
                self.tick()
                final = index == len(path.parts) - 2
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if not final:
                    flags |= os.O_DIRECTORY
                next_fd = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = next_fd
                current /= part
                self.check(fd, sticky_ancestor=not final)
                self.anchor(current, os.fstat(fd))
            yield fd
        finally:
            os.close(fd)

    def file(self, fd: int, path: Path) -> str:
        self.count()
        before = self.check(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("model or content must be a regular file")
        self.remember(path, before)
        if self.bytes_read + before.st_size > self.limits.max_bytes:
            raise ValueError("content exceeds byte bound")
        digest = hashlib.sha256()
        read = 0
        while True:
            self.tick()
            chunk = os.read(fd, min(1024**2, self.limits.max_bytes - self.bytes_read + 1))
            self.tick()
            if not chunk:
                break
            read += len(chunk)
            self.bytes_read += len(chunk)
            if self.bytes_read > self.limits.max_bytes or read > before.st_size:
                raise ValueError("content grew or exceeds byte bound")
            digest.update(chunk)
        if read != before.st_size or _stamp(self.check(fd)) != _stamp(before):
            raise ValueError("content changed during read")
        return "sha256:" + digest.hexdigest()

    def tree(self, fd: int, path: Path, digest: _Digest, *, depth: int = 0) -> None:
        before = self.check(fd)
        self.remember(path, before)
        if depth > 64:
            raise ValueError("content tree exceeds depth bound")
        if stat.S_ISREG(before.st_mode):
            self.frame(digest, ["file", self.file(fd, path)])
            return
        self.count()
        names: list[str] = []
        with os.scandir(fd) as entries:
            for entry in entries:
                self.tick()
                _name(entry.name)
                names.append(entry.name)
                if len(names) + self.entries > self.limits.max_entries:
                    raise ValueError("content inventory exceeds entry bound")
        self.frame(digest, ["directory", sorted(names)])
        for name in sorted(names):
            self.tick()
            child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                self.frame(digest, ["entry", name])
                self.tree(child, path / name, digest, depth=depth + 1)
            finally:
                os.close(child)
        if _stamp(self.check(fd)) != _stamp(before):
            raise ValueError("content tree changed during traversal")

    @staticmethod
    def frame(digest: _Digest, value: object) -> None:
        raw = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()
        digest.update(len(raw).to_bytes(8, "big") + raw)

    def group(self, roots: Mapping[str, Path]) -> str:
        digest = hashlib.sha256()
        self.frame(digest, ["protected-content-tree", 1])
        for label, path in sorted(roots.items()):
            self.frame(digest, ["root", label])
            with self.open(path) as fd:
                self.tree(fd, path, digest)
        return "sha256:" + digest.hexdigest()

    def model(self, path: Path) -> str:
        with self.open(path) as fd:
            if not os.fstat(fd).st_size:
                raise ValueError("empty model content")
            return self.file(fd, path)

    def recheck(self) -> None:
        # Reopen through protected components: never trust a previously opened
        # inode that was renamed out of the current runtime path. No hash cache.
        for path, stamp in list(self.observed.items()):
            with self.open(path) as fd:
                if _stamp(self.check(fd)) != stamp:
                    raise ValueError("content identity changed after measurement")
        self.tick()
