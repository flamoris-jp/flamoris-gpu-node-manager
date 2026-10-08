"""Updater uses the same RuntimeManager and host lock as every local adapter."""

from __future__ import annotations

from pathlib import Path

from flamoris_update_core.admission import CURRENT
from flamoris_update_core.errors import UpdateError
from flamoris_update_core.owner import ApplicationOwner, DomainState, OwnerConfiguration
from flamoris_update_core.owner_cli import serve
from flamoris_update_core.postgres import PostgresResource
from flamoris_update_core.resources import TreeResource
from flamoris_update_core.wire import digest, dumps

from .application.manager import RuntimeManager
from .bootstrap import build_manager
from .domain.models import RuntimeState
from .infrastructure.config import load_registry

SCHEMAS = {"configuration": "gpu-profiles-1", "evidence": "runtime-evidence-1"}


def manager(config: OwnerConfiguration) -> RuntimeManager:
    root = next(Path(binding.path) for binding in config.trees if binding.id == "configuration")
    settings = config.domain_configuration
    if set(settings) != {"lock_path"} or not isinstance(settings["lock_path"], str):
        raise UpdateError("invalid_profile")
    lock_path = Path(settings["lock_path"])
    if not lock_path.is_absolute() or lock_path.resolve() != lock_path:
        raise UpdateError("invalid_profile")
    token = CURRENT.set("owner-maintenance")
    try:
        return build_manager(root, lock_path=lock_path)
    finally:
        CURRENT.reset(token)


def inspect_domain(
    config: OwnerConfiguration, resources: dict[str, TreeResource | PostgresResource]
) -> DomainState:
    if set(resources) != set(SCHEMAS) or any(
        not isinstance(r, TreeResource) for r in resources.values()
    ):
        raise UpdateError("invalid_profile")
    configuration, evidence = resources["configuration"], resources["evidence"]
    assert isinstance(configuration, TreeResource) and isinstance(evidence, TreeResource)
    for runtime in load_registry(configuration.root):
        if runtime.evidence_record is not None and not runtime.evidence_record.is_relative_to(
            evidence.root
        ):
            raise UpdateError("invalid_profile")
    status = manager(config).reconstruct()
    uncertain = bool(status.anomalies) or any(
        r.state == RuntimeState.FAILED for r in status.runtimes
    )
    active = any(r.state != RuntimeState.OFF for r in status.runtimes)
    return DomainState(
        schemas=SCHEMAS,
        active_work=active,
        unknown_work=uncertain,
        configuration_digest=digest(dumps(configuration.inventory())),
    )


def quiesce(config: OwnerConfiguration) -> None:
    authority = manager(config)
    for runtime in authority.list_runtimes():
        result = authority.stop(runtime.id)
        if not any(
            status.runtime == runtime.id and status.state == RuntimeState.OFF for status in result
        ):
            raise UpdateError("recovery_required")


def factory(config: OwnerConfiguration) -> ApplicationOwner:
    return ApplicationOwner(config, "flamoris-gpu-node-manager", "1.2.0", inspect_domain, quiesce)


def main() -> None:
    serve(factory)
