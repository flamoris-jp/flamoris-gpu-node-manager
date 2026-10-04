from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from flamoris_gpu_node_manager.bootstrap import build_manager
from flamoris_gpu_node_manager.domain.errors import ProfileValidationError
from flamoris_gpu_node_manager.infrastructure.config import load_registry
from flamoris_gpu_node_manager.infrastructure.runtime_evidence import provision_evidence_slot


def valid_profile(runtime_id: str = "alpha") -> dict[str, object]:
    return {
        "id": runtime_id,
        "display_name": runtime_id.upper(),
        "service": f"{runtime_id}.service",
        "resource_class": "gpu-heavy",
        "enabled": True,
        "health": {"type": "systemd-active"},
        "release": {
            "type": "process",
            "processes": [{"name": runtime_id, "cmdline_contains": [f"/opt/{runtime_id}/"]}],
        },
        "timeouts": {
            "stop_seconds": 1,
            "release_seconds": 1,
            "start_seconds": 1,
            "health_seconds": 1,
        },
    }


def write_profile(directory: Path, data: dict[str, object], name: str | None = None) -> None:
    path = directory / f"{name or data['id']}.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.mark.parametrize(
    "value",
    [
        True,
        1,
        "runtime.json",
        "/a/../b.json",
        "/a//b.json",
        "/a/./b.json",
        "/a/b.lock",
        "/a/b\n.json",
    ],
)
def test_invalid_evidence_path_is_rejected(tmp_path, value):
    data = valid_profile()
    data["evidence_record"] = value
    write_profile(tmp_path, data)
    with pytest.raises(ProfileValidationError, match="evidence_record"):
        load_registry(tmp_path)


def test_bootstrap_requires_provisioned_evidence_slot_and_keeps_existing_api(tmp_path):
    config = tmp_path / "profiles"
    config.mkdir(mode=0o700)
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    record = evidence / "runtime.json"
    data = valid_profile()
    data["evidence_record"] = str(record)
    write_profile(config, data)
    with pytest.raises(ProfileValidationError, match="safely provisioned"):
        build_manager(config)
    provision_evidence_slot(record)
    manager = build_manager(config)
    assert manager.list_runtimes()[0].evidence_record == record
    with pytest.raises(ProfileValidationError, match="overlap"):
        build_manager(config, lock_path=Path(str(record) + ".lock"))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda data: data.update({"surprise": True}), "unknown field"),
        (lambda data: data.update({"service": "not a unit"}), "service is invalid"),
        (lambda data: data.update({"health": {"type": "magic"}}), "unsupported health"),
        (
            lambda data: data["timeouts"].update({"stop_seconds": 0}),  # type: ignore[union-attr]
            "positive number",
        ),
    ],
)
def test_invalid_profile_is_rejected(tmp_path: Path, mutate: object, message: str) -> None:
    data = valid_profile()
    mutate(data)  # type: ignore[operator]
    write_profile(tmp_path, data)

    with pytest.raises(ProfileValidationError, match=message):
        load_registry(tmp_path)


def test_malformed_yaml_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "alpha.yaml").write_text("id: [", encoding="utf-8")

    with pytest.raises(ProfileValidationError, match="cannot load"):
        load_registry(tmp_path)


def test_duplicate_yaml_field_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "alpha.yaml").write_text("id: alpha\nid: beta\n", encoding="utf-8")

    with pytest.raises(ProfileValidationError, match="duplicate field"):
        load_registry(tmp_path)


def test_duplicate_runtime_id_is_rejected(tmp_path: Path) -> None:
    write_profile(tmp_path, valid_profile(), "alpha")
    write_profile(tmp_path, valid_profile(), "copy")

    with pytest.raises(ProfileValidationError, match="filename must match|duplicate"):
        load_registry(tmp_path)


def test_adding_only_config_registers_new_runtime(tmp_path: Path) -> None:
    write_profile(tmp_path, valid_profile("alpha"))
    registry = load_registry(tmp_path)
    assert len(registry) == 1

    write_profile(tmp_path, valid_profile("whisper"))
    registry = load_registry(tmp_path)

    assert len(registry) == 2
    assert registry.get("whisper").service == "whisper.service"


def test_http_and_tcp_health_must_be_loopback(tmp_path: Path) -> None:
    data = valid_profile()
    data["health"] = {"type": "http", "url": "http://192.0.2.10:8000/health"}
    write_profile(tmp_path, data)

    with pytest.raises(ProfileValidationError, match="loopback"):
        load_registry(tmp_path)


def test_http_json_health_parses_readiness_contract(tmp_path: Path) -> None:
    data = valid_profile()
    data["health"] = {
        "type": "http-json",
        "url": "http://127.0.0.1:8088/health",
        "json_pointer": "/runtime/loaded",
        "equals": True,
    }
    write_profile(tmp_path, data)

    health = load_registry(tmp_path).get("alpha").health

    assert health.type == "http-json"
    assert health.json_pointer == "/runtime/loaded"
    assert health.equals is True


@pytest.mark.parametrize(
    "health",
    [
        {
            "type": "http-json",
            "url": "http://127.0.0.1:8088/health",
            "json_pointer": "runtime/loaded",
            "equals": True,
        },
        {
            "type": "http-json",
            "url": "http://127.0.0.1:8088/health",
            "json_pointer": "/runtime/~2loaded",
            "equals": True,
        },
        {
            "type": "http-json",
            "url": "http://127.0.0.1:8088/health",
            "json_pointer": "/runtime/loaded",
            "equals": {"nested": True},
        },
    ],
)
def test_http_json_health_rejects_invalid_contract(
    tmp_path: Path, health: dict[str, object]
) -> None:
    data = valid_profile()
    data["health"] = health
    write_profile(tmp_path, data)

    with pytest.raises(ProfileValidationError):
        load_registry(tmp_path)


def test_gpu_heavy_runtime_cannot_skip_release_check(tmp_path: Path) -> None:
    data = valid_profile()
    data["release"] = {"type": "none"}
    write_profile(tmp_path, data)

    with pytest.raises(ProfileValidationError, match="gpu-heavy"):
        load_registry(tmp_path)


@pytest.mark.parametrize(
    "expected",
    [date(2026, 1, 1), b"ready", {"ready"}, float("inf"), float("nan"), [True]],
)
def test_http_json_health_rejects_non_json_scalars(tmp_path: Path, expected: object) -> None:
    data = valid_profile()
    data["health"] = {
        "type": "http-json",
        "url": "http://127.0.0.1:8088/health",
        "json_pointer": "/runtime/loaded",
        "equals": expected,
    }
    write_profile(tmp_path, data)

    with pytest.raises(ProfileValidationError, match="finite JSON scalar"):
        load_registry(tmp_path)


@pytest.mark.parametrize("expected", [None, True, 1, 1.5, "ready"])
def test_http_json_health_accepts_json_scalars(tmp_path: Path, expected: object) -> None:
    data = valid_profile()
    data["health"] = {
        "type": "http-json",
        "url": "http://127.0.0.1:8088/health",
        "json_pointer": "/runtime/loaded",
        "equals": expected,
    }
    write_profile(tmp_path, data)

    assert load_registry(tmp_path).get("alpha").health.equals == expected
