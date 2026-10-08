from types import SimpleNamespace

import pytest
from flamoris_update_core.errors import UpdateError
from flamoris_update_core.resources import TreeBinding, TreeResource

from flamoris_gpu_node_manager import updater
from flamoris_gpu_node_manager.domain.models import RuntimeState


def tree(tmp_path, name):
    path = tmp_path / name
    path.mkdir()
    return TreeResource(TreeBinding(id=name, path=str(path), max_files=20, max_bytes=4096))


def test_owner_manager_uses_the_deployment_shared_lock(tmp_path, monkeypatch):
    configuration = tmp_path / "configuration"
    configuration.mkdir()
    lock_path = tmp_path / "transition.lock"
    lock_path.touch()
    config = SimpleNamespace(
        trees=[SimpleNamespace(id="configuration", path=str(configuration))],
        domain_configuration={"lock_path": str(lock_path)},
    )
    calls = []
    authority = object()
    monkeypatch.setattr(
        updater,
        "build_manager",
        lambda root, *, lock_path: calls.append((root, lock_path)) or authority,
    )
    assert updater.manager(config) is authority
    assert calls == [(configuration, lock_path)]


@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"lock_path": "relative/transition.lock"},
        {"lock_path": 42},
        {"lock_path": "/run/manager/transition.lock", "other": True},
    ],
)
def test_owner_manager_rejects_ambiguous_lock_configuration(tmp_path, settings):
    configuration = tmp_path / "configuration"
    configuration.mkdir()
    config = SimpleNamespace(
        trees=[SimpleNamespace(id="configuration", path=str(configuration))],
        domain_configuration=settings,
    )
    with pytest.raises(UpdateError) as caught:
        updater.manager(config)
    assert caught.value.code == "invalid_profile"


@pytest.mark.parametrize(
    "runtime_state,anomalies,active,unknown",
    [
        (RuntimeState.OFF, (), False, False),
        (RuntimeState.READY, (), True, False),
        (RuntimeState.FAILED, (), True, True),
        (RuntimeState.OFF, ("inspection-failed",), False, True),
    ],
)
def test_owner_uses_existing_authority_and_preserves_failed_state(
    tmp_path, monkeypatch, runtime_state, anomalies, active, unknown
):
    resources = {name: tree(tmp_path, name) for name in updater.SCHEMAS}
    status = SimpleNamespace(runtimes=(SimpleNamespace(state=runtime_state),), anomalies=anomalies)
    authority = SimpleNamespace(reconstruct=lambda: status)
    monkeypatch.setattr(updater, "manager", lambda config: authority)
    monkeypatch.setattr(updater, "load_registry", lambda root: ())
    result = updater.inspect_domain(None, resources)
    assert result.active_work is active
    assert result.unknown_work is unknown
    assert status.runtimes[0].state == runtime_state


def test_quiesce_requires_confirmed_off_from_existing_manager(monkeypatch):
    calls = []
    authority = SimpleNamespace(
        list_runtimes=lambda: [SimpleNamespace(id="synthetic")],
        stop=lambda runtime: (
            calls.append(runtime) or (SimpleNamespace(runtime=runtime, state=RuntimeState.FAILED),)
        ),
    )
    monkeypatch.setattr(updater, "manager", lambda config: authority)
    with pytest.raises(UpdateError):
        updater.quiesce(None)
    assert calls == ["synthetic"]
