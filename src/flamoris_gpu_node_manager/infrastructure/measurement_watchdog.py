"""Authority-side spawned measurement, bounded JSON return and owned-worker kill."""

from __future__ import annotations

import json
import math
import multiprocessing
import os
import select
import struct
import time
from multiprocessing.connection import Connection

from .evidence_publication import (
    EvidencePublicationError,
    ManifestSource,
    MeasuredManifest,
    MeasuredNode,
)
from .observation_transport import _unique

_MAX_MANIFEST = 1024 * 1024


def _measure_worker(source: ManifestSource, deadline: float, channel: Connection) -> None:
    try:
        manifest = source.measure(deadline=deadline)
        if type(manifest) is not MeasuredManifest:
            raise ValueError("unsupported measurement result")
        data = json.dumps(manifest.value(), separators=(",", ":"), allow_nan=False).encode()
        if len(data) > _MAX_MANIFEST:
            raise ValueError("measurement result exceeds bound")
        pending = memoryview(struct.pack("!I", len(data)) + data)
        while pending:
            pending = pending[os.write(channel.fileno(), pending) :]
    except Exception:
        # The parent receives EOF, never private paths, exception text or pickle.
        pass
    finally:
        channel.close()


def _read_exact(fd: int, count: int, deadline: float) -> bytes:
    data = bytearray()
    while len(data) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise TimeoutError()
        block = os.read(fd, min(count - len(data), 65536))
        if not block:
            raise ValueError("truncated measurement response")
        data.extend(block)
    return bytes(data)


class SpawnedManifestSource:
    """Wrap a trusted spawn-safe source in the non-GPU authority process.

    Python composition, not a command/configuration API. Source constructors must
    be inert and pickle-safe; only trusted parent inputs use spawn serialization.
    Worker responses are bounded JSON. Never fork a live GPU provider.
    """

    def __init__(self, source: ManifestSource) -> None:
        self.source = source

    def measure(self, *, deadline: float) -> MeasuredManifest:
        remaining = deadline - time.monotonic()
        if not math.isfinite(remaining) or remaining <= 0 or remaining > 3600:
            raise EvidencePublicationError("runtime measurement unavailable")
        context = multiprocessing.get_context("spawn")
        reader, writer = context.Pipe(duplex=False)
        worker = context.Process(
            target=_measure_worker,
            args=(self.source, deadline, writer),
            daemon=True,
            name="runtime-evidence-measurement",
        )
        started = False
        try:
            worker.start()
            started = True
            writer.close()
            # Deadline applies to every frame fragment, including a partial body.
            size = struct.unpack("!I", _read_exact(reader.fileno(), 4, deadline))[0]
            if not 0 < size <= _MAX_MANIFEST:
                raise ValueError("measurement response exceeds bound")
            data = _read_exact(reader.fileno(), size, deadline)
            if time.monotonic() >= deadline:
                raise TimeoutError()
            value = json.loads(data, object_pairs_hook=_unique)
            if (
                type(value) is not dict
                or set(value)
                != {
                    "core",
                    "dependencies",
                    "config",
                    "nodes",
                    "models",
                }
                or type(value["nodes"]) is not dict
                or type(value["models"]) is not dict
            ):
                raise ValueError("unsupported measurement response")
            nodes = {}
            for name, node in value["nodes"].items():
                if type(node) is not dict or set(node) != {"interface", "implementation"}:
                    raise ValueError("unsupported measurement response")
                nodes[name] = MeasuredNode(node["interface"], node["implementation"])
            result = MeasuredManifest(
                value["core"], value["dependencies"], value["config"], nodes, value["models"]
            )
            if time.monotonic() >= deadline:
                raise TimeoutError()
            return result
        except Exception as error:
            raise EvidencePublicationError("runtime measurement unavailable") from error
        finally:
            reader.close()
            writer.close()
            if started:
                # Signal only our unreaped child handle; never target the provider.
                if worker.is_alive():
                    worker.kill()
                worker.join(timeout=1.0)
                if worker.is_alive():
                    raise EvidencePublicationError("runtime measurement unavailable")
                worker.close()
