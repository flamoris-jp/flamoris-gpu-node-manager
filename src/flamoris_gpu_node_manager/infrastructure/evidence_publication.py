"""Anchored evidence publication for reviewed trusted measurement/lifetime ports.

This module never discovers, starts, stops or certifies a runtime. Ports must
establish the complete protected closure and all-writer lifetime contract; absent
or partial instrumentation must raise rather than manufacture a manifest.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import urlsplit

from flamoris_gpu_node_manager.domain.errors import NodeManagerError
from flamoris_gpu_node_manager.infrastructure.runtime_evidence import (
    FileEvidenceInvalidator,
    _check_file,
    _epoch,
    _identity,
    _load_slot,
    _parent,
    _read_json,
    _unique_object,
)

_MAX_MANIFEST = 1024 * 1024
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class EvidencePublicationError(NodeManagerError):
    """Qualification is unavailable; public text never includes port metadata."""


class _RefreshCancelled(EvidencePublicationError):
    """Internal expected stop signal, distinct from an unavailable source."""


def _digest(value: object) -> None:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError("invalid measured content digest")


def _name(value: object) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 4096
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError("invalid measured identity name")


@dataclass(frozen=True)
class MeasuredNode:
    interface: str
    implementation: str

    def __post_init__(self) -> None:
        _digest(self.interface)
        _digest(self.implementation)


@dataclass(frozen=True)
class MeasuredManifest:
    core_identity: str
    dependency_identity: str
    config_identity: str
    nodes: Mapping[str, MeasuredNode]
    models: Mapping[str, str]

    def __post_init__(self) -> None:
        for value in (self.core_identity, self.dependency_identity, self.config_identity):
            _digest(value)
        nodes, models = dict(self.nodes), dict(self.models)
        if not nodes or not models or len(nodes) > 20_000 or len(models) > 20_000:
            raise ValueError("incomplete or excessive measured manifest")
        for name, node in nodes.items():
            _name(name)
            if type(node) is not MeasuredNode:
                raise ValueError("invalid measured node")
            _digest(node.interface)
            _digest(node.implementation)
        for name, digest in models.items():
            _name(name)
            kind, separator, model = name.partition(":")
            if not separator or not kind or not model or "/" in kind:
                raise ValueError("model identity must use kind:name")
            _digest(digest)
        object.__setattr__(self, "nodes", MappingProxyType(nodes))
        object.__setattr__(self, "models", MappingProxyType(models))
        if len(_canonical(self.value())) > _MAX_MANIFEST:
            raise ValueError("measured manifest exceeds bound")

    def value(self) -> dict[str, Any]:
        return {
            "core": self.core_identity,
            "dependencies": self.dependency_identity,
            "config": self.config_identity,
            "nodes": {
                name: {"interface": node.interface, "implementation": node.implementation}
                for name, node in self.nodes.items()
            },
            "models": dict(self.models),
        }


class ManifestSource(Protocol):
    def measure(self, *, deadline: float) -> MeasuredManifest:
        """Trusted measurement, with an absolute monotonic cooperative deadline."""


class LifetimeFence(Protocol):
    def observe(self) -> str:
        """Return a known/alive exact lifetime token, or raise when unavailable."""


@dataclass(frozen=True)
class PublishedEvidence:
    provider_epoch: str
    manifest_digest: str
    expires_at: float


@dataclass(frozen=True)
class _Publication:
    public: PublishedEvidence
    lifetime: str
    manifest: bytes
    monotonic_expiry: float


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _positive(value: float, maximum: float) -> None:
    if type(value) not in {int, float} or not math.isfinite(value) or not 0 < value <= maximum:
        raise ValueError("invalid evidence publication bound")


class EvidencePublicationLease:
    """One-shot mutation/publication capability inside an anchored lock context."""

    def __init__(self, owner: FileEvidencePublisher, directory: int) -> None:
        self._owner = owner
        self._directory = directory
        self._epoch: str | None = None
        self._thread = threading.get_ident()
        self._active = True
        self._used = False

    def begin_mutation(self) -> None:
        if not self._active or self._epoch is not None or self._thread != threading.get_ident():
            raise EvidencePublicationError("runtime evidence publication unavailable")
        self._owner._invalidate(self._directory)
        try:
            self._epoch = self._owner._current_epoch(self._directory)
        except Exception as exc:
            raise EvidencePublicationError("runtime evidence publication unavailable") from exc

    def publish(self) -> PublishedEvidence:
        if (
            not self._active
            or self._used
            or self._epoch is None
            or self._thread != threading.get_ident()
        ):
            raise EvidencePublicationError("runtime evidence publication unavailable")
        self._used = True
        return self._owner._publish(self._directory, self._epoch)


class FileEvidencePublisher:
    """Use the invalidator's exact anchored flock/epoch, never a new authority.

    Ports are installed by trusted Python composition. There is intentionally no
    CLI, remote manifest parameter, record import or best-effort fallback source.
    Source/lifetime callbacks need a deployment watchdog for stalled OS calls.
    """

    def __init__(
        self,
        record: Path,
        provider_url: str,
        source: ManifestSource,
        lifetime: LifetimeFence,
        *,
        ttl: float = 120.0,
        measurement_timeout: float = 900.0,
    ) -> None:
        _positive(ttl, 300)
        _positive(measurement_timeout, 3600)
        if not isinstance(provider_url, str):
            raise ValueError("invalid exact provider URL")
        try:
            parsed = urlsplit(provider_url)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("invalid exact provider URL") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.netloc.endswith(":")
            or "%" in parsed.netloc
            or "\\" in provider_url
            or (port is not None and not 1 <= port <= 65535)
            or re.search(r"%(?![0-9a-fA-F]{2})", provider_url)
            or provider_url != provider_url.rstrip("/")
            or len(provider_url) > 4096
            or any(ord(c) <= 32 or ord(c) == 127 for c in provider_url)
        ):
            raise ValueError("invalid exact provider URL")
        self._slot = _load_slot(record)
        self._provider_url = provider_url
        self._source = source
        self._lifetime = lifetime
        self._ttl = float(ttl)
        self._measurement_timeout = float(measurement_timeout)
        self._mutex = threading.Lock()
        self._publication: _Publication | None = None
        self._refresh_owner: AutomaticEvidenceRefresh | None = None

    @property
    def record(self) -> Path:
        return self._slot.record

    @contextmanager
    def _exclusive(self, timeout: float) -> Iterator[int]:
        _positive(timeout, 3600)
        deadline = time.monotonic() + timeout
        if not self._mutex.acquire(timeout=timeout):
            raise EvidencePublicationError("runtime evidence publication unavailable")
        entered = False
        try:
            with _parent(self._slot.record) as directory:
                self._check_anchor(directory)
                fd = os.open(
                    self._slot.record.name + ".lock",
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory,
                )
                try:
                    _check_file(os.fstat(fd))
                    if _identity(os.fstat(fd)) != self._slot.lock_identity:
                        raise OSError("evidence lock identity changed")
                    while True:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("runtime evidence lock deadline")
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise TimeoutError("runtime evidence is in use") from None
                            time.sleep(min(0.05, remaining))
                    self._check_anchor(directory)
                    if time.monotonic() >= deadline:
                        raise TimeoutError("runtime evidence lock deadline")
                    entered = True
                    yield directory
                finally:
                    os.close(fd)
        except EvidencePublicationError:
            raise
        except Exception as exc:
            if entered:
                raise
            raise EvidencePublicationError("runtime evidence publication unavailable") from exc
        finally:
            self._mutex.release()

    def _check_anchor(self, directory: int) -> None:
        if (
            _identity(os.fstat(directory)) != self._slot.directory_identity
            or _load_slot(self._slot.record) != self._slot
        ):
            raise OSError("evidence slot identity changed")

    def _current_epoch(self, directory: int) -> str:
        value = _read_json(directory, self._slot.record.name + ".epoch.json")
        counter = value.get("counter")
        if (
            type(value.get("schema_version")) is not int
            or type(counter) is not int
            or not 0 <= counter < 2**64
            or value != _epoch(self._slot.authority_id, counter)
        ):
            raise ValueError("invalid evidence epoch")
        return str(value["provider_epoch"])

    def _invalidate(self, directory: int) -> None:
        self._publication = None
        try:
            self._check_anchor(directory)
            FileEvidenceInvalidator._withdraw(self._slot, directory)
        except Exception as exc:
            raise EvidencePublicationError("runtime evidence publication unavailable") from exc

    @contextmanager
    def locked(self, *, timeout: float = 5.0) -> Iterator[EvidencePublicationLease]:
        """Acquire/revalidate only; a coordinator may lock all slots before begin."""
        with self._exclusive(timeout) as directory:
            lease = EvidencePublicationLease(self, directory)
            try:
                yield lease
                current = self._publication
                if current is not None and current.public.provider_epoch == lease._epoch:
                    try:
                        self._observe(current.lifetime)
                        if (
                            self._current_epoch(directory) != lease._epoch
                            or time.time() >= current.public.expires_at
                            or time.monotonic() >= current.monotonic_expiry
                            or self._read_record(directory)
                            != self._record_value(
                                current.public.provider_epoch,
                                current.manifest,
                                current.public.expires_at,
                            )
                        ):
                            raise ValueError("evidence changed before mutation guard release")
                        self._check_anchor(directory)
                    except Exception as exc:
                        raise EvidencePublicationError(
                            "runtime evidence publication unavailable"
                        ) from exc
            except BaseException:
                # Even a failure after successful publication removes that result
                # before releasing the exclusive guard. Never restore old records.
                if lease._epoch is not None:
                    self._invalidate(directory)
                raise
            finally:
                lease._active = False

    @contextmanager
    def mutation(self, *, timeout: float = 5.0) -> Iterator[EvidencePublicationLease]:
        """Withdraw/allocate before body, retaining the exclusive flock through it."""
        with self.locked(timeout=timeout) as lease:
            lease.begin_mutation()
            yield lease

    def issue(self, *, timeout: float = 5.0) -> PublishedEvidence:
        with self.mutation(timeout=timeout) as lease:
            return lease.publish()

    def _observe(self, expected: str | None = None) -> str:
        token = self._lifetime.observe()
        _name(token)
        if expected is not None and token != expected:
            raise ValueError("runtime lifetime changed")
        return token

    def _record_value(self, epoch: str, manifest: bytes, expires: float) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "continuity": "exclusive-mutation-lock-v1",
            "provider_url": self._provider_url,
            "provider_epoch": epoch,
            "expires_at": expires,
            "manifest": json.loads(manifest),
        }

    def _write(self, directory: int, value: dict[str, Any]) -> None:
        self._check_anchor(directory)
        raw = _canonical(value)
        if len(raw) > _MAX_MANIFEST:
            raise ValueError("evidence record exceeds bound")
        name = self._slot.record.name
        with suppress(FileNotFoundError):
            _check_file(os.stat(name, dir_fd=directory, follow_symlinks=False))
        lock = os.stat(name + ".lock", dir_fd=directory, follow_symlinks=False)
        temporary = name + ".manifest.tmp-" + secrets.token_hex(16)
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            with os.fdopen(fd, "wb") as stream:
                if lock.st_mode & 0o040:
                    os.fchown(stream.fileno(), -1, lock.st_gid)
                    os.fchmod(stream.fileno(), 0o640)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            self._check_anchor(directory)
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory)

    def _publish(
        self,
        directory: int,
        epoch: str,
        previous: _Publication | None = None,
        cancel: threading.Event | None = None,
    ) -> PublishedEvidence:
        try:
            started = time.monotonic()
            deadline = started + self._measurement_timeout
            if previous is not None:
                deadline = min(deadline, previous.monotonic_expiry)
            lifetime = self._observe(previous.lifetime if previous else None)
            measured = self._source.measure(deadline=deadline)
            if type(measured) is not MeasuredManifest:
                raise ValueError("trusted source returned no measured manifest")
            manifest = _canonical(measured.value())
            if previous is not None and manifest != previous.manifest:
                raise ValueError("runtime manifest changed without a new epoch")
            self._observe(lifetime)
            if time.monotonic() >= deadline:
                raise ValueError("evidence measurement deadline")
            if cancel is not None and cancel.is_set():
                raise _RefreshCancelled("runtime evidence publication unavailable")
            self._check_anchor(directory)
            if self._current_epoch(directory) != epoch:
                raise ValueError("evidence epoch changed")
            issued_monotonic = time.monotonic()
            expires = time.time() + self._ttl
            result = PublishedEvidence(
                epoch, "sha256:" + hashlib.sha256(manifest).hexdigest(), expires
            )
            self._write(directory, self._record_value(epoch, manifest, expires))
            self._observe(lifetime)
            self._check_anchor(directory)
            if self._current_epoch(directory) != epoch:
                raise ValueError("evidence epoch changed after publication")
            if (
                time.monotonic() >= deadline
                or time.monotonic() >= issued_monotonic + self._ttl
                or time.time() >= expires
            ):
                raise ValueError("evidence publication deadline")
            if cancel is not None and cancel.is_set():
                raise _RefreshCancelled("runtime evidence publication unavailable")
            self._publication = _Publication(
                result, lifetime, manifest, issued_monotonic + self._ttl
            )
            return result
        except _RefreshCancelled:
            self._invalidate(directory)
            raise
        except Exception as exc:
            self._invalidate(directory)
            raise EvidencePublicationError("runtime evidence publication unavailable") from exc

    def _read_record(self, directory: int) -> dict[str, Any]:
        fd = os.open(
            self._slot.record.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        try:
            _check_file(os.fstat(fd))
            raw = os.read(fd, _MAX_MANIFEST + 1)
        finally:
            os.close(fd)
        if len(raw) > _MAX_MANIFEST:
            raise ValueError("evidence record exceeds bound")
        value = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(value, dict):
            raise ValueError("invalid evidence record")
        return value

    def refresh(self, *, timeout: float = 5.0) -> PublishedEvidence:
        return self._refresh(timeout=timeout)

    def _refresh(
        self,
        *,
        timeout: float,
        cancel: threading.Event | None = None,
        expected_epoch: str | None = None,
    ) -> PublishedEvidence:
        with self._exclusive(timeout) as directory:
            previous = self._publication
            if previous is None:
                raise EvidencePublicationError("runtime evidence publication unavailable")
            if expected_epoch is not None and previous.public.provider_epoch != expected_epoch:
                raise EvidencePublicationError("runtime evidence publication unavailable")
            if cancel is not None and cancel.is_set():
                raise _RefreshCancelled("runtime evidence publication unavailable")
            # A different publisher/managed transition owns the later epoch.
            # Never erase that owner's record from a stale refresh session.
            try:
                current_epoch = self._current_epoch(directory)
            except Exception as exc:
                self._invalidate(directory)
                raise EvidencePublicationError("runtime evidence publication unavailable") from exc
            if current_epoch != previous.public.provider_epoch:
                self._publication = None
                raise EvidencePublicationError("runtime evidence publication unavailable")
            try:
                if (
                    time.time() >= previous.public.expires_at
                    or time.monotonic() >= previous.monotonic_expiry
                    or self._read_record(directory)
                    != self._record_value(
                        previous.public.provider_epoch,
                        previous.manifest,
                        previous.public.expires_at,
                    )
                ):
                    raise ValueError("evidence expired or record changed")
            except Exception as exc:
                self._invalidate(directory)
                raise EvidencePublicationError("runtime evidence publication unavailable") from exc
            return self._publish(directory, previous.public.provider_epoch, previous, cancel)

    def withdraw(self, *, timeout: float = 5.0) -> None:
        """Withdraw this instance's still-current result; never erase a new owner."""
        self._withdraw(timeout=timeout)

    def _withdraw(self, *, timeout: float, expected_epoch: str | None = None) -> None:
        with self._exclusive(timeout) as directory:
            previous = self._publication
            if (
                expected_epoch is not None
                and previous is not None
                and previous.public.provider_epoch != expected_epoch
            ):
                return
            self._publication = None
            if previous is not None:
                try:
                    current_epoch = self._current_epoch(directory)
                except Exception as exc:
                    self._invalidate(directory)
                    raise EvidencePublicationError(
                        "runtime evidence publication unavailable"
                    ) from exc
                if current_epoch == previous.public.provider_epoch:
                    self._invalidate(directory)

    @contextmanager
    def automatic_refresh(
        self, *, interval: float = 30.0, timeout: float = 5.0
    ) -> Iterator[AutomaticEvidenceRefresh]:
        """Refresh issued evidence; terminate unavailable on any error or close."""
        _positive(interval, self._ttl)
        _positive(timeout, 3600)
        if not self._mutex.acquire(timeout=timeout):
            raise EvidencePublicationError("runtime evidence publication unavailable")
        try:
            if (
                interval >= self._ttl
                or self._publication is None
                or self._refresh_owner is not None
            ):
                raise ValueError("automatic refresh needs one owner and live evidence below TTL")
            worker = AutomaticEvidenceRefresh(
                self, interval, timeout, self._publication.public.provider_epoch
            )
            self._refresh_owner = worker
        finally:
            self._mutex.release()
        try:
            worker.start()
        except Exception as exc:
            worker._stop.set()
            if worker._thread.is_alive():
                worker._thread.join(timeout)
            self._refresh_owner = None
            with suppress(EvidencePublicationError):
                self._withdraw(timeout=timeout, expected_epoch=worker._epoch)
            raise EvidencePublicationError("runtime evidence publication unavailable") from exc
        try:
            yield worker
        finally:
            try:
                worker.close()
            finally:
                self._refresh_owner = None


class AutomaticEvidenceRefresh:
    """No runtime authority: an owned worker only renews the existing identity."""

    def __init__(
        self, publisher: FileEvidencePublisher, interval: float, timeout: float, epoch: str
    ) -> None:
        self._publisher = publisher
        self._interval = interval
        self._timeout = timeout
        self._epoch = epoch
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.failure: EvidencePublicationError | None = None

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._publisher._refresh(
                    timeout=self._timeout, cancel=self._stop, expected_epoch=self._epoch
                )
            except _RefreshCancelled:
                return
            except Exception:
                self.failure = EvidencePublicationError("runtime evidence publication unavailable")
                with suppress(EvidencePublicationError):
                    self._publisher._withdraw(timeout=self._timeout, expected_epoch=self._epoch)
                return

    def close(self) -> None:
        self._stop.set()
        self._thread.join(self._timeout)
        try:
            self._publisher._withdraw(timeout=self._timeout, expected_epoch=self._epoch)
        except EvidencePublicationError:
            self.failure = EvidencePublicationError("runtime evidence publication unavailable")
        if self._thread.is_alive() or self.failure is not None:
            raise EvidencePublicationError("runtime evidence publication unavailable")
