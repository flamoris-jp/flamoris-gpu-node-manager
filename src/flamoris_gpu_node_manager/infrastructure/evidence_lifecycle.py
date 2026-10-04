"""Join trusted publication to the existing RuntimeManager mutation boundary.

This opt-in port requires reviewed runtime-owned measurement/lifetime bindings.
It does not discover them, install hooks, or cover external systemd/writer paths.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..domain.errors import TransitionError
from ..domain.models import RuntimeProfile
from .evidence_publication import FileEvidencePublisher


@dataclass(frozen=True)
class EvidencePublicationBinding:
    profile: RuntimeProfile
    publisher: FileEvidencePublisher
    eligible: Callable[[], bool]


class RuntimeEvidenceLifecycle:
    """EvidenceInvalidationPort with publication after successful transitions.

    RuntimeManager still owns service selection, release/start/health and the
    host-wide lock. All slot locks are acquired before withdrawal; publication
    uses their existing leases, never a recursively acquired startup flock.
    The trusted eligibility port reports actual active/initialized/healthy state,
    not a persisted Manager status or an end-user permission. Stopped runtimes
    leave evidence absent. A long-lived trusted owner separately runs renewal.
    """

    def __init__(self, bindings: Sequence[EvidencePublicationBinding]) -> None:
        copied = tuple(bindings)
        self._bindings = {binding.profile.id: binding for binding in copied}
        records = [binding.profile.evidence_record for binding in copied]
        reserved: set[Path] = set()
        overlap = False
        for record in records:
            if record is None:
                continue
            names = {
                record,
                *(
                    type(record)(str(record) + suffix)
                    for suffix in (".lock", ".identity.json", ".epoch.json")
                ),
            }
            overlap = overlap or bool(reserved & names)
            reserved.update(names)
        if (
            len(copied) != len(self._bindings)
            or any(record is None for record in records)
            or len(records) != len(set(records))
            or overlap
            or any(
                binding.publisher.record != binding.profile.evidence_record for binding in copied
            )
        ):
            raise ValueError("runtime evidence publication bindings are inconsistent")

    @contextmanager
    def hold(self, profiles: Sequence[RuntimeProfile], timeout: float) -> Iterator[None]:
        if type(timeout) not in {float, int} or not math.isfinite(timeout) or timeout <= 0:
            raise TransitionError("runtime evidence lifecycle unavailable")
        selected: dict[str, EvidencePublicationBinding] = {}
        for profile in profiles:
            if profile.evidence_record is None:
                continue
            binding = self._bindings.get(profile.id)
            if binding is None or binding.profile != profile:
                raise TransitionError("runtime evidence lifecycle unavailable")
            selected[profile.id] = binding
        ordered = sorted(
            selected.values(), key=lambda binding: str(binding.profile.evidence_record)
        )
        deadline = time.monotonic() + timeout
        with ExitStack() as stack:
            try:
                leases = []
                for binding in ordered:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("evidence slot lock budget exhausted")
                    lease = stack.enter_context(binding.publisher.locked(timeout=remaining))
                    leases.append((binding, lease))
                for _binding, lease in leases:
                    lease.begin_mutation()
            except Exception as error:
                raise TransitionError("runtime evidence lifecycle unavailable") from error
            # Caller exceptions reach every lease; no prior record is restored.
            yield
            try:
                for binding, lease in leases:
                    eligible = binding.eligible()
                    if type(eligible) is not bool:
                        raise ValueError("unknown runtime publication eligibility")
                    if eligible:
                        lease.publish()
            except Exception as error:
                # ExitStack withdraws even successfully published earlier slots.
                raise TransitionError("runtime evidence lifecycle unavailable") from error
