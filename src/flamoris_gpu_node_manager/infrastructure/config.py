"""Strict YAML runtime-profile loading."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from flamoris_gpu_node_manager.domain.errors import ProfileValidationError
from flamoris_gpu_node_manager.domain.models import (
    HealthConfig,
    ProcessMatcher,
    ReleaseConfig,
    RuntimeProfile,
    TimeoutConfig,
)
from flamoris_gpu_node_manager.domain.registry import RuntimeRegistry

_ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_SERVICE_RE = re.compile(r"^[A-Za-z0-9_.@:-]+\.service$")
_PROCESS_RE = re.compile(r"^[A-Za-z0-9_.+-]+$")
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class _StrictSafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _StrictSafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    loader.flatten_mapping(node)
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate field: {key!r}",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _mapping(value: object, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ProfileValidationError(f"{location} must be a mapping")
    return value


def _strict_keys(
    data: Mapping[str, Any], *, allowed: set[str], required: set[str], location: str
) -> None:
    unknown = set(data) - allowed
    missing = required - set(data)
    if unknown:
        raise ProfileValidationError(f"{location}: unknown field(s): {', '.join(sorted(unknown))}")
    if missing:
        raise ProfileValidationError(f"{location}: missing field(s): {', '.join(sorted(missing))}")


def _positive_number(value: object, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ProfileValidationError(f"{location} must be a positive number")
    return float(value)


def _parse_health(raw: object, location: str) -> HealthConfig:
    data = _mapping(raw, location)
    health_type = data.get("type")
    variants: dict[str, tuple[set[str], set[str]]] = {
        "http": ({"type", "url"}, {"type", "url"}),
        "http-json": (
            {"type", "url", "json_pointer", "equals"},
            {"type", "url", "json_pointer", "equals"},
        ),
        "tcp": ({"type", "host", "port"}, {"type", "host", "port"}),
        "process": ({"type", "process_name"}, {"type", "process_name"}),
        "systemd-active": ({"type"}, {"type"}),
        "none": ({"type"}, {"type"}),
    }
    if health_type not in variants:
        raise ProfileValidationError(f"{location}.type: unsupported health type: {health_type!r}")
    allowed, required = variants[health_type]
    _strict_keys(data, allowed=allowed, required=required, location=location)

    if health_type in {"http", "http-json"}:
        url = data["url"]
        if not isinstance(url, str):
            raise ProfileValidationError(f"{location}.url must be a string")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in _LOOPBACK_HOSTS:
            raise ProfileValidationError(f"{location}.url must be an HTTP loopback URL")
        if health_type == "http-json":
            pointer = data["json_pointer"]
            expected = data["equals"]
            if not isinstance(pointer, str) or not pointer.startswith("/"):
                raise ProfileValidationError(
                    f"{location}.json_pointer must be a non-empty JSON Pointer"
                )
            if re.search(r"~(?:[^01]|$)", pointer):
                raise ProfileValidationError(f"{location}.json_pointer contains an invalid escape")
            if type(expected) not in {type(None), bool, int, float, str} or (
                isinstance(expected, float) and not math.isfinite(expected)
            ):
                raise ProfileValidationError(f"{location}.equals must be a finite JSON scalar")
            return HealthConfig(
                type=health_type,
                url=url,
                json_pointer=pointer,
                equals=expected,
            )
        return HealthConfig(type=health_type, url=url)

    if health_type == "tcp":
        host, port = data["host"], data["port"]
        if host not in _LOOPBACK_HOSTS:
            raise ProfileValidationError(f"{location}.host must be loopback")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ProfileValidationError(f"{location}.port must be an integer from 1 to 65535")
        return HealthConfig(type=health_type, host=host, port=port)

    if health_type == "process":
        name = data["process_name"]
        if not isinstance(name, str) or not _PROCESS_RE.fullmatch(name):
            raise ProfileValidationError(f"{location}.process_name is invalid")
        return HealthConfig(type=health_type, process_name=name)

    return HealthConfig(type=health_type)


def _parse_release(raw: object, location: str, resource_class: str) -> ReleaseConfig:
    data = _mapping(raw, location)
    release_type = data.get("type")
    if release_type == "none":
        _strict_keys(data, allowed={"type"}, required={"type"}, location=location)
        if resource_class == "gpu-heavy":
            raise ProfileValidationError(f"{location}.type cannot be none for gpu-heavy runtime")
        return ReleaseConfig(type="none")
    if release_type != "process":
        raise ProfileValidationError(f"{location}.type: unsupported release type: {release_type!r}")
    _strict_keys(
        data, allowed={"type", "processes"}, required={"type", "processes"}, location=location
    )
    processes = data["processes"]
    if not isinstance(processes, list) or not processes:
        raise ProfileValidationError(f"{location}.processes must be a non-empty list")
    matchers: list[ProcessMatcher] = []
    for index, raw_matcher in enumerate(processes):
        item_location = f"{location}.processes[{index}]"
        matcher = _mapping(raw_matcher, item_location)
        _strict_keys(
            matcher,
            allowed={"name", "cmdline_contains"},
            required={"name"},
            location=item_location,
        )
        name = matcher["name"]
        if not isinstance(name, str) or not _PROCESS_RE.fullmatch(name):
            raise ProfileValidationError(f"{item_location}.name is invalid")
        tokens = matcher.get("cmdline_contains", [])
        if not isinstance(tokens, list) or not all(
            isinstance(token, str) and token for token in tokens
        ):
            raise ProfileValidationError(f"{item_location}.cmdline_contains must be strings")
        matchers.append(ProcessMatcher(name=name, cmdline_contains=tuple(tokens)))
    return ReleaseConfig(type="process", processes=tuple(matchers))


def parse_profile(raw: object, *, source: str = "profile") -> RuntimeProfile:
    data = _mapping(raw, source)
    _strict_keys(
        data,
        allowed={
            "id",
            "display_name",
            "service",
            "resource_class",
            "enabled",
            "health",
            "release",
            "timeouts",
        },
        required={
            "id",
            "display_name",
            "service",
            "resource_class",
            "enabled",
            "health",
            "release",
            "timeouts",
        },
        location=source,
    )
    runtime_id = data["id"]
    display_name = data["display_name"]
    service = data["service"]
    resource_class = data["resource_class"]
    enabled = data["enabled"]
    if not isinstance(runtime_id, str) or not _ID_RE.fullmatch(runtime_id):
        raise ProfileValidationError(f"{source}.id is invalid")
    if not isinstance(display_name, str) or not display_name.strip():
        raise ProfileValidationError(f"{source}.display_name must be a non-empty string")
    if not isinstance(service, str) or not _SERVICE_RE.fullmatch(service):
        raise ProfileValidationError(f"{source}.service is invalid")
    if not isinstance(resource_class, str) or not _ID_RE.fullmatch(resource_class):
        raise ProfileValidationError(f"{source}.resource_class is invalid")
    if not isinstance(enabled, bool):
        raise ProfileValidationError(f"{source}.enabled must be a boolean")

    raw_timeouts = _mapping(data["timeouts"], f"{source}.timeouts")
    timeout_fields = {"stop_seconds", "release_seconds", "start_seconds", "health_seconds"}
    _strict_keys(
        raw_timeouts,
        allowed=timeout_fields,
        required=timeout_fields,
        location=f"{source}.timeouts",
    )
    timeouts = TimeoutConfig(
        **{
            field: _positive_number(raw_timeouts[field], f"{source}.timeouts.{field}")
            for field in timeout_fields
        }
    )
    return RuntimeProfile(
        id=runtime_id,
        display_name=display_name,
        service=service,
        resource_class=resource_class,
        enabled=enabled,
        health=_parse_health(data["health"], f"{source}.health"),
        release=_parse_release(data["release"], f"{source}.release", resource_class),
        timeouts=timeouts,
    )


def load_registry(directory: Path) -> RuntimeRegistry:
    if not directory.is_dir():
        raise ProfileValidationError(f"runtime profile directory does not exist: {directory}")
    profiles: list[RuntimeProfile] = []
    seen: dict[str, Path] = {}
    paths = sorted((*directory.glob("*.yaml"), *directory.glob("*.yml")))
    if not paths:
        raise ProfileValidationError(f"no runtime profiles found in: {directory}")
    for path in paths:
        try:
            raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_StrictSafeLoader)
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise ProfileValidationError(f"cannot load {path}: {exc}") from exc
        profile = parse_profile(raw, source=str(path))
        if path.stem != profile.id:
            raise ProfileValidationError(f"{path}: filename must match runtime id {profile.id!r}")
        if profile.id in seen:
            raise ProfileValidationError(
                f"duplicate runtime id {profile.id!r}: {seen[profile.id]} and {path}"
            )
        seen[profile.id] = path
        profiles.append(profile)
    return RuntimeRegistry(profiles)
